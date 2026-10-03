// The `cfoperator` tool: how a session looks at the agent it is running next to
// (CFOP-66), and — in a plain terminal session — how the operator closes and
// acts on what they see (CFOP-256).
//
// Registered only when the presence probe actually found an instance — a tool
// that can only fail teaches a model to work around it. Reads go through
// Client, whose transport refuses any method outside GET. Writes go through
// ActionClient, which allows four POSTs and nothing else, and only the plain
// session registers them. `cfassist attach` stays read-only.

package tools

import (
	"context"
	"errors"
	"fmt"
	"strings"

	"github.com/aachtenberg/cfoperator/cfassist-go/internal/cfoperator"
	"github.com/aachtenberg/cfoperator/cfassist-go/internal/client"
)

// Row limits are deliberately smaller than the API's own defaults. These rows
// are going into a model's context, sometimes an 8k local one, and a queue dump
// that crowds out the incident is not a favour.
const (
	defaultListLimit   = 10
	defaultSearchLimit = 5
	// Ceilings, not just defaults. A model is free to ask for limit: 10000, and
	// neither /api/investigations nor /api/remediations caps it server-side —
	// so without a clamp here the "small default" above is advisory and one
	// hopeful argument dumps the whole queue into an 8k context. maxSearchLimit
	// mirrors the cap /api/kb/search already applies to itself.
	maxListLimit   = 50
	maxSearchLimit = 25
	// Long free-text fields (recommendations, conclusions, KB bodies) are
	// clipped per value rather than the payload being truncated as a whole, so
	// a caller still gets every row it asked for.
	maxFieldChars = 400
	// The briefing get_investigation returns is the same artifact `cfassist
	// attach` seeds, at the same budget.
	briefingChars = 4000
)

// readOnlyActions are what `cfassist attach` may do. The plain terminal
// session adds the writes below; attach must not, because its client refuses
// every non-GET in the transport.
var readOnlyActions = []string{
	"health", "list_investigations", "get_investigation",
	"list_remediations", "get_remediation", "search_knowledge",
}

// writeActions are what an operator sitting at a terminal can ask for.
// Closing a remediation does not triage the investigation it came from.
var writeActions = []string{
	"approve_remediation", "reject_remediation", "resolve_remediation",
	"triage_investigation",
}

// AddCFOperator registers the read-only cfoperator tool against a live client.
// `cfassist attach` uses this. A plain terminal session uses
// AddCFOperatorInteractive.
func (r *Registry) AddCFOperator(api *cfoperator.Client) {
	r.addCFOperator(api, nil)
}

// AddCFOperatorInteractive registers the same tool plus the writes an operator
// can ask for from a terminal: approve, reject or resolve a remediation, and
// triage an investigation. Attach does not call this.
func (r *Registry) AddCFOperatorInteractive(api *cfoperator.Client) {
	if api == nil {
		return
	}
	r.addCFOperator(api, api.Actions())
}

func (r *Registry) addCFOperator(api *cfoperator.Client, act *cfoperator.ActionClient) {
	if api == nil {
		return
	}
	actions := append([]string{}, readOnlyActions...)
	description := "Query the CFOperator SRE agent reachable from this machine (read-only). " +
		"CFOperator is the autonomous agent that investigates alerts, queues remediation " +
		"proposals for human approval, and keeps a knowledge base of what it learned. " +
		"Use this — not ps, systemctl, docker or kubectl — to answer anything about " +
		"cfoperator itself: whether it is up, what it is investigating, what is in the " +
		"remediation queue, and what it has already learned about a host or symptom. " +
		"It cannot approve, reject or queue anything."
	actionHelp := "health: is it up, what version, is it investigating now. " +
		"list_investigations: recent investigations, newest first. " +
		"get_investigation: full briefing for one id — trigger, conclusion, " +
		"linked remediations, related learnings. " +
		"list_remediations: the remediation queue. " +
		"get_remediation: one queue row in full. " +
		"search_knowledge: search past learnings."
	if act != nil {
		actions = append(actions, writeActions...)
		description = "Query and act on the CFOperator SRE agent reachable from this machine. " +
			"CFOperator is the autonomous agent that investigates alerts, queues remediation " +
			"proposals for human approval, and keeps a knowledge base of what it learned. " +
			"Use this — not ps, systemctl, docker or kubectl — to answer anything about " +
			"cfoperator itself: whether it is up, what it is investigating, what is in the " +
			"remediation queue, and what it has already learned about a host or symptom. " +
			"When the operator asks you to close or act on a row, do it: " +
			"approve_remediation sends it to the executor, reject_remediation and " +
			"resolve_remediation close it (a note is required), triage_investigation " +
			"records their verdict on an investigation (resolved or ack, note required). " +
			"Closing a remediation does not triage the investigation it came from. " +
			"Do not approve, reject, resolve or triage unless they asked."
		actionHelp += " approve_remediation: queue the row for the executor. " +
			"reject_remediation: close it as unwanted; note required. " +
			"resolve_remediation: close it as done; note required. Does not run anything. " +
			"triage_investigation: record the operator's verdict (verdict resolved or ack); note required."
	}
	r.tools["cfoperator"] = tool{
		schema: client.ToolSchema{
			Type: "function",
			Function: client.ToolSchemaFunction{
				Name:        "cfoperator",
				Description: description,
				Parameters: map[string]any{
					"type": "object",
					"properties": map[string]any{
						"action": map[string]any{
							"type":        "string",
							"enum":        actions,
							"description": actionHelp,
						},
						"id": map[string]any{
							"type":        "integer",
							"description": "Investigation or remediation id (get_*, and the write actions)",
						},
						"note": map[string]any{
							"type": "string",
							"description": "Why, in the operator's words. Required for reject_remediation, " +
								"resolve_remediation and triage_investigation.",
						},
						"verdict": map[string]any{
							"type": "string",
							"enum": []string{"resolved", "ack"},
							"description": "triage_investigation only. resolved: the problem is handled or moot. " +
								"ack: seen and accepted, not claimed fixed.",
						},
						"query": map[string]any{
							"type":        "string",
							"description": "Search text (search_knowledge)",
						},
						"status": map[string]any{
							"type": "string",
							"description": "Filter the queue by status (list_remediations): queued, claimed, " +
								"executing, pr-open, verifying, resolved, failed, needs-human, filed, rejected",
						},
						"limit": map[string]any{
							"type": "integer",
							"description": fmt.Sprintf("Rows to return (default %d, capped at %d; %d for search)",
								defaultListLimit, maxListLimit, maxSearchLimit),
						},
					},
					"required": []string{"action"},
				},
			},
		},
		execute: func(ctx context.Context, args map[string]any) map[string]any {
			// Bound to the turn: these are network calls against an agent that
			// may be behind a wedged port-forward, and a turn the operator has
			// stopped must not sit through every one of their timeouts.
			var bound *cfoperator.ActionClient
			if act != nil {
				bound = act.WithContext(ctx)
			}
			return cfoperatorExecute(api.WithContext(ctx), bound, args)
		},
	}
}

func cfoperatorExecute(api *cfoperator.Client, act *cfoperator.ActionClient, args map[string]any) map[string]any {
	action, _ := args["action"].(string)
	limit := argInt(args, "limit")

	switch strings.TrimSpace(action) {
	case "health":
		health, err := api.Health()
		if err != nil {
			return cfoperatorError(err)
		}
		return map[string]any{"url": api.URL, "health": health}

	case "list_investigations":
		rows, err := api.ListInvestigations(clampLimit(limit, defaultListLimit, maxListLimit))
		if err != nil {
			return cfoperatorError(err)
		}
		return map[string]any{"count": len(rows), "investigations": clipRows(rows)}

	case "get_investigation":
		id := argInt(args, "id")
		if id <= 0 {
			return map[string]any{"error": "get_investigation needs an investigation id"}
		}
		// The briefing rather than the raw row: it is the artifact this whole
		// feature exists to serve, it is already bounded, and it flattens the
		// list/detail shape difference that has caught callers out before.
		ctx, err := api.CollectAttachContext(id, defaultSearchLimit, 200)
		if err != nil {
			return cfoperatorError(err)
		}
		return map[string]any{
			"investigation_id": id,
			"briefing":         cfoperator.BuildBriefing(ctx, briefingChars),
		}

	case "list_remediations":
		status, _ := args["status"].(string)
		rows, err := api.ListRemediations(strings.TrimSpace(status), clampLimit(limit, defaultListLimit, maxListLimit))
		if err != nil {
			return cfoperatorError(err)
		}
		return map[string]any{"count": len(rows), "remediations": clipRows(rows)}

	case "get_remediation":
		id := argInt(args, "id")
		if id <= 0 {
			return map[string]any{"error": "get_remediation needs a remediation id"}
		}
		row, err := api.GetRemediation(id)
		if err != nil {
			return cfoperatorError(err)
		}
		return map[string]any{"remediation": clipRow(row)}

	case "search_knowledge":
		query, _ := args["query"].(string)
		if strings.TrimSpace(query) == "" {
			return map[string]any{"error": "search_knowledge needs a query"}
		}
		rows, mode, err := api.SearchKnowledge(query, clampLimit(limit, defaultSearchLimit, maxSearchLimit))
		if err != nil {
			return cfoperatorError(err)
		}
		return map[string]any{"count": len(rows), "mode": mode, "learnings": clipRows(rows)}

	case "approve_remediation", "reject_remediation", "resolve_remediation":
		return closeRemediation(api, act, action, args)
	case "triage_investigation":
		return triageInvestigation(act, args)
	}

	return map[string]any{"error": fmt.Sprintf("unknown action %q", action)}
}

// inflightRemediation is a row the executor still holds. Closing it strands
// the job, which will complete against a row that has already moved on.
// Keyed on status, not claimed_at: a finished pr-open row still looks claimed.
//
// Best-effort. The reject and resolve routes do not check status, so a claim
// that lands between this GET and the POST still goes through.
var inflightRemediation = map[string]bool{"claimed": true, "executing": true}

// closedRemediation is a row approve must not send back to the executor.
// The approve route only refuses manual-class rows and rows with an open PR,
// so without this a "close it" followed by a confused approve would re-queue
// a row the operator just finished.
var closedRemediation = map[string]bool{"resolved": true, "rejected": true}

func closeRemediation(api *cfoperator.Client, act *cfoperator.ActionClient, action string, args map[string]any) map[string]any {
	if act == nil {
		return map[string]any{"error": "this session cannot act on remediations. " +
			"cfassist attach is read-only; run cfassist from a terminal."}
	}
	id := argInt(args, "id")
	if id <= 0 {
		return map[string]any{"error": action + " needs a remediation id"}
	}
	note, _ := args["note"].(string)
	// Before the GET, so a missing note is the error the model sees, not
	// "unreachable" or "still running" from a call that was never going to post.
	if action != "approve_remediation" && strings.TrimSpace(note) == "" {
		return map[string]any{"error": "a note is required — it is the only record of why this was closed"}
	}
	row, err := api.GetRemediation(id)
	if err != nil {
		return cfoperatorError(err)
	}
	status, _ := row["status"].(string)
	if inflightRemediation[status] {
		return map[string]any{"error": fmt.Sprintf(
			"Remediation #%d is leased by the executor and still running (status %q). "+
				"Closing it now would strand that job. Wait for it to finish, then close it.",
			id, status)}
	}
	if action == "approve_remediation" && closedRemediation[status] {
		return map[string]any{"error": fmt.Sprintf(
			"Remediation #%d is already %s. Approving it would queue it for the executor again.",
			id, status)}
	}
	var updated map[string]any
	switch action {
	case "approve_remediation":
		updated, err = act.ApproveRemediation(id)
	case "reject_remediation":
		updated, err = act.RejectRemediation(id, note)
	default:
		updated, err = act.ResolveRemediation(id, note)
	}
	if err != nil {
		return cfoperatorError(err)
	}
	return map[string]any{"remediation": clipRow(updated)}
}

func triageInvestigation(act *cfoperator.ActionClient, args map[string]any) map[string]any {
	if act == nil {
		return map[string]any{"error": "this session cannot triage investigations. " +
			"cfassist attach is read-only; run cfassist from a terminal."}
	}
	id := argInt(args, "id")
	if id <= 0 {
		return map[string]any{"error": "triage_investigation needs an investigation id"}
	}
	verdict, _ := args["verdict"].(string)
	note, _ := args["note"].(string)
	updated, err := act.TriageInvestigation(id, verdict, note)
	if err != nil {
		return cfoperatorError(err)
	}
	return map[string]any{"investigation": clipRow(updated)}
}

// cfoperatorError passes the client's operator-facing hint through to the
// model. Most failures here are configuration — no token, wrong address — and
// the hint is the actual fix, which is worth more to the operator than a
// retried call.
func cfoperatorError(err error) map[string]any {
	out := map[string]any{"error": err.Error()}
	var apiErr *cfoperator.Error
	if errors.As(err, &apiErr) && apiErr.Hint != "" {
		out["hint"] = apiErr.Hint
	}
	return out
}

// clampLimit turns a model-supplied row count into one this context can hold.
// Unasked-for (<= 0) takes the default; too large takes the ceiling.
func clampLimit(requested, fallback, max int) int {
	if requested <= 0 {
		return fallback
	}
	if requested > max {
		return max
	}
	return requested
}

func argInt(args map[string]any, key string) int {
	switch v := args[key].(type) {
	case float64: // JSON numbers arrive as float64
		return int(v)
	case int:
		return v
	}
	return 0
}

func clipRows(rows []map[string]any) []map[string]any {
	out := make([]map[string]any, 0, len(rows))
	for _, row := range rows {
		out = append(out, clipRow(row))
	}
	return out
}

// clipRow shortens long free-text values in place of dropping rows.
func clipRow(row map[string]any) map[string]any {
	out := make(map[string]any, len(row))
	for k, v := range row {
		if s, ok := v.(string); ok && len(s) > maxFieldChars {
			out[k] = s[:maxFieldChars] + "… [clipped]"
			continue
		}
		out[k] = v
	}
	return out
}
