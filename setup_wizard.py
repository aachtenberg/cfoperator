"""``cfoperator init`` — validated, discovery-driven config generation (CFOP-45).

The wizard's one hard constraint: **it has no field list of its own.** Every
question is generated at call time from the schema module (``cfshared.config``):
observability sections from ``_BACKEND_ALIASES``, notification sinks from
``_NOTIFY_ALIASES``, LLM keys from ``_LLM_ALIASES``, profile choices from
``PROFILES``. An alias added to the schema shows up here as a question (with a
generic reachability probe) without this file changing — and a key the wizard
deliberately does not ask about is listed in the final summary as
edit-the-file, from the same tables. ``test_setup_wizard.py`` guards exactly
this by extending the schema and asserting the plan grows.

Validation is the real loader, not a re-implementation: the emitted mapping
must normalise+merge into only known schema keys before anything is written,
and the written file is loaded back through ``cfshared.config.load_config`` and
compared against the answers. If the wizard and the loader ever disagree, the
wizard fails loudly instead of producing a confidently-wrong file.

Every answer is probed at the point of entry (stdlib urllib, no dependencies
beyond PyYAML — which the loader itself needs to honour the written file).
Interactively a failed probe re-prompts with the failure named; under
``--non-interactive`` it exits non-zero naming the section, so CI and the demo
drive the same path a human takes.

Outputs are ``config.yaml`` (getting-started alias shape, non-secrets inline,
secrets as ``${VAR}``) and ``.env`` (secrets, admin bootstrap credentials, plus
the endpoint variables under the established compose-trial names, because the
trial's ``deploy/compose/config.yaml`` reads those variables rather than this
file). The admin step writes ``CFOP_ADMIN_USERNAME``/``CFOP_ADMIN_PASSWORD``
for ``scripts/compose_bootstrap.py`` to seed at first boot — the database does
not exist yet at init time, so a direct write here would only race the seeding
path that already works.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from cfshared import config as cfg


class WizardError(Exception):
    """A fatal wizard failure whose message names what failed."""


# ---------------------------------------------------------------------------
# Presentation metadata
# ---------------------------------------------------------------------------
# Everything here is keyed on names that come *from the schema*; nothing below
# enumerates config fields. A schema entry with no metadata still gets asked —
# appended after the known sections, optional, generically probed — so a new
# alias surfaces instead of silently vanishing.

#: Interview order for observability sections, per the issue: what unblocks
#: what. Sections the schema grows beyond these are appended after `logs`.
_SECTION_ORDER = ("metrics", "alerts", "logs")

#: Sections the first run cannot do without. Everything else is skippable.
_REQUIRED_SECTIONS = frozenset({"metrics"})

#: notify keys whose established env names (compose, .env.example) predate the
#: derived `KEY.upper()` convention. Fallback for unlisted keys is derived, so
#: a new sink needs no entry here.
_ENV_NAME_OVERRIDES = {
    "slack_webhook": "SLACK_WEBHOOK_URL",
    "discord_webhook": "DISCORD_WEBHOOK_URL",
}

#: LLM alias keys the wizard asks about; the rest are reported as
#: edit-the-file in the summary, computed from the schema table.
_LLM_ASKED = ("provider", "url", "model", "api_key")

#: Shipped provider names (mirrors config.yaml.example). These are field
#: *values*, not fields — an unlisted provider is accepted with a warning.
_PROVIDERS = ("ollama", "groq", "xai", "gemini", "anthropic")

_OLLAMA_DEFAULT_URL = "http://localhost:11434"


def _env_name(key: str) -> str:
    return _ENV_NAME_OVERRIDES.get(key, key.upper())


# ---------------------------------------------------------------------------
# The plan: questions generated from the schema
# ---------------------------------------------------------------------------


def build_plan() -> List[Dict[str, Any]]:
    """The ordered interview, derived from ``cfshared.config`` at call time.

    Reading the tables here rather than at import keeps the derivation honest:
    the schema-extension test monkeypatches the tables and must see the plan
    change without this module being touched.
    """
    llm_keys = dict(cfg._LLM_ALIASES)
    llm_spec = {
        "kind": "llm",
        "keys": llm_keys,
        # Canonical (post-alias) key names the wizard never prompts for; the
        # summary points these at the file, so an alias key added to the
        # schema is surfaced even before anyone teaches the wizard to ask it.
        "unasked": sorted({llm_keys[k] for k in llm_keys} - set(_LLM_ASKED)),
        "defaults": cfg.default_config()["llm"]["primary"],
    }

    backends = []
    for alias, (section, backend) in cfg._BACKEND_ALIASES.items():
        backends.append({
            "kind": "backend",
            "alias": alias,
            "section": section,
            "backend": backend,
            "required": section in _REQUIRED_SECTIONS,
            "env_var": f"{alias.upper()}_URL",
        })
    rank = {name: i for i, name in enumerate(_SECTION_ORDER)}
    backends.sort(key=lambda spec: (rank.get(spec["section"], len(rank)), spec["alias"]))

    sinks = [
        {"alias": required, "backend": backend, "keys": dict(mapping)}
        for required, backend, mapping in cfg._NOTIFY_ALIASES
    ]

    return [llm_spec] + backends + [
        {"kind": "notify", "sinks": sinks},
        {"kind": "profile", "choices": list(cfg.PROFILES), "default": cfg.PROFILE_INVESTIGATE},
        {"kind": "admin"},
    ]


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------
# Each probe returns (ok, message) and never raises: the message is shown to
# the operator either way, and interactively a failure is a re-prompt, not a
# crash.


def _fetch(url: str, *, data: Optional[bytes] = None,
           headers: Optional[Dict[str, str]] = None, timeout: int = 8) -> Tuple[Optional[bytes], str]:
    """GET/POST returning (body, "") or (None, error). 4xx/5xx are errors here;
    reachability-only checks catch HTTPError themselves."""
    req = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec - operator-entered URL
            return resp.read(), ""
    except urllib.error.HTTPError as exc:
        return None, f"HTTP {exc.code} {exc.reason}"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return None, str(getattr(exc, "reason", None) or exc)


def _fetch_json(url: str, timeout: int = 8) -> Tuple[Optional[Any], str]:
    body, err = _fetch(url, headers={"Accept": "application/json"}, timeout=timeout)
    if body is None:
        return None, err
    try:
        return json.loads(body.decode("utf-8")), ""
    except ValueError as exc:
        return None, f"answered, but not with JSON: {exc}"


def probe_prometheus(url: str) -> Tuple[bool, str]:
    data, err = _fetch_json(f"{url.rstrip('/')}/api/v1/targets")
    if data is None:
        return False, err
    active = (data.get("data") or {}).get("activeTargets")
    if active is None:
        return False, "answered, but /api/v1/targets has no target list — is this Prometheus?"
    jobs = {(t.get("labels") or {}).get("job", "?") for t in active}
    up = sum(1 for t in active if t.get("health") == "up")
    return True, f"{len(active)} targets in {len(jobs)} jobs, {up} up"


def probe_alertmanager(url: str) -> Tuple[bool, str]:
    data, err = _fetch_json(f"{url.rstrip('/')}/api/v2/status")
    if data is None:
        return False, err
    version = ((data.get("versionInfo") or {}).get("version")) or "unknown version"
    return True, f"Alertmanager {version}"


def probe_loki(url: str) -> Tuple[bool, str]:
    data, err = _fetch_json(f"{url.rstrip('/')}/loki/api/v1/labels")
    if data is None:
        return False, err
    labels = data.get("data") or []
    return True, f"Loki answered, {len(labels)} labels"


def probe_reachable(url: str) -> Tuple[bool, str]:
    """Generic fallback for backends without a dedicated probe: any HTTP
    answer, error status included, proves something is listening there."""
    body, err = _fetch(url)
    if body is not None:
        return True, "answered"
    if err.startswith("HTTP "):
        return True, f"answered ({err})"
    return False, err


_BACKEND_PROBES: Dict[str, Callable[[str], Tuple[bool, str]]] = {
    "prometheus": probe_prometheus,
    "alertmanager": probe_alertmanager,
    "loki": probe_loki,
}


def probe_backend(backend: str, url: str) -> Tuple[bool, str]:
    return _BACKEND_PROBES.get(backend, probe_reachable)(url)


def probe_ollama(url: str) -> Tuple[bool, str, List[str]]:
    data, err = _fetch_json(f"{url.rstrip('/')}/api/tags")
    if data is None:
        return False, err, []
    models = [m.get("name", "") for m in data.get("models") or [] if m.get("name")]
    if not models:
        return True, "Ollama answered, but no models are pulled — `ollama pull` one first", []
    shown = ", ".join(models[:6]) + (", …" if len(models) > 6 else "")
    return True, f"models pulled: {shown}", models


def probe_notify(backend: str, values: Dict[str, str]) -> Tuple[bool, str]:
    """A real test post — only ever called on explicit confirmation, because a
    probe that spams a live channel by default is worse than no probe."""
    text = "cfoperator init: test notification — your sink is wired up."
    if backend in ("slack", "discord"):
        field = "text" if backend == "slack" else "content"
        body, err = _fetch(values.get("webhook_url", ""),
                           data=json.dumps({field: text}).encode("utf-8"),
                           headers={"Content-Type": "application/json"})
        return (True, "test post accepted") if body is not None else (False, err)
    if backend == "ntfy":
        url = values.get("url", "").rstrip("/")
        topic = values.get("topic", "")
        headers = {}
        if values.get("token"):
            headers["Authorization"] = f"Bearer {values['token']}"
        body, err = _fetch(f"{url}/{topic}", data=text.encode("utf-8"), headers=headers)
        return (True, "test post accepted") if body is not None else (False, err)
    return True, "no test post implemented for this sink; verified at first notification"


# ---------------------------------------------------------------------------
# Discovery-driven defaults
# ---------------------------------------------------------------------------
# Facts, not interpretation: bounded reads of what Prometheus already knows,
# offered as defaults. Keyed by backend name so a schema-added backend simply
# has no discoverer rather than breaking the loop.


def discover_alertmanager(prom_url: str) -> str:
    data, _ = _fetch_json(f"{prom_url.rstrip('/')}/api/v1/alertmanagers")
    if data is None:
        return ""
    for am in (data.get("data") or {}).get("activeAlertmanagers") or []:
        url = am.get("url") or ""
        # Prometheus reports the push endpoint (…/api/v2/alerts); the config
        # wants the base URL.
        cut = url.find("/api/")
        return url[:cut] if cut > 0 else url
    return ""


def discover_loki(prom_url: str) -> str:
    data, _ = _fetch_json(f"{prom_url.rstrip('/')}/api/v1/targets")
    if data is None:
        return ""
    for target in (data.get("data") or {}).get("activeTargets") or []:
        if "loki" in ((target.get("labels") or {}).get("job") or ""):
            scrape = target.get("scrapeUrl") or ""
            parsed = urllib.parse.urlsplit(scrape)
            if parsed.scheme and parsed.netloc:
                return f"{parsed.scheme}://{parsed.netloc}"
    return ""


_DISCOVERERS: Dict[str, Callable[[str], str]] = {
    "alertmanager": discover_alertmanager,
    "loki": discover_loki,
}


# ---------------------------------------------------------------------------
# Answer collection
# ---------------------------------------------------------------------------


def _ask(prompt: str, default: str = "", secret: bool = False) -> str:
    shown = f" [{default}]" if default and not secret else ""
    reader = getpass.getpass if secret else input
    return (reader(f"{prompt}{shown}: ") or "").strip() or default


def _confirm(prompt: str, default: bool = False) -> bool:
    hint = "Y/n" if default else "y/N"
    answer = (input(f"{prompt} [{hint}]: ") or "").strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes")


def _say(msg: str = "") -> None:
    print(msg)


def _report(ok: bool, name: str, message: str) -> None:
    _say(f"  {'ok' if ok else 'FAIL'} [{name}] {message}")


def _collect_llm_interactive(spec: Dict[str, Any], environ: Dict[str, str]) -> Dict[str, str]:
    _say("\n-- LLM (the model that runs triage and investigations)")
    provider = _ask("provider " + "/".join(_PROVIDERS), default="ollama").lower()
    if provider not in _PROVIDERS:
        _say(f"  note: {provider!r} is not a shipped provider ({', '.join(_PROVIDERS)}); "
             "keeping it, but the agent must know how to speak it")
    answer = {"provider": provider, "url": "", "model": "", "api_key": ""}
    if provider == "ollama":
        while True:
            url = _ask("ollama url", default=answer["url"] or environ.get("OLLAMA_URL", "") or _OLLAMA_DEFAULT_URL)
            ok, message, models = probe_ollama(url)
            _report(ok, "llm", message)
            if ok:
                answer["url"] = url
                default_model = environ.get("OLLAMA_MODEL", "") or (models[0] if models else "")
                while True:
                    model = _ask("model", default=default_model)
                    if not models or model in models or _confirm(
                            f"{model!r} is not in the pulled list — keep it anyway?"):
                        answer["model"] = model
                        return answer
            answer["url"] = url  # failed answer becomes the default for the retry
    key_env = f"{provider.upper()}_API_KEY"
    while True:
        has_env = bool(environ.get(key_env, "").strip())
        key = _ask(f"{key_env}" + (" (already set in your environment; enter to keep)" if has_env else ""),
                   secret=True)
        if key or has_env:
            answer["api_key"] = key  # empty = rely on the ambient env var
            break
        _report(False, "llm", f"{key_env} is empty — a hosted provider cannot work without it")
    answer["model"] = _ask("model (blank = the provider default)")
    _report(True, "llm", f"{provider} key present; connectivity is exercised on the first request")
    return answer


def _collect_backend_interactive(spec: Dict[str, Any], answers: Dict[str, Any]) -> str:
    alias, backend = spec["alias"], spec["backend"]
    required = spec["required"]
    _say(f"\n-- {alias} ({'required' if required else 'optional — enter to skip'})")
    discovered = ""
    prom_url = answers["backends"].get("prometheus", "")
    if prom_url and backend in _DISCOVERERS:
        discovered = _DISCOVERERS[backend](prom_url)
        if discovered:
            _say(f"  found via prometheus: {discovered}")
    default = discovered or os.getenv(spec["env_var"], "")
    while True:
        url = _ask(f"{alias} url", default=default)
        if not url:
            if required:
                _report(False, alias, "this one is required — the agent is blind without it")
                continue
            _say(f"  skipped ({alias} off; add it later in config.yaml)")
            return ""
        ok, message = probe_backend(backend, url)
        _report(ok, alias, message)
        if ok:
            return url
        default = url


def _collect_notify_interactive(spec: Dict[str, Any]) -> Dict[str, str]:
    _say("\n-- notify (optional — how you SEE triage; enter to skip a sink)")
    values: Dict[str, str] = {}
    for sink in spec["sinks"]:
        primary = sink["alias"]
        entered = _ask(f"{sink['backend']}: {primary}", secret="token" in primary or "webhook" in primary)
        if not entered:
            continue
        collected = {primary: entered}
        for key in sink["keys"]:
            if key == primary:
                continue
            extra = _ask(f"{sink['backend']}: {key} (enter to leave unset)",
                         secret="token" in key)
            if extra:
                collected[key] = extra
        # Default False, deliberately: Enter is what a first-time operator
        # presses through an unfamiliar wizard, and this sink is a live on-call
        # channel. Opting in must be an act, not the absence of one.
        if _confirm(f"send a test message through {sink['backend']} now?", default=False):
            ok, message = probe_notify(
                sink["backend"], {sink["keys"][k]: v for k, v in collected.items()})
            _report(ok, sink["backend"], message)
            if not ok and not _confirm("keep this sink anyway?"):
                continue
        values.update(collected)
    return values


def _collect_admin_interactive(environ: Dict[str, str]) -> Dict[str, Any]:
    _say("\n-- console admin (seeded at first boot; recovery: scripts/create_admin.py <user> --reset)")
    username = _ask("admin username", default=environ.get("CFOP_ADMIN_USERNAME", "") or "admin")
    while True:
        password = _ask("admin password (blank = generate one)", secret=True)
        if not password:
            return {"username": username, "password": secrets.token_urlsafe(12), "generated": True}
        if password == _ask("confirm password", secret=True):
            return {"username": username, "password": password, "generated": False}
        _report(False, "admin", "passwords do not match")


def collect_interactive(plan: List[Dict[str, Any]], environ: Dict[str, str]) -> Dict[str, Any]:
    answers: Dict[str, Any] = {"backends": {}, "notify": {}, "warnings": []}
    for spec in plan:
        if spec["kind"] == "llm":
            answers["llm"] = _collect_llm_interactive(spec, environ)
        elif spec["kind"] == "backend":
            answers["backends"][spec["alias"]] = _collect_backend_interactive(spec, answers)
        elif spec["kind"] == "notify":
            answers["notify"] = _collect_notify_interactive(spec)
        elif spec["kind"] == "profile":
            _say("\n-- profile (how much CFOperator is allowed to do)")
            _say("   investigate: observe, triage, investigate, notify — never proposes a change")
            _say("   remediate:   additionally lets the queue/executor/PR flags take effect")
            while True:
                choice = _ask("profile", default=spec["default"]).lower()
                if choice in spec["choices"]:
                    answers["profile"] = choice
                    break
                _report(False, "profile", f"choose one of: {', '.join(spec['choices'])}")
        elif spec["kind"] == "admin":
            answers["admin"] = _collect_admin_interactive(environ)
    return answers


def collect_noninteractive(plan: List[Dict[str, Any]], environ: Dict[str, str],
                           profile_flag: str = "") -> Dict[str, Any]:
    """Same interview, answered from the env names ``.env.example`` documents.

    Missing required values are reported all at once (a CI run should learn
    every gap in one failure, not one per run), and every probe that fails
    names its section.
    """
    answers: Dict[str, Any] = {"backends": {}, "notify": {}, "warnings": []}
    missing: List[str] = []
    failures: List[str] = []

    provider = (environ.get("CFOP_INIT_LLM_PROVIDER", "") or "ollama").strip().lower()
    llm = {"provider": provider, "url": "", "model": "", "api_key": ""}
    if provider == "ollama":
        llm["url"] = environ.get("OLLAMA_URL", "").strip()
        llm["model"] = environ.get("OLLAMA_MODEL", "").strip()
        if not llm["url"]:
            missing.append("OLLAMA_URL")
        if not llm["model"]:
            missing.append("OLLAMA_MODEL")
        if llm["url"]:
            ok, message, models = probe_ollama(llm["url"])
            if not ok:
                failures.append(f"llm: {message}")
            elif llm["model"] and models and llm["model"] not in models:
                failures.append(f"llm: model {llm['model']!r} is not pulled "
                                f"(available: {', '.join(models[:6])})")
    else:
        key_env = f"{provider.upper()}_API_KEY"
        if not environ.get(key_env, "").strip():
            missing.append(key_env)
        llm["model"] = environ.get("CFOP_INIT_LLM_MODEL", "").strip()
    answers["llm"] = llm

    for spec in (s for s in plan if s["kind"] == "backend"):
        url = environ.get(spec["env_var"], "").strip()
        if not url:
            if spec["required"]:
                missing.append(spec["env_var"])
            answers["backends"][spec["alias"]] = ""
            continue
        ok, message = probe_backend(spec["backend"], url)
        if not ok:
            failures.append(f"{spec['alias']}: {message}")
        answers["backends"][spec["alias"]] = url

    notify_spec = next(s for s in plan if s["kind"] == "notify")
    for sink in notify_spec["sinks"]:
        for key in sink["keys"]:
            value = environ.get(_env_name(key), "").strip()
            if value:
                answers["notify"][key] = value

    profile_spec = next(s for s in plan if s["kind"] == "profile")
    profile = (profile_flag or environ.get("CFOP_INIT_PROFILE", "") or profile_spec["default"]).lower()
    if profile not in profile_spec["choices"]:
        failures.append(f"profile: {profile!r} is not one of {', '.join(profile_spec['choices'])}")
    answers["profile"] = profile

    password = environ.get("CFOP_ADMIN_PASSWORD", "").strip()
    answers["admin"] = {
        "username": environ.get("CFOP_ADMIN_USERNAME", "").strip() or "admin",
        "password": password or secrets.token_urlsafe(12),
        "generated": not password,
    }

    if missing:
        raise WizardError("missing required values: " + ", ".join(missing))
    if failures:
        raise WizardError("probes failed:\n  " + "\n  ".join(failures))
    return answers


# ---------------------------------------------------------------------------
# Emission
# ---------------------------------------------------------------------------


def emit_config_mapping(plan: List[Dict[str, Any]], answers: Dict[str, Any]) -> Dict[str, Any]:
    """The getting-started-shaped mapping, in interview order. Secrets never
    appear literally — notify values are ``${VAR}`` references into .env."""
    mapping: Dict[str, Any] = {"profile": answers["profile"]}

    llm = answers["llm"]
    block: Dict[str, Any] = {"backend": llm["provider"]}
    if llm.get("url"):
        block["url"] = llm["url"]
    if llm.get("model"):
        block["model"] = llm["model"]
    mapping["llm"] = block

    for spec in (s for s in plan if s["kind"] == "backend"):
        url = answers["backends"].get(spec["alias"], "")
        if url:
            mapping[spec["alias"]] = {"url": url}

    if answers["notify"]:
        mapping["notify"] = {key: f"${{{_env_name(key)}}}" for key in answers["notify"]}

    # The guard this whole design exists for: everything emitted must fold into
    # known schema keys. A key the loader would not recognise is a wizard bug,
    # and failing here beats writing a confidently-wrong file.
    unknown = set(cfg.normalize_aliases(mapping)) - set(cfg.DEFAULT_CONFIG)
    if unknown:
        raise WizardError(f"wizard bug: emitted keys outside the schema: {sorted(unknown)}")
    return mapping


def env_lines(plan: List[Dict[str, Any]], answers: Dict[str, Any]) -> List[Tuple[str, str, bool]]:
    """(name, value, is_secret) triples for .env, derived from the same plan."""
    lines: List[Tuple[str, str, bool]] = []
    llm = answers["llm"]
    if llm["provider"] == "ollama":
        lines.append(("OLLAMA_URL", llm["url"], False))
        lines.append(("OLLAMA_MODEL", llm["model"], False))
    elif llm.get("api_key"):
        lines.append((f"{llm['provider'].upper()}_API_KEY", llm["api_key"], True))
    for spec in (s for s in plan if s["kind"] == "backend"):
        url = answers["backends"].get(spec["alias"], "")
        if url:
            lines.append((spec["env_var"], url, False))
    for key, value in answers["notify"].items():
        lines.append((_env_name(key), value, True))
    admin = answers["admin"]
    lines.append(("CFOP_ADMIN_USERNAME", admin["username"], False))
    lines.append(("CFOP_ADMIN_PASSWORD", admin["password"], True))
    return lines


_CONFIG_HEADER = """\
# CFOperator configuration — generated by `cfoperator init`.
#
# Commit this file to your deploy repo; the merge button is the deploy path.
# Secrets are ${VAR} references into the .env written next to it, which stays
# out of git. Everything not named here has a default (cfshared/config.py);
# the annotated reference for every option is docs/config-reference.md.
"""

_ENV_HEADER = """\
# Generated by `cfoperator init`. Secrets live here, not in config.yaml —
# keep this file out of git (the repo's .gitignore already covers it).
# The docker-compose trial reads these variables directly
# (deploy/compose/config.yaml), so `docker compose up -d` works from this file
# alone; the config.yaml next to it serves every other deployment shape.
"""


def render_config(mapping: Dict[str, Any]) -> str:
    import yaml

    chunks = [_CONFIG_HEADER.rstrip("\n")]
    for key, value in mapping.items():
        chunks.append(yaml.safe_dump({key: value}, default_flow_style=False, sort_keys=False).strip())
    return "\n\n".join(chunks) + "\n"


def render_env(lines: List[Tuple[str, str, bool]]) -> str:
    return _ENV_HEADER + "\n" + "\n".join(f"{name}={value}" for name, value, _ in lines) + "\n"


def verify_written(config_path: Path, plan: List[Dict[str, Any]], answers: Dict[str, Any]) -> None:
    """Load the written file back through the real loader and compare.

    This — not a schema re-implementation — is what "valid config" means. Any
    mismatch is a wizard bug and must stop the run before the operator trusts
    the file.
    """
    resolved = cfg.load_config(str(config_path))
    problems: List[str] = []
    llm = resolved.get("llm", {}).get("primary", {})
    for key in ("provider", "url", "model"):
        want = answers["llm"].get(key if key != "provider" else "provider", "")
        if want and llm.get(key) != want:
            problems.append(f"llm.primary.{key}: wrote {want!r}, loader resolved {llm.get(key)!r}")
    for spec in (s for s in plan if s["kind"] == "backend"):
        want = answers["backends"].get(spec["alias"], "")
        got = resolved.get("observability", {}).get(spec["section"], {}).get("url", "")
        if want and got != want:
            problems.append(f"{spec['alias']}: wrote {want!r}, loader resolved {got!r}")
    if resolved.get("profile") != answers["profile"]:
        problems.append(f"profile: wrote {answers['profile']!r}, "
                        f"loader resolved {resolved.get('profile')!r}")
    if problems:
        raise WizardError("wizard bug — the written file does not resolve to the answers "
                          "given:\n  " + "\n  ".join(problems))


def write_verified(directory: Path, config_text: str, lines: List[Tuple[str, str, bool]],
                   plan: List[Dict[str, Any]], answers: Dict[str, Any]) -> None:
    """Stage, verify, then rename into place — never the other way round.

    ``verify_written`` needs a real file to hand the loader, so the check
    cannot happen purely in memory; staging is what keeps a failed check from
    leaving output behind. Writing first would put a ``config.yaml`` on disk
    that no summary ever described and that the next ``--non-interactive`` run
    then refuses to overwrite without ``--force`` — the same failure the
    pre-write schema guard exists to prevent, one step later.
    """
    config_tmp = directory / "config.yaml.tmp"
    env_tmp = directory / ".env.tmp"
    try:
        config_tmp.write_text(config_text, encoding="utf-8")
        # 0o600 from creation, not chmod-after: the .env holds the admin
        # password and every webhook, and a world-readable moment is still a
        # disclosure. os.replace preserves the mode.
        fd = os.open(env_tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(render_env(lines))
        verify_written(config_tmp, plan, answers)
        os.replace(config_tmp, directory / "config.yaml")
        os.replace(env_tmp, directory / ".env")
    finally:
        for leftover in (config_tmp, env_tmp):
            if leftover.exists():
                leftover.unlink()


# ---------------------------------------------------------------------------
# Validate-only (the re-run path)
# ---------------------------------------------------------------------------


def validate_existing(directory: Path, plan: List[Dict[str, Any]]) -> int:
    """Load an existing config through the real loader and re-probe everything
    it names. Reports healthy per section; writes nothing, ever."""
    config_path = directory / "config.yaml"
    try:
        resolved = cfg.load_config(str(config_path))
    except cfg.ConfigError as exc:
        _say(f"FAIL [config] {exc}")
        return 1

    failed = False
    llm = resolved.get("llm", {}).get("primary", {})
    provider, url, model = llm.get("provider", ""), llm.get("url", ""), llm.get("model", "")
    if provider == "ollama" and url:
        ok, message, models = probe_ollama(url)
        if ok and model and models and model not in models:
            ok, message = False, f"model {model!r} is not pulled (available: {', '.join(models[:6])})"
        _report(ok, "llm", message)
        failed |= not ok
    elif url:
        ok, message = probe_reachable(url)
        _report(ok, "llm", message)
        failed |= not ok
    else:
        key_env = f"{provider.upper()}_API_KEY" if provider else ""
        has_key = bool(key_env and os.getenv(key_env, "").strip())
        _report(has_key or not provider, "llm",
                f"{provider}: {'key present' if has_key else 'no url and no ' + key_env}")
        failed |= bool(provider) and not has_key

    for spec in (s for s in plan if s["kind"] == "backend"):
        url = resolved.get("observability", {}).get(spec["section"], {}).get("url", "")
        if not url:
            _report(True, spec["alias"], "not configured (off)")
            continue
        ok, message = probe_backend(spec["backend"], url)
        _report(ok, spec["alias"], message)
        failed |= not ok

    sinks = resolved.get("observability", {}).get("notifications", [])
    _say(f"  --  [notify] {len(sinks)} sink(s) configured (test posts only on request during init)")

    _say("\nvalidate-only: nothing was written.")
    _say("unhealthy — fix the sections marked FAIL" if failed else "healthy")
    return 1 if failed else 0


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def print_summary(directory: Path, config_text: str,
                  lines: List[Tuple[str, str, bool]],
                  plan: List[Dict[str, Any]], answers: Dict[str, Any]) -> None:
    _say(f"\n--- {directory / 'config.yaml'}")
    for line in config_text.splitlines():
        _say(f"+ {line}")
    _say(f"\n--- {directory / '.env'}")
    for name, value, secret in lines:
        _say(f"+ {name}={'********' if secret else value}")

    llm_spec = next(s for s in plan if s["kind"] == "llm")
    _say("\nNext steps:")
    _say("  1. Commit config.yaml to your deploy repo — the merge button is the deploy")
    _say("     path. .env holds secrets and stays out of git (.gitignore covers it).")
    _say("  2. docker compose up -d — first boot seeds the console admin from")
    _say("     CFOP_ADMIN_USERNAME/CFOP_ADMIN_PASSWORD in .env.")
    if answers["admin"]["generated"]:
        _say(f"     A password was GENERATED for {answers['admin']['username']!r}; it is in .env")
        _say("     (CFOP_ADMIN_PASSWORD). Recovery: scripts/create_admin.py <user> --reset.")
    _say("  3. Everything not asked here is edit-the-file: docs/config-reference.md.")
    if llm_spec["unasked"]:
        _say(f"     (also available under llm: {', '.join(llm_spec['unasked'])})")


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cfoperator init",
        description="Validated, discovery-driven config generation. Probes every "
                    "answer as it is given; writes config.yaml + .env.")
    parser.add_argument("--dir", default=".", help="directory to write into (default: .)")
    parser.add_argument("--non-interactive", action="store_true",
                        help="no prompts; answers come from the env names .env.example documents")
    parser.add_argument("--validate-only", action="store_true",
                        help="load the existing config.yaml, re-probe everything it names, write nothing")
    parser.add_argument("--force", action="store_true",
                        help="overwrite an existing config.yaml/.env (non-interactive)")
    parser.add_argument("--profile", choices=list(cfg.PROFILES), default="",
                        help="profile for --non-interactive (default: investigate)")
    args = parser.parse_args(argv)

    directory = Path(args.dir).expanduser()
    config_path = directory / "config.yaml"
    plan = build_plan()

    try:
        if args.validate_only:
            if not config_path.exists():
                print(f"error: {config_path} does not exist — nothing to validate", file=sys.stderr)
                return 1
            return validate_existing(directory, plan)

        if config_path.exists():
            if args.non_interactive:
                if not args.force:
                    print(f"error: {config_path} exists; use --validate-only to check it "
                          "or --force to regenerate", file=sys.stderr)
                    return 1
            else:
                _say(f"{config_path} already exists.")
                if not _confirm("regenerate it? (No = validate the existing config instead)"):
                    return validate_existing(directory, plan)

        if args.non_interactive:
            answers = collect_noninteractive(plan, dict(os.environ), args.profile)
        else:
            answers = collect_interactive(plan, dict(os.environ))

        mapping = emit_config_mapping(plan, answers)
        lines = env_lines(plan, answers)
        config_text = render_config(mapping)

        directory.mkdir(parents=True, exist_ok=True)
        write_verified(directory, config_text, lines, plan, answers)
        print_summary(directory, config_text, lines, plan, answers)
        return 0
    except WizardError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\naborted — nothing further was written", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
