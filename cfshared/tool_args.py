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
# The value is a quoted string, or runs to the end of the line (or the next
# `KEY=`), so `password: my long phrase` and `password: abc,def` lose the whole
# value and `PASSWORD=x user=bob` keeps `user=bob`.

_SECRET_WORD = (r'(?:passw(?:or)?d|secret|token|api[_-]?key|apikey|private[_-]?key'
                r'|access[_-]?key|credentials?|authorization)')
# The prefix before the secret word is optional, or a bare `api_key:` / `token=`
# would never match. The lookbehind anchors the key at the start of an
# identifier run: without it the lazy prefix is retried from every character
# of a long run, which is quadratic on a hex dump or a base64 blob, and this
# runs before the size cap (CodeRabbit on #309 pointed at the regex). The already-redacted lookahead sits before the trailing
# whitespace, or the engine backtracks into that whitespace and redacts `***`
# a second time, eating the space (`password:***`).
_KEY_VALUE = re.compile(
    r'(?<![A-Za-z0-9_.-])'
    r'(?P<key>(?P<kq>["\']?)(?:[A-Za-z_][A-Za-z0-9_.-]*?)?' + _SECRET_WORD + r'(?P=kq))'
    r'(?P<sep>[ \t]*[=:])'
    # …or `Authorization: Bearer ***`, which the bearer rule already handled
    # and which keeps the scheme visible to the model.
    r'(?![ \t]*["\']?(?:(?:bearer|basic)[ \t]+)?\*\*\*)'
    r'(?P<ws>[ \t]*)'
    # A quoted value, escape-aware, so `"abc\"suffix"` is one value (CodeRabbit
    # on #309); a quote that never closes takes the rest of the line, so it
    # fails closed rather than leaving the value in place (claude-review); a
    # bare value runs to the end of the line, or to the next `KEY=` so one-line
    # env output keeps its other fields.
    r'(?:(?P<q>["\'])(?P<qval>(?:(?!(?P=q))[^\\\n]|\\.)*)(?P=q)'
    r'|(?P<uq>["\'])(?P<tail>[^\n]*)'
    r'|(?P<val>(?:(?![ \t]+[A-Za-z_][A-Za-z0-9_]*=)[^"\'\n])+))',
    re.IGNORECASE)
#: Command-line flags — `--password=x`, `--password x`, `--db-password x` —
#: the shape of ps, docker inspect Args and systemctl status output. The key
#: rule's lookbehind rejects a key after `-` on purpose (it anchors identifier
#: runs for linear time), so flags get their own rule (claude-review on #309).
#: A space-separated value must not start with `-`: `--password --help` is two
#: flags. `--token-file /x` and `--password-stdin` do not end in a secret word.
_FLAG_VALUE = re.compile(
    r'(?<![A-Za-z0-9_.-])(?P<flag>--?(?:[A-Za-z][A-Za-z0-9_.-]*?)?' + _SECRET_WORD + r')'
    r'(?P<sep>=|[ \t]+(?!-))'
    r'(?!["\']?\*\*\*)'
    r'(?P<val>"(?:[^"\\\n]|\\.)*"|\'(?:[^\'\\\n]|\\.)*\'|\S+)',
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
#: whatever its key is called. Matched only when the text says kind: Secret. The
#: body takes blank lines too, so a block scalar (`conf: |`) with an empty line
#: in it is one body, not two halves with the second one outside the rule.
_SECRET_DATA_BLOCK = re.compile(
    r'^(?P<ind>[ \t]*)(data|stringData):[ \t]*\n(?P<body>(?:(?:(?P=ind)[ \t]+\S.*|[ \t]*)\n?)+)', re.M)
_YAML_ITEM = re.compile(r'^([ \t]+[^:\s][^:\n]*:)[ \t]*(?!\*\*\*)\S.*$', re.M)
#: The same Secret as JSON — `kubectl get secret -o json` arrives over ssh as a
#: string, and `-o yaml` carries the whole object again as one-line JSON in the
#: kubectl.kubernetes.io/last-applied-configuration annotation (review of #309).
_JSON_SECRET_KIND = re.compile(r'"kind"\s*:\s*"Secret"')
_JSON_STRING = r'"(?:[^"\\]|\\.)*"'
#: The object body as a run of JSON pairs with string-aware tokens, not
#: `[^{}]*`: a stringData value holding serialized JSON has braces inside its
#: quotes (CodeRabbit on #309).
_JSON_DATA_OBJECT = re.compile(
    r'("(?:data|stringData)"\s*:\s*\{)'
    r'(\s*(?:' + _JSON_STRING + r'\s*:\s*(?:' + _JSON_STRING + r'|[^,}\s"]+)\s*,?\s*)*)'
    r'(\})')
_JSON_PAIR_VALUE = re.compile(r'("(?:[^"\\]|\\.)*"\s*:\s*)(?!"?\*\*\*)("(?:[^"\\]|\\.)*"|[^,}\s]+)')


def _key_value_replacement(m) -> str:
    """The matched key, separator and whitespace, then *** in the value's own quoting."""
    head = f"{m.group('key')}{m.group('sep')}{m.group('ws')}"
    if m.group('q'):
        return f"{head}{m.group('q')}{PLACEHOLDER}{m.group('q')}"
    if m.group('uq'):
        return f"{head}{m.group('uq')}{PLACEHOLDER}"
    return head + PLACEHOLDER


def _flag_replacement(m) -> str:
    """The flag and its separator, then *** in the value's own quoting."""
    val = m.group('val')
    q = val[0] if val[:1] in ('"', "'") and val[-1:] == val[:1] and len(val) > 1 else ''
    return f"{m.group('flag')}{m.group('sep')}{q}{PLACEHOLDER}{q}"


def _redact_text(text: str) -> Tuple[str, int]:
    """One string with every pattern above applied; returns it and the count."""
    count = 0

    def counted(pattern, repl, s):
        """Apply one pattern and add its substitutions to the running count."""
        nonlocal count
        s, n = pattern.subn(repl, s)
        count += n
        return s

    text = counted(
        _PEM, lambda m: f'-----BEGIN {m.group(1)}-----\n{PLACEHOLDER}\n-----END {m.group(1)}-----', text)
    if 'kind: Secret' in text:
        def scrub_block(m):
            """Every item under data:/stringData: loses its value, and a block
            scalar's continuation lines (`conf: |` …) go with it."""
            nonlocal count
            out, item_indent = [], None
            for line in m.group('body').splitlines(keepends=True):
                stripped = line.lstrip(' \t')
                indent = len(line) - len(stripped)
                if not stripped.strip():
                    continue  # a blank inside a block scalar: part of the value
                if item_indent is None:
                    item_indent = indent
                if indent > item_indent:
                    continue  # deeper than the key: a continuation of its value
                new, n = _YAML_ITEM.subn(lambda i: f'{i.group(1)} {PLACEHOLDER}', line)
                count += n
                out.append(new)
            return f"{m.group('ind')}{m.group(2)}:\n{''.join(out)}"
        text = _SECRET_DATA_BLOCK.sub(scrub_block, text)
    if _JSON_SECRET_KIND.search(text):
        def scrub_json(m):
            """Every pair inside "data": {…} / "stringData": {…} loses its value."""
            nonlocal count
            body, n = _JSON_PAIR_VALUE.subn(lambda i: f'{i.group(1)}"{PLACEHOLDER}"', m.group(2))
            count += n
            return f'{m.group(1)}{body}{m.group(3)}'
        text = _JSON_DATA_OBJECT.sub(scrub_json, text)
    text = counted(_BEARER, lambda m: f'{m.group(1)} {PLACEHOLDER}', text)
    text = counted(_FLAG_VALUE, _flag_replacement, text)
    text = counted(_KEY_VALUE, _key_value_replacement, text)
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
        """Copy one value; in_secret_data marks the inside of a Secret's data."""
        nonlocal count
        if isinstance(v, dict):
            secret_kind = str(v.get('kind', '')) == 'Secret'
            out = {}
            for key, item in v.items():
                if in_secret_data or _key_is_secret(key):
                    # A flag is not a credential: `secret: false` stays, and
                    # stays out of the count (claude-review on #309). A number
                    # does not: a PIN or an OTP is a secret.
                    if isinstance(item, bool) or item in (None, '', PLACEHOLDER):
                        out[key] = item
                    else:
                        count += 1
                        out[key] = PLACEHOLDER
                elif secret_kind and key in ('data', 'stringData') and isinstance(item, dict):
                    out[key] = walk(item, in_secret_data=True)
                else:
                    out[key] = walk(item)
            return out
        if isinstance(v, (list, tuple)):
            items = [walk(item, in_secret_data) for item in v]
            if isinstance(v, list):
                return items
            if hasattr(v, '_fields'):  # a namedtuple takes its fields positionally
                return type(v)(*items)
            try:
                return type(v)(items)
            except TypeError:
                return tuple(items)
        if isinstance(v, str):
            text, n = _redact_text(v)
            count += n
            return text
        return v

    return walk(value), count
