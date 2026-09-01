"""The one place that decides what a CFOperator config *means*.

Before CFOP-26 there were two loaders. ``agent.agent._load_config`` read the
YAML file and, if the file existed at all, ignored its own ``_default_config()``
entirely — so every setting the file omitted resolved to whatever literal
happened to be written at the call site, and ``_default_config()`` itself had
drifted into being incomplete (it had no ``llm`` section at all).
``event_runtime.bootstrap._load_root_config`` read the same file, returned
``{}`` on any problem, and then applied a *different* set of per-field
``env -> config -> literal`` fallbacks inline. Two loaders, two default
philosophies, one config file.

This module replaces both. The file is merged over a complete default schema,
so a getting-started config can be a dozen lines and a fully-specified one still
resolves exactly as it did.

Three rules make that safe:

**Dicts merge, lists and scalars replace.** A list in this config is always an
ordered choice made by the operator — the notification sinks, the LLM fallback
chain, the container backends. There is no defensible identity rule for merging
those element-wise, and any attempt at one makes "delete the default sink"
impossible to express.

**Defaults are shapes with empty endpoints, never guessed endpoints.** This is
the rule that makes merging under an existing config a no-op. The old
``_default_config()`` guessed ``http://loki:3100`` and a local Docker socket;
merged under a config that omits ``logs:``, that guess would quietly start a
Loki client aimed at a host that does not resolve — a behaviour change wearing a
default's clothes. So the optional backends default to ``url: ""`` and their
init paths read an empty URL as a clean "disabled", which is exactly what an
omitted section does today.

**A default may not contradict a call site.** Adding ``foo: 3`` here changes
what ``config.get('foo', 7)`` returns somewhere else. Every value below either
matches the literal already written at its call site or is a pure shape/
enablement key. Where no site-neutral value exists (``remediation.default_repo``
names somebody's manifest repo) the key is deliberately absent so the call
site's own fallback still wins.
"""

from __future__ import annotations

import copy
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger("cfoperator.config")


class ConfigError(Exception):
    """A config file exists but cannot be honoured.

    Raised rather than falling back to defaults, so that a broken file stops
    the process instead of producing a healthy-looking one that silently is
    not doing what its config says. See ``_read_yaml``.
    """


# ---------------------------------------------------------------------------
# Scope ladder
# ---------------------------------------------------------------------------
# Restated from auth/models.py rather than imported: that module declares
# SQLAlchemy models, and event_runtime's loader has to keep working on a host
# with nothing but the stdlib. Restating three strings is the lesser evil, but
# only while something checks the copies agree — test_config_merge.py does.

SCOPE_READ = "read"
SCOPE_INVESTIGATE = "investigate"
SCOPE_REMEDIATE = "remediate"
SCOPES = (SCOPE_READ, SCOPE_INVESTIGATE, SCOPE_REMEDIATE)

# The two console roles, restated from auth/models.py for the same reason:
# tools/ is imported wherever cfshared is, with nothing heavier on the path.
# The same agreement test pins these copies.
ROLE_ADMIN = "admin"
ROLE_MEMBER = "member"
ROLES = (ROLE_ADMIN, ROLE_MEMBER)

_SCOPE_IMPLIES = {
    SCOPE_READ: (SCOPE_READ,),
    SCOPE_INVESTIGATE: (SCOPE_INVESTIGATE, SCOPE_READ),
    SCOPE_REMEDIATE: (SCOPE_REMEDIATE, SCOPE_INVESTIGATE, SCOPE_READ),
}


def expand_scopes(scopes) -> frozenset:
    """Close a scope list over the tier hierarchy (``remediate`` implies ``read``)."""
    granted: set = set()
    for scope in scopes or ():
        granted.update(_SCOPE_IMPLIES.get(scope, ()))
    return frozenset(granted)


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------
# A profile is a *ceiling* on the scope ladder, not a separate concept: it is
# the deployment-wide analogue of ROLE_SCOPE_CEILING in auth/models.py. The
# console can toggle remediation flags live from the database, so the ceiling is
# applied twice — once to the merged config here, and again when a live override
# is read — or the console would be an escalation path around the profile.

PROFILE_INVESTIGATE = "investigate"
PROFILE_REMEDIATE = "remediate"
PROFILES = (PROFILE_INVESTIGATE, PROFILE_REMEDIATE)

PROFILE_CEILING = {
    PROFILE_INVESTIGATE: expand_scopes([SCOPE_INVESTIGATE]),
    PROFILE_REMEDIATE: expand_scopes([SCOPE_REMEDIATE]),
}

#: Boolean flags under ``remediation:`` that require the ``remediate`` scope.
REMEDIATION_FLAGS = (
    "enabled",
    "open_prs",
    "deep_open_prs",
    "queue_feed",
    "queue_drain",
    "queue_reap",
    "queue_verify",
)


def profile_allows(profile: str | None, scope: str) -> bool:
    """Does ``profile`` permit ``scope``?

    ``None`` means *unprofiled* and permits everything. That is not laziness:
    configs written before profiles existed must keep behaving exactly as they
    did, and the deployment that matters most is one of them. See
    ``resolve_profile``.
    """
    if profile is None:
        return True
    ceiling = PROFILE_CEILING.get(profile)
    if ceiling is None:
        # Unknown profile: fall back to the safer of the two rather than to
        # "anything goes", so a typo cannot silently arm the remediation path.
        ceiling = PROFILE_CEILING[PROFILE_INVESTIGATE]
    return scope in ceiling


# ---------------------------------------------------------------------------
# The default schema
# ---------------------------------------------------------------------------

DEFAULT_CONFIG: dict = {
    # None means "unprofiled". Only an explicit key (or a wholly absent config
    # file) engages the ceiling.
    "profile": None,

    "observability": {
        # `url: ""` is the disabled state. Every one of these is optional, and
        # loki explicitly so — the getting-started config names only what it has.
        "metrics": {"backend": "prometheus", "url": "", "timeout": 30},
        "logs": {"backend": "loki", "url": "", "timeout": 30},
        "alerts": {"backend": "alertmanager", "url": ""},
        # Empty rather than a guessed backend: nothing should dial out to a
        # runtime the operator did not name. When the file says nothing at all
        # and we can prove we are inside a cluster, load_config fills this in —
        # that is the "k8s can self-describe" path, and it cannot fire for a
        # config that already made a choice.
        "containers": [],
        "notifications": [],
    },

    "llm": {
        "primary": {"provider": "ollama", "url": "", "model": "", "timeout": 180},
        "fallback": [],
        "embeddings": {"provider": "ollama", "url": "", "model": "nomic-embed-text"},
    },

    # Not optional — the agent cannot run without its knowledge base — so this
    # gets conventional service-name defaults rather than an empty shape. The
    # password is filled from POSTGRES_PASSWORD at load time.
    "database": {
        "host": "postgres",
        "port": 5432,
        "database": "cfoperator",
        "user": "cfoperator",
        "password": "",
    },

    # Absence of a host inventory is a first-class state, not an error: the SSH
    # and discovery tools already switch themselves off when this is empty.
    "infrastructure": {"hosts": {}},

    "git": {"github": {}, "repos": []},

    "ooda": {
        "alert_check_interval": 10,
        "sweep_interval": 1800,
        "sweep": {
            "metrics": True,
            "logs": True,
            "containers": True,
            "baseline_drift": True,
            "learning_consolidation": True,
            "max_iterations": 12,
        },
        "noise": {
            "enabled": True,
            "recovered_restart_threshold": 3,
            "recovered_ready_stable_seconds": 600,
        },
        "morning_summary": {"enabled": True, "hour_start": 7, "hour_end": 9},
        "remediation_reap_interval_seconds": 300,
        "remediation_drain_interval_seconds": 60,
        "remediation_verify_interval_seconds": 300,
    },

    "chat": {
        "enabled": True,
        "port": 8083,
        "websocket": True,
        "max_tool_iterations": 10,
        "max_tool_result_chars": 6000,
    },

    "event_runtime": {
        "scheduler": {"backend": "json-file", "misfire_grace_time_seconds": 300},
        "persistence": {"postgres": {"enabled": False, "table_name": "event_runtime_events"}},
        "host_observability": {
            "enabled": True,
            "refresh_interval_seconds": 300,
            "default_to_local": True,
            "include_discovered_targets": True,
            "providers": [{"type": "local"}],
        },
        # Shape and enablement only. build_deep_investigation_config owns the
        # rest of this block's defaults and enumerating them here would be two
        # sources of truth for the same numbers.
        "deep_investigation": {"enabled": False},
    },

    # `default_repo` is deliberately absent: every value would be somebody's
    # manifest repo, and the call site's own fallback is the honest answer.
    "remediation": {
        "enabled": False,
        "open_prs": False,
        "deep_open_prs": False,
        "queue_feed": False,
        "queue_drain": False,
        "queue_reap": False,
        "queue_verify": False,
        "max_open_prs": 3,
        "max_drain_per_tick": 3,
        # CFOP-148: how cluster changes reach this site. Shape and enablement
        # only -- `repo` and `tool` are absent for the same reason
        # `default_repo` is, and `none` renders no prompt guidance at all, so
        # an installation that has not said how it deploys is never told a
        # guess about its own cluster.
        "delivery": {"mode": "none"},
        "executor": {"node_action": {"enabled": False}},
    },

    "kubernetes": {},

    "skills": {"directory": "./skills"},
}


def default_config() -> dict:
    """A fresh deep copy of the default schema."""
    return copy.deepcopy(DEFAULT_CONFIG)


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------


def deep_merge(base: Any, override: Any) -> Any:
    """Merge ``override`` onto ``base``: dicts recurse, everything else replaces.

    Neither argument is mutated — a loader that handed out references into
    ``DEFAULT_CONFIG`` would let one deployment's reload leak into the next.
    """
    if isinstance(base, dict) and isinstance(override, dict):
        merged = copy.deepcopy(base)
        for key, value in override.items():
            if key in merged:
                merged[key] = deep_merge(merged[key], value)
            else:
                merged[key] = copy.deepcopy(value)
        return merged
    return copy.deepcopy(override)


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------


def expand_env_vars(value: Any) -> Any:
    """Recursively expand whole-value ``${VAR}`` references.

    Deliberately not ``os.path.expandvars``: only a value that is *entirely* a
    placeholder is substituted, so a password containing a literal ``$`` cannot
    be mangled. An unset variable becomes ``""``, which every consumer already
    reads as "not configured".
    """
    if isinstance(value, dict):
        return {key: expand_env_vars(item) for key, item in value.items()}
    if isinstance(value, list):
        return [expand_env_vars(item) for item in value]
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        return os.getenv(value[2:-1], "")
    return value


def load_env_file(config_path: str | None = None) -> None:
    """Load a colocated ``.env`` so ``${VAR}`` placeholders resolve consistently.

    ``setdefault`` throughout: the real environment always beats the file, which
    is what makes a committed ``.env`` safe to keep next to a config.

    Set ``CFOP_NO_DOTENV`` to opt a process out entirely. That exists because
    the test suite must not inherit the developer's credentials: it used to,
    and a failing config assertion rendered live API keys, a database password
    and a GitHub token into its output (CFOP-44).

    A kill switch rather than a list of variables to blank, deliberately. The
    suite already blanked the two webhook variables and was still leaking six
    credentials, because ``.env`` grew and the list did not. Anything keyed on
    variable *names* rots the same way; this is keyed on the mechanism.
    """
    if os.getenv("CFOP_NO_DOTENV", "").strip():
        return

    candidates: list[Path] = []
    if config_path:
        candidates.append(Path(config_path).expanduser().resolve().parent / ".env")
    else:
        from_env = os.getenv("CONFIG_PATH", "").strip()
        if from_env:
            candidates.append(Path(from_env).expanduser().resolve().parent / ".env")
    candidates.append(Path.cwd() / ".env")

    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if not candidate.exists():
            continue
        try:
            lines = candidate.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            logger.warning("Could not read %s: %s", candidate, exc)
            continue
        for raw_line in lines:
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


# ---------------------------------------------------------------------------
# Aliases
# ---------------------------------------------------------------------------
# The getting-started shape from CFOP-26. These exist only in the file: they are
# folded into the canonical layout before the merge, so no consumer has to know
# there are two ways to spell a Prometheus URL.

_BACKEND_ALIASES = {
    "prometheus": ("metrics", "prometheus"),
    "loki": ("logs", "loki"),
    "alertmanager": ("alerts", "alertmanager"),
}

#: `notify:` key -> (backend, {notify key: sink key}), in emission order.
_NOTIFY_ALIASES = (
    ("slack_webhook", "slack", {"slack_webhook": "webhook_url"}),
    ("discord_webhook", "discord", {"discord_webhook": "webhook_url"}),
    ("ntfy_url", "ntfy", {"ntfy_url": "url", "ntfy_topic": "topic", "ntfy_token": "token"}),
)

#: Flat `llm:` key -> key under `llm.primary`.
_LLM_ALIASES = {
    "backend": "provider",
    "provider": "provider",
    "url": "url",
    "model": "model",
    "timeout": "timeout",
    "api_key": "api_key",
}


def normalize_aliases(raw: dict) -> dict:
    """Fold the short getting-started keys into the canonical layout.

    Canonical always wins: a file that writes both ``prometheus.url`` and
    ``observability.metrics.url`` gets the latter, because the long form is the
    more specific statement of intent.
    """
    if not isinstance(raw, dict):
        return {}
    config = copy.deepcopy(raw)
    observability = config.get("observability")
    observability = dict(observability) if isinstance(observability, dict) else {}

    for alias, (section, backend) in _BACKEND_ALIASES.items():
        block = config.pop(alias, None)
        if not isinstance(block, dict):
            continue
        folded = {"backend": backend}
        folded.update(block)
        # The canonical section, where present, overrides the alias key by key.
        existing = observability.get(section)
        observability[section] = deep_merge(folded, existing if isinstance(existing, dict) else {})

    notify = config.pop("notify", None)
    if isinstance(notify, dict) and not isinstance(observability.get("notifications"), list):
        sinks = []
        for required, backend, mapping in _NOTIFY_ALIASES:
            if not str(notify.get(required) or "").strip():
                # An unset ${VAR} expands to "" — never build a dead sink from it.
                continue
            sink = {"backend": backend}
            for notify_key, sink_key in mapping.items():
                value = str(notify.get(notify_key) or "").strip()
                if value:
                    sink[sink_key] = value
            sinks.append(sink)
        if sinks:
            observability["notifications"] = sinks

    if observability:
        config["observability"] = observability

    llm = config.get("llm")
    if isinstance(llm, dict):
        llm = dict(llm)
        primary = dict(llm.get("primary")) if isinstance(llm.get("primary"), dict) else {}
        for alias, canonical in _LLM_ALIASES.items():
            if alias in llm and not isinstance(llm[alias], (dict, list)):
                value = llm.pop(alias)
                primary.setdefault(canonical, value)
        if primary:
            llm["primary"] = primary
        config["llm"] = llm

    return config


# ---------------------------------------------------------------------------
# Profile resolution and clamping
# ---------------------------------------------------------------------------


def resolve_profile(raw: dict | None, *, file_present: bool) -> str | None:
    """Decide the effective profile.

    Three cases, and the middle one is the important one:

    * No config file at all -> ``investigate``. A brand-new install that has
      configured nothing should be read-only.
    * A file that does not mention ``profile:`` -> ``None`` (unprofiled). This
      is not what the issue asked for, and it is deliberate. Production runs
      ``open_prs`` and ``deep_open_prs`` with no ``profile:`` key and deploys
      automatically on merge to main, so defaulting an unprofiled file to
      ``investigate`` would switch the remediation pipeline off during a routine
      deploy, with no window in which anyone would notice. The getting-started
      config writes ``profile: investigate`` explicitly, so new installs still
      get the safe default; it simply is not applied retroactively.
    * An explicit ``profile:`` -> that profile, with an unrecognised value
      falling back to ``investigate`` so a typo cannot arm remediation.
    """
    if not file_present:
        return PROFILE_INVESTIGATE
    if not isinstance(raw, dict) or "profile" not in raw:
        return None
    value = raw.get("profile")
    if value is None:
        return None
    profile = str(value).strip().lower()
    if profile not in PROFILES:
        logger.warning(
            "Unknown profile %r in config; falling back to %r. Valid profiles: %s",
            value, PROFILE_INVESTIGATE, ", ".join(PROFILES),
        )
        return PROFILE_INVESTIGATE
    return profile


def clamp_to_profile(config: dict, profile: str | None) -> dict:
    """Force the remediation surface off when the profile does not reach it.

    Only the remediation flags are clamped. The deep-investigation tier is not,
    despite spawning Jobs: it is read-only by design and anything it proposes
    goes through the very PR gates this clamps, so gating it would make
    ``investigate`` mean *less* investigation.
    """
    if profile_allows(profile, SCOPE_REMEDIATE):
        return config

    remediation = config.get("remediation")
    if not isinstance(remediation, dict):
        return config
    disabled = [flag for flag in REMEDIATION_FLAGS if remediation.get(flag)]
    for flag in REMEDIATION_FLAGS:
        remediation[flag] = False
    executor = remediation.get("executor")
    if isinstance(executor, dict):
        node_action = executor.get("node_action")
        if isinstance(node_action, dict) and node_action.get("enabled"):
            node_action["enabled"] = False
            disabled.append("executor.node_action.enabled")
    if disabled:
        logger.warning(
            "profile: %s does not grant the '%s' scope — forcing remediation flags off: %s",
            profile, SCOPE_REMEDIATE, ", ".join(disabled),
        )
    return config


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _read_yaml(config_path: str) -> tuple[dict, bool]:
    """Return (mapping, file_was_present).

    Absent file -> defaults, which is a supported way to run. **File present
    but unusable -> raise.** Those are not the same situation and must not
    share an outcome:

    An operator who mounts a ConfigMap has stated an intent. Silently
    substituting defaults for it produces a process that looks healthy —
    ``/api/health`` is a liveness probe, not a config check — while running
    with empty Prometheus and Loki URLs and every remediation flag off. On a
    live deployment that is an unannounced outage of investigation and of
    ``open_prs``, with ArgoCD reporting Healthy throughout.

    Refusing to start is louder and cheaper to diagnose: under ``Recreate``
    the pod fails visibly instead of quietly doing nothing. This is what the
    pre-merge ``_load_config`` did by accident (it raised), and keeping it is
    deliberate.
    """
    if not os.path.exists(config_path):
        logger.warning("Config file %s not found, using defaults", config_path)
        return {}, False
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover - PyYAML is a hard dependency
        raise ConfigError(
            f"{config_path} exists but PyYAML is not installed, so it cannot be "
            "honoured. Refusing to start on defaults."
        ) from exc
    try:
        with open(config_path, "r", encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle)
    except Exception as exc:
        raise ConfigError(
            f"Failed to parse {config_path}: {exc}. Refusing to start on "
            "defaults — fix the file or remove it to run defaulted."
        ) from exc
    if loaded is None:
        # An empty file is not malformed: it says "no overrides", which is
        # exactly what the defaults express.
        return {}, True
    if not isinstance(loaded, dict):
        raise ConfigError(
            f"Config {config_path} is a {type(loaded).__name__}, not a mapping. "
            "Refusing to start on defaults."
        )
    return loaded, True


def _autodetect_containers() -> list:
    """Container backends to assume when the operator named none.

    ``KUBERNETES_SERVICE_HOST`` is injected into every in-cluster pod, so this
    is a fact about where we are running rather than a guess about what the
    operator wants — which is the whole "Kubernetes can self-describe" idea. It
    can only fire when the config says nothing about containers at all, so no
    existing deployment can reach it.
    """
    if os.getenv("KUBERNETES_SERVICE_HOST"):
        logger.info("No container backends configured; running in-cluster, defaulting to kubernetes")
        return [{"backend": "kubernetes"}]
    return []


def _apply_env_fallbacks(config: dict, raw: dict) -> None:
    """Restore the env fallbacks that call sites used to provide themselves.

    ``primary.get('url', os.getenv('OLLAMA_URL'))`` stops reaching its fallback
    the moment the merge guarantees the key exists, so the fallback has to move
    here or it silently disappears.
    """
    primary = config.get("llm", {}).get("primary")
    if isinstance(primary, dict) and not str(primary.get("url") or "").strip():
        primary["url"] = os.getenv("OLLAMA_URL", "")

    database = config.get("database")
    if isinstance(database, dict) and not str(database.get("password") or ""):
        database["password"] = os.getenv("POSTGRES_PASSWORD", "")

    observability = config.get("observability", {})
    if isinstance(observability, dict) and not observability.get("containers"):
        if not _user_set(raw, "observability", "containers"):
            observability["containers"] = _autodetect_containers()


def _user_set(raw: dict, *path: str) -> bool:
    node: Any = raw
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return False
        node = node[key]
    return True


def load_config(config_path: str | None = None) -> dict:
    """Load, expand, normalise, merge and clamp the root config.

    The order matters: aliases are folded before the merge (so the short and
    long forms cannot produce different defaults), and the profile is resolved
    from the *raw* file (so "did the operator write ``profile:``?" is still
    answerable after the merge has filled it in).
    """
    if config_path is None:
        config_path = os.getenv("CONFIG_PATH", "").strip() or "config.yaml"

    load_env_file(config_path)
    raw, file_present = _read_yaml(config_path)
    raw = expand_env_vars(raw)
    if not isinstance(raw, dict):
        raw = {}

    profile = resolve_profile(raw, file_present=file_present)
    normalized = normalize_aliases(raw)
    config = deep_merge(DEFAULT_CONFIG, normalized)
    config["profile"] = profile
    _apply_env_fallbacks(config, normalized)
    return clamp_to_profile(config, profile)
