"""One redaction for a tool's arguments.

The pod log and the chat transcript both write those arguments down. They
have to agree on which keys are a credential, or the log shows ``***`` while
the transcript — readable by any member — keeps the value.
"""

from typing import Any

# How much of one argument value is worth an INFO line. Callers that store
# the value pass a higher limit; the key check does not change.
LOG_ARG_LIMIT = 1000

# Argument *names* that carry a credential. The value is redacted. Command
# text is not — the point of writing the arguments down is to show what ran —
# and none of the mutating tools pass these keys today.
_SECRET_ARG_KEYS = frozenset({
    'password', 'token', 'secret', 'api_key', 'authorization', 'credential',
})


def _key_is_secret(key: Any) -> bool:
    name = str(key).lower()
    if name in _SECRET_ARG_KEYS:
        return True
    return name.endswith(('_password', '_token', '_secret', '_api_key'))


def redact_tool_args(value: Any, limit: int = LOG_ARG_LIMIT) -> Any:
    """A copy of tool arguments: secret-shaped keys replaced, long strings cut."""
    if isinstance(value, dict):
        return {
            key: '***' if _key_is_secret(key) else redact_tool_args(item, limit)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_tool_args(item, limit) for item in value]
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + '…'
    return value
