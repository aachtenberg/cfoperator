// Package dedupe decides whether a remediation the agent is about to enqueue
// is new, belongs on a row that is already open, or is already queued
// (CFOP-509).
//
// The service is stateless and only decides. The agent sends everything the
// decision needs and applies the answer under its own lock, so two replicas
// cannot race and the service never touches the database.
//
// Contract:
//
//	POST /v1/decide   {candidate, open_rows, closed_rows, vocabulary} -> 200 Response
//	GET  /healthz     liveness (unauthenticated)
//	GET  /livez       liveness (unauthenticated)
//
// When CFOP_DEDUPE_SHARED_SECRET is set, /v1/decide requires X-CFOP-Token
// (the same idiom as cfop-tracker and changerecord). Unset, requests are
// accepted and the server says so once at startup.
//
// Request fields:
//
//   - candidate: the row about to be enqueued. dedupe_key, recommendation,
//     targets, fold_targets, host, title, trigger, remediation_class, source.
//     host is the raw host field, not the defaulted host_id. A blank host
//     makes the node tier read title instead, which is the morning summary's
//     rule (CFOP-130).
//   - open_rows: non-terminal rows, NEWEST FIRST, the order
//     list_open_remediations returns. Each is {id, status, payload}, and the
//     payload needs only the fields the tiers read: dedupe_key,
//     dedupe_aliases, recommendation, targets, fold_targets. A row whose
//     status is resolved or rejected is ignored.
//   - closed_rows: accepted and unused until the rejected-row window
//     (CFOP-509 phase 4).
//   - vocabulary.notready_nodes: registered names of nodes that are not
//     Ready. The other lists (hosts, workloads, providers) are accepted and
//     unused until subject matching (phase 4).
//
// Response:
//
//   - decision: "new", "fold" (record onto row_id) or "suppress" (row_id
//     already carries this key; enqueue nothing).
//   - tier: node | target | identifier | key, or empty for new.
//   - dedupe_key: the key a new row must carry, which the node tier may have
//     rewritten. host: set when the node tier names the node the row is
//     about; the caller uses it for host_id and target.host.
//   - absorbed / alias: on a fold, what the agent appends to the surviving
//     row's payload.absorbed and payload.dedupe_aliases.
//   - reason / shared: why, and the subject both rows name.
//
// Phase 1 is a parity port: the tiers decide exactly as the agent's
// _maybe_queue_remediation and the knowledge base's enqueue check do on
// origin/main. testdata/ holds the cases, and agent/test_dedupe_parity.py runs
// the same files against the Python code, so a case one side fails is a
// parity break rather than a matter of opinion.
package dedupe
