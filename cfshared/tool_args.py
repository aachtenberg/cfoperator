"""One redaction for a tool's arguments, and one for its results.

The pod log and the chat transcript both write the arguments down. They have
to agree on which keys are a credential, or the log shows ``***`` while the
transcript — readable by any member — keeps the value.

Results are the bigger surface (CFOP-272). ``docker inspect`` prints a
container's env, ``kubectl get secret -o yaml`` prints the Secret, ``cat .env``
prints the file, and all of it is read-only, so the unattended classifier
rightly lets it run. Where it then goes is the prompt — to every provider on
the fallback chain, hosted ones included — and the transcript. The agent
applies ``redact_tool_result`` at its one tool-execution site, so every
provider branch and the transcript see the same text, with the key kept
(``POSTGRES_PASSWORD=***``) so the model still knows the variable is set.
"""

import re
from typing import Any, Tuple

# How much of one argument value is worth an INFO line. Callers that store
# the value pass a higher limit; the key check does not change.
LOG_ARG_LIMIT = 1000

PLACEHOLDER = '***'

# Argument *names* that carry a credential. The value is redacted. Command
# text is not — the point of writing the arguments down is to show what ran —
# and none of the mutating tools pass these keys today. Shared with results,
# so a key that is *** in the transcript's tool_call line is *** in its
# tool_result line.
_SECRET_ARG_KEYS = frozenset({
    'password', 'passwd', 'token', 'secret', 'api_key', 'apikey', 'authorization',
    'credential', 'credentials', 'private_key', 'access_key', 'secret_access_key',
    'client_secret',
})
_SECRET_KEY_SUFFIXES = (
    '_password', '_passwd', '_token', '_secret', '_api_key', '_apikey',
    '_private_key', '_access_key', '_credential', '_credentials',
)


def _key_is_secret(key: Any) -> bool:
    name = str(key).lower()
    if name in _SECRET_ARG_KEYS:
        return True
    return name.endswith(_SECRET_KEY_SUFFIXES)


def redact_tool_args(value: Any, limit: int = LOG_ARG_LIMIT) -> Any:
    """A copy of tool arguments: secret-shaped keys replaced, long strings cut."""
    if isinstance(value, dict):
        return {
            key: PLACEHOLDER if _key_is_secret(key) else redact_tool_args(item, limit)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_tool_args(item, limit) for item in value]
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + '…'
    return value


# ---------------------------------------------------------------------------
# Results: text patterns, because ssh and kubectl output is one big string
# ---------------------------------------------------------------------------
# Each pattern names the secret by its *shape*. The one that needs care is the
# key=value rule: the key has to END with a secret word, so `token_count: 512`,
# `tokens_total 9`, `password_reset: requested` and `TOKEN_FILE=/run/x` survive,
# while `GITHUB_TOKEN=ghp_…`, `"api_key": "sk-…"` and `secret: hunter2` do not.
# The value is a quoted string, or runs to the end of the line / the next
# delimiter, so `password: my long phrase` loses the whole phrase, not one word.

_SECRET_WORD = (r'(?:passw(?:or)?d|secret|token|api[_-]?key|apikey|private[_-]?key'
                r'|access[_-]?key|credentials?|authorization)')
# The prefix before the secret word is optional, or a bare `api_key:` / `token=`
# would never match. The already-redacted lookahead sits before the trailing
# whitespace, or the engine backtracks into that whitespace and redacts `***`
# a second time, eating the space (`password:***`).
_KEY_VALUE = re.compile(
    r'(?P<key>(?P<kq>["\']?)(?:[A-Za-z_][A-Za-z0-9_.-]*?)?' + _SECRET_WORD + r'(?P=kq))'
    r'(?P<sep>[ \t]*[=:])'
    # …or `Authorization: Bearer ***`, which the bearer rule already handled
    # and which keeps the scheme visible to the model.
    r'(?![ \t]*["\']?(?:(?:bearer|basic)[ \t]+)?\*\*\*)'
    r'(?P<ws>[ \t]*)'
    r'(?:(?P<q>["\'])(?P<qval>[^"\'\n]*)(?P=q)|(?P<val>[^"\'\n,;}\]]+))',
    re.IGNORECASE)
_BEARER = re.compile(r'(?i)\b(bearer|basic)[ \t]+(?!\*\*\*)[A-Za-z0-9._~+/=-]{8,}')
_PEM = re.compile(r'-----BEGIN ([A-Z ]*PRIVATE KEY)-----[\s\S]*?-----END \1-----')
_URL_USERINFO = re.compile(r'(://[^/\s:@]+:)(?!\*\*\*)([^@\s/]+)(@)')
_WEBHOOK = re.compile(
    r'(https://(?:hooks\.slack\.com/services|discord(?:app)?\.com/api/webhooks)/)(?!\*\*\*)\S+')
#: Token shapes with a recognisable prefix. The prefix is kept (`sk-***`,
#: `cfop_***`) so the model still knows what kind of credential it saw.
_TOKEN_SHAPES = re.compile(
    r'\b(?:sk-[A-Za-z0-9_-]{16,}|gsk_[A-Za-z0-9]{16,}|xai-[A-Za-z0-9]{16,}'
    r'|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|glpat-[A-Za-z0-9_-]{16,}'
    r'|xox[baprs]-[A-Za-z0-9-]{10,}|hf_[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{16}'
    r'|cfop_[A-Za-z0-9_-]{8,})')
_TOKEN_PREFIX = re.compile(r'^[A-Za-z]+[-_]?')
#: `kubectl get secret -o yaml`: every value under data:/stringData: is a secret,
#: whatever its key is called. Matched only when the text says kind: Secret.
_SECRET_DATA_BLOCK = re.compile(
    r'^(?P<ind>[ \t]*)(data|stringData):[ \t]*\n(?P<body>(?:(?P=ind)[ \t]+\S.*\n?)+)', re.M)
_YAML_ITEM = re.compile(r'^([ \t]+[^:\s][^:\n]*:)[ \t]*(?!\*\*\*)\S.*$', re.M)


def _redact_text(text: str) -> Tuple[str, int]:
    count = 0

    def counted(pattern, repl, s):
        nonlocal count
        s, n = pattern.subn(repl, s)
        count += n
        return s

    text = counted(
        _PEM, lambda m: f'-----BEGIN {m.group(1)}-----\n{PLACEHOLDER}\n-----END {m.group(1)}-----', text)
    if 'kind: Secret' in text:
        def scrub_block(m):
            nonlocal count
            body, n = _YAML_ITEM.subn(lambda i: f'{i.group(1)} {PLACEHOLDER}', m.group('body'))
            count += n
            return f"{m.group('ind')}{m.group(2)}:\n{body}"
        text = _SECRET_DATA_BLOCK.sub(scrub_block, text)
    text = counted(_BEARER, lambda m: f'{m.group(1)} {PLACEHOLDER}', text)
    text = counted(
        _KEY_VALUE,
        lambda m: f"{m.group('key')}{m.group('sep')}{m.group('ws')}{m.group('q') or ''}{PLACEHOLDER}{m.group('q') or ''}",
        text)
    text = counted(_URL_USERINFO, lambda m: f'{m.group(1)}{PLACEHOLDER}{m.group(3)}', text)
    text = counted(_WEBHOOK, lambda m: f'{m.group(1)}{PLACEHOLDER}', text)
    text = counted(
        _TOKEN_SHAPES, lambda m: f'{_TOKEN_PREFIX.match(m.group(0)).group(0)}{PLACEHOLDER}', text)
    return text, count


def redact_tool_result(value: Any) -> Tuple[Any, int]:
    """A copy of a tool result with secret-shaped values replaced, and how many.

    Dicts and lists are walked: a secret-shaped key loses its value whatever
    its type, a ``kind: Secret`` object loses every value under ``data`` and
    ``stringData``, and every string is run through the text patterns above.
    Nothing is truncated here; the caller's size cap does that.
    """
    count = 0

    def walk(v, in_secret_data=False):
        nonlocal count
        if isinstance(v, dict):
            secret_kind = str(v.get('kind', '')) == 'Secret'
            out = {}
            for key, item in v.items():
                if in_secret_data or _key_is_secret(key):
                    if item not in (None, '', PLACEHOLDER):
                        count += 1
                        out[key] = PLACEHOLDER
                    else:
                        out[key] = item
                elif secret_kind and key in ('data', 'stringData') and isinstance(item, dict):
                    out[key] = walk(item, in_secret_data=True)
                else:
                    out[key] = walk(item)
            return out
        if isinstance(v, list):
            return [walk(item, in_secret_data) for item in v]
        if isinstance(v, str):
            text, n = _redact_text(v)
            count += n
            return text
        return v

    return walk(value), count
