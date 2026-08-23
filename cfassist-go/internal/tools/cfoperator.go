// The `cfoperator` tool: how a session looks at the agent it is running next to
// (CFOP-66).
//
// Registered only when the presence probe actually found an instance — a tool
// that can only fail teaches a model to work around it. Everything here goes
// through internal/cfoperator's client, whose transport refuses any method
// outside GET, so this cannot grow a write by accident.

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

// AddCFOperator registers the read-only cfoperator tool against a live client.
func (r *Registry) AddCFOperator(api *cfoperator.Client) {
	if api == nil {
		return
	}
	r.tools["cfoperator"] = tool{
		schema: client.ToolSchema{
			Type: "function",
			Function: client.ToolSchemaFunction{
				Name: "cfoperator",
				Description: "Query the CFOperator SRE agent reachable from this machine (read-only). " +
					"CFOperator is the autonomous agent that investigates alerts, queues remediation " +
					"proposals for human approval, and keeps a knowledge base of what it learned. " +
					"Use this — not ps, systemctl, docker or kubectl — to answer anything about " +
					"cfoperator itself: whether it is up, what it is investigating, what is in the " +
					"remediation queue, and what it has already learned about a host or symptom. " +
					"It cannot approve, reject or queue anything.",
				Parameters: map[string]any{
					"type": "object",
					"properties": map[string]any{
						"action": map[string]any{
							"type": "string",
							"enum": []string{
								"health", "list_investigations", "get_investigation",
								"list_remediations", "get_remediation", "search_knowledge",
							},
							"description": "health: is it up, what version, is it investigating now. " +
								"list_investigations: recent investigations, newest first. " +
								"get_investigation: full briefing for one id — trigger, conclusion, " +
								"linked remediations, related learnings. " +
								"list_remediations: the remediation queue. " +
								"get_remediation: one queue row in full. " +
								"search_knowledge: search past learnings.",
						},
						"id": map[string]any{
							"type":        "integer",
							"description": "Investigation or remediation id (get_investigation, get_remediation)",
						},
						"query": map[string]any{
							"type":        "string",
							"description": "Search text (search_knowledge)",
						},
						"status": map[string]any{
							"type": "string",
							"description": "Filter the queue by status (list_remediations): queued, claimed, " +
								"executing, pr-open, verifying, resolved, failed, needs-human, rejected",
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
			return cfoperatorExecute(api, args)
		},
	}
}

func cfoperatorExecute(api *cfoperator.Client, args map[string]any) map[string]any {
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
	}

	return map[string]any{"error": fmt.Sprintf("unknown action %q", action)}
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
