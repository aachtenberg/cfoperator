#!/usr/bin/env python3
"""
Web Server for CFOperator
==========================

Serves the chat UI and provides WebSocket + HTTP APIs for:
- Chat with agent (infrastructure Q&A)
- Pending questions (bidirectional Q&A during investigations)
- System status

Runs in separate thread alongside OODA loop.
"""

import json
import logging
import os
import queue
import re
import threading
import uuid
from typing import Dict, Any, Optional
from flask import Flask, request, jsonify, redirect, send_from_directory
import time
import requests

from cfshared import repos as shared_repos
from web_auth import ROLE_ADMIN, install_auth, require_role, require_token_scope
from auth.bootstrap import init_auth_store
from auth.models import (
    EVENT_COCKPIT_BRIDGE, EVENT_COCKPIT_SESSION, EVENT_TOKEN_CREATED, EVENT_TOKEN_REVOKED, ROLE_MEMBER)
# The cockpit spawn mints a session token server-side, and it must resolve the
# caller exactly as POST /api/auth/tokens does — same role ceiling, same audit
# actor. Importing those helpers is deliberate: a second copy of "who is
# calling" is how the two answers start disagreeing.
from auth.routes import _actor, _caller_user_id, _effective_role, build_auth_blueprint
from auth.store import AuthError
from cockpit_spawn import (
    TIER_POD, CockpitSpawnError, CockpitSpawner, build_cockpit_config, clamp_ttl)
# Tiers 2/3 of the same ladder: most of this fleet is not in the cluster, and
# the cockpit contract has to hold on a bare Pi exactly as it does in a pod.
from cockpit_ladder import (
    TIER_AUTO, HostCockpitSpawner, build_ladder_config, choose_tier, resolve_target_host)
# The handoff line the console drawer offers for copying (CFOP-73). Imported
# rather than restated: notifications.py is the one definition of that command,
# and test_cockpit_attach_contract.py already pins it against the verb the Go
# binary registers. A template literal in investigations.html would be a third
# copy of it that nothing checks — and the failure mode of drift is an operator
# pasting a command that no longer exists, mid-incident.
from event_runtime.notifications import ATTACH_COMMAND

# WebSocket support - disabled because Waitress (WSGI) doesn't support it
# The UI uses HTTP polling via /api/chat instead
WEBSOCKET_AVAILABLE = False

logger = logging.getLogger("cfoperator.web")


def _positive_int(value: Any) -> int:
    """Coerce a client-supplied count. Junk and negatives read as 0 rather than
    raising: a session record is worth keeping even when one of its numbers is
    nonsense, and the summary is the part that matters."""
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


#: How long a browser has to present its bridge ticket (CFOP-59). The ticket is
#: minted by ``/api/cockpit/<id>/open``, sent once in the bridge's auth frame,
#: and revoked the moment it verifies — so this is the lifetime of a handshake,
#: not of a session. Generous enough for a slow LAN, short enough that a
#: credential which leaked from a page is dead before anyone could use it.
BRIDGE_TICKET_TTL_SECONDS = 120

#: Token labels the cockpit mints. The session token lives on the host and dies
#: with the session; the ticket exists to get a browser through one handshake.
COCKPIT_SESSION_TOKEN_LABEL = "cockpit-inv-{investigation_id}"
COCKPIT_BRIDGE_TICKET_LABEL = "cockpit-bridge-{investigation_id}"
#: A ticket label is exactly this and nothing else. Matched strictly so a
#: token an admin happened to label "cockpit-bridge-note" is not silently
#: spent on its first bridge use, and so the prefix cannot be widened by a
#: later label sharing it.
_BRIDGE_TICKET_LABEL_RE = re.compile(r"cockpit-bridge-\d+\Z")


def json_object() -> Dict[str, Any]:
    """Request body as a dict, for the operator-console POSTs.

    ``request.get_json`` happily decodes a bare list/string/number, so the
    ``(get_json(silent=True) or {}).get(...)`` idiom raised AttributeError
    (a 500) on ``POST [1,2]``. Anything that isn't a JSON object reads as an
    empty body, so the endpoint's own field validation decides the status.
    The ``/v1/*`` machine endpoints stay stricter — they 400 on a non-object.
    """
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else {}


class WebServer:
    """
    Web server for CFOperator UI and APIs.

    Provides:
    - Static file serving (UI)
    - WebSocket endpoint for real-time chat
    - HTTP APIs for chat and Q&A
    """

    def __init__(self, operator, host: str = "0.0.0.0", port: int = 8083):
        self.operator = operator
        self.host = host
        self.port = port

        # Flask app
        self.app = Flask(__name__, static_folder='ui', static_url_path='')

        # WebSocket support (only if flask-sock available and not using Waitress)
        if WEBSOCKET_AVAILABLE:
            self.sock = Sock(self.app)
            self.ws_clients = []
        else:
            self.sock = None
            self.ws_clients = []
            logger.warning("WebSocket disabled - use HTTP /api/chat endpoint instead")

        # Chat sessions for polling-based streaming
        # {chat_id: {'events': [...], 'done': bool, 'created': time}}
        self._chat_sessions = {}
        self._sessions_lock = threading.Lock()

        # Setup routes
        self._setup_routes()

        # Console auth. Installed AFTER _setup_routes so the before_request gate
        # covers every route registered above — :8083 is bound to the node's LAN
        # interface (hostNetwork), and /api/remediations/<id>/approve hands work
        # straight to the executor, so none of it can be left open. Browsers get
        # a session cookie, services send Authorization: Bearer <token>.
        # See web_auth.py for the exempt list and the fail-closed behaviour.
        #
        # The store backs logins and API tokens with the database. It is None
        # when no auth database is reachable, which drops the console back to
        # the legacy single-user environment credentials rather than taking it
        # offline — see auth/bootstrap.py.
        self.auth_store = init_auth_store()
        if self.auth_store is not None:
            # Registered before install_auth so the gate covers these routes
            # too — user and token management must sit behind the same
            # authentication as everything else, not in front of it.
            self.app.register_blueprint(build_auth_blueprint(self.auth_store))
        self.auth = install_auth(self.app, ui_dir='ui', store=self.auth_store)

        logger.info(f"Web server initialized on {host}:{port}")

    def _setup_routes(self):
        """Setup Flask routes and WebSocket handlers.

        Role policy
        -----------
        install_auth() establishes *that* a caller is known; @require_role(
        ROLE_ADMIN) below establishes *what* they may do. Admin is required for
        anything that changes how the system behaves or what it will act on:
        the remediation lifecycle (approve/reject/resolve/reclassify, manual
        enqueue, the flags, run-feed), the settings and model/provider
        selection, the linked repo registry (which repos the tools may read
        and the executor may target), config reload, the on-demand deep
        sweep, and pool instance toggles. Members keep everything a person needs to understand the
        system — every GET, chat, Q&A, feedback and KB search — because the
        point of the member role is to let people read the console without
        being able to point the executor at production.

        The /v1/* endpoints are deliberately NOT decorated. They are machine
        callbacks (executor Jobs, the deep-investigation worker, event_runtime)
        that authenticate with the X-CFOP-Token completion secret via their own
        verify_completion_auth() call — that secret belongs to no user and
        carries no role, so a role check here would 403 every callback and
        strand remediations mid-flight. Keep in sync with
        _COMPLETION_TOKEN_PATHS in web_auth.py.
        """

        # Static UI
        @self.app.route('/')
        def index():
            return send_from_directory('ui', 'index.html')

        # The shared console header (nav, active-page state, identity, logout).
        # Authenticated like the pages that load it — an anonymous browser is
        # redirected to /login and never asks for this. login.html does not
        # use it, so there is no bootstrap ordering problem.
        @self.app.route('/nav.js')
        def nav_js():
            return send_from_directory('ui', 'nav.js', mimetype='application/javascript')

        # Health check
        @self.app.route('/api/health')
        def health():
            return jsonify({
                'status': 'ok',
                'version': '1.0.8',
                'current_investigation': self.operator.current_investigation is not None,
                'uptime_seconds': time.time() - self.operator.start_time if hasattr(self.operator, 'start_time') else 0
            })

        # Config reload (hot-reload hosts without restart)
        @self.app.route('/api/config/reload', methods=['POST'])
        @require_role(ROLE_ADMIN)
        def reload_config():
            result = self.operator.reload_config()
            return jsonify({'status': 'ok', **result})

        # Trigger a deep system sweep on demand
        @self.app.route('/api/sweep', methods=['POST'])
        @require_role(ROLE_ADMIN)
        def trigger_sweep():
            """Trigger an immediate deep system sweep (async in background thread)."""
            def _run_sweep():
                try:
                    logger.info("API-triggered deep system sweep starting")
                    self.operator._deep_system_sweep()
                    self.operator.last_sweep = time.time()
                    logger.info("API-triggered deep system sweep completed")
                except Exception as e:
                    logger.error(f"API-triggered sweep failed: {e}", exc_info=True)

            sweep_thread = threading.Thread(target=_run_sweep, daemon=True, name="api-sweep")
            sweep_thread.start()
            return jsonify({
                'status': 'ok',
                'message': 'Deep system sweep triggered',
            })

        # Async investigation entry point — called by event_runtime's HTTP
        # investigate handler (see issue #15 + alert pipeline unification).
        # Accepts an event_runtime Alert dict (severity, summary, details,
        # alert_id), enqueues for the in-process worker, returns 202.
        @self.app.route('/v1/investigate', methods=['POST'])
        def http_investigate():
            payload = request.get_json(silent=True)
            if not isinstance(payload, dict):
                return jsonify({'error': 'JSON object body required'}), 400
            if not payload.get('summary'):
                return jsonify({'error': 'Field summary is required'}), 400
            try:
                enqueued = self.operator.enqueue_investigation(payload)
            except queue.Full:
                return jsonify({'error': 'investigation queue full'}), 503
            return jsonify({
                'status': 'accepted',
                'alert_id': enqueued.get('alert_id'),
                'queue_depth': enqueued.get('queue_depth'),
            }), 202

        # Deep-investigation report ingest — called (best-effort) by the
        # ephemeral worker Job after it posts its completion to the event
        # runtime. Stores the report in the KB with embeddings so future
        # triage retrieves it, and optionally routes a proposed diff through
        # the remediation PR gates. Auth: same X-CFOP-Token shared secret as
        # the completion endpoint. Storage runs in a background thread (KB +
        # embedding + possible GitHub round-trips would block the worker's
        # short post timeout).
        @self.app.route('/v1/deep-investigations', methods=['POST'])
        def deep_investigations():
            from event_runtime.http_actions import COMPLETION_AUTH_HEADER, verify_completion_auth
            auth_error = verify_completion_auth(request.headers.get(COMPLETION_AUTH_HEADER))
            if auth_error:
                return jsonify({'error': auth_error}), 401
            payload = request.get_json(silent=True)
            if not isinstance(payload, dict):
                return jsonify({'error': 'JSON object body required'}), 400
            alert = payload.get('alert')
            result = payload.get('result')
            if not isinstance(alert, dict) or not isinstance(result, dict):
                return jsonify({'error': "Body must contain 'alert' and 'result' objects"}), 400

            def _store():
                try:
                    self.operator.store_deep_investigation(alert, result)
                except Exception as e:
                    logger.error(f"Deep-investigation ingest failed: {e}", exc_info=True)

            threading.Thread(target=_store, daemon=True, name="deep-ingest").start()
            return jsonify({'status': 'accepted'}), 202

        # Remediation executor completion. The executor Job posts here to drive
        # its RemediationQueue row's state machine (pr-open / needs-human /
        # failed). Same shared-secret auth as the deep-investigation callback.
        @self.app.route('/v1/remediations/<int:remediation_id>/complete', methods=['POST'])
        def remediation_complete(remediation_id):
            from event_runtime.http_actions import COMPLETION_AUTH_HEADER, verify_completion_auth
            auth_error = verify_completion_auth(request.headers.get(COMPLETION_AUTH_HEADER))
            if auth_error:
                return jsonify({'error': auth_error}), 401
            payload = request.get_json(silent=True)
            if not isinstance(payload, dict):
                return jsonify({'error': 'JSON object body required'}), 400
            status = payload.get('status')
            if not isinstance(status, str) or not status:
                return jsonify({'error': "'status' is required"}), 400
            try:
                ok = self.operator.kb.update_remediation_status(
                    remediation_id=remediation_id,
                    status=status,
                    pr_url=payload.get('pr_url'),
                    result=payload.get('result'),
                    last_error=payload.get('detail'),
                )
            except Exception as e:
                logger.error(f"Remediation completion failed: {e}", exc_info=True)
                return jsonify({'error': str(e)}), 500
            if not ok:
                return jsonify({'error': 'remediation not found'}), 404
            return jsonify({'status': 'recorded', 'id': remediation_id}), 200

        # On-demand: enqueue remediations from recent sweep findings (the data
        # behind the morning summary). Same shared-secret auth. Lets an operator
        # backfill the queue without waiting for the 7-9am summary run.
        @self.app.route('/v1/remediations/feed-sweeps', methods=['POST'])
        def feed_sweeps():
            from event_runtime.http_actions import COMPLETION_AUTH_HEADER, verify_completion_auth
            auth_error = verify_completion_auth(request.headers.get(COMPLETION_AUTH_HEADER))
            if auth_error:
                return jsonify({'error': auth_error}), 401
            try:
                n = self.operator.feed_remediations_from_recent_sweeps()
            except Exception as e:
                logger.error(f"feed-sweeps failed: {e}", exc_info=True)
                return jsonify({'error': str(e)}), 500
            return jsonify({'status': 'ok', 'enqueued': n}), 200

        # On-demand: regenerate the morning summary (LLM + live checks), which
        # feeds the queue from its structured recommendations. Async — the
        # summary is slow — and does NOT send a notification (that path is only
        # the scheduled 7-9am run). Lets an operator surface today's recs now.
        @self.app.route('/v1/remediations/feed-summary', methods=['POST'])
        def feed_summary():
            from event_runtime.http_actions import COMPLETION_AUTH_HEADER, verify_completion_auth
            auth_error = verify_completion_auth(request.headers.get(COMPLETION_AUTH_HEADER))
            if auth_error:
                return jsonify({'error': auth_error}), 401

            def _run():
                try:
                    self.operator._generate_morning_summary()
                except Exception as e:
                    logger.error(f"feed-summary run failed: {e}", exc_info=True)

            threading.Thread(target=_run, daemon=True, name="feed-summary").start()
            return jsonify({'status': 'accepted'}), 202

        # Synchronous triage classifier. Called by event_runtime's
        # HTTPTriageDecisionEngine before it decides whether to route an
        # alert to /v1/investigate. Returns {action, reason, confidence}
        # in <2s (fallback chain handles slow primary providers). On any
        # internal failure run_triage() returns action="investigate" so
        # callers never need to handle a non-200 path differently.
        @self.app.route('/v1/triage', methods=['POST'])
        def http_triage():
            payload = request.get_json(silent=True)
            if not isinstance(payload, dict):
                return jsonify({'error': 'JSON object body required'}), 400
            if not payload.get('summary'):
                return jsonify({'error': 'Field summary is required'}), 400
            decision = self.operator.run_triage(payload)
            return jsonify(decision), 200

        # Prometheus metrics endpoint
        @self.app.route('/metrics')
        def metrics():
            """Expose Prometheus metrics for Grafana dashboard."""
            from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
            return generate_latest(), 200, {'Content-Type': CONTENT_TYPE_LATEST}

        # Available providers endpoint
        @self.app.route('/api/providers')
        def list_providers():
            """List available LLM providers based on configuration."""
            providers = [
                {'id': 'auto', 'name': 'Auto', 'description': 'Automatic fallback', 'available': True}
            ]

            # Check Ollama
            ollama_url = self.operator.config.get('llm', {}).get('primary', {}).get('url', '')
            providers.append({
                'id': 'ollama',
                'name': 'Ollama',
                'description': 'Local inference',
                'available': bool(ollama_url)
            })

            # Check Groq
            providers.append({
                'id': 'groq',
                'name': 'Groq',
                'description': 'Fast cloud inference',
                'available': bool(os.getenv('GROQ_API_KEY', ''))
            })

            # Check Anthropic
            providers.append({
                'id': 'anthropic',
                'name': 'Anthropic',
                'description': 'Claude models',
                'available': bool(os.getenv('ANTHROPIC_API_KEY', ''))
            })

            # Check xAI Grok
            providers.append({
                'id': 'xai',
                'name': 'xAI Grok',
                'description': 'Grok models',
                'available': bool(os.getenv('XAI_API_KEY', ''))
            })

            return jsonify({'providers': providers})

        @self.app.route('/api/models/<backend>')
        def list_models(backend):
            """List available models for a given backend."""
            try:
                if backend == 'ollama':
                    # Use OLLAMA_DIRECT_URL for model listing if set, otherwise fall back to primary URL
                    # This allows llm-gateway for inference but direct Ollama for model listing
                    ollama_url = os.getenv('OLLAMA_DIRECT_URL', '')
                    if not ollama_url:
                        ollama_url = self.operator.config.get('llm', {}).get('primary', {}).get('url', '')
                    if not ollama_url:
                        return jsonify({'error': 'No Ollama URL configured', 'models': []}), 500
                    resp = requests.get(f"{ollama_url.rstrip('/')}/api/tags", timeout=5)
                    resp.raise_for_status()
                    data = resp.json()
                    models = [m['name'] for m in data.get('models', [])]
                    selected = self.operator.kb.get_setting('ollama_selected_model', '')
                    return jsonify({'models': models, 'selected': selected})

                elif backend == 'groq':
                    api_key = os.getenv('GROQ_API_KEY', '')
                    if not api_key:
                        return jsonify({'error': 'GROQ_API_KEY not set', 'models': []}), 500
                    resp = requests.get(
                        'https://api.groq.com/openai/v1/models',
                        headers={'Authorization': f'Bearer {api_key}'},
                        timeout=5
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    models = sorted([m['id'] for m in data.get('data', []) if m.get('active', True)])
                    selected = self.operator.kb.get_setting('groq_selected_model', '')
                    return jsonify({'models': models, 'selected': selected})

                elif backend == 'xai':
                    api_key = os.getenv('XAI_API_KEY', '')
                    if not api_key:
                        return jsonify({'error': 'XAI_API_KEY not set', 'models': []}), 500
                    resp = requests.get(
                        'https://api.x.ai/v1/models',
                        headers={'Authorization': f'Bearer {api_key}'},
                        timeout=5
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    models = sorted([m['id'] for m in data.get('data', [])])
                    selected = self.operator.kb.get_setting('xai_selected_model', '')
                    return jsonify({'models': models, 'selected': selected})

                elif backend == 'anthropic':
                    api_key = os.getenv('ANTHROPIC_API_KEY', '')
                    if not api_key:
                        return jsonify({'error': 'ANTHROPIC_API_KEY not set', 'models': []}), 500
                    resp = requests.get(
                        'https://api.anthropic.com/v1/models',
                        headers={
                            'x-api-key': api_key,
                            'anthropic-version': '2023-06-01'
                        },
                        timeout=5
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    models = [m['id'] for m in data.get('data', [])]
                    selected = self.operator.kb.get_setting('anthropic_selected_model', '')
                    return jsonify({'models': models, 'selected': selected})

                else:
                    return jsonify({'error': f'Unknown backend: {backend}', 'models': []}), 400

            except Exception as e:
                logger.error(f"Error fetching {backend} models: {e}")
                return jsonify({'error': str(e), 'models': []}), 500

        @self.app.route('/api/models/<backend>/select', methods=['POST'])
        @require_role(ROLE_ADMIN)
        def select_model(backend):
            """Persist the user's model selection for a backend AND set as default provider."""
            data = request.json
            model_name = data.get('model', '')
            setting_key = f'{backend}_selected_model'
            try:
                # Save model selection for this backend
                self.operator.kb.set_setting(setting_key, model_name)
                # Also set this backend as the system-wide default provider
                self.operator.kb.set_setting('selected_backend', backend)
                logger.info(f"Set default provider to {backend}/{model_name}")
            except Exception as e:
                logger.warning(f"Could not persist {backend} model selection (DB down?): {e}")
            return jsonify({'success': True, 'backend': backend, 'model': model_name})

        @self.app.route('/api/settings/provider')
        def get_selected_provider():
            """Get the currently selected LLM provider."""
            backend = self.operator.kb.get_setting('selected_backend', 'auto')
            model = ''
            if backend and backend != 'auto':
                model = self.operator.kb.get_setting(f'{backend}_selected_model', '')
            return jsonify({'backend': backend, 'model': model})

        @self.app.route('/api/settings/provider', methods=['POST'])
        @require_role(ROLE_ADMIN)
        def set_selected_provider():
            """Set the default LLM provider (without changing model)."""
            data = request.json
            backend = data.get('backend', 'auto')
            if backend not in ('auto', 'ollama', 'groq', 'anthropic', 'xai'):
                return jsonify({'error': f'Invalid backend: {backend}'}), 400
            try:
                self.operator.kb.set_setting('selected_backend', backend if backend != 'auto' else '')
                logger.info(f"Set default provider to: {backend}")
            except Exception as e:
                logger.warning(f"Could not persist provider selection: {e}")
            return jsonify({'success': True, 'backend': backend})

        # Settings API
        @self.app.route('/api/settings/max_tool_iterations')
        def get_max_tool_iterations():
            """Get current max tool iterations setting."""
            val = self.operator._get_max_tool_iterations()
            return jsonify({'max_tool_iterations': val})

        @self.app.route('/api/settings/max_tool_iterations', methods=['POST'])
        @require_role(ROLE_ADMIN)
        def set_max_tool_iterations():
            """Persist max tool iterations setting."""
            data = request.json
            val = max(1, min(50, int(data.get('max_tool_iterations', 10))))
            try:
                self.operator.kb.set_setting('max_tool_iterations', str(val))
            except Exception as e:
                logger.warning(f"Could not persist max_tool_iterations (DB down?): {e}")
            return jsonify({'success': True, 'max_tool_iterations': val})

        # Dedicated triage model (CFOP-58). DB setting overrides the
        # llm.triage_model config key so the model can be switched or
        # disabled live — a config-only change needs a deploy commit AND a
        # manual rollout restart. Resolution semantics live in
        # CFOperator._triage_model: empty DB value = unset (use config),
        # the literal 'off' disables despite config.
        @self.app.route('/api/settings/triage_model')
        def get_triage_model():
            db_val = ''
            try:
                db_val = self.operator.kb.get_setting('triage_model', '') or ''
            except Exception as e:
                logger.warning(f"Could not read triage_model setting: {e}")
            cfg = self.operator.config.get('llm', {}) if isinstance(self.operator.config, dict) else {}
            return jsonify({
                'effective': self.operator._triage_model(),
                'db': db_val,
                'config': str(cfg.get('triage_model') or ''),
            })

        @self.app.route('/api/settings/triage_model', methods=['POST'])
        @require_role(ROLE_ADMIN)
        def set_triage_model():
            data = request.json or {}
            model = str(data.get('model') or '').strip()
            if len(model) > 200:
                return jsonify({'error': 'model tag too long'}), 400
            try:
                self.operator.kb.set_setting('triage_model', model)
            except Exception as e:
                logger.warning(f"Could not persist triage_model (DB down?): {e}")
                return jsonify({'error': str(e)}), 500
            return jsonify({
                'success': True,
                'effective': self.operator._triage_model(),
                'db': model,
            })

        # OODA interval settings
        @self.app.route('/api/settings/ooda')
        def get_ooda_settings():
            """Get current OODA loop intervals."""
            return jsonify({
                'alert_check_interval': self.operator._get_alert_check_interval(),
                'sweep_interval': self.operator._get_sweep_interval(),
            })

        @self.app.route('/api/settings/ooda', methods=['POST'])
        @require_role(ROLE_ADMIN)
        def set_ooda_settings():
            """Persist OODA loop interval settings."""
            data = request.json
            result = {}
            errors = []
            if 'alert_check_interval' in data:
                val = max(5, min(300, int(data['alert_check_interval'])))
                try:
                    self.operator.kb.set_setting('alert_check_interval', str(val))
                    result['alert_check_interval'] = val
                except Exception as e:
                    logger.warning(f"Could not persist alert_check_interval: {e}")
                    errors.append('alert_check_interval')
            if 'sweep_interval' in data:
                val = max(60, min(86400, int(data['sweep_interval'])))
                try:
                    self.operator.kb.set_setting('sweep_interval', str(val))
                    result['sweep_interval'] = val
                except Exception as e:
                    logger.warning(f"Could not persist sweep_interval: {e}")
                    errors.append('sweep_interval')
            if errors:
                return jsonify({'success': False, 'error': f"Database unavailable, could not save: {', '.join(errors)}", **result}), 503
            return jsonify({'success': True, **result})

        @self.app.route('/api/settings/fallback')
        def get_fallback_settings():
            """Get current fallback/paid escalation setting."""
            val = self.operator.kb.get_setting('allow_paid_escalation', 'false')
            enabled = val == 'true' or val is True
            return jsonify({'allow_paid_escalation': enabled})

        @self.app.route('/api/settings/fallback', methods=['POST'])
        @require_role(ROLE_ADMIN)
        def set_fallback_settings():
            """Toggle paid LLM fallback."""
            data = request.json
            enabled = bool(data.get('allow_paid_escalation', False))
            try:
                self.operator.kb.set_setting('allow_paid_escalation', 'true' if enabled else 'false')
            except Exception as e:
                logger.warning(f"Could not persist fallback setting: {e}")
            return jsonify({'success': True, 'allow_paid_escalation': enabled})

        # ── Linked repo registry (CFOP-77) ──
        # One list feeds the github_*/git_* tools (repo names ride in their
        # schema descriptions, so it is prompt-visible), the remediation
        # proposer's PR target, and the event runtime's commit enrichment. It
        # lives in the DB and resolves over config.yaml's git.repos, because
        # in k8s that file is a read-only ConfigMap — linking a repo used to
        # cost a deploy commit plus a rollout restart, and on the Helm path
        # was not expressible at all.

        def _repo_registry():
            """(payload, effective list). Reads the DB, not the process copy,
            so the console reports what is actually persisted."""
            file_repos = self.operator.file_git_repos()
            raw, db_error = '', None
            try:
                raw = self.operator.kb.get_setting(shared_repos.SETTING_KEY, '') or ''
            except Exception as e:
                db_error = str(e)
                logger.warning(f"Could not read the repo registry setting: {e}")
            repos, source = shared_repos.resolve(file_repos, raw)
            linked = {str(r.get('name') or '') for r in repos}
            git_cfg = (self.operator.config.get('git') or {}) if isinstance(self.operator.config, dict) else {}
            token = str((git_cfg.get('github') or {}).get('token') or '').strip() \
                or os.getenv('GITHUB_TOKEN', '').strip()
            payload = {
                'repos': shared_repos.public_list(repos),
                'source': source,
                'config_repos': shared_repos.public_list(file_repos),
                # Named, not just counted: an operator who edits config.yaml
                # while the console list is active deserves to be told which
                # of their entries the running system is ignoring.
                'shadowed': sorted(
                    name for name in (str(r.get('name') or '') for r in file_repos)
                    if name and name not in linked),
                'github_token_configured': bool(token),
                'db_error': db_error,
            }
            return payload, repos

        def _persist_repos(updated, message):
            """Save, then make it live in this process. A save that cannot be
            applied is still reported as saved — it survives a restart."""
            try:
                self.operator.kb.set_setting(shared_repos.SETTING_KEY, shared_repos.dumps(updated))
            except Exception as e:
                logger.warning(f"Could not persist the repo registry (DB down?): {e}")
                return jsonify({'error': f'Database unavailable, could not save: {e}'}), 503
            try:
                self.operator.apply_git_repos(updated)
            except Exception as e:
                logger.warning(f"Repo registry saved but not applied live: {e}")
            logger.info(message)
            payload, _ = _repo_registry()
            return jsonify({'success': True, **payload})

        @self.app.route('/api/git/repos')
        def get_git_repos():
            payload, _ = _repo_registry()
            return jsonify(payload)

        @self.app.route('/api/git/repos', methods=['POST'])
        @require_role(ROLE_ADMIN)
        def upsert_git_repo():
            """Add or edit one repo. Fields the body omits keep their stored
            value, so a form that cannot render an entry's ssh block cannot
            drop it either.

            Note ``path``: it becomes a working tree GitTools runs git in on
            the agent host. That is the same power config.yaml always had, but
            it is easier to reach from here — which is why the write is admin
            only, like every other behaviour-changing console route.
            """
            try:
                name, fields = shared_repos.parse_repo_input(json_object())
            except shared_repos.RepoError as e:
                return jsonify({'error': str(e)}), 400
            _, current = _repo_registry()
            try:
                updated = shared_repos.upsert(current, name, fields)
            except shared_repos.RepoError as e:
                return jsonify({'error': str(e)}), 400
            return _persist_repos(updated, f"Linked repo saved: {name}")

        @self.app.route('/api/git/repos/import', methods=['POST'])
        @require_role(ROLE_ADMIN)
        def import_git_repos():
            """Copy config.yaml's entries into the console list, whole.

            The console cannot do this by posting back what GET showed it:
            that view reduces an ``ssh`` block to a boolean, and an import
            creates *new* entries, so there is no stored row for the upsert
            to merge the missing keys from. A file-declared repo would come
            back GitHub-API-only, having quietly lost its SSH access. The
            copy is made here, from the file's own entries.

            Body: ``{"names": [...]}`` to pick, or nothing for every config
            entry the console list does not already have.
            """
            body = json_object()
            names = body.get('names')
            file_repos = shared_repos.sanitize(self.operator.file_git_repos())
            available = {str(r.get('name') or ''): r for r in file_repos}
            _, current = _repo_registry()
            linked = {str(r.get('name') or '') for r in current}

            if names is None:
                wanted = [n for n in available if n not in linked]
            else:
                if not isinstance(names, list):
                    return jsonify({'error': 'names must be a list'}), 400
                wanted = [str(n) for n in names]
                unknown = [n for n in wanted if n not in available]
                if unknown:
                    return jsonify({'error': f"config.yaml declares no repo named {', '.join(unknown)}"}), 400
                wanted = [n for n in wanted if n not in linked]

            if not wanted:
                payload, _ = _repo_registry()
                return jsonify({'success': True, 'imported': [], **payload})
            updated = list(current) + [available[n] for n in wanted]
            resp = _persist_repos(updated, f"Linked repos imported from config.yaml: {', '.join(wanted)}")
            if isinstance(resp, tuple):
                return resp
            merged = resp.get_json()
            merged['imported'] = wanted
            return jsonify(merged)

        # <path:...> because a config entry with no name of its own falls back
        # to its owner/repo slug, and the default converter stops at the slash
        # — leaving such a row impossible to unlink from the console.
        @self.app.route('/api/git/repos/<path:name>', methods=['DELETE'])
        @require_role(ROLE_ADMIN)
        def delete_git_repo(name):
            _, current = _repo_registry()
            updated, found = shared_repos.remove(current, name)
            if not found:
                return jsonify({'error': f'No linked repo named {name}'}), 404
            return _persist_repos(updated, f"Linked repo removed: {name}")

        @self.app.route('/api/git/repos/revert', methods=['POST'])
        @require_role(ROLE_ADMIN)
        def revert_git_repos():
            """Drop the console list and go back to config.yaml's git.repos.

            An empty setting reads as unset (the flags convention) — the
            settings store has no delete, and 'saved as empty' would mean
            'no repos at all', which is a different and much worse thing."""
            try:
                self.operator.kb.set_setting(shared_repos.SETTING_KEY, '')
            except Exception as e:
                logger.warning(f"Could not clear the repo registry (DB down?): {e}")
                return jsonify({'error': f'Database unavailable, could not save: {e}'}), 503
            try:
                self.operator.apply_git_repos(None)
            except Exception as e:
                logger.warning(f"Repo registry cleared but not applied live: {e}")
            logger.info("Linked repos reverted to config.yaml")
            payload, _ = _repo_registry()
            return jsonify({'success': True, **payload})

        # Chat API — starts chat in background, returns chat_id for polling
        @self.app.route('/api/chat', methods=['POST'])
        def api_chat():
            """
            Start a chat in the background and return a chat_id.
            Poll /api/chat/events/<chat_id> for tool_call/tool_result/done events.
            """
            data = request.json
            message = data.get('message', '')
            history = data.get('history', [])
            backend = data.get('backend', 'auto')
            model = data.get('model')

            if not message:
                return jsonify({'error': 'No message provided'}), 400

            chat_id = str(uuid.uuid4())[:8]
            with self._sessions_lock:
                self._chat_sessions[chat_id] = {
                    'events': [],
                    'cursor': 0,
                    'done': False,
                    'cancelled': False,
                    'created': time.time()
                }

            def run_chat():
                stream = self.operator.handle_chat_message_stream(
                    message, history, backend, model=model
                )
                try:
                    logger.debug(f"Chat {chat_id} started")
                    for evt in stream:
                        with self._sessions_lock:
                            session = self._chat_sessions.get(chat_id)
                            if session is None:
                                return
                            if session['cancelled']:
                                session['events'].append({
                                    'event': 'done',
                                    'data': {'response': '', 'stopped': True}
                                })
                                session['done'] = True
                                logger.info(f"Chat {chat_id} stopped by user")
                                return
                            session['events'].append(evt)
                            if evt['event'] in ('done', 'error'):
                                session['done'] = True
                except Exception as e:
                    logger.error(f"Chat session {chat_id} failed: {e}", exc_info=True)
                    with self._sessions_lock:
                        session = self._chat_sessions.get(chat_id)
                        if session:
                            session['events'].append({'event': 'error', 'data': {'error': str(e)}})
                            session['done'] = True
                finally:
                    # Closing the generator raises GeneratorExit at its current
                    # yield point, unwinding the tool-iteration loop so a stopped
                    # chat stops calling the LLM instead of running to completion.
                    stream.close()

            thread = threading.Thread(target=run_chat, daemon=True)
            thread.start()

            return jsonify({'chat_id': chat_id})

        # Poll for chat events
        @self.app.route('/api/chat/events/<chat_id>')
        def api_chat_events(chat_id):
            """
            Return new events since last poll. Client tracks cursor via 'cursor' field.
            """
            with self._sessions_lock:
                session = self._chat_sessions.get(chat_id)
                if not session:
                    return jsonify({'error': 'Unknown chat_id'}), 404

                cursor = int(request.args.get('cursor', 0))
                new_events = session['events'][cursor:]
                new_cursor = len(session['events'])
                done = session['done']

                # Clean up old sessions (>5 min after done)
                if done and time.time() - session['created'] > 300:
                    del self._chat_sessions[chat_id]

            return jsonify({
                'events': new_events,
                'cursor': new_cursor,
                'done': done
            })

        # Stop an in-flight chat
        @self.app.route('/api/chat/<chat_id>/stop', methods=['POST'])
        def api_chat_stop(chat_id):
            """
            Signal an in-flight chat to stop. The worker thread checks the flag
            at the next streamed event, closes the LLM stream, and emits a final
            'done' event with stopped=True.
            """
            with self._sessions_lock:
                session = self._chat_sessions.get(chat_id)
                if not session:
                    return jsonify({'error': 'Unknown chat_id'}), 404
                if session['done']:
                    return jsonify({'status': 'already_done'})
                session['cancelled'] = True
            logger.info(f"Chat {chat_id} stop requested")
            return jsonify({'status': 'stopping'})

        # Chat sessions API
        @self.app.route('/api/chat-sessions')
        def list_chat_sessions():
            try:
                limit = request.args.get('limit', 30, type=int)
                sessions = self.operator.kb.list_chat_sessions(limit=limit)
                return jsonify({'sessions': sessions})
            except Exception as e:
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/chat-sessions', methods=['POST'])
        def create_chat_session():
            try:
                data = request.json or {}
                session_id = self.operator.kb.create_chat_session(title=data.get('title'))
                return jsonify({'id': session_id})
            except Exception as e:
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/chat-sessions/<int:session_id>')
        def get_chat_session(session_id):
            try:
                session = self.operator.kb.get_chat_session(session_id)
                if not session:
                    return jsonify({'error': 'Session not found'}), 404
                return jsonify(session)
            except Exception as e:
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/chat-sessions/<int:session_id>/messages', methods=['POST'])
        def add_chat_message(session_id):
            data = request.json or {}
            role = data.get('role', '')
            content = data.get('content', '')
            if not role or not content:
                return jsonify({'error': 'role and content required'}), 400
            try:
                ok = self.operator.kb.append_chat_message(
                    session_id, role, content,
                    backend=data.get('backend', ''),
                    model=data.get('model', '')
                )
                if not ok:
                    return jsonify({'error': 'Session not found'}), 404
                return jsonify({'success': True})
            except Exception as e:
                return jsonify({'error': str(e)}), 500

        # Not admin-gated despite being a DELETE: a chat session is the caller's
        # own transcript, not system state, and members who may hold a
        # conversation must be able to discard it.
        @self.app.route('/api/chat-sessions/<int:session_id>', methods=['DELETE'])
        def delete_chat_session(session_id):
            try:
                ok = self.operator.kb.delete_chat_session(session_id)
                if not ok:
                    return jsonify({'error': 'Session not found'}), 404
                return jsonify({'success': True})
            except Exception as e:
                return jsonify({'error': str(e)}), 500

        # Q&A API (HTTP)
        @self.app.route('/api/qa', methods=['GET', 'POST'])
        def api_qa():
            """
            Get pending questions or submit answers.

            GET: List all pending questions
            POST: Submit answer to a question
            """
            if request.method == 'GET':
                # Get pending questions
                try:
                    questions = self.operator.kb.get_pending_questions()
                    return jsonify(questions)
                except Exception as e:
                    logger.error(f"Error fetching questions: {e}")
                    return jsonify({'error': str(e)}), 500
            else:
                # POST: Answer a question
                data = request.json
                question_id = data.get('question_id')
                answer = data.get('answer')

                if not question_id or not answer:
                    return jsonify({'error': 'Missing question_id or answer'}), 400

                try:
                    self.operator.answer_question(question_id, answer)
                    return jsonify({'success': True})
                except Exception as e:
                    logger.error(f"Error answering question: {e}")
                    return jsonify({'error': str(e)}), 500

        # Feedback API — thumbs up/down on chat responses
        @self.app.route('/api/feedback', methods=['POST'])
        def api_feedback():
            """Record user feedback on a chat response to close the learning loop."""
            data = request.json
            feedback_type = data.get('type')  # 'thumbs_up' or 'thumbs_down'
            learning_ids = data.get('learning_ids', [])

            if feedback_type not in ('thumbs_up', 'thumbs_down'):
                return jsonify({'error': 'Invalid feedback type'}), 400

            successful = (feedback_type == 'thumbs_up')
            updated = 0
            for lid in learning_ids:
                try:
                    if self.operator.kb._kb.record_learning_application(int(lid), successful):
                        updated += 1
                except Exception as e:
                    logger.warning(f"Failed to record feedback for learning {lid}: {e}")

            # Thumbs up also marks learnings as human-verified
            if successful:
                for lid in learning_ids:
                    try:
                        self.operator.kb._kb.verify_learning(int(lid), True)
                    except Exception as e:
                        logger.warning(f"Failed to mark learning {lid} as verified: {e}")

            return jsonify({'success': True, 'updated': updated})

        # Sweep Reports API
        # Remediation queue read API + operator console (see docs/OBSERVABILITY.md).
        @self.app.route('/api/remediations')
        def list_remediations_api():
            """List remediation queue rows. Query: status=, limit=."""
            try:
                status = request.args.get('status')
                limit = request.args.get('limit', 200, type=int)
                rows = self.operator.kb.list_remediations(status=status, limit=limit)
                return jsonify({'remediations': rows, 'count': len(rows)})
            except Exception as e:
                logger.error(f"Error listing remediations: {e}")
                return jsonify({'error': str(e), 'remediations': []}), 500

        # Admin because this is not merely "propose": queue_remediation() runs
        # the auto-execute gate on the caller-supplied class/risk/confidence, so
        # a low-risk mechanizable body lands straight in 'queued' and the
        # executor drains it without anyone pressing approve.
        @self.app.route('/api/remediations', methods=['POST'])
        @require_role(ROLE_ADMIN)
        def create_remediation_api():
            """Manually enqueue a remediation (operator-authored)."""
            try:
                b = json_object()
                rec = str(b.get('recommendation') or '').strip()
                if not rec:
                    return jsonify({'error': 'recommendation required'}), 400
                conf = b.get('confidence')
                if isinstance(conf, str):
                    conf = float(conf) if conf.strip() else None
                elif not isinstance(conf, (int, float)):
                    conf = None
                key = str(b.get('dedupe_key') or f"manual-{int(time.time())}")
                rid = self.operator.kb.queue_remediation(
                    remediation_class=str(b.get('remediation_class') or 'manual'),
                    payload={'recommendation': rec, 'title': str(b.get('title') or rec[:60]),
                             'repo': (str(b.get('repo') or '').strip() or None),
                             'source': 'manual', 'dedupe_key': key},
                    host_id=str(b.get('host_id') or 'default')[:64],
                    risk=str(b.get('risk') or 'high'), confidence=conf, dedupe_key=key)
                if not rid:
                    return jsonify({'status': 'deduped'}), 200
                return jsonify(self.operator.kb.get_remediation(rid)), 201
            except Exception as e:
                logger.error(f"create remediation failed: {e}", exc_info=True)
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/remediations/<int:remediation_id>')
        def get_remediation_api(remediation_id):
            """Fetch one remediation row (full detail)."""
            try:
                row = self.operator.kb.get_remediation(remediation_id)
                if not row:
                    return jsonify({'error': 'not found'}), 404
                return jsonify(row)
            except Exception as e:
                logger.error(f"Error fetching remediation {remediation_id}: {e}")
                return jsonify({'error': str(e)}), 500

        @self.app.route('/remediations')
        def remediations_page():
            """Operator console: the remediation worklist."""
            return send_from_directory('ui', 'remediations.html')

        # Account is for every role (password + own tokens). Admin holds
        # user/token administration and LLM runtime config. Markup is not
        # role-gated — every /api/* call is authorised on its own. The Admin
        # nav link is filtered client-side in ui/nav.js; members who hit
        # /admin see a banner and empty/403 API responses rather than a hard
        # page gate.
        @self.app.route('/account')
        def account_page():
            """Operator console: self-service password and own API tokens."""
            return send_from_directory('ui', 'account.html')

        @self.app.route('/admin')
        def admin_page():
            """Operator console: admin panel (users, tokens, LLM settings)."""
            return send_from_directory('ui', 'admin.html')

        # Bookmarks and docs still point at /users and /tokens.
        @self.app.route('/users')
        def users_page_redirect():
            return redirect('/admin?tab=users', code=302)

        @self.app.route('/tokens')
        def tokens_page_redirect():
            return redirect('/admin?tab=tokens', code=302)

        # --- operator console actions (internal /api, like the other UI POSTs) ---
        @self.app.route('/api/remediations/<int:remediation_id>/approve', methods=['POST'])
        @require_role(ROLE_ADMIN)
        def approve_remediation(remediation_id):
            """Send a row to the executor: status -> queued.

            Refused (409) for manual-class rows — human-only work has nothing
            for the executor to mechanize, and queuing it burns a Job that
            lands right back at needs-human (CFOP-49). The API is the gate so
            the console, MCP, and any future caller hit the same wall.
            """
            try:
                row = self.operator.kb.get_remediation(remediation_id)
                if not row:
                    return jsonify({'error': 'not found'}), 404
                conflict = self.operator.kb.remediation_approve_conflict(row)
                if conflict:
                    return jsonify({'error': conflict,
                                    'remediation_class': row.get('remediation_class'),
                                    'action': 'reclassify'}), 409
                ok = self.operator.kb.update_remediation_status(remediation_id, 'queued')
                if not ok:
                    return jsonify({'error': 'not found'}), 404
                return jsonify(self.operator.kb.get_remediation(remediation_id))
            except Exception as e:
                logger.error(f"approve remediation {remediation_id} failed: {e}")
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/remediations/<int:remediation_id>/reject', methods=['POST'])
        @require_role(ROLE_ADMIN)
        def reject_remediation(remediation_id):
            try:
                note = json_object().get('note')
                ok = self.operator.kb.update_remediation_status(remediation_id, 'rejected', last_error=note)
                if not ok:
                    return jsonify({'error': 'not found'}), 404
                return jsonify(self.operator.kb.get_remediation(remediation_id))
            except Exception as e:
                logger.error(f"reject remediation {remediation_id} failed: {e}")
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/remediations/<int:remediation_id>/resolve', methods=['POST'])
        @require_role(ROLE_ADMIN)
        def resolve_remediation(remediation_id):
            """Operator closes a row as done (fixed by hand, or no longer needed).

            The automated paths to 'resolved' are merge-reconcile and executor
            completion; this is the manual one. The note is the *why* — it lands
            in result.resolution_note, not last_error (which means failure).
            """
            try:
                note = json_object().get('note')
                note = (str(note).strip()[:2000] or None) if note else None
                ok = self.operator.kb.update_remediation_status(
                    remediation_id, 'resolved',
                    result={'resolved_by': 'operator', 'resolution_note': note})
                if not ok:
                    return jsonify({'error': 'not found'}), 404
                return jsonify(self.operator.kb.get_remediation(remediation_id))
            except Exception as e:
                logger.error(f"resolve remediation {remediation_id} failed: {e}")
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/remediations/<int:remediation_id>/reclassify', methods=['POST'])
        @require_role(ROLE_ADMIN)
        def reclassify_remediation_api(remediation_id):
            try:
                b = json_object()
                conf = b.get('confidence')
                if isinstance(conf, str):
                    conf = float(conf) if conf.strip() else None
                elif not isinstance(conf, (int, float)):
                    conf = None
                row = self.operator.kb.reclassify_remediation(
                    remediation_id, remediation_class=b.get('remediation_class'),
                    risk=b.get('risk'), confidence=conf, repo=b.get('repo'))
                if not row:
                    return jsonify({'error': 'not found'}), 404
                return jsonify(row)
            except Exception as e:
                logger.error(f"reclassify remediation {remediation_id} failed: {e}")
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/remediation/flags')
        def get_remediation_flags():
            try:
                return jsonify({f: self.operator._remediation_flag(f)
                                for f in self.operator._REMEDIATION_FLAGS})
            except Exception as e:
                logger.error(f"get remediation flags failed: {e}")
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/remediation/flags', methods=['POST'])
        @require_role(ROLE_ADMIN)
        def set_remediation_flag():
            try:
                b = json_object()
                name, value = b.get('name'), b.get('value')
                if name not in self.operator._REMEDIATION_FLAGS:
                    return jsonify({'error': 'unknown flag'}), 400
                self.operator.kb.set_setting('remediation_' + name, '1' if value else '0')
                return jsonify({f: self.operator._remediation_flag(f)
                                for f in self.operator._REMEDIATION_FLAGS})
            except Exception as e:
                logger.error(f"set remediation flag failed: {e}")
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/remediation/run-feed', methods=['POST'])
        @require_role(ROLE_ADMIN)
        def run_remediation_feed():
            """Regenerate the summary (which feeds the queue); async, no notify."""
            def _run():
                try:
                    self.operator._generate_morning_summary()
                except Exception as e:
                    logger.error(f"run-feed failed: {e}", exc_info=True)
            threading.Thread(target=_run, daemon=True, name="run-feed").start()
            return jsonify({'status': 'accepted'}), 202

        @self.app.route('/api/investigations')
        def list_investigations_api():
            """List recent investigations (summary rows). (was feat/investigations-read-api)"""
            try:
                limit = request.args.get('limit', 50, type=int)
                rows = self.operator.kb.get_recent_investigations(limit=limit)
                return jsonify({'investigations': rows, 'count': len(rows)})
            except Exception as e:
                logger.error(f"Error listing investigations: {e}")
                return jsonify({'error': str(e), 'investigations': []}), 500

        @self.app.route('/api/investigations/<int:investigation_id>')
        def get_investigation_api(investigation_id):
            """Investigation drill-in (full findings) for the console + worklist drawer."""
            try:
                inv = self.operator.kb.get_investigation(investigation_id)
                if not inv:
                    return jsonify({'error': 'not found'}), 404
                # Rendered here rather than in the page so the console's copy
                # of the handoff line is the same string Slack prints (CFOP-73).
                # Copied rather than mutated: the KB row is not ours to grow a
                # field on.
                payload = dict(inv)
                payload['attach_command'] = ATTACH_COMMAND.format(
                    investigation_id=investigation_id)
                return jsonify(payload)
            except Exception as e:
                logger.error(f"get investigation {investigation_id} failed: {e}")
                return jsonify({'error': str(e)}), 500

        # Operator triage of an investigation (CFOP-65). Until now the
        # investigations table was append-only from the console's side: the
        # read API returned triage_action/operator_notes but nothing could set
        # them, so an operator who had verified a fix had no way to say so —
        # and an attached cfassist session told a human to use a console
        # control that did not exist.
        #
        # Only 'resolved' and 'ack' are accepted here. 'retry'/'context' need
        # the re-investigation path wired (enqueue_investigation is the live
        # one, not the dead InvestigationQueue table) and 'suppress' needs a
        # reader in the alert path before the button would mean anything.
        _INVESTIGATION_TRIAGE_ACTIONS = ('resolved', 'ack')

        @self.app.route('/api/investigations/<int:investigation_id>/triage', methods=['POST'])
        @require_role(ROLE_ADMIN)
        def triage_investigation_api(investigation_id):
            body = json_object()
            action = str(body.get('action') or '').strip()
            if action not in _INVESTIGATION_TRIAGE_ACTIONS:
                return jsonify({
                    'error': f'action must be one of '
                             f'{", ".join(_INVESTIGATION_TRIAGE_ACTIONS)}'}), 400
            note = str(body.get('note') or '').strip()[:2000] or None
            try:
                # Deliberately NOT passing outcome=, though the KB helper
                # supports it. `outcome` is the agent's own conclusion and it
                # is what find_similar_investigations_hybrid cites as
                # precedent to future triage decisions — overwriting it with
                # an operator verdict would edit the corpus that later
                # classifications reason from. The human's verdict lives in
                # triage_action; the agent's finding stays intact.
                ok = self.operator.kb.update_investigation_triage(
                    investigation_id, action, operator_notes=note)
                if not ok:
                    return jsonify({'error': 'not found'}), 404
                return jsonify(self.operator.kb.get_investigation(investigation_id))
            except Exception as e:
                logger.error(f"triage investigation {investigation_id} failed: {e}")
                return jsonify({'error': str(e)}), 500

        @self.app.route('/investigations')
        def investigations_page():
            """Operator console: recent investigations + conclusions."""
            return send_from_directory('ui', 'investigations.html')

        # ---- cockpit (CFOP-35, ladder CFOP-36) --------------------------
        # Spawn the ephemeral cockpit for an investigation. Server-side rather
        # than "cfassist creates the workload" because the console button
        # (CFOP-59) needs this same path, and because the guards — dedupe,
        # concurrency cap, token mint, audit — must be central rather than
        # re-implemented in every client that grows a spawn button.
        #
        # Admin-gated: this creates a workload and mints a credential. It hands
        # out no shell of its own at any tier — the interactive attach is always
        # the operator's own kubectl or ssh.
        @self.app.route('/api/cockpit/spawn', methods=['POST'])
        @require_role(ROLE_ADMIN)
        def spawn_cockpit():
            body = json_object()
            try:
                investigation_id = int(body.get('investigation_id'))
            except (TypeError, ValueError):
                return jsonify({'error': 'investigation_id is required'}), 400

            try:
                inv = self.operator.kb.get_investigation(investigation_id)
            except Exception as e:
                logger.error(f"cockpit spawn: investigation lookup failed: {e}")
                return jsonify({'error': str(e)}), 500
            if not inv:
                # A cockpit exists to work an investigation. Spawning one for an
                # id that does not exist would produce a session whose first act
                # is to fail fetching its own briefing.
                return jsonify({'error': 'not found'}), 404

            ttl = clamp_ttl(body.get('ttl_seconds'),
                            self._cockpit_spawner().config.ttl_seconds)
            try:
                host, provenance = self._resolve_cockpit_host(investigation_id, inv,
                                                              str(body.get('host') or ''))
                tier, tier_note, node = self._choose_cockpit_tier(
                    host, str(body.get('tier') or TIER_AUTO))
                logger.info("cockpit for #%s: host=%r (%s) -> %s",
                            investigation_id, host, provenance, tier_note)

                if tier == TIER_POD:
                    # The node lookup is handed on rather than repeated: two
                    # answers to "is this host in the cluster" could differ.
                    result = self._cockpit_spawner().spawn(
                        investigation_id, host=host, ttl_seconds=ttl, node=node)
                else:
                    result = self._cockpit_ladder().spawn(
                        investigation_id, host=host, tier=tier, ttl_seconds=ttl)
            except CockpitSpawnError as e:
                logger.warning(f"cockpit spawn refused for #{investigation_id}: {e}")
                return jsonify({'error': str(e)}), e.status
            except Exception as e:
                logger.error(f"cockpit spawn failed for #{investigation_id}: {e}",
                             exc_info=True)
                return jsonify({'error': str(e)}), 500

            result['host_provenance'] = provenance
            result['tier_note'] = tier_note
            return jsonify(result), 200 if result.get('status') == 'existing' else 201

        # ---- the browser cockpit (CFOP-59) --------------------------------
        # "Open cockpit" from the investigation drawer. The bridge (CFOP-75)
        # carries the bytes; this hands the browser what it needs to reach it:
        # a live session and a credential. The console holds a cookie, the
        # bridge takes an `investigate` bearer in its auth frame, and nothing
        # else turns one into the other.
        #
        # It *is* spawn, plus a ticket, so it is admin-gated for spawn's
        # reasons. And it refuses before spawning anything when the terminal
        # could not be opened afterwards — a session created for a bridge that
        # is off, or for an origin the bridge would reject, is a workload and a
        # credential for nothing. Each refusal carries the attach line, so the
        # drawer can fall back to the copy button rather than to an error.
        @self.app.route('/api/cockpit/<int:investigation_id>/open', methods=['POST'])
        @require_role(ROLE_ADMIN)
        def open_cockpit(investigation_id):
            body = json_object()
            fallback = ATTACH_COMMAND.format(investigation_id=investigation_id)

            def refuse(code: str, reason: str, status: int = 409):
                return jsonify({'error': reason, 'code': code,
                                'attach_command': fallback}), status

            bridge = self._bridge_config()
            if not bridge.enabled:
                return refuse('bridge_disabled',
                              'the cockpit bridge is not enabled on this agent '
                              '(cockpit.bridge_enabled) — attach from a terminal instead')
            origin = self._console_origin()
            if origin not in bridge.allowed_origins:
                return refuse('origin',
                              f"this console's origin ({origin}) is not in "
                              "cockpit.bridge_origins, so the bridge would refuse the "
                              "terminal; add it there")

            try:
                inv = self.operator.kb.get_investigation(investigation_id)
            except Exception as e:
                logger.error(f"cockpit open: investigation lookup failed: {e}")
                return jsonify({'error': str(e)}), 500
            if not inv:
                return jsonify({'error': 'not found'}), 404

            ttl = clamp_ttl(body.get('ttl_seconds'),
                            self._cockpit_spawner().config.ttl_seconds)
            try:
                host, provenance = self._resolve_cockpit_host(
                    investigation_id, inv, str(body.get('host') or ''))
                tier, tier_note, node = self._choose_cockpit_tier(
                    host, str(body.get('tier') or TIER_AUTO))
                _node = node
                if tier == TIER_POD:
                    if not bridge.pod_tier:
                        return refuse('tier',
                                      'this investigation is in the cluster; the browser '
                                      'terminal serves the host tiers only unless '
                                      'cockpit.bridge_pod_tier is on (and the chart grants '
                                      'pods/attach) — use cfassist attach --spawn')
                    result = self._cockpit_spawner().spawn(
                        investigation_id, host=host, ttl_seconds=ttl, node=_node)
                else:
                    result = self._cockpit_ladder().spawn(
                        investigation_id, host=host, tier=tier, ttl_seconds=ttl)
                ticket = self._mint_bridge_ticket(investigation_id, tier=tier, host=host)
            except CockpitSpawnError as e:
                logger.warning(f"cockpit open refused for #{investigation_id}: {e}")
                return jsonify({'error': str(e), 'attach_command': fallback}), e.status
            except Exception as e:
                logger.error(f"cockpit open failed for #{investigation_id}: {e}",
                             exc_info=True)
                return jsonify({'error': str(e)}), 500

            result['host_provenance'] = provenance
            result['tier_note'] = tier_note
            result['bridge'] = {
                'url': self._bridge_url(bridge, investigation_id),
                'origin': origin,
                'ticket': ticket['secret'],
                'ticket_ttl_seconds': BRIDGE_TICKET_TTL_SECONDS,
                'scope': 'investigate',
            }
            return jsonify(result), 200 if result.get('status') == 'existing' else 201

        # Kill. The janitor removes what has expired; an operator closing a
        # session wants the host clean now and the credential dead now, not
        # at the next sweep. Same removals the sweep makes, applied to one
        # investigation regardless of its deadline, then every token minted
        # for it is revoked.
        @self.app.route('/api/cockpit/<int:investigation_id>/close', methods=['POST'])
        @require_role(ROLE_ADMIN)
        def close_cockpit(investigation_id):
            body = json_object()
            try:
                inv = self.operator.kb.get_investigation(investigation_id)
            except Exception as e:
                logger.error(f"cockpit close: investigation lookup failed: {e}")
                return jsonify({'error': str(e)}), 500
            if not inv:
                return jsonify({'error': 'not found'}), 404
            try:
                host, _provenance = self._resolve_cockpit_host(
                    investigation_id, inv, str(body.get('host') or ''))
                tier, _note, _node = self._choose_cockpit_tier(host, TIER_AUTO)
                # A pod cockpit is a Job, not a host artifact: deleting it (the
                # grant the ladder's own spawn already holds) tears down the pod
                # and GCs its token Secret, where ladder.destroy() would SSH the
                # node, find nothing, and revoke the Secret out from under a Job
                # left running. Kill is one thing across tiers or it is not kill.
                if tier == TIER_POD:
                    removed = self._cockpit_spawner().destroy(investigation_id)
                else:
                    removed = self._cockpit_ladder().destroy(investigation_id, host=host)
            except CockpitSpawnError as e:
                logger.warning(f"cockpit close refused for #{investigation_id}: {e}")
                return jsonify({'error': str(e)}), e.status
            except Exception as e:
                logger.error(f"cockpit close failed for #{investigation_id}: {e}",
                             exc_info=True)
                return jsonify({'error': str(e)}), 500
            revoked = self._revoke_cockpit_tokens(investigation_id)
            self.record_bridge_event(event='killed', actor=_actor(),
                                     investigation_id=investigation_id, host=host,
                                     session_name=(removed[0]['name'] if removed else None))
            return jsonify({'status': 'closed', 'host': host, 'removed': removed,
                            'tokens_revoked': revoked}), 200

        # ---- cockpit write-back (CFOP-37) --------------------------------
        # What a human and an agent worked out in a cockpit, coming back from
        # the session that is about to be destroyed. "Compute disposable, state
        # central" only holds if the state actually arrives.
        #
        # Scope, not role: this is written by the SESSION, using the
        # per-investigation token minted for it (CFOP-32, `investigate` scope)
        # — the same scope POST /api/learnings already takes. Gating on admin
        # would mean the credential that dies with the session cannot record
        # what the session learned, which is the one thing it exists to do.
        _SESSION_OUTCOMES = ('resolved', 'mitigated', 'diagnosed',
                             'no_change', 'inconclusive', 'escalated')

        @self.app.route('/api/investigations/<int:investigation_id>/session',
                        methods=['POST'])
        @require_token_scope('investigate')
        def record_cockpit_session(investigation_id):
            """Append one cockpit session to an investigation.

            Deliberately an *append*, never an edit of ``findings``: that field
            is the corpus later triage decisions reason from, and the rule that
            the agent's finding stays intact while a human's verdict lives
            elsewhere is already recorded on the triage endpoint below. A
            session summary is a human verdict by another name.
            """
            body = json_object()
            summary = str(body.get('summary') or '').strip()
            if not summary:
                # A session record with no summary is a row that says a human
                # was here and nothing about what they found — worse than
                # nothing, because it looks like write-back working.
                return jsonify({'error': 'summary is required'}), 400

            outcome = str(body.get('outcome') or 'inconclusive').strip().lower()
            if outcome not in _SESSION_OUTCOMES:
                return jsonify({
                    'error': f"outcome must be one of {', '.join(_SESSION_OUTCOMES)}"}), 400

            try:
                inv = self.operator.kb.get_investigation(investigation_id)
            except Exception as e:
                logger.error(f"cockpit session: investigation lookup failed: {e}")
                return jsonify({'error': str(e)}), 500
            if not inv:
                return jsonify({'error': 'not found'}), 404

            detail = {
                'tier': str(body.get('tier') or '')[:32],
                'host': str(body.get('host') or '')[:64],
                'duration_seconds': _positive_int(body.get('duration_seconds')),
                'exchanges': _positive_int(body.get('exchanges')),
                'commands': [str(c).strip()[:300] for c in (body.get('commands') or [])
                             if str(c).strip()][:20],
                'learning_id': _positive_int(body.get('learning_id')) or None,
                'model': str(body.get('model') or '')[:120],
            }
            # A summary the client could not distil is stored as the raw tail,
            # marked. Losing it would throw away the only record of the session;
            # presenting it as a summary would be a lie about its quality.
            degraded = bool(body.get('degraded'))
            actor = _actor() or 'unknown'

            try:
                self.operator.kb.record_cockpit_session(
                    investigation_id,
                    summary=summary[:20000],
                    outcome=outcome,
                    actor=actor[:255],
                    detail=detail,
                    degraded=degraded,
                )
            except Exception as e:
                logger.error(f"cockpit session write-back for #{investigation_id} failed: {e}")
                return jsonify({'error': str(e)}), 500

            # The audit half of the issue's "who attached, scope, tier,
            # duration, outcome". It lands here rather than in changerecord:
            # that service's Intent is an approval workflow for a proposed
            # change (remediation_id, commands, executor image), and a session
            # is none of those — it already happened and needed no approval.
            # CFOP-32 already writes mint/revoke here with actor, investigation
            # and scopes; CFOP-36 added tier and host. This closes the row.
            store = getattr(self, 'auth_store', None)
            if store is not None:
                store.record(EVENT_COCKPIT_SESSION, actor=actor,
                             target=f"investigation:{investigation_id}",
                             source_ip=request.remote_addr,
                             investigation_id=investigation_id, outcome=outcome,
                             tier=detail['tier'], host=detail['host'],
                             duration_seconds=detail['duration_seconds'],
                             learning_id=detail['learning_id'], degraded=degraded)

            logger.info("cockpit session recorded on #%s by %s (%s, tier=%s, %ss)",
                        investigation_id, actor, outcome, detail['tier'] or '-',
                        detail['duration_seconds'])
            return jsonify({'investigation_id': investigation_id,
                            'outcome': outcome, 'degraded': degraded}), 201

        @self.app.route('/api/kb/search')
        def kb_search():
            """Thin KB search for the MCP facade (search_knowledge tool).

            Hybrid (vector + FTS) when the embedding service is up, FTS-only
            otherwise — same fallback ladder the OODA loop uses. Kept as an
            HTTP route so the MCP server never imports knowledge_base
            (facade rule: no DB/embedding deps outside the agent).
            """
            q = (request.args.get('q') or '').strip()
            if not q:
                return jsonify({'error': 'query parameter q is required'}), 400
            limit = min(request.args.get('limit', 5, type=int), 25)
            try:
                query_embedding = None
                try:
                    if self.operator.embeddings.is_available():
                        query_embedding = self.operator.embeddings.generate_embedding(q)
                except Exception as e:
                    logger.warning(f"Query embedding failed, falling back to FTS search: {e}")
                if query_embedding:
                    results = self.operator.kb._kb.find_learnings_hybrid(
                        query_text=q, query_embedding=query_embedding, limit=limit)
                    mode = 'hybrid'
                else:
                    results = self.operator.kb.find_learnings(query=q, limit=limit)
                    mode = 'fts'
                return jsonify({'query': q, 'mode': mode,
                                'results': results, 'count': len(results)})
            except Exception as e:
                logger.error(f"KB search failed: {e}")
                return jsonify({'error': str(e), 'results': []}), 500

        @self.app.route('/api/learnings', methods=['POST'])
        @require_token_scope('investigate')
        def create_learning():
            """Write seam for out-of-process components (CFOP-47).

            The discovery pass — and later the cockpit write-back (CFOP-37) —
            seed learnings through here rather than touching the DB: the KB
            schema stays private to the monolith and token scopes stay the
            security model. Provenance (source / inferred / confidence) is
            folded into tags rather than new columns, so nothing needs a
            migration and the stamps are searchable.

            applies_when is required: store_learning() auto-deprecates a
            learning without a trigger condition (retrieval can never match
            it), so accepting one would report success while seeding nothing.
            """
            body = request.get_json(silent=True)
            if not isinstance(body, dict):
                return jsonify({'error': 'JSON object body required'}), 400
            missing = [f for f in ('learning_type', 'title', 'description', 'applies_when')
                       if not str(body.get(f) or '').strip()]
            if missing:
                return jsonify({'error': f"missing required fields: {', '.join(missing)}"}), 400
            allowed_types = ('pattern', 'solution', 'root_cause', 'antipattern', 'insight')
            learning_type = str(body['learning_type']).strip()
            if learning_type not in allowed_types:
                return jsonify({'error': f"learning_type must be one of {allowed_types}"}), 400

            tags = [str(t).strip() for t in (body.get('tags') or [])
                    if isinstance(t, (str, int, float)) and str(t).strip()][:20]
            source = str(body.get('source') or '').strip()
            if source:
                tags.append(f'source:{source[:50]}')
            if body.get('inferred'):
                tags.append('inferred')
            confidence = body.get('confidence')
            if isinstance(confidence, (int, float)) and 0 <= confidence <= 1:
                tags.append(f'confidence:{confidence:.2f}')

            try:
                learning_id = self.operator.kb.store_learning({
                    'learning_type': learning_type,
                    'title': str(body['title']).strip()[:500],
                    'description': str(body['description']).strip()[:20000],
                    'applies_when': str(body['applies_when']).strip()[:5000],
                    'solution_steps': body.get('solution_steps'),
                    'services': [str(s).strip() for s in (body.get('services') or [])
                                 if str(s).strip()][:20],
                    'tags': tags,
                    'category': (str(body['category']).strip()[:100]
                                 if body.get('category') else None),
                })
            except Exception as e:
                logger.error(f"store learning failed: {e}")
                return jsonify({'error': str(e)}), 500
            if not learning_id or learning_id < 0:
                # The buffered KB returns -1 when the DB is unreachable; that is
                # an outage, and 201 here would vanish the learning silently.
                return jsonify({'error': 'knowledge base unavailable'}), 503
            return jsonify({'id': learning_id, 'tags': tags}), 201

        @self.app.route('/api/learnings/<int:learning_id>', methods=['DELETE'])
        @require_role(ROLE_ADMIN)
        def delete_learning(learning_id):
            """Retire one learning (soft: deprecate, reversible in the DB).

            The reviewability contract for discovery-seeded learnings — wrong
            inferred context must be individually removable, because it feeds
            every future investigation's orient phase.
            """
            try:
                ok = self.operator.kb.deprecate_learning(learning_id)
            except Exception as e:
                logger.error(f"deprecate learning {learning_id} failed: {e}")
                return jsonify({'error': str(e)}), 500
            if not ok:
                return jsonify({'error': 'not found'}), 404
            return jsonify({'id': learning_id, 'deprecated': True})

        @self.app.route('/api/sweep-reports')
        def sweep_reports():
            """Get recent sweep reports."""
            try:
                limit = request.args.get('limit', 20, type=int)
                reports = self.operator.kb.get_recent_sweep_reports(limit=limit)
                return jsonify({'reports': reports})
            except Exception as e:
                logger.error(f"Error fetching sweep reports: {e}")
                return jsonify({'error': str(e), 'reports': []}), 500

        # Findings API
        @self.app.route('/api/findings')
        def list_findings():
            """List recent findings across sweep reports.

            Query params:
                limit: max reports to scan (default 10)
                status: filter by status (e.g., 'new', 'resolved')
            """
            try:
                limit = request.args.get('limit', 10, type=int)
                status_filter = request.args.get('status')
                reports = self.operator.kb.get_recent_sweep_reports(limit=limit)
                findings = []
                for report in reports:
                    for i, f in enumerate(report.get('findings', [])):
                        f_status = f.get('status', 'new')
                        if status_filter and f_status != status_filter:
                            continue
                        findings.append({
                            'report_id': report['id'],
                            'finding_index': i,
                            'swept_at': report['swept_at'],
                            'status': f_status,
                            'severity': f.get('severity', 'unknown'),
                            'finding': f.get('finding', ''),
                            'evidence': f.get('evidence', ''),
                            'resolution': f.get('resolution', ''),
                        })
                return jsonify({'findings': findings, 'count': len(findings)})
            except Exception as e:
                logger.error(f"Error fetching findings: {e}")
                return jsonify({'error': str(e)}), 500

        # Borderline — it edits a finding's triage state, not the fleet. Gated
        # anyway because marking a finding resolved or false_positive is what
        # stops it being fed to the remediation queue, so it silently decides
        # what the system will and will not act on.
        @self.app.route('/api/findings/<int:report_id>/<int:finding_index>', methods=['PATCH'])
        @require_role(ROLE_ADMIN)
        def update_finding(report_id, finding_index):
            """Update a finding's status and resolution.

            JSON body:
                status: 'resolved', 'acknowledged', 'investigating', 'false_positive'
                resolution: free-text description of what was done
            """
            try:
                data = request.json or {}
                status = data.get('status')
                resolution = data.get('resolution', '')

                if not status:
                    return jsonify({'error': 'status is required'}), 400

                valid_statuses = ('resolved', 'acknowledged', 'investigating', 'false_positive')
                if status not in valid_statuses:
                    return jsonify({'error': f'status must be one of: {", ".join(valid_statuses)}'}), 400

                finding_id = data.get('finding_id', '')
                ok = self.operator.kb.update_sweep_finding(
                    report_id, finding_index=finding_index, status=status,
                    resolution=resolution, finding_id=finding_id
                )
                if not ok:
                    return jsonify({'error': 'Finding not found'}), 404

                ref = finding_id or finding_index
                return jsonify({'success': True, 'report_id': report_id, 'finding': ref, 'status': status})
            except Exception as e:
                logger.error(f"Error updating finding: {e}")
                return jsonify({'error': str(e)}), 500

        # Ollama Pool status API
        @self.app.route('/api/pool/status')
        def pool_status():
            """Get Ollama pool status (instances, health, availability)."""
            if not hasattr(self.operator, 'ollama_pool') or not self.operator.ollama_pool:
                return jsonify({'error': 'Ollama pool not configured', 'instances': [], 'total': 0, 'healthy': 0, 'available': 0})
            return jsonify(self.operator.ollama_pool.status())

        @self.app.route('/api/pool/instance', methods=['POST'])
        @require_role(ROLE_ADMIN)
        def toggle_pool_instance():
            """Enable or disable an Ollama pool instance."""
            if not hasattr(self.operator, 'ollama_pool') or not self.operator.ollama_pool:
                return jsonify({'error': 'Ollama pool not configured'}), 400
            data = request.json
            name = data.get('name', '')
            enabled = data.get('enabled', True)
            ok = self.operator.ollama_pool.set_enabled(name, enabled)
            if not ok:
                return jsonify({'error': f'Instance {name} not found'}), 404
            return jsonify(self.operator.ollama_pool.status())

        # WebSocket endpoint (only if available)
        if self.sock:
            @self.sock.route('/ws')
            def websocket(ws):
                """
                WebSocket handler for real-time chat.

                Messages from client:
                    {"type": "chat", "message": "...", "history": [...], "backend": "auto"}
                    {"type": "answer", "question_id": 123, "answer": "..."}

                Messages to client:
                    {"type": "chat", "text": "...", "backend": "ollama", "model": "qwen3:14b"}
                    {"type": "question", "id": 123, "question": "...", "context": "..."}
                    {"type": "tool_call", "tool_name": "prometheus_query", "input": {...}}
                    {"type": "tool_result", "tool_name": "prometheus_query", "result": {...}}
                """
                logger.info("WebSocket client connected")
                self.ws_clients.append(ws)

                try:
                    while True:
                        message = ws.receive()
                        if message is None:
                            break

                        try:
                            data = json.loads(message)
                            self._handle_ws_message(ws, data)
                        except json.JSONDecodeError:
                            ws.send(json.dumps({'error': 'Invalid JSON'}))
                        except Exception as e:
                            logger.error(f"Error handling WS message: {e}", exc_info=True)
                            ws.send(json.dumps({'error': str(e)}))
                finally:
                    logger.info("WebSocket client disconnected")
                    self.ws_clients.remove(ws)

    # ---- cockpit (CFOP-35) ----------------------------------------------

    def _cockpit_spawner(self) -> CockpitSpawner:
        """The cockpit Job launcher, built once from the agent's config.

        Lazy rather than built in ``__init__`` so an install with no cockpit
        image configured pays nothing for it, and so tests can substitute a
        spawner with an injected kubectl runner before the first call.
        """
        spawner = getattr(self, '_cockpit', None)
        if spawner is None:
            spawner = CockpitSpawner(
                build_cockpit_config(getattr(self.operator, 'config', None)),
                token_minter=self._mint_cockpit_token,
                token_revoker=self._revoke_cockpit_token,
            )
            self._cockpit = spawner
        return spawner

    def _cockpit_ladder(self) -> HostCockpitSpawner:
        """Tiers 2/3 of the cockpit ladder, built once from the agent's config.

        Lazy for the same reason the tier-1 spawner is: an install with no SSH
        inventory never builds one, and a test can substitute a spawner with an
        injected ssh runner before the first call.
        """
        ladder = getattr(self, '_ladder', None)
        if ladder is None:
            ladder = HostCockpitSpawner(
                build_ladder_config(getattr(self.operator, 'config', None),
                                    self._cockpit_spawner().config),
                token_minter=self._mint_cockpit_token,
                token_revoker=self._revoke_cockpit_token,
            )
            self._ladder = ladder
        return ladder

    def _resolve_cockpit_host(self, investigation_id: int, inv: Dict[str, Any],
                              requested: str) -> tuple:
        """Which machine the incident is on.

        Explicitly *not* ``inv['host_id']`` — see ``resolve_target_host``, which
        documents why that field reads like the answer and is not one.
        """
        try:
            rows = self.operator.kb.list_remediations_for_investigation(investigation_id)
        except Exception as e:
            # A queue that cannot be read costs precision, not the spawn: the
            # trigger text and the caller's own --host are still there.
            logger.warning(f"cockpit: remediation lookup for #{investigation_id} failed: {e}")
            rows = []
        return resolve_target_host(
            requested=requested,
            remediation_hosts=[str(r.get('host_id') or '') for r in rows],
            investigation=inv,
            known_hosts=self._cockpit_ladder().known_host_names(),
        )

    def _choose_cockpit_tier(self, host: str, requested: str) -> tuple:
        """Ladder decision. Returns ``(tier, note, node)`` — the node lookup
        comes back with it so the tier-1 spawn can reuse it instead of asking
        the cluster the same question a second time.

        The ssh probe runs when it can change the answer, which is two cases:

        * the host is not a cluster node, so a pod is not where the session
          belongs; or
        * a host tier was *explicitly asked for*. Most of this fleet is both —
          a Kubernetes node and an entry in ``infrastructure.hosts`` — and for
          those, "give me a shell on the machine, not a pod scheduled onto it"
          is a real and frequent request. raspberrypi4 is the clearest case: it
          drives a physical kiosk, and a pod on that node cannot see the
          session, the display or the USB devices the incident is about.
          Without this branch the probe never ran for such a host, so
          ``choose_tier`` had no capabilities to honour the request with and
          refused it — correct on the evidence it had, and useless.

        Auto is deliberately unchanged: a cluster node still resolves to tier 1
        without an ssh round trip. The operator asks for the host when they
        want the host.
        """
        ladder = self._cockpit_ladder()
        node = self._cockpit_spawner().get_node(host) if host else None
        wants_host_tier = (requested or "").strip().lower() not in ("", TIER_AUTO, TIER_POD)
        caps = None
        if host and host in ladder.config.hosts and (node is None or wants_host_tier):
            # refresh on an explicit request: --tier is what an operator reaches
            # for after changing something on the host.
            caps = ladder.probe(host, refresh=wants_host_tier)
        tier, note = choose_tier(caps, requested=requested,
                                 is_cluster_node=node is not None,
                                 has_host=bool(host), allow_sudo=ladder.config.allow_sudo)
        return tier, note, node

    # ---- the browser bridge's server-side half (CFOP-75) -----------------

    def resolve_cockpit_session(self, investigation_id: int) -> Optional[Dict[str, Any]]:
        """Coordinates of the live cockpit for an investigation, or None.

        Host and tier are re-derived here exactly the way ``/api/cockpit/spawn``
        derives them, rather than being taken from the caller. A client-supplied
        host would let anyone holding a token aim a terminal at any machine in
        the inventory — the bridge authenticates *a person*, not *a target*.

        A tier-1 investigation is looked up through the pod spawner when the
        bridge serves that tier (Phase B); otherwise it comes back as a stub
        carrying only the tier, which the bridge refuses by name — and "this one
        is a pod, turn on the flag" is a more useful thing to read than "no
        session".
        """
        inv = self.operator.kb.get_investigation(investigation_id)
        if not inv:
            return None
        host, _provenance = self._resolve_cockpit_host(investigation_id, inv, '')
        tier, _note, _node = self._choose_cockpit_tier(host, TIER_AUTO)
        if tier == TIER_POD:
            if not self._bridge_config().pod_tier:
                return {'tier': TIER_POD, 'host': host,
                        'investigation_id': investigation_id}
            return self._cockpit_spawner().live_session(investigation_id)
        return self._cockpit_ladder().live_session(investigation_id, host=host)

    def verify_bridge_token(self, presented: str):
        """The same token check every other caller gets, or None with no store.

        No store means auth is disabled, which is a local-development posture —
        and a development posture must not silently become "anyone may open a
        terminal on a production host". Refusing here is the safe direction.
        """
        store = getattr(self, 'auth_store', None)
        if store is None:
            return None
        identity = store.verify_token(presented)
        # A bridge ticket is spent by being verified. It was minted for this
        # one handshake (see /api/cockpit/<id>/open), so once the bridge has
        # its answer the credential has no further purpose — and a page that
        # somehow kept it holds nothing. The session token is not a ticket
        # and is left alone: it is what the session on the host authenticates
        # with, for as long as the session lives.
        if (identity is not None and identity.token_id
                and _BRIDGE_TICKET_LABEL_RE.match(str(identity.label or ''))):
            try:
                row = store.revoke_token(identity.token_id, actor=identity.label)
                store.record(EVENT_TOKEN_REVOKED, actor=identity.label,
                             target=row['token_prefix'], source_ip=None,
                             token_id=row['id'], label=row['label'],
                             cockpit=True, bridge=True, spent=True)
            except Exception as e:
                logger.warning(f"cockpit bridge ticket could not be spent: {e}")
        return identity

    # ---- the browser cockpit's console half (CFOP-59) --------------------

    def _bridge_config(self):
        """The bridge's own reading of the ``cockpit:`` block, so the console
        refuses on exactly the facts the listener would. Imported here rather
        than at module load: cockpit_bridge is optional in the image."""
        from cockpit_bridge import build_bridge_config
        return build_bridge_config(getattr(self.operator, 'config', None))

    @staticmethod
    def _console_origin() -> str:
        """Where this request's page lives, in the bridge's normalised form.

        The Origin header when the browser sent one; otherwise the scheme and
        host the page was served from, which is the same value for a
        same-origin fetch. Checked against ``bridge_origins`` *here* so the
        operator reads "add http://x:8083 to bridge_origins" from the console,
        instead of a 4403 from a websocket that never says which origin it saw.
        """
        from cockpit_bridge import normalize_origin
        return normalize_origin(request.headers.get('Origin')
                                or f"{request.scheme}://{request.host}")

    @staticmethod
    def _bridge_url(bridge, investigation_id: int) -> str:
        """The websocket the page should open.

        Computed here rather than in the page: the agent is hostNetwork, so
        the bridge answers on the console's own host at its own port, and the
        console knows both. ``ws`` because the console is served over plain
        HTTP on the LAN; ``wss`` follows an https console.
        """
        host = request.host
        if host.startswith('['):
            host = host.split(']', 1)[0] + ']'
        else:
            host = host.rsplit(':', 1)[0]
        scheme = 'wss' if request.scheme == 'https' else 'ws'
        return f"{scheme}://{host}:{bridge.port}/cockpit/{int(investigation_id)}"

    def _mint_bridge_ticket(self, investigation_id: int, *, tier: str = "",
                            host: str = "") -> Dict[str, Any]:
        """A credential for one handshake, through the same mint as every
        other token — one more caller of it, not a second way to create
        credentials. ``investigate`` scope: what the bridge requires, and
        what `cfassist attach` mints, so a browser terminal is no more reach
        than a terminal one."""
        store = getattr(self, 'auth_store', None)
        if store is None:
            raise CockpitSpawnError(
                'the token store is unavailable, so no bridge ticket can be minted', 503)
        label = COCKPIT_BRIDGE_TICKET_LABEL.format(investigation_id=investigation_id)
        try:
            row, secret = store.create_token(
                label, ['investigate'],
                created_by=_caller_user_id(),
                creator_role=_effective_role() or ROLE_MEMBER,
                ttl_seconds=BRIDGE_TICKET_TTL_SECONDS,
            )
        except AuthError as e:
            raise CockpitSpawnError(str(e), 400)
        except Exception as e:
            logger.error(f"bridge ticket mint failed: {e}", exc_info=True)
            raise CockpitSpawnError('authentication backend unavailable', 503)
        store.record(EVENT_TOKEN_CREATED, actor=_actor(), target=row['token_prefix'],
                     source_ip=request.remote_addr, token_id=row['id'],
                     label=row['label'], scopes=row['scopes'],
                     investigation_id=investigation_id,
                     ttl_seconds=BRIDGE_TICKET_TTL_SECONDS,
                     cockpit=True, bridge=True, tier=tier, host=host)
        return {'id': row['id'], 'prefix': row['token_prefix'], 'secret': secret}

    def _revoke_cockpit_tokens(self, investigation_id: int) -> int:
        """Every active token minted for this investigation's cockpit — the
        session token on the host and any ticket not yet spent. Returns how
        many were revoked. "The token dead" is the half of a kill the host
        cannot do for itself."""
        store = getattr(self, 'auth_store', None)
        if store is None:
            return 0
        labels = {COCKPIT_SESSION_TOKEN_LABEL.format(investigation_id=investigation_id),
                  COCKPIT_BRIDGE_TICKET_LABEL.format(investigation_id=investigation_id)}
        revoked = 0
        for token in store.list_tokens():
            if token.get('label') in labels and token.get('status') == 'active':
                self._revoke_cockpit_token(int(token['id']))
                revoked += 1
        return revoked

    def record_bridge_event(self, *, event: str, actor: str, investigation_id=None,
                            tier=None, host=None, session_name=None) -> None:
        """Audit a terminal opening and closing.

        Opening a shell on a production host is exactly what someone asks about
        afterwards, and 'who' plus 'which machine' is the answer. Source IP is
        absent on purpose rather than by omission: this runs off the Flask
        request context, so there is no `request.remote_addr` to read, and a
        wrong one would be worse than none.
        """
        store = getattr(self, 'auth_store', None)
        if store is None:
            return
        try:
            store.record(EVENT_COCKPIT_BRIDGE, actor=actor,
                         target=f"investigation:{investigation_id}",
                         source_ip=None, investigation_id=investigation_id,
                         phase=event, tier=tier, host=host,
                         session_name=session_name)
        except Exception as e:
            logger.warning(f"cockpit bridge audit failed: {e}")

    def reap_cockpits(self) -> int:
        """Janitor entry point for the agent's worker loop (CFOP-36).

        Kubernetes reaps tier 1 for free (activeDeadlineSeconds plus ownership
        GC); nothing reaps a container or a /tmp directory on a Pi, and the
        session that leaked is by definition the one whose operator's laptop
        went away. Returns how many artifacts were removed.
        """
        try:
            return len(self._cockpit_ladder().reap())
        except Exception as e:
            logger.warning(f"cockpit janitor sweep failed: {e}")
            return 0

    def _mint_cockpit_token(self, investigation_id: int, ttl_seconds: int, *,
                            tier: str = TIER_POD, host: str = "") -> Dict[str, Any]:
        """Mint the pod's credential through the CFOP-32 session-token path.

        Same store call, same role ceiling and same audit event as
        ``POST /api/auth/tokens`` — the cockpit is one more caller of that
        mint, not a second way to create credentials. ``investigate`` scope
        only: the cockpit reads, and the write path stays the PR/console gate
        even from inside a pod on the affected node.
        """
        store = getattr(self, 'auth_store', None)
        if store is None:
            raise CockpitSpawnError(
                'the token store is unavailable, so no per-investigation session '
                'token can be minted', 503)
        try:
            row, secret = store.create_token(
                COCKPIT_SESSION_TOKEN_LABEL.format(investigation_id=investigation_id),
                ['investigate'],
                created_by=_caller_user_id(),
                creator_role=_effective_role() or ROLE_MEMBER,
                ttl_seconds=ttl_seconds,
            )
        except AuthError as e:
            # The auth API's rule, kept here: the caller got it wrong (400) and
            # the database being down (503) must never be reported as the same
            # thing, and neither is a permission decision.
            raise CockpitSpawnError(str(e), 400)
        except Exception as e:
            logger.error(f"cockpit token mint failed: {e}", exc_info=True)
            raise CockpitSpawnError('authentication backend unavailable', 503)
        store.record(EVENT_TOKEN_CREATED, actor=_actor(), target=row['token_prefix'],
                     source_ip=request.remote_addr, token_id=row['id'],
                     label=row['label'], scopes=row['scopes'],
                     investigation_id=investigation_id, ttl_seconds=ttl_seconds,
                     cockpit=True, tier=tier, host=host)
        return {'id': row['id'], 'prefix': row['token_prefix'], 'secret': secret}

    def _revoke_cockpit_token(self, token_id: int) -> None:
        """Undo a mint whose Job never started."""
        store = getattr(self, 'auth_store', None)
        if store is None:
            return
        row = store.revoke_token(token_id, actor=_actor())
        store.record(EVENT_TOKEN_REVOKED, actor=_actor(), target=row['token_prefix'],
                     source_ip=request.remote_addr, token_id=row['id'],
                     label=row['label'], cockpit=True)

    def _handle_ws_message(self, ws, data: Dict[str, Any]):
        """Handle incoming WebSocket message."""
        msg_type = data.get('type')

        if msg_type == 'chat':
            # User sent chat message
            message = data.get('message', '')
            history = data.get('history', [])
            backend = data.get('backend', 'auto')
            model = data.get('model')

            # Handle in background to not block WebSocket
            def handle_chat():
                try:
                    result = self.operator.handle_chat_message(message, history, backend, model=model)
                    ws.send(json.dumps({
                        'type': 'chat',
                        'text': result.get('response', ''),
                        'backend': result.get('backend', ''),
                        'model': result.get('model', '')
                    }))
                except Exception as e:
                    logger.error(f"Error in chat handler: {e}", exc_info=True)
                    ws.send(json.dumps({'type': 'error', 'error': str(e)}))

            thread = threading.Thread(target=handle_chat, daemon=True)
            thread.start()

        elif msg_type == 'answer':
            # User answered a question
            question_id = data.get('question_id')
            answer = data.get('answer')

            try:
                self.operator.answer_question(question_id, answer)
                ws.send(json.dumps({'type': 'ack', 'question_id': question_id}))
            except Exception as e:
                ws.send(json.dumps({'type': 'error', 'error': str(e)}))

    def broadcast(self, message: Dict[str, Any]):
        """Broadcast message to all connected WebSocket clients."""
        msg_json = json.dumps(message)
        for ws in self.ws_clients:
            try:
                ws.send(msg_json)
            except Exception as e:
                logger.error(f"Error broadcasting to client: {e}")

    def broadcast_question(self, question_id: int, question: str, context: str = '', investigation_id: Optional[int] = None):
        """Broadcast a pending question to all clients."""
        self.broadcast({
            'type': 'question',
            'id': question_id,
            'question': question,
            'context': context,
            'investigation_id': investigation_id
        })

    def broadcast_tool_call(self, tool_name: str, tool_input: Dict[str, Any]):
        """Broadcast tool execution to all clients."""
        self.broadcast({
            'type': 'tool_call',
            'tool_name': tool_name,
            'input': tool_input
        })

    def broadcast_tool_result(self, tool_name: str, result: Any):
        """Broadcast tool result to all clients."""
        self.broadcast({
            'type': 'tool_result',
            'tool_name': tool_name,
            'result': result
        })

    def run(self):
        """Start the web server using Waitress (blocking, production-ready)."""
        from waitress import serve
        logger.info(f"Starting Waitress web server on {self.host}:{self.port}")
        # Waitress is production-ready, multi-threaded, and works great with Flask
        serve(self.app, host=self.host, port=self.port, threads=8)

    def run_threaded(self):
        """Start the web server in a separate thread."""
        thread = threading.Thread(target=self.run, daemon=True)
        thread.start()
        logger.info("Web server thread started (Waitress)")
        return thread
