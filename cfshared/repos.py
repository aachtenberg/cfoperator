"""What a linked repo is, and which list of them the process is running on.

``git.repos`` in config.yaml has always been the registry: the GitHub and local
git tools are built from it (repo names land in the *tool schema descriptions*,
so it is prompt-visible), the remediation proposer resolves a PR target through
it, and the event runtime enriches alerts with commits from it. The only way to
change it was to edit the file — which in k8s is a read-only ConfigMap, so
"link a repo" cost a deploy commit plus a rollout restart, and on the Helm path
was not expressible at all (the chart templates the executor's ``gitRepo`` and
nothing else).

CFOP-77 adds a console-managed list stored in ``agent_settings`` under
``git_repos``, following the DB-over-config convention already used by
``triage_model`` (CFOP-58) and the remediation flags. This module is the one
definition of the entry shape, the validation, and the precedence, so the API,
the agent and the event runtime cannot drift into three opinions about it.

Two decisions worth keeping:

**The stored list is the whole list, not an overlay.** The first console write
seeds from config.yaml and from then on the DB list *is* the registry. An
additive overlay would need a merge identity for list elements to express
"remove a repo config.yaml still declares" — exactly the invention
``cfshared/config.py`` refuses to make with its "lists replace, dicts merge"
rule. The cost is that config.yaml goes quiet, so the API reports which of its
entries are being shadowed rather than letting an operator discover it later.

**Unknown keys survive an edit.** A config-seeded entry can carry an ``ssh``
block (or anything a future version adds) that the console does not render.
Writes merge over the stored entry instead of replacing it, so editing a
branch through a form cannot silently drop SSH access to that repo.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from typing import Any

logger = logging.getLogger("cfoperator.repos")

#: ``agent_settings`` key holding the console-managed list (JSON array).
#: An absent or empty value means "unset" — config.yaml wins — matching the
#: flags convention, and letting a revert be a write rather than a DELETE the
#: settings store does not offer.
SETTING_KEY = "git_repos"

#: Guard rails, not policy: a registry this size is already unusable in the
#: console, and the settings row is a single TEXT column.
MAX_REPOS = 200
MAX_LIST_ITEMS = 100

#: The registry key, and how tools/git.py and tools/github.py address a repo.
#: Deliberately narrower than a GitHub repo name: it is also a display label
#: and a value the LLM types back to us in a tool call.
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

#: Rejects the characters git itself forbids in a ref name, plus whitespace.
BRANCH_RE = re.compile(r"^[^\s~^:?*\[\\]{1,200}$")

#: Fields the console owns. Anything else in a stored entry is passed through
#: untouched (see the module docstring).
EDITABLE_FIELDS = ("github", "branch", "path", "hosts", "services")
LIST_FIELDS = ("hosts", "services")


#: What GitHub itself accepts for an ``owner/repo`` segment. This lives here
#: rather than in ``event_runtime.github_client`` — which re-exports it — so
#: that the dependency runs host-package -> shared, not the other way round.
#: One definition either way; only the direction changed.
REPO_SEGMENT_RE = r"[a-zA-Z0-9](?:[a-zA-Z0-9._-]{0,98}[a-zA-Z0-9])?"
REPO_SLUG_RE = re.compile(rf"^{REPO_SEGMENT_RE}/{REPO_SEGMENT_RE}$")


class RepoError(ValueError):
    """A repo entry a caller sent cannot be honoured. Message is user-facing."""


def validate_repo_slug(slug: str) -> str:
    """Validate and return an ``owner/repo`` slug."""
    if not REPO_SLUG_RE.fullmatch(slug):
        raise ValueError(f"Invalid repo slug: {slug}")
    return slug


def _validate_slug(slug: str) -> str:
    """The same rule, with the message the console shows an operator."""
    try:
        return validate_repo_slug(slug)
    except ValueError as exc:
        raise RepoError(f"invalid GitHub repo: {slug} (expected owner/repo)") from exc


def _clean_str(value: Any, field: str, *, limit: int) -> str:
    if not isinstance(value, str):
        raise RepoError(f"{field} must be a string")
    text = value.strip()
    if "\x00" in text or "\n" in text:
        raise RepoError(f"{field} contains an illegal character")
    if len(text) > limit:
        raise RepoError(f"{field} is too long (max {limit} characters)")
    return text


def _clean_list(value: Any, field: str) -> list[str]:
    """Accept a JSON array or a comma/newline-separated string.

    The console posts a text input; an API caller posts a list. Both mean the
    same thing and neither should have to know what the other does.
    """
    if isinstance(value, str):
        items = [part.strip() for part in re.split(r"[,\n]", value)]
    elif isinstance(value, (list, tuple)):
        items = [str(part).strip() for part in value]
    else:
        raise RepoError(f"{field} must be a list or a comma-separated string")
    items = [item for item in items if item]
    if len(items) > MAX_LIST_ITEMS:
        raise RepoError(f"{field} has too many entries (max {MAX_LIST_ITEMS})")
    for item in items:
        _clean_str(item, f"{field} entry", limit=200)
    return items


def parse_repo_input(data: Any) -> tuple[str, dict[str, Any]]:
    """Validate one console/API write. Returns ``(name, fields)``.

    ``fields`` carries only the keys the caller actually sent, and a value of
    ``None`` means "clear this field" (the form posts an empty string for a
    path the operator removed). Keys the caller omitted are left to the stored
    entry by :func:`upsert` — that is what keeps an unrendered ``ssh`` block
    alive through a branch edit.
    """
    if not isinstance(data, dict):
        raise RepoError("expected a JSON object")

    name = _clean_str(data.get("name", ""), "name", limit=64)
    if not NAME_RE.fullmatch(name):
        raise RepoError(
            "name must be 1-64 characters of letters, digits, dot, dash or underscore")

    fields: dict[str, Any] = {}
    for field in EDITABLE_FIELDS:
        if field not in data:
            continue
        value = data[field]
        if value is None:
            fields[field] = None
            continue
        if field in LIST_FIELDS:
            items = _clean_list(value, field)
            fields[field] = items or None
            continue
        text = _clean_str(value, field, limit=512 if field == "path" else 200)
        if not text:
            fields[field] = None
            continue
        if field == "github":
            text = _validate_slug(text)
        elif field == "branch" and not BRANCH_RE.fullmatch(text):
            raise RepoError(f"invalid branch name: {text}")
        fields[field] = text

    if fields.get("github") is None and "github" in fields:
        raise RepoError("github (owner/repo) is required")
    return name, fields


def sanitize(entries: Any) -> list[dict[str, Any]]:
    """Coerce a stored or config-file list into usable entries.

    Deliberately lenient where :func:`parse_repo_input` is strict: this runs
    over config.yaml, which predates the console and may carry entries the
    write path would now reject (a missing slug, a name with a slash in it).
    Refusing those would take the whole registry offline over one bad row, so
    the row is kept and only the unusable ones — no name at all — are dropped,
    with a log line rather than silence.
    """
    if not isinstance(entries, (list, tuple)):
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in entries[:MAX_REPOS]:
        if not isinstance(entry, dict):
            logger.warning("Ignoring non-object entry in repo list: %r", entry)
            continue
        name = str(entry.get("name") or entry.get("github") or "").strip()
        if not name:
            logger.warning("Ignoring repo entry with no name or github slug: %r", entry)
            continue
        if name in seen:
            logger.warning("Ignoring duplicate repo entry: %s", name)
            continue
        seen.add(name)
        clean = copy.deepcopy(entry)
        clean["name"] = name
        out.append(clean)
    return out


def parse_stored(raw: str | None) -> list[dict[str, Any]] | None:
    """Decode the ``git_repos`` setting. ``None`` means "unset, use config".

    Invalid JSON also reads as unset. The alternative — treating a corrupt
    value as an empty registry — would silently unlink every repo, which is
    the one outcome an operator would not think to check for.
    """
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        decoded = json.loads(raw)
    except (ValueError, TypeError) as exc:
        logger.warning("Ignoring invalid %s setting, falling back to config: %s", SETTING_KEY, exc)
        return None
    if not isinstance(decoded, list):
        logger.warning("Ignoring non-list %s setting, falling back to config", SETTING_KEY)
        return None
    return sanitize(decoded)


def dumps(repos: list[dict[str, Any]]) -> str:
    """Serialize the list for the settings row."""
    return json.dumps(sanitize(repos), separators=(",", ":"), sort_keys=True)


def resolve(config_repos: Any, raw_setting: str | None) -> tuple[list[dict[str, Any]], str]:
    """Effective registry and where it came from: ``('db'|'config')``."""
    stored = parse_stored(raw_setting)
    if stored is not None:
        return stored, "db"
    return sanitize(config_repos), "config"


def find(repos: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    for entry in repos:
        if str(entry.get("name") or "") == name:
            return entry
    return None


def upsert(repos: list[dict[str, Any]], name: str, fields: dict[str, Any]) -> list[dict[str, Any]]:
    """Add or edit one entry, merging over what is already stored.

    Raises when creating an entry with no ``github`` slug — an edit may omit
    the field (it keeps the stored one), a creation may not.
    """
    out = [copy.deepcopy(entry) for entry in sanitize(repos)]
    existing = find(out, name)
    if existing is None:
        if not fields.get("github"):
            raise RepoError("github (owner/repo) is required")
        if len(out) >= MAX_REPOS:
            raise RepoError(f"too many repos (max {MAX_REPOS})")
        existing = {"name": name}
        out.append(existing)
    for field, value in fields.items():
        if value is None:
            existing.pop(field, None)
        else:
            existing[field] = value
    if not existing.get("github"):
        raise RepoError("github (owner/repo) is required")
    return out


def remove(repos: list[dict[str, Any]], name: str) -> tuple[list[dict[str, Any]], bool]:
    """Drop one entry by name. Second element reports whether it was there."""
    kept = [entry for entry in sanitize(repos) if str(entry.get("name") or "") != name]
    return kept, len(kept) != len(sanitize(repos))


def public_view(entry: dict[str, Any]) -> dict[str, Any]:
    """The console's view of an entry.

    An ``ssh`` block is reported as a flag rather than echoed: it names a user,
    an address and a key path, and the console has no reason to render any of
    that to answer "which repos are linked".
    """
    view = {
        "name": str(entry.get("name") or ""),
        "github": str(entry.get("github") or ""),
        "branch": str(entry.get("branch") or ""),
        "path": str(entry.get("path") or ""),
        "hosts": [str(h) for h in (entry.get("hosts") or [])],
        "services": [str(s) for s in (entry.get("services") or [])],
        "has_ssh": bool(entry.get("ssh")),
    }
    return view


def public_list(repos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [public_view(entry) for entry in sanitize(repos)]
