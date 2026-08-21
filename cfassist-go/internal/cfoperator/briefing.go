// Turn a CFOperator investigation into a briefing a terminal agent can use.
//
// This is the product of CFOP-29. CFOperator's most valuable output is not the
// console page, it is the *briefing*: what it observed, what it concluded, what
// it already queued. `attach` seeds that into an LLM session so the operator's
// first question is "what do we do" rather than "what happened".
//
// Pure functions over plain maps — no HTTP, no cobra, no TUI — so the assembly
// can be tested against deliberately broken payloads.

package cfoperator

import (
	"encoding/json"
	"fmt"
	"regexp"
	"strconv"
	"strings"
)

// AttachVerb is the cockpit handoff verb. event_runtime/notifications.py prints
// this command into every investigation-bearing Slack/Discord/ntfy notification
// and lives in a different deploy artifact (the agent image vs. the operator's
// laptop), so the two are kept honest by a contract test
// (mcp_server/tests/test_mcp_recipe.py) rather than by a shared import.
const AttachVerb = "attach"

// AttachGuidance is appended to the system prompt (not shown in the terminal —
// the operator can already see the briefing). It states the two things a model
// gets wrong when handed a snapshot: that it is current, and that it can act on
// CFOperator through it.
const AttachGuidance = `You are attached to a CFOperator investigation as the operator's terminal
copilot. The briefing below is CFOperator's own account of the incident: what it
observed, what it concluded, and what it queued as a result.

Two things about it:

- It is a snapshot taken when this session started. Anything time-sensitive
  (pod state, disk usage, whether a service recovered) must be re-checked with
  your own tools before you act on it or report it as current.
- This session has read-only access to CFOperator. Approving, rejecting or
  queueing a remediation happens in the console or through the MCP server, not
  here. Recommend those actions; do not claim to have taken them.

You do have real hands on this machine via your own shell and file tools. Use
them to verify and to fix, and say plainly when the briefing and reality differ.
`

// The id must be the whole string or the tail of a path/query, never just the
// trailing digits of arbitrary text: a bare \d+$ accepted "-4" (as 4) and would
// accept "release-8" too, which is how you end up attached to the wrong
// incident.
var refPattern = regexp.MustCompile(`(?:^|[/=?#])(\d+)\s*$`)

// AttachCommand is the copy-pasteable one-liner. Single source of truth for the
// verb on the Go side.
func AttachCommand(investigationID int) string {
	return fmt.Sprintf("cfassist %s %d", AttachVerb, investigationID)
}

// ParseInvestigationRef coerces an operator-supplied reference to an
// investigation id.
//
// Accepts what people actually paste: "1889", "#1889", and a console URL or any
// other string ending in the id ("http://cfop/investigations/1889"). Returns an
// error otherwise rather than guessing, because attaching to the wrong incident
// is worse than a retype.
func ParseInvestigationRef(raw string) (int, error) {
	text := strings.TrimSpace(raw)
	text = strings.TrimSpace(strings.TrimLeft(text, "#"))
	// A browser or reverse proxy will hand back ".../investigations/1889/",
	// and docs/cockpit.md tells operators to paste a console URL. Trimming the
	// trailing slash here keeps the anchored pattern below intact, so "-4" and
	// "release-8" are still rejected.
	text = strings.TrimRight(text, "/")
	if text == "" {
		return 0, fmt.Errorf("no investigation id given")
	}
	match := refPattern.FindStringSubmatch(text)
	if match == nil {
		return 0, fmt.Errorf("not an investigation id: %q", raw)
	}
	value, err := strconv.Atoi(match[1])
	if err != nil || value <= 0 {
		return 0, fmt.Errorf("not an investigation id: %q", raw)
	}
	return value, nil
}

// Facts is an investigation flattened into the fields the briefing renders.
type Facts struct {
	ID                    int
	Outcome               string
	Trigger               string
	HostID                string
	StartedAt             string
	CompletedAt           string
	DurationSeconds       any
	ToolCalls             any
	ParentInvestigationID int
	TriageAction          string
	OperatorNotes         string
	Provider              string
	Recommendation        string
	Report                string
	Deep                  bool
	Verification          string
}

// InvestigationFacts flattens an investigation into the fields the briefing
// renders.
//
// Exists because of a real API trap: /api/investigations returns `outcome` at
// the top level and no findings, while /api/investigations/<id> nests
// `provider`, `response` and `recommendation` *inside* `findings`. Reading the
// top level for a report yields an empty string and a briefing that says
// nothing, which is exactly the failure this function makes impossible to
// reintroduce quietly.
func InvestigationFacts(investigation map[string]any) Facts {
	inv := investigation
	if inv == nil {
		inv = map[string]any{}
	}

	findings := map[string]any{}
	switch v := inv["findings"].(type) {
	case map[string]any:
		findings = v
	case string:
		// Older/degraded rows store findings as a bare string.
		findings = map[string]any{"response": v}
	}

	return Facts{
		ID:                    asInt(inv["id"]),
		Outcome:               strings.TrimSpace(asString(inv["outcome"])),
		Trigger:               strings.TrimSpace(asString(inv["trigger"])),
		HostID:                strings.TrimSpace(asString(inv["host_id"])),
		StartedAt:             strings.TrimSpace(asString(inv["started_at"])),
		CompletedAt:           strings.TrimSpace(asString(inv["completed_at"])),
		DurationSeconds:       inv["duration_seconds"],
		ToolCalls:             inv["tool_calls_count"],
		ParentInvestigationID: asInt(inv["parent_investigation_id"]),
		TriageAction:          strings.TrimSpace(asString(inv["triage_action"])),
		OperatorNotes:         strings.TrimSpace(asString(inv["operator_notes"])),
		Provider:              strings.TrimSpace(asString(findings["provider"])),
		Recommendation:        strings.TrimSpace(asString(findings["recommendation"])),
		Report:                strings.TrimSpace(asString(findings["response"])),
		Deep:                  asBool(findings["deep"]),
		Verification:          strings.TrimSpace(asString(findings["outcome_verification"])),
	}
}

// --- small value coercions ---------------------------------------------------
//
// JSON numbers decode as float64 through `any`, so every numeric read goes
// through asInt rather than a type assertion that would silently miss.

func asInt(v any) int {
	switch n := v.(type) {
	case int:
		return n
	case int64:
		return int(n)
	case float64:
		return int(n)
	case json.Number:
		i, err := n.Int64()
		if err != nil {
			return 0
		}
		return int(i)
	case string:
		i, err := strconv.Atoi(strings.TrimSpace(n))
		if err != nil {
			return 0
		}
		return i
	default:
		return 0
	}
}

func asString(v any) string {
	switch s := v.(type) {
	case nil:
		return ""
	case string:
		return s
	case float64:
		return strconv.FormatFloat(s, 'f', -1, 64)
	case bool:
		return strconv.FormatBool(s)
	default:
		return fmt.Sprintf("%v", s)
	}
}

func asBool(v any) bool {
	b, ok := v.(bool)
	return ok && b
}

func asFloat(v any) (float64, bool) {
	switch n := v.(type) {
	case float64:
		return n, true
	case int:
		return float64(n), true
	case int64:
		return float64(n), true
	default:
		return 0, false
	}
}

// --- formatting helpers ------------------------------------------------------

func truncate(text string, limit int) string {
	if limit > 0 && len(text) > limit {
		return strings.TrimRight(text[:limit], " \t\n") +
			"\n… [truncated; full text in the console]"
	}
	return text
}

func indent(text string) string {
	lines := strings.Split(text, "\n")
	for i, line := range lines {
		if line != "" {
			lines[i] = "  " + line
		}
	}
	return strings.Join(lines, "\n")
}

func formatDuration(seconds any) string {
	value, ok := asFloat(seconds)
	if !ok {
		return ""
	}
	if value >= 60 {
		return fmt.Sprintf("%dm%02ds", int(value)/60, int(value)%60)
	}
	return fmt.Sprintf("%.1fs", value)
}

func headline(f Facts) string {
	bits := []string{fmt.Sprintf("investigation #%d", f.ID)}
	if f.Outcome != "" {
		bits = append(bits, "outcome="+f.Outcome)
	}
	if f.HostID != "" {
		bits = append(bits, "host="+f.HostID)
	}
	if d := formatDuration(f.DurationSeconds); d != "" {
		bits = append(bits, d)
	}
	if f.ToolCalls != nil {
		bits = append(bits, fmt.Sprintf("%d tool calls", asInt(f.ToolCalls)))
	}
	if f.Deep {
		bits = append(bits, "deep")
	}
	return strings.Join(bits, " | ")
}

func formatRemediation(row map[string]any) string {
	if row == nil {
		row = map[string]any{}
	}
	payload, _ := row["payload"].(map[string]any)
	if payload == nil {
		payload = map[string]any{}
	}

	head := []string{fmt.Sprintf("#%d", asInt(row["id"]))}
	status := strings.TrimSpace(asString(row["status"]))
	if status == "" {
		status = "?"
	}
	head = append(head, status)
	if class := strings.TrimSpace(asString(row["remediation_class"])); class != "" {
		head = append(head, class)
	}
	if risk := strings.TrimSpace(asString(row["risk"])); risk != "" {
		head = append(head, "risk="+risk)
	}
	if confidence, ok := asFloat(row["confidence"]); ok {
		head = append(head, fmt.Sprintf("confidence=%.2f", confidence))
	}

	lines := []string{"  " + strings.Join(head, " | ")}
	title := strings.TrimSpace(asString(payload["title"]))
	if title != "" {
		lines = append(lines, "      "+title)
	}
	recommendation := strings.TrimSpace(asString(payload["recommendation"]))
	if recommendation != "" && recommendation != title {
		lines = append(lines, "      recommendation: "+truncate(recommendation, 400))
	}
	if prURL := strings.TrimSpace(asString(row["pr_url"])); prURL != "" {
		lines = append(lines, "      pr: "+prURL)
	}
	if lastError := strings.TrimSpace(asString(row["last_error"])); lastError != "" {
		if len(lastError) > 300 {
			lastError = lastError[:300]
		}
		lines = append(lines, "      last error: "+lastError)
	}
	return strings.Join(lines, "\n")
}

func formatLearning(row map[string]any, related bool) string {
	if row == nil {
		row = map[string]any{}
	}
	marker := "  "
	if related {
		marker = "* "
	}

	head := []string{fmt.Sprintf("#%d", asInt(row["id"]))}
	if lt := strings.TrimSpace(asString(row["learning_type"])); lt != "" {
		head = append(head, lt)
	}
	if cat := strings.TrimSpace(asString(row["category"])); cat != "" {
		head = append(head, cat)
	}

	title := strings.TrimSpace(asString(row["title"]))
	lines := []string{"  " + marker + strings.Join(head, " | ") + " — " + title}
	if description := strings.TrimSpace(asString(row["description"])); description != "" {
		lines = append(lines, "      "+truncate(description, 400))
	}
	if applies := strings.TrimSpace(asString(row["applies_when"])); applies != "" {
		lines = append(lines, "      applies when: "+truncate(applies, 200))
	}
	return strings.Join(lines, "\n")
}

// sortLearnings splits learnings so the ones this investigation itself produced
// come first.
//
// Only possible when the KB search ran in FTS mode — the hybrid SQL path does
// not select investigation_id. Absent the field nothing is hoisted, which is the
// correct degradation rather than a crash. This was hit live: attaching to
// investigation #2204 returned three learnings *from that very investigation*
// that could not be attributed, because the search had run in hybrid mode.
// BuildBriefing prints the mode for exactly this reason.
func sortLearnings(learnings []map[string]any, investigationID int) (own, other []map[string]any) {
	for _, row := range learnings {
		raw, present := row["investigation_id"]
		if present && asInt(raw) == investigationID && investigationID != 0 {
			own = append(own, row)
			continue
		}
		other = append(other, row)
	}
	return own, other
}

// BuildBriefing renders the seeded-session briefing from CollectAttachContext
// output. maxReportChars <= 0 uses the 4000-char default.
func BuildBriefing(ctx *AttachContext, maxReportChars int) string {
	if ctx == nil {
		ctx = &AttachContext{}
	}
	if maxReportChars <= 0 {
		maxReportChars = 4000
	}
	f := InvestigationFacts(ctx.Investigation)
	consoleURL := strings.TrimRight(strings.TrimSpace(ctx.ConsoleURL), "/")
	rule := strings.Repeat("=", 72)

	out := []string{
		rule,
		"CFOperator briefing — " + headline(f),
		rule,
	}

	if f.StartedAt != "" || f.CompletedAt != "" {
		started, completed := f.StartedAt, f.CompletedAt
		if started == "" {
			started = "?"
		}
		if completed == "" {
			completed = "(still running)"
		}
		out = append(out, fmt.Sprintf("started: %s   completed: %s", started, completed))
	}
	if f.Provider != "" {
		out = append(out, "investigated by: "+f.Provider)
	}
	if f.ParentInvestigationID != 0 {
		out = append(out, fmt.Sprintf("follow-up to investigation #%d", f.ParentInvestigationID))
	}

	if f.Trigger != "" {
		out = append(out, "", "Trigger:", indent(truncate(f.Trigger, 1500)))
	}

	if f.TriageAction != "" || f.OperatorNotes != "" {
		note := f.TriageAction
		if note == "" {
			note = "(no action recorded)"
		}
		if f.OperatorNotes != "" {
			note += " — " + f.OperatorNotes
		}
		out = append(out, "", "Operator triage:", indent(truncate(note, 1500)))
	}

	// Prior cockpit sessions come *before* the agent's recommendation, and that
	// ordering is the point rather than a layout preference: "a human already
	// tried X and it did not work" changes how the recommendation below reads.
	// It sits beside operator triage because it is the same kind of fact — what
	// a person concluded — just with more of the working shown (CFOP-37).
	if sessions := cockpitSessions(ctx.Investigation); len(sessions) > 0 {
		out = append(out, "", fmt.Sprintf("Previous cockpit sessions (%d, newest first):",
			len(sessions)))
		for _, row := range sessions {
			out = append(out, formatSession(row))
		}
	}

	if f.Recommendation != "" {
		out = append(out, "", "Recommendation:", indent(truncate(f.Recommendation, 1500)))
	}

	if f.Verification != "" {
		out = append(out, "", "Outcome verification:", indent(truncate(f.Verification, 1000)))
	}

	if f.Report != "" {
		out = append(out, "", "What the agent found:", indent(truncate(f.Report, maxReportChars)))
	} else {
		// Worth saying out loud: an empty final response is a known failure mode
		// of the local model (~40% on some builds), and a session that silently
		// shows nothing here looks like a working attach against a boring
		// incident rather than a broken investigation.
		out = append(out, "", "What the agent found:",
			"  (no report recorded — the investigation stored an empty response)")
	}

	if len(ctx.Remediations) > 0 {
		out = append(out, "", fmt.Sprintf("Linked remediation queue rows (%d):", len(ctx.Remediations)))
		for _, row := range ctx.Remediations {
			out = append(out, formatRemediation(row))
		}
	}

	if len(ctx.Learnings) > 0 {
		mode := ctx.LearningsMode
		if mode == "" {
			mode = "unknown"
		}
		own, other := sortLearnings(ctx.Learnings, f.ID)
		header := fmt.Sprintf("Related knowledge base learnings (search mode: %s", mode)
		if len(own) > 0 {
			header += "; * = from this investigation"
		} else if mode == "hybrid" {
			// The hybrid row shape has no investigation_id at all, so "none from
			// this investigation" is unknowable rather than false. Say so, or the
			// absence of a * reads as a positive claim.
			header += "; hybrid rows carry no investigation_id, so provenance is unknown"
		}
		out = append(out, "", header+"):")
		for _, row := range own {
			out = append(out, formatLearning(row, true))
		}
		for _, row := range other {
			out = append(out, formatLearning(row, false))
		}
	}

	if len(ctx.Warnings) > 0 {
		out = append(out, "", "Incomplete briefing:")
		for _, w := range ctx.Warnings {
			out = append(out, "  - "+w)
		}
	}

	if consoleURL != "" && f.ID != 0 {
		out = append(out, "", fmt.Sprintf("Console: %s/investigations (investigation #%d)",
			consoleURL, f.ID))
	}

	out = append(out, rule)
	return strings.Join(out, "\n")
}

// cockpitSessions pulls the write-back rows off the investigation.
//
// They ride on the investigation rather than arriving from an endpoint of their
// own: every consumer that wants the investigation wants these too, and a
// second round trip would be one more thing to degrade separately mid-incident.
func cockpitSessions(inv map[string]any) []map[string]any {
	raw, ok := inv["sessions"].([]any)
	if !ok {
		return nil
	}
	rows := make([]map[string]any, 0, len(raw))
	for _, item := range raw {
		if row, ok := item.(map[string]any); ok {
			rows = append(rows, row)
		}
	}
	return rows
}

// formatSession renders one prior session. Outcome first: "what happened" is
// what decides whether the rest is worth reading.
func formatSession(row map[string]any) string {
	outcome := strings.TrimSpace(asString(row["outcome"]))
	if outcome == "" {
		outcome = "unrecorded"
	}
	head := "  - " + outcome
	if actor := strings.TrimSpace(asString(row["actor"])); actor != "" {
		head += " by " + actor
	}
	if at := strings.TrimSpace(asString(row["recorded_at"])); at != "" {
		head += " (" + at + ")"
	}

	detail, _ := row["detail"].(map[string]any)
	if where := sessionWhere(detail); where != "" {
		head += " — " + where
	}
	lines := []string{head}

	// A degraded row is a raw transcript tail, not a summary. Saying so stops
	// the next reader treating a fragment as a conclusion.
	if degraded, _ := row["degraded"].(bool); degraded {
		lines = append(lines, "    (summary unavailable — raw session tail)")
	}
	if summary := strings.TrimSpace(asString(row["summary"])); summary != "" {
		lines = append(lines, indent(truncate(summary, 1200)))
	}
	if detail != nil {
		if cmds, ok := detail["commands"].([]any); ok && len(cmds) > 0 {
			lines = append(lines, "    commands:")
			for _, c := range cmds {
				if text := strings.TrimSpace(asString(c)); text != "" {
					lines = append(lines, "      $ "+truncate(text, 200))
				}
			}
		}
	}
	return strings.Join(lines, "\n")
}

// sessionWhere renders the tier/host pair a session ran on, when it had one.
func sessionWhere(detail map[string]any) string {
	if detail == nil {
		return ""
	}
	tier := strings.TrimSpace(asString(detail["tier"]))
	host := strings.TrimSpace(asString(detail["host"]))
	switch {
	case tier != "" && host != "":
		return "tier " + tier + " on " + host
	case tier != "":
		return "tier " + tier
	case host != "":
		return "on " + host
	}
	return ""
}
