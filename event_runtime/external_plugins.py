"""Load plugins the core does not name (CFOP-208).

``CFOP_EVENT_RUNTIME_PLUGINS`` lists plugins to load once the built-ins are
registered: comma-separated ``module`` or ``module:callable`` entries, the
callable defaulting to ``register``. Each is called as
``register(plugins, context)`` -- ``plugins`` is the runtime's
``PluginManager``, ``context`` a ``PluginContext`` -- and registers whatever
it provides through the manager's ``register_*`` methods.

Loading runs last so a plugin sees the built-ins, and can replace one on
purpose: action handlers are keyed by name, which is how the HTTP investigate
handler already replaces the stub in ``build_portable_runtime``.

A plugin the operator named but that cannot be loaded stops the runtime at
startup. Skipping it would leave the operator believing, say, Dynatrace
problems are being watched when nothing is watching them. Unset, the variable
changes nothing.
"""

from __future__ import annotations

import importlib
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

from .escalation import EscalationLedger
from .plugin_manager import PluginManager

logger = logging.getLogger(__name__)

PLUGINS_ENV = "CFOP_EVENT_RUNTIME_PLUGINS"
DEFAULT_CALLABLE = "register"


class PluginLoadError(RuntimeError):
    """A plugin named in CFOP_EVENT_RUNTIME_PLUGINS could not be loaded."""


@dataclass(frozen=True)
class PluginContext:
    """What a plugin's register callable receives besides the PluginManager."""

    # The root config merged over the shared default schema, exactly as the
    # rest of the runtime sees it (cfshared.config.load_config).
    config: Dict[str, Any] = field(default_factory=dict)
    config_path: str | None = None
    # Shared by reference with the runtime. An alert source that reports
    # clears passes it on, as AlertmanagerAlertSource does, so an escalated
    # alert still gets its one "Resolved:" notice. None when resolution
    # notices are switched off.
    escalation_ledger: EscalationLedger | None = None


def parse_plugin_specs(raw: str) -> List[Tuple[str, str]]:
    """Split the variable into ``(module, callable)`` pairs, in order, deduplicated."""
    specs: List[Tuple[str, str]] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        module_name, sep, attr = (part.strip() for part in entry.partition(":"))
        if not module_name or (sep and not attr):
            raise PluginLoadError(
                f"{PLUGINS_ENV}: malformed entry {entry!r}; expected 'module' or 'module:callable'"
            )
        spec = (module_name, attr or DEFAULT_CALLABLE)
        # Naming a plugin twice would register its sources twice and emit
        # every alert twice. Load it once.
        if spec in specs:
            logger.warning("%s names %s:%s more than once; loading it once", PLUGINS_ENV, *spec)
            continue
        specs.append(spec)
    return specs


def load_external_plugins(
    plugins: PluginManager,
    context: PluginContext,
    raw: str | None = None,
) -> List[str]:
    """Load every plugin named in ``CFOP_EVENT_RUNTIME_PLUGINS``; return the specs loaded."""
    raw = os.getenv(PLUGINS_ENV, "") if raw is None else raw
    loaded: List[str] = []
    for module_name, attr in parse_plugin_specs(raw):
        spec = f"{module_name}:{attr}"
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:
            raise PluginLoadError(f"{PLUGINS_ENV}: cannot import {spec}: {exc}") from exc
        register = getattr(module, attr, None)
        if not callable(register):
            raise PluginLoadError(f"{PLUGINS_ENV}: {module_name} has no callable {attr!r} ({spec})")
        try:
            register(plugins, context)
        except Exception as exc:
            raise PluginLoadError(f"{PLUGINS_ENV}: {spec} failed while registering: {exc}") from exc
        logger.info("Loaded event runtime plugin %s", spec)
        loaded.append(spec)
    return loaded
