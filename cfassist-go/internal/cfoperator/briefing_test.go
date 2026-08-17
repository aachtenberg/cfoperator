package cfoperator

import (
	"strings"
	"testing"
)

// --- reference parsing --------------------------------------------------------

func TestParseInvestigationRefAcceptsWhatPeoplePaste(t *testing.T) {
	cases := map[string]int{
		"1889":                                 1889,
		"#1889":                                1889,
		"  1889  ":                             1889,
		"http://cfop:8083/investigations/1889": 1889,
		"https://console/investigations?id=42": 42,
		"http://cfop/investigations#7":         7,
		// A browser or reverse proxy adds the trailing slash; docs tell
		// operators to paste a console URL.
		"http://cfop:8083/investigations/1889/":  1889,
		"http://cfop:8083/investigations/1889//": 1889,
	}
	for raw, want := range cases {
		got, err := ParseInvestigationRef(raw)
		if err != nil {
			t.Errorf("ParseInvestigationRef(%q) errored: %v", raw, err)
			continue
		}
		if got != want {
			t.Errorf("ParseInvestigationRef(%q) = %d, want %d", raw, got, want)
		}
	}
}

// TestParseInvestigationRefRejectsNearMisses is the guard that matters: attaching
// to the wrong incident is worse than a retype. "-4" and "release-8" both end in
// digits, and a bare \d+$ would have accepted them as 4 and 8.
func TestParseInvestigationRefRejectsNearMisses(t *testing.T) {
	for _, raw := range []string{
		"", "   ", "#", "abc", "-4", "release-8", "v1.2", "1889a", "0", "#0",
	} {
		if got, err := ParseInvestigationRef(raw); err == nil {
			t.Errorf("ParseInvestigationRef(%q) should have failed, got %d", raw, got)
		}
	}
}

// --- the nested-findings trap -------------------------------------------------

// TestInvestigationFactsReadsNestedFindings is the regression guard for the API
// shape trap: the detail endpoint puts provider/response/recommendation inside
// `findings` while outcome stays at the top level. A naive top-level read gives
// an empty report and a briefing that says nothing.
func TestInvestigationFactsReadsNestedFindings(t *testing.T) {
	facts := InvestigationFacts(map[string]any{
		"id":      float64(1889), // JSON numbers arrive as float64
		"outcome": "needs_action",
		"trigger": "etcd leader elections",
		"host_id": "ubuntu-cm5-01",
		"findings": map[string]any{
			"provider":             "gemma4:26b",
			"response":             "the full report",
			"recommendation":       "replace the NIC",
			"deep":                 true,
			"outcome_verification": "verified against the node",
		},
	})

	if facts.ID != 1889 {
		t.Errorf("ID = %d, want 1889", facts.ID)
	}
	if facts.Outcome != "needs_action" {
		t.Errorf("Outcome = %q — outcome is top-level", facts.Outcome)
	}
	if facts.Report != "the full report" {
		t.Errorf("Report = %q — response is nested under findings", facts.Report)
	}
	if facts.Recommendation != "replace the NIC" {
		t.Errorf("Recommendation = %q — nested under findings", facts.Recommendation)
	}
	if facts.Provider != "gemma4:26b" {
		t.Errorf("Provider = %q — nested under findings", facts.Provider)
	}
	if !facts.Deep {
		t.Error("Deep should be read from findings")
	}
	if facts.Verification != "verified against the node" {
		t.Errorf("Verification = %q", facts.Verification)
	}
}

func TestInvestigationFactsToleratesStringFindings(t *testing.T) {
	// Older/degraded rows store findings as a bare string rather than an object.
	facts := InvestigationFacts(map[string]any{
		"id":       float64(12),
		"findings": "a legacy plain-text report",
	})
	if facts.Report != "a legacy plain-text report" {
		t.Errorf("Report = %q, want the legacy string", facts.Report)
	}
}

func TestInvestigationFactsToleratesGarbage(t *testing.T) {
	for _, inv := range []map[string]any{
		nil,
		{},
		{"id": nil, "findings": nil},
		{"id": float64(3), "findings": []any{"not an object"}},
		{"id": "88", "outcome": nil, "trigger": float64(5)},
	} {
		facts := InvestigationFacts(inv) // must not panic
		_ = facts
	}
}

// --- learning provenance ------------------------------------------------------

// TestSortLearningsHoistsOwnRows covers FTS mode, where investigation_id is
// present and provenance is knowable.
func TestSortLearningsHoistsOwnRows(t *testing.T) {
	own, other := sortLearnings([]map[string]any{
		{"id": float64(1), "investigation_id": float64(2204)},
		{"id": float64(2), "investigation_id": float64(9)},
		{"id": float64(3), "investigation_id": float64(2204)},
	}, 2204)

	if len(own) != 2 {
		t.Fatalf("expected 2 own learnings, got %d", len(own))
	}
	if len(other) != 1 {
		t.Fatalf("expected 1 other learning, got %d", len(other))
	}
}

// TestSortLearningsToleratesHybridRows is the live-hit case: attaching to #2204
// returned three learnings *from that very investigation* that could not be
// attributed, because hybrid mode's SELECT list has no investigation_id. The
// correct behaviour is to hoist nothing, not to crash and not to guess.
func TestSortLearningsToleratesHybridRows(t *testing.T) {
	hybrid := []map[string]any{
		{"id": float64(1), "title": "a", "combined_score": 0.8},
		{"id": float64(2), "title": "b", "combined_score": 0.6},
	}
	own, other := sortLearnings(hybrid, 2204)
	if len(own) != 0 {
		t.Errorf("hybrid rows carry no investigation_id; nothing can be hoisted, got %d", len(own))
	}
	if len(other) != 2 {
		t.Errorf("all rows should still be shown, got %d", len(other))
	}
}

// TestSortLearningsDoesNotMatchOnZero is the trap the `present` and
// `investigationID != 0` guards exist for: a hybrid row has no
// investigation_id, so it coerces to 0, and a degenerate investigation also has
// id 0. Comparing them naively marks *every* learning as "from this
// investigation" — a confident false claim rather than a missing one.
func TestSortLearningsDoesNotMatchOnZero(t *testing.T) {
	rows := []map[string]any{
		{"id": float64(1), "title": "hybrid row, no investigation_id"},
		{"id": float64(2), "investigation_id": nil},
	}
	own, other := sortLearnings(rows, 0)
	if len(own) != 0 {
		t.Errorf("id 0 must not match rows that merely lack the field, got %d", len(own))
	}
	if len(other) != 2 {
		t.Errorf("all rows should still be listed, got %d", len(other))
	}
}

// TestBriefingSaysProvenanceIsUnknownInHybridMode: with no * markers, an
// operator would otherwise read "none of these came from this investigation",
// which is a claim the hybrid row shape cannot support.
func TestBriefingSaysProvenanceIsUnknownInHybridMode(t *testing.T) {
	out := BuildBriefing(&AttachContext{
		Investigation: map[string]any{"id": float64(2204)},
		Learnings:     []map[string]any{{"id": float64(1), "title": "a learning"}},
		LearningsMode: "hybrid",
	}, 0)

	if !strings.Contains(out, "search mode: hybrid") {
		t.Error("briefing should name the search mode")
	}
	if !strings.Contains(out, "provenance is unknown") {
		t.Errorf("hybrid briefing should say provenance is unknowable, got:\n%s", out)
	}
}

func TestBriefingMarksOwnLearningsInFTSMode(t *testing.T) {
	out := BuildBriefing(&AttachContext{
		Investigation: map[string]any{"id": float64(2204)},
		Learnings: []map[string]any{
			{"id": float64(1), "title": "from this one", "investigation_id": float64(2204)},
		},
		LearningsMode: "fts",
	}, 0)

	if !strings.Contains(out, "* = from this investigation") {
		t.Errorf("FTS briefing should explain the marker, got:\n%s", out)
	}
	if !strings.Contains(out, "* #1") {
		t.Errorf("own learning should be marked, got:\n%s", out)
	}
}

// --- rendering ----------------------------------------------------------------

func TestBriefingRendersTheWholeIncident(t *testing.T) {
	out := BuildBriefing(&AttachContext{
		Investigation: map[string]any{
			"id":               float64(1889),
			"outcome":          "needs_action",
			"trigger":          "PodUnschedulable on headless-gpu",
			"host_id":          "headless-gpu",
			"started_at":       "2026-08-16T10:00:00Z",
			"completed_at":     "2026-08-16T10:02:00Z",
			"duration_seconds": float64(125),
			"tool_calls_count": float64(11),
			"triage_action":    "investigate",
			"operator_notes":   "recurring since the reboot",
			"findings": map[string]any{
				"provider":       "gemma4:26b",
				"response":       "the node is out of memory",
				"recommendation": "raise the limit",
			},
		},
		Remediations: []map[string]any{
			{
				"id": float64(42), "status": "queued", "risk": "low",
				"remediation_class": "manifest", "confidence": 0.82,
				"payload": map[string]any{"title": "bump memory limit"},
				"pr_url":  "https://github.com/x/y/pull/9",
			},
		},
		ConsoleURL: "http://127.0.0.1:8083",
	}, 0)

	for _, want := range []string{
		"investigation #1889",
		"outcome=needs_action",
		"host=headless-gpu",
		"2m05s",
		"11 tool calls",
		"PodUnschedulable on headless-gpu",
		"investigate — recurring since the reboot",
		"raise the limit",
		"the node is out of memory",
		"Linked remediation queue rows (1):",
		"#42 | queued | manifest | risk=low | confidence=0.82",
		"bump memory limit",
		"https://github.com/x/y/pull/9",
		"Console: http://127.0.0.1:8083/investigations",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("briefing missing %q, got:\n%s", want, out)
		}
	}
}

// TestBriefingCallsOutAnEmptyReport: an empty final response is a known failure
// mode of the local model (~40% on some builds). Silently rendering nothing
// makes a broken investigation look like a boring one.
func TestBriefingCallsOutAnEmptyReport(t *testing.T) {
	out := BuildBriefing(&AttachContext{
		Investigation: map[string]any{"id": float64(7), "findings": map[string]any{}},
	}, 0)

	if !strings.Contains(out, "no report recorded") {
		t.Errorf("an empty report must be stated, got:\n%s", out)
	}
}

func TestBriefingShowsWarnings(t *testing.T) {
	out := BuildBriefing(&AttachContext{
		Investigation: map[string]any{"id": float64(7)},
		Warnings:      []string{"remediation queue unavailable: boom"},
	}, 0)

	if !strings.Contains(out, "Incomplete briefing:") {
		t.Errorf("warnings should be visible, got:\n%s", out)
	}
	if !strings.Contains(out, "remediation queue unavailable: boom") {
		t.Errorf("warning text should be printed, got:\n%s", out)
	}
}

func TestBriefingTruncatesLongReports(t *testing.T) {
	long := strings.Repeat("x", 9000)
	out := BuildBriefing(&AttachContext{
		Investigation: map[string]any{
			"id":       float64(7),
			"findings": map[string]any{"response": long},
		},
	}, 100)

	if !strings.Contains(out, "truncated; full text in the console") {
		t.Errorf("long report should be truncated with a pointer, got:\n%s", out)
	}
	if len(out) > 4000 {
		t.Errorf("truncation did not bound the output: %d chars", len(out))
	}
}

func TestBriefingSurvivesAnEmptyContext(t *testing.T) {
	if out := BuildBriefing(nil, 0); out == "" {
		t.Error("a nil context should still render something")
	}
	if out := BuildBriefing(&AttachContext{}, 0); !strings.Contains(out, "investigation #0") {
		t.Errorf("empty context should render a degenerate headline, got:\n%s", out)
	}
}

// --- the advertised command ----------------------------------------------------

func TestAttachCommandMatchesTheVerb(t *testing.T) {
	if got := AttachCommand(1889); got != "cfassist attach 1889" {
		t.Errorf("AttachCommand(1889) = %q", got)
	}
	if AttachVerb != "attach" {
		t.Errorf("AttachVerb = %q; the notification line depends on this", AttachVerb)
	}
}

func TestAttachGuidanceStatesTheTwoTraps(t *testing.T) {
	for _, want := range []string{"snapshot", "read-only"} {
		if !strings.Contains(AttachGuidance, want) {
			t.Errorf("guidance should mention %q", want)
		}
	}
}
