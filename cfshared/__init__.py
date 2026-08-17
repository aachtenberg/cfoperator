"""Logic shared by the agent and the event runtime.

Kept deliberately small and dependency-light. Anything in here is imported by
both `agent/` and `event_runtime/`, and `event_runtime` is expected to run on a
stdlib-only host, so modules here may not import third-party packages at module
scope (`cfshared.config` imports PyYAML lazily).
"""

from .config import ConfigError  # noqa: F401  - re-exported for callers
