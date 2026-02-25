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
import threading
import uuid
from typing import Dict, Any, Optional
from flask import Flask, request, jsonify, send_from_directory
import time
import requests

# WebSocket support - disabled because Waitress (WSGI) doesn't support it
# The UI uses HTTP polling via /api/chat instead
WEBSOCKET_AVAILABLE = False

logger = logging.getLogger("cfoperator.web")

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

        logger.info(f"Web server initialized on {host}:{port}")

    def _setup_routes(self):
        """Setup Flask routes and WebSocket handlers."""

        # Static UI
        @self.app.route('/')
        def index():
            return send_from_directory('ui', 'index.html')

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
        def reload_config():
            result = self.operator.reload_config()
            return jsonify({'status': 'ok', **result})

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
        def select_model(backend):
            """Persist the user's model selection for a backend AND set as default provider."""
            data = request.json
            model_name = data.get('model', '')
            setting_key = f'{backend}_selected_model'
            try:
                # Save model selection for this backend
                self.operator.kb._kb.set_setting(setting_key, model_name)
                # Also set this backend as the system-wide default provider
                self.operator.kb._kb.set_setting('selected_backend', backend)
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
        def set_selected_provider():
            """Set the default LLM provider (without changing model)."""
            data = request.json
            backend = data.get('backend', 'auto')
            if backend not in ('auto', 'ollama', 'groq', 'anthropic'):
                return jsonify({'error': f'Invalid backend: {backend}'}), 400
            try:
                self.operator.kb._kb.set_setting('selected_backend', backend if backend != 'auto' else '')
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
        def set_max_tool_iterations():
            """Persist max tool iterations setting."""
            data = request.json
            val = max(1, min(50, int(data.get('max_tool_iterations', 10))))
            try:
                self.operator.kb._kb.set_setting('max_tool_iterations', str(val))
            except Exception as e:
                logger.warning(f"Could not persist max_tool_iterations (DB down?): {e}")
            return jsonify({'success': True, 'max_tool_iterations': val})

        # OODA interval settings
        @self.app.route('/api/settings/ooda')
        def get_ooda_settings():
            """Get current OODA loop intervals."""
            return jsonify({
                'alert_check_interval': self.operator._get_alert_check_interval(),
                'sweep_interval': self.operator._get_sweep_interval(),
            })

        @self.app.route('/api/settings/ooda', methods=['POST'])
        def set_ooda_settings():
            """Persist OODA loop interval settings."""
            data = request.json
            result = {}
            if 'alert_check_interval' in data:
                val = max(5, min(300, int(data['alert_check_interval'])))
                try:
                    self.operator.kb._kb.set_setting('alert_check_interval', str(val))
                except Exception as e:
                    logger.warning(f"Could not persist alert_check_interval: {e}")
                result['alert_check_interval'] = val
            if 'sweep_interval' in data:
                val = max(60, min(86400, int(data['sweep_interval'])))
                try:
                    self.operator.kb._kb.set_setting('sweep_interval', str(val))
                except Exception as e:
                    logger.warning(f"Could not persist sweep_interval: {e}")
                result['sweep_interval'] = val
            return jsonify({'success': True, **result})

        @self.app.route('/api/settings/fallback')
        def get_fallback_settings():
            """Get current fallback/paid escalation setting."""
            val = self.operator.kb.get_setting('allow_paid_escalation', 'false')
            enabled = val == 'true' or val is True
            return jsonify({'allow_paid_escalation': enabled})

        @self.app.route('/api/settings/fallback', methods=['POST'])
        def set_fallback_settings():
            """Toggle paid LLM fallback."""
            data = request.json
            enabled = bool(data.get('allow_paid_escalation', False))
            try:
                self.operator.kb._kb.set_setting('allow_paid_escalation', 'true' if enabled else 'false')
            except Exception as e:
                logger.warning(f"Could not persist fallback setting: {e}")
            return jsonify({'success': True, 'allow_paid_escalation': enabled})

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
                    'created': time.time()
                }

            def run_chat():
                try:
                    logger.debug(f"Chat {chat_id} started")
                    for evt in self.operator.handle_chat_message_stream(message, history, backend, model=model):
                        with self._sessions_lock:
                            session = self._chat_sessions.get(chat_id)
                            if session:
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
                    except Exception:
                        pass

            return jsonify({'success': True, 'updated': updated})

        # Sweep Reports API
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

        # Ollama Pool status API
        @self.app.route('/api/pool/status')
        def pool_status():
            """Get Ollama pool status (instances, health, availability)."""
            if not hasattr(self.operator, 'ollama_pool') or not self.operator.ollama_pool:
                return jsonify({'error': 'Ollama pool not configured', 'instances': [], 'total': 0, 'healthy': 0, 'available': 0})
            return jsonify(self.operator.ollama_pool.status())

        @self.app.route('/api/pool/instance', methods=['POST'])
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
