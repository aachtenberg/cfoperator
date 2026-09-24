"""Dynatrace, as an event runtime plugin rather than a shipped backend.

``docs/infrastructure-config.md`` keeps Dynatrace under "not planned" for the
core: the target user self-hosts, and Dynatrace customers already have Davis.
This package lets cfoperator be plugged into a Dynatrace environment anyway,
without the core learning about it.

So far it holds the Grail query client (``grail.py``). The ``register`` entry
point arrives with the first plugin, the Davis problem source (CFOP-204).
"""
