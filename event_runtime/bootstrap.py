"""Portable runtime bootstrap for minimal setup deployments."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote

from .defaults import (
    HostContextProvider,
    JsonFileScheduler,
    OpenReasoningDecisionEngine,
    build_default_alert_policies,
    build_default_action_handlers,
    build_default_host_observability_plugins,
)
from .git_context import GitChangeContextProvider
from .github_actions import build_github_action_handlers
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
    plugins.register_decision_engine(OpenReasoningDecisionEngine())
    for policy in build_default_alert_policies(str(base_dir)):
        plugins.register_alert_policy(policy)
    plugins.register_context_provider(HostContextProvider())
    host_observability_providers, host_context = build_default_host_observability_plugins(config_path=config_path)
    for provider in host_observability_providers:
        plugins.register_host_observability_provider(provider)
    if host_context is not None:
        plugins.register_context_provider(host_context)
    plugins.register_scheduler(JsonFileScheduler(directory=schedule_dir))
    for handler in build_default_action_handlers().values():
        plugins.register_action_handler(handler)

    # Git / GitHub integration (gated on config or env vars)
    git_config = _load_git_config(config_path)
    git_repos = git_config.get("repos") or []
    github_settings = git_config.get("github") or {}
    github_token = os.getenv("CFOP_GITHUB_TOKEN", "").strip() or str(github_settings.get("token") or "").strip()
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

    alertmanager_url = os.getenv("CFOP_EVENT_RUNTIME_ALERTMANAGER_URL", "").strip()
    if alertmanager_url:
        plugins.register_alert_source(AlertmanagerAlertSource(url=alertmanager_url))

    return EventRuntime(plugins)


def build_portable_worker(
    runtime: EventRuntime | None = None,
    config_path: str | None = None,
) -> BackgroundAlertWorker | None:
    """Build an optional background worker queue for async alert processing."""
    worker_count = int(os.getenv("CFOP_EVENT_RUNTIME_WORKER_COUNT", "1"))
    if worker_count <= 0:
        return None
    max_queue_size = int(os.getenv("CFOP_EVENT_RUNTIME_MAX_QUEUE_SIZE", "1000"))
    max_terminal_jobs = int(os.getenv("CFOP_EVENT_RUNTIME_MAX_TERMINAL_JOBS", "1000"))
    base_dir = Path(os.getenv("CFOP_EVENT_RUNTIME_DIR", str(Path.home() / ".cfoperator" / "event-runtime")))
    queue_path = os.getenv("CFOP_EVENT_RUNTIME_QUEUE_STATE_PATH", str(base_dir / "queue" / "jobs.json"))
    return BackgroundAlertWorker(
        runtime=runtime or build_portable_runtime(config_path=config_path),
        worker_count=worker_count,
        max_queue_size=max_queue_size,
        max_terminal_jobs=max_terminal_jobs,
        state=FileBackedWorkerState(queue_path),
    )


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
        except _json.JSONDecodeError:
            pass

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


def _load_root_config(config_path: str | None = None) -> dict:
    """Load the expanded root YAML config when available."""
    _load_env_file(config_path)
    if config_path is None:
        config_path = os.getenv("CONFIG_PATH")
    if not config_path:
        return {}

    try:
        import yaml  # type: ignore[import-untyped]

        with open(config_path, "r") as fh:
            cfg = yaml.safe_load(fh) or {}
        if not isinstance(cfg, dict):
            return {}
        expanded = _expand_env_vars(cfg)
        return expanded if isinstance(expanded, dict) else {}
    except Exception:
        return {}


def _load_env_file(config_path: str | None = None) -> None:
    """Load a colocated .env file so runtime config placeholders resolve consistently."""
    path_candidates: list[Path] = []
    if config_path:
        path_candidates.append(Path(config_path).expanduser().resolve().parent / ".env")
    else:
        config_env = os.getenv("CONFIG_PATH", "").strip()
        if config_env:
            path_candidates.append(Path(config_env).expanduser().resolve().parent / ".env")
    path_candidates.append(Path.cwd() / ".env")

    seen: set[Path] = set()
    for candidate in path_candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if candidate.exists():
            for raw_line in candidate.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                if not key:
                    continue
                value = value.strip()
                if value and value[0] == value[-1] and value[0] in {'"', "'"}:
                    value = value[1:-1]
                os.environ.setdefault(key, value)


def _expand_env_vars(config: object) -> object:
    """Recursively expand ${VAR} references in config values."""
    if isinstance(config, dict):
        return {key: _expand_env_vars(value) for key, value in config.items()}
    if isinstance(config, list):
        return [_expand_env_vars(item) for item in config]
    if isinstance(config, str) and config.startswith("${") and config.endswith("}"):
        return os.getenv(config[2:-1], "")
    return config


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