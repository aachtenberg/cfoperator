"""Portable runtime bootstrap for minimal setup deployments."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote

# Config semantics shared with the agent — one loader, one default schema, so
# the two consumers of config.yaml cannot drift. `cfshared` is stdlib-only (it
# imports PyYAML lazily) precisely so this runtime keeps its portable posture.
from cfshared import config as shared_config

from .defaults import (
    HostContextProvider,
    JsonFileScheduler,
    OpenReasoningDecisionEngine,
    build_default_alert_policies,
    build_default_action_handlers,
    build_default_host_observability_plugins,
)
from .deep_investigation import (
    DeepInvestigationActionHandler,
    EscalationRoutingDecisionEngine,
    build_deep_investigation_config,
)
from .git_context import GitChangeContextProvider
from .github_actions import build_github_action_handlers
from .http_actions import (
    build_http_investigate_handler,
    build_http_triage_engine,
    log_completion_endpoint_status,
    log_runtime_auth_status,
)
from .escalation import EscalationLedger
from .notifications import SlackNotificationSink, DiscordNotificationSink, NtfyNotificationSink
from .plugins import AlertSource
from .sources import AlertmanagerAlertSource
from .engine import EventRuntime
from .plugin_manager import PluginManager
from .state.composite import CompositeStateSink
from .state.local_outbox import LocalOutboxStateSink
from .state.postgres import PostgresStateSink
from .state.replay import ReplayingStateSink
from .worker import BackgroundAlertWorker, FileBackedWorkerState


def build_portable_runtime(config_path: str | None = None) -> EventRuntime:
    """Build a runtime that runs with only Python stdlib dependencies."""
    _load_env_file(config_path)
    base_dir = Path(os.getenv("CFOP_EVENT_RUNTIME_DIR", str(Path.home() / ".cfoperator" / "event-runtime")))
    outbox_dir = os.getenv("CFOP_EVENT_RUNTIME_OUTBOX_DIR", str(base_dir / "outbox"))
    schedule_dir = os.getenv("CFOP_EVENT_RUNTIME_SCHEDULE_DIR", str(base_dir / "scheduled"))
    replay_interval = int(os.getenv("CFOP_EVENT_RUNTIME_REPLAY_INTERVAL_SECONDS", "30"))
    pg_settings = _load_postgres_sink_config(config_path)

    local_sink = LocalOutboxStateSink(directory=outbox_dir)
    if pg_settings["dsn"]:
        sink = ReplayingStateSink(
            local_sink=local_sink,
            remote_sinks=[PostgresStateSink(dsn=pg_settings["dsn"], table_name=pg_settings["table_name"])],
            replay_interval_seconds=replay_interval,
        )
    else:
        sink = CompositeStateSink([local_sink])

    plugins = PluginManager()
    plugins.register_state_sink(sink)
    # Decision engine: when CFOP_AGENT_URL is set, route triage through the
    # agent's /v1/triage endpoint so the LLM classifies each alert into
    # log_only / notify / investigate / escalate. Falls back to the
    # portable OpenReasoningDecisionEngine (which always returns
    # 'investigate') when no agent is configured.
    agent_url_for_triage = os.getenv("CFOP_AGENT_URL", "").strip()
    triage_engine = build_http_triage_engine(agent_url_for_triage or None)
    decision_engine = triage_engine or OpenReasoningDecisionEngine()
    # Deep-investigation tier (disabled by default). When enabled, host-shaped
    # escalate / low-confidence-investigate verdicts are rerouted to an
    # ephemeral forensics Job, and the previously-unhandled `escalate` action
    # gains a notify fallback for workload alerts. Requires a completion base
    # URL so the Job can post its report back — missing URL keeps the tier
    # off (same skip-when-unconfigured pattern as the ntfy sink).
    deep_cfg = build_deep_investigation_config(_load_root_config(config_path).get("event_runtime") or {})
    if deep_cfg.enabled and not deep_cfg.completion_base_url:
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "Deep investigation enabled but no completion_base_url / "
            "CFOP_DEEP_COMPLETION_BASE_URL configured; tier stays disabled."
        )
    if deep_cfg.enabled and deep_cfg.completion_base_url:
        decision_engine = EscalationRoutingDecisionEngine(
            decision_engine,
            confidence_threshold=deep_cfg.confidence_threshold,
            route_escalate=deep_cfg.route_escalate,
            route_low_confidence_investigate=deep_cfg.route_low_confidence_investigate,
            escalate_fallback_action=deep_cfg.escalate_fallback_action,
            route_boot_forensics=deep_cfg.route_boot_forensics,
        )
        plugins.register_action_handler(DeepInvestigationActionHandler(deep_cfg))
    plugins.register_decision_engine(decision_engine)
    for policy in build_default_alert_policies(str(base_dir)):
        plugins.register_alert_policy(policy)
    plugins.register_context_provider(HostContextProvider())
    host_observability_providers, host_context = build_default_host_observability_plugins(config_path=config_path)
    for provider in host_observability_providers:
        plugins.register_host_observability_provider(provider)
    if host_context is not None:
        plugins.register_context_provider(host_context)
    scheduler = _build_scheduler_plugin(config_path=config_path, schedule_dir=schedule_dir, pg_settings=pg_settings)
    plugins.register_scheduler(scheduler)
    if isinstance(scheduler, AlertSource):
        plugins.register_alert_source(scheduler)
    for handler in build_default_action_handlers().values():
        plugins.register_action_handler(handler)

    # HTTP-backed investigate handler — replaces the stub when an agent URL
    # is configured (CFOP_AGENT_URL). PluginManager keys by action_name, so
    # registering this overwrites the default. Without an agent URL we keep
    # the stub so portable deployments still work.
    agent_url = os.getenv("CFOP_AGENT_URL", "").strip()
    http_investigate = build_http_investigate_handler(agent_url or None)
    if http_investigate is not None:
        plugins.register_action_handler(http_investigate)
    # Logging is configured by the time this runs (both __main__ and the
    # FastAPI adapter call _configure_logging / uvicorn's setup before
    # build_portable_runtime). Module-level logging at import time would
    # fire too early and get swallowed.
    log_completion_endpoint_status()
    log_runtime_auth_status()

    # Git / GitHub integration (gated on config or env vars)
    git_config = _load_git_config(config_path)
    git_repos = git_config.get("repos") or []
    github_settings = git_config.get("github") or {}
    github_token = (
        os.getenv("CFOP_GITHUB_TOKEN", "").strip()
        or os.getenv("GITHUB_TOKEN", "").strip()
        or str(github_settings.get("token") or "").strip()
    )
    github_api_url = os.getenv("CFOP_GITHUB_API_URL", "").strip() or str(github_settings.get("api_url") or "https://api.github.com")
    if git_repos:
        plugins.register_context_provider(
            GitChangeContextProvider(
                repos=git_repos,
                github_token=github_token or None,
                github_api_url=github_api_url,
            )
        )
        for handler in build_github_action_handlers(
            repos=git_repos,
            github_token=github_token or None,
            github_api_url=github_api_url,
        ).values():
            plugins.register_action_handler(handler)

    # Notification sinks (from observability.notifications config or env vars)
    for sink in _build_notification_sinks(config_path):
        plugins.register_notification_sink(sink)

    # Resolution notifications: when an escalated alert clears, emit a one-time
    # ":white_check_mark: Resolved:" notice. The ledger is shared by reference
    # between the source (which detects the clear) and the runtime (which marks
    # the escalation). Disable with CFOP_EVENT_RUNTIME_RESOLUTION_NOTIFICATIONS=false.
    resolution_enabled = os.getenv(
        "CFOP_EVENT_RUNTIME_RESOLUTION_NOTIFICATIONS", "true"
    ).strip().lower() not in ("0", "false", "no")
    escalation_ledger = EscalationLedger() if resolution_enabled else None

    alertmanager_url = os.getenv("CFOP_EVENT_RUNTIME_ALERTMANAGER_URL", "").strip()
    if alertmanager_url:
        plugins.register_alert_source(
            AlertmanagerAlertSource(url=alertmanager_url, escalation_ledger=escalation_ledger)
        )

    return EventRuntime(plugins, escalation_ledger=escalation_ledger)


def build_portable_worker(
    runtime: EventRuntime | None = None,
    config_path: str | None = None,
) -> BackgroundAlertWorker | None:
    """Build an optional background worker queue for async alert processing."""
    worker_count = int(os.getenv("CFOP_EVENT_RUNTIME_WORKER_COUNT", "1"))
    if worker_count <= 0:
        return None
    max_queue_size = int(os.getenv("CFOP_EVENT_RUNTIME_MAX_QUEUE_SIZE", "1000"))
    max_terminal_jobs = int(os.getenv("CFOP_EVENT_RUNTIME_MAX_TERMINAL_JOBS", "100"))
    max_retries = int(os.getenv("CFOP_EVENT_RUNTIME_MAX_RETRIES", "2"))
    base_dir = Path(os.getenv("CFOP_EVENT_RUNTIME_DIR", str(Path.home() / ".cfoperator" / "event-runtime")))
    queue_path = os.getenv("CFOP_EVENT_RUNTIME_QUEUE_STATE_PATH", str(base_dir / "queue" / "jobs.json"))
    return BackgroundAlertWorker(
        runtime=runtime or build_portable_runtime(config_path=config_path),
        worker_count=worker_count,
        max_queue_size=max_queue_size,
        max_terminal_jobs=max_terminal_jobs,
        max_retries=max_retries,
        state=FileBackedWorkerState(queue_path),
    )


def _build_notification_sinks(config_path: str | None = None) -> list:
    """Build notification sinks from config or environment variables.

    Reads ``observability.notifications`` from the YAML config (same block
    the agent uses) and falls back to ``SLACK_WEBHOOK_URL`` /
    ``DISCORD_WEBHOOK_URL`` environment variables.
    """
    import logging as _logging

    _log = _logging.getLogger(__name__)
    _load_env_file(config_path)

    sinks: list = []
    cfg = _load_root_config(config_path)
    notifications_cfg = (cfg.get("observability") or {}).get("notifications") or []

    for entry in notifications_cfg:
        backend = str(entry.get("backend") or "").lower()
        webhook = str(entry.get("webhook_url") or "").strip()
        if backend == "slack":
            if not webhook:
                _log.info("Slack notification sink skipped (no webhook URL)")
                continue
            sinks.append(SlackNotificationSink(webhook_url=webhook))
            _log.info("Initialized Slack notification sink")
        elif backend == "discord":
            if not webhook:
                _log.info("Discord notification sink skipped (no webhook URL)")
                continue
            sinks.append(DiscordNotificationSink(webhook_url=webhook))
            _log.info("Initialized Discord notification sink")
        elif backend == "ntfy":
            # Every knob is config-driven; url/topic are required (and usually
            # ${NTFY_URL}/${NTFY_TOPIC} so the topic stays out of the public
            # repo). Empty url/topic -> skip, so the entry is inert until set.
            base_url = str(entry.get("url") or "").strip()
            topic = str(entry.get("topic") or "").strip()
            if not base_url or not topic:
                _log.info("ntfy notification sink skipped (missing url or topic)")
                continue
            sinks.append(
                NtfyNotificationSink(
                    base_url=base_url,
                    topic=topic,
                    title=entry.get("title"),
                    priority_map=entry.get("priority_map"),
                    tags_map=entry.get("tags_map"),
                    token=str(entry.get("token") or "").strip(),
                    timeout=int(entry.get("timeout") or 10),
                    default_priority=entry.get("default_priority"),
                )
            )
            # Don't log the topic: on a public ntfy server it's the only access
            # control, so emitting it would leak that secret into logs.
            _log.info("Initialized ntfy notification sink")

    # Fallback: env vars when no config entries matched
    if not sinks:
        slack_url = os.getenv("SLACK_WEBHOOK_URL", "").strip()
        if slack_url:
            sinks.append(SlackNotificationSink(webhook_url=slack_url))
            _log.info("Initialized Slack notification sink from SLACK_WEBHOOK_URL")
        discord_url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
        if discord_url:
            sinks.append(DiscordNotificationSink(webhook_url=discord_url))
            _log.info("Initialized Discord notification sink from DISCORD_WEBHOOK_URL")
        ntfy_url = os.getenv("NTFY_URL", "").strip()
        ntfy_topic = os.getenv("NTFY_TOPIC", "").strip()
        if ntfy_url and ntfy_topic:
            sinks.append(
                NtfyNotificationSink(
                    base_url=ntfy_url,
                    topic=ntfy_topic,
                    title=os.getenv("NTFY_TITLE") or None,
                    token=os.getenv("NTFY_TOKEN", "").strip(),
                    timeout=int(os.getenv("NTFY_TIMEOUT") or 10),
                    priority_map=_json_dict_or_none(os.getenv("NTFY_PRIORITY_MAP")),
                    tags_map=_json_dict_or_none(os.getenv("NTFY_TAGS_MAP")),
                    default_priority=os.getenv("NTFY_DEFAULT_PRIORITY") or None,
                )
            )
            _log.info("Initialized ntfy notification sink from NTFY_URL/NTFY_TOPIC")

    return sinks


def _json_dict_or_none(raw: str | None) -> dict | None:
    """Parse a JSON object env var (e.g. NTFY_PRIORITY_MAP) into a dict.

    Returns None on empty/invalid input so the sink falls back to its
    configurable defaults rather than erroring at startup.
    """
    if not raw or not raw.strip():
        return None
    import json as _json

    try:
        value = _json.loads(raw)
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _load_git_repos(config_path: str | None = None) -> list[dict]:
    """Load git repo config from env var or YAML config file.

    Precedence:
      1. ``CFOP_GIT_REPOS_JSON`` env var (inline JSON array)
      2. ``git.repos`` in the YAML config file
    """
    return _load_git_config(config_path).get("repos") or []


def _load_git_config(config_path: str | None = None) -> dict:
    """Load and expand the git config block from env var or YAML config file."""
    import json as _json

    _load_env_file(config_path)

    repos_json = os.getenv("CFOP_GIT_REPOS_JSON", "").strip()
    if repos_json:
        try:
            repos = _json.loads(repos_json)
            if isinstance(repos, list):
                return {"repos": repos, "github": {}}
        except _json.JSONDecodeError as exc:
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "Invalid JSON in CFOP_GIT_REPOS_JSON, falling back to YAML config: %s", exc
            )

    cfg = _load_root_config(config_path)
    git_cfg = cfg.get("git") or {}
    return {
        "repos": git_cfg.get("repos") or [],
        "github": git_cfg.get("github") or {},
    }


def _load_postgres_sink_config(config_path: str | None = None) -> dict:
    """Resolve runtime audit persistence settings from env vars and config.yaml."""
    cfg = _load_root_config(config_path)
    event_runtime_cfg = cfg.get("event_runtime") or {}
    persistence_cfg = (event_runtime_cfg.get("persistence") or {}).get("postgres") or {}
    env_dsn = os.getenv("CFOP_EVENT_RUNTIME_PG_DSN", "").strip()
    config_dsn = str(persistence_cfg.get("dsn") or "").strip()

    enabled = _env_flag(
        "CFOP_EVENT_RUNTIME_PG_ENABLED",
        bool(persistence_cfg.get("enabled")) or bool(env_dsn) or bool(config_dsn),
    )
    table_name = str(
        os.getenv("CFOP_EVENT_RUNTIME_PG_TABLE_NAME", "").strip()
        or persistence_cfg.get("table_name")
        or "event_runtime_events"
    )
    dsn = env_dsn or config_dsn
    if not dsn and enabled:
        dsn = _build_postgres_dsn(cfg.get("database") or {})

    return {
        "enabled": enabled,
        "dsn": dsn if enabled else "",
        "table_name": table_name,
    }


def _build_scheduler_plugin(config_path: str | None, schedule_dir: str, pg_settings: dict):
    scheduler_cfg = _load_scheduler_config(config_path=config_path, schedule_dir=schedule_dir, pg_settings=pg_settings)
    backend = scheduler_cfg["backend"]
    if backend == "apscheduler":
        from .scheduler_backends import APSchedulerScheduler

        return APSchedulerScheduler(
            jobstore_url=scheduler_cfg["jobstore_url"],
            spool_path=scheduler_cfg["spool_path"],
            misfire_grace_time_seconds=scheduler_cfg["misfire_grace_time_seconds"],
        )
    return JsonFileScheduler(directory=schedule_dir)


def _load_scheduler_config(config_path: str | None, schedule_dir: str, pg_settings: dict) -> dict:
    cfg = _load_root_config(config_path)
    event_runtime_cfg = cfg.get("event_runtime") or {}
    scheduler_cfg = (event_runtime_cfg.get("scheduler") or {}) if isinstance(event_runtime_cfg, dict) else {}

    raw_backend = str(
        os.getenv("CFOP_EVENT_RUNTIME_SCHEDULER_BACKEND", "").strip()
        or scheduler_cfg.get("backend")
        or "json-file"
    ).strip().lower()
    if raw_backend in {"json", "json-file", "json_file"}:
        backend = "json-file"
    elif raw_backend == "apscheduler":
        backend = "apscheduler"
    else:
        backend = "json-file"

    default_spool_path = str(Path(schedule_dir) / "apscheduler-fired.jsonl")
    spool_path = str(
        os.getenv("CFOP_EVENT_RUNTIME_APSCHEDULER_SPOOL_PATH", "").strip()
        or scheduler_cfg.get("spool_path")
        or default_spool_path
    )
    default_jobstore_url = _default_scheduler_jobstore_url(schedule_dir=schedule_dir, pg_settings=pg_settings)
    jobstore_url = str(
        os.getenv("CFOP_EVENT_RUNTIME_APSCHEDULER_JOBSTORE_URL", "").strip()
        or scheduler_cfg.get("jobstore_url")
        or default_jobstore_url
    )
    misfire_grace_time_seconds = int(
        os.getenv("CFOP_EVENT_RUNTIME_APSCHEDULER_MISFIRE_GRACE_SECONDS", "").strip()
        or scheduler_cfg.get("misfire_grace_time_seconds")
        or 300
    )

    return {
        "backend": backend,
        "jobstore_url": jobstore_url,
        "spool_path": spool_path,
        "misfire_grace_time_seconds": misfire_grace_time_seconds,
    }


def _default_scheduler_jobstore_url(schedule_dir: str, pg_settings: dict) -> str:
    dsn = str(pg_settings.get("dsn") or "").strip()
    if dsn:
        return dsn
    sqlite_path = Path(schedule_dir) / "apscheduler.sqlite"
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{sqlite_path}"


def _load_root_config(config_path: str | None = None) -> dict:
    """Load the root config, merged over the shared default schema.

    Delegates to ``cfshared.config`` so this runtime and the agent resolve the
    same file identically. This used to return ``{}`` on anything unexpected and
    then apply its own per-field ``env -> config -> literal`` fallbacks inline;
    those fallbacks still exist below, but they now sit on top of a config that
    always has the expected shape rather than compensating for its absence.
    """
    return shared_config.load_config(config_path)


def _load_env_file(config_path: str | None = None) -> None:
    """Load a colocated .env file so runtime config placeholders resolve consistently."""
    shared_config.load_env_file(config_path)


def _expand_env_vars(config: object) -> object:
    """Recursively expand ${VAR} references in config values."""
    return shared_config.expand_env_vars(config)


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _build_postgres_dsn(database_cfg: dict) -> str:
    """Build a PostgreSQL DSN from the expanded database config block."""
    if not isinstance(database_cfg, dict):
        return ""
    host = str(database_cfg.get("host") or "").strip()
    database = str(database_cfg.get("database") or "").strip()
    user = str(database_cfg.get("user") or "").strip()
    password = str(database_cfg.get("password") or "")
    if not host or not database or not user:
        return ""

    port = str(database_cfg.get("port") or "").strip()
    credentials = quote(user, safe="")
    if password:
        credentials = f"{credentials}:{quote(password, safe='')}"
    authority = f"{credentials}@{host}"
    if port:
        authority = f"{authority}:{port}"
    return f"postgresql://{authority}/{quote(database, safe='')}"