"""The cockpit feature: the agent-side launcher, and the Job image it launches.

Three modules, all previously loose at the repo root as ``cockpit_spawn.py`` /
``cockpit_ladder.py`` / ``cockpit_bridge.py``:

  - ``spawn``  — the Job launcher behind ``POST /api/cockpit/spawn`` (CFOP-35)
  - ``ladder`` — tiers 2/3 of that endpoint (CFOP-36)
  - ``bridge`` — the browser PTY bridge (CFOP-75)

``Dockerfile`` and ``entrypoint.sh`` in this directory build the ephemeral
interactive Job image those modules launch, so the whole feature — the code
that starts a cockpit and the image a cockpit runs — lives together.

Deliberately empty of imports. ``bridge`` is imported lazily from
``agent.agent._start_cockpit_bridge`` precisely so a missing dependency degrades
to a closed port rather than crash-looping the pod; re-exporting it here would
pull it in at package import and undo that.
"""
