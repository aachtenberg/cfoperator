# Noise reduction — signal over a sea of red

**Problem:** every anomaly → investigate → `needs_action` → red notification. Result
is alert fatigue. A healthy pod with one old recovered restart (faster-whisper:
healthy 20h, restartCount 1, exit 255) should never page red.

**Principle:** **red = "broken right now and a human must act."** Almost nothing
clears that bar. Everything else is recorded quietly, batched into a digest, or
downgraded. Severity = urgency-to-act, gated on *current* state, surfaced on a
channel proportional to severity.

The levers mostly already exist (triage, `file-backed-cooldown`, finding-status
API `acknowledged`/`false_positive`, the morning-summary digest) — they're just
not tuned for signal.

## Tiers

### Tier 1 — kill the biggest noise class (deterministic, in the agent)  ← building now
Both gate on the same precise condition: **the alert is about a recoverable
runtime condition (restart / terminated / exit code / not-ready / crashloop /
oomkill) AND the pod is healthy right now.** A healthy pod that's genuinely
mis-configured (a non-runtime concern) still gets `needs_action`.

- **1a — state-aware downgrade.** Mirror of B1: after classification, downgrade
  `needs_action → monitoring` when the condition has recovered. (B1 already does
  the opposite: `resolved → needs_action` when still broken.)
- **1b — early-exit / don't investigate healthy things.** *Before* the LLM loop,
  if the pod is healthy-now + recovered + restartCount ≤ threshold (default 3),
  short-circuit to `monitoring` with a logged reason and a lightweight
  investigation record. Skips the expensive investigation (227s/882s) and never
  generates a `needs_action`. High restart counts (flapping) still investigate —
  1a is the safety net for those.

### Tier 2 — match channel to severity (stops the *red*, not the signal)
- **2c — severity→channel mapping.** Real-time red only for `escalate` /
  broken-now. `monitoring` + recovered findings route to the morning-summary
  digest instead of an instant page.
- **2d — recurrence suppression.** A finding that recurs every sweep (as svclb
  did) notifies once, then stays quiet until its state changes or severity
  escalates. Honor `acknowledged`/`false_positive` finding status so dismissed
  items don't return red.

### Tier 3 — learn the cluster's known noise
Feed `acknowledged`/`false_positive` findings into the KB so the agent learns
this cluster's benign patterns (the exit-255/Unknown-restart class is endemic
here — power-outage aftermath, SD flakiness).

## Status
- **Tier 1 (1a + 1b): implemented, default-on** (`ooda.noise.enabled: true`,
  `recovered_restart_threshold: 3`). `_recovered_and_healthy()` +
  `_early_exit_monitoring()` in `agent/agent.py`; tests in
  `agent/test_noise_filter.py`. faster-whisper-class alerts now early-exit to
  `monitoring`; flapping (restarts > threshold) and still-broken pods still
  investigate.
- **Tier-1 coverage extended to the sweep + correlation paths** (the gaps
  observation exposed): the noise filter only covered the alert/investigation
  path, so the *sweep* kept re-flagging recovered restarts (faster-whisper) and
  *previously-persisted* false correlations kept generating insights.
  - Sweep findings: ground-truth suppressor drops "container restarted ≤
    threshold + pod healthy now" findings (`_restart_finding_is_noise`).
  - Correlations: purge previously-persisted false rows for ephemeral CronJob
    services + guard against recording new ones
    (`purge_correlations_for_services`, runs each sweep).
- **Correctness fix (feeds every tier): Job/CronJob churn no longer read as
  failure.** Ephemeral CronJob-run pods (`<name>-<timestamp>-<hash>`) come and go
  by schedule; the container-baseline diff was recording their disappearance as
  `container_change` drift, which surfaced as false "stopped" findings and false
  "co-failure" correlations (e.g. freshet-alerter ↔ ingest). Now filtered at the
  baseline (`_update_baselines`) and at correlation read-time
  (`find_service_failure_patterns`) via `is_ephemeral_job_pod()`. A completed
  job is success, not drift.
- **Tier 2c (severity→channel): shipped.** Real-time Slack now only for act-now
  classes (critical, escalated, needs_action, failed, and plain warning
  findings). Routed to the digest (suppressed real-time, still in
  activity/history + morning summary): resolutions, recovered `monitoring`,
  `resolved`, and `info`. Two surfaces gated:
  - event_runtime completions/findings — `_is_realtime_worthy()`; toggle with
    `CFOP_DIGEST_LOW_SEVERITY=0` (default on).
  - agent correlation insights — stored as learnings, suppressed real-time;
    re-enable with `notifications.realtime_correlation_insights: true`.
  Caveat: the "digest" is the existing morning summary + queryable history; a
  richer real-time-suppressed roll-up can come later. **2d (recurrence
  suppression) not yet done.**
- Tier 3 (learn known noise): not started.

## Notes
- 1b doubles as the long-wanted **early-exit guard** for over-investigation.
- Deterministic on purpose — no new LLM unpredictability in the noise filter.
- Config: `ooda.noise` (thresholds) — default-on, conservative thresholds.
