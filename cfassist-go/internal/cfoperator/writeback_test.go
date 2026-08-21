package cfoperator

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/aachtenberg/cfoperator/cfassist-go/internal/client"
)

// --- the transport guard -----------------------------------------------------

// TestWriteBackClientRefusesAnythingButItsTwoEndpoints is the
// guard-in-the-transport pattern, fourth instance: Client is GET-only,
// SessionTokenClient may touch two token paths, SpawnClient may POST one path,
// and this may POST exactly two. A write-back client that could POST anywhere
// would be a general-purpose write client with a reassuring name — and it runs
// holding a credential, at the end of a session, when nobody is watching.
func TestWriteBackClientRefusesAnythingButItsTwoEndpoints(t *testing.T) {
	c := NewWriteBackClient("http://example.invalid", "tok", time.Second)

	for _, tc := range []struct{ method, path string }{
		{http.MethodPost, "/api/remediations/7/approve"},
		{http.MethodPost, "/api/auth/tokens"},
		{http.MethodPost, "/api/cockpit/spawn"},
		{http.MethodDelete, LearningsPath},
		{http.MethodGet, LearningsPath},
		{http.MethodPut, SessionPath(1)},
		// Traversal out of the session path: a HasPrefix check would admit it.
		{http.MethodPost, "/api/investigations/1/session/../../auth/tokens"},
		{http.MethodPost, "/api/investigations//session"},
		{http.MethodPost, "/api/investigations/abc/session"},
	} {
		if _, err := c.do(tc.method, tc.path, nil); err == nil {
			t.Errorf("write-back client allowed %s %s", tc.method, tc.path)
		} else if !strings.Contains(err.Error(), "refuses") {
			t.Errorf("%s %s failed for the wrong reason: %v", tc.method, tc.path, err)
		}
	}
}

func TestWriteBackClientAllowsItsOwnTwoPaths(t *testing.T) {
	for _, path := range []string{LearningsPath, SessionPath(1889)} {
		if !(path == LearningsPath || isSessionPath(path)) {
			t.Errorf("%s should be allowed", path)
		}
	}
}

// --- the learning quality gate ----------------------------------------------

// TestALearningWithNoTriggerConditionIsRefusedBeforeItIsSent: store_learning
// auto-deprecates a learning with no applies_when, because retrieval can never
// match one. Sending it anyway would report success and seed nothing — the
// exact failure this feature exists to avoid.
func TestALearningWithNoTriggerConditionIsRefusedBeforeItIsSent(t *testing.T) {
	var called bool
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		called = true
		w.WriteHeader(http.StatusCreated)
	}))
	defer srv.Close()

	c := NewWriteBackClient(srv.URL, "tok", time.Second)
	for _, l := range []*Learning{
		{Title: "t", Description: "d"},                   // no applies_when
		{Title: "t", AppliesWhen: "when x"},              // no description
		{Description: "d", AppliesWhen: "when x"},        // no title
		{Title: " ", Description: " ", AppliesWhen: " "}, // whitespace only
	} {
		if _, err := c.RecordLearning(l); err == nil {
			t.Errorf("a learning missing a required field should be refused: %+v", l)
		}
	}
	if called {
		t.Error("nothing should have reached the agent")
	}
}

func TestRecordLearningSendsTheCFOP47Shape(t *testing.T) {
	var got map[string]any
	var path string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		path = r.URL.Path
		json.NewDecoder(r.Body).Decode(&got)
		w.WriteHeader(http.StatusCreated)
		json.NewEncoder(w).Encode(map[string]any{"id": 77})
	}))
	defer srv.Close()

	id, err := NewWriteBackClient(srv.URL, "tok", time.Second).RecordLearning(&Learning{
		LearningType: "solution", Title: "NIC hang on cm5",
		Description: "macb driver stops passing traffic",
		AppliesWhen: "etcd timeouts on ubuntu-cm5-01 with no packet loss elsewhere",
		Source:      "cockpit",
	})
	if err != nil {
		t.Fatalf("RecordLearning: %v", err)
	}
	if id != 77 {
		t.Errorf("learning id = %d, want 77", id)
	}
	if path != LearningsPath {
		t.Errorf("posted to %s, want the CFOP-47 seam %s", path, LearningsPath)
	}
	if got["applies_when"] == "" || got["source"] != "cockpit" {
		t.Errorf("payload lost required fields: %+v", got)
	}
}

// --- the session record ------------------------------------------------------

func TestRecordSessionPostsToTheInvestigationsOwnPath(t *testing.T) {
	var got map[string]any
	var path string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		path = r.URL.Path
		json.NewDecoder(r.Body).Decode(&got)
		w.WriteHeader(http.StatusCreated)
	}))
	defer srv.Close()

	err := NewWriteBackClient(srv.URL, "tok", time.Second).RecordSession(&SessionRecord{
		InvestigationID: 1889, Outcome: "resolved", Summary: "restarted the mount unit",
		Tier: "host", Host: "raspberrypi5", DurationSeconds: 640, Exchanges: 12,
		LearningID: 77,
	})
	if err != nil {
		t.Fatalf("RecordSession: %v", err)
	}
	if path != "/api/investigations/1889/session" {
		t.Errorf("posted to %s", path)
	}
	if got["outcome"] != "resolved" || got["tier"] != "host" || got["learning_id"] != float64(77) {
		t.Errorf("payload lost fields: %+v", got)
	}
	if _, present := got["investigation_id"]; present {
		t.Error("the investigation id belongs in the path, not the body")
	}
}

func TestRecordSessionWithoutASummaryIsRefused(t *testing.T) {
	c := NewWriteBackClient("http://example.invalid", "tok", time.Second)
	if err := c.RecordSession(&SessionRecord{InvestigationID: 1, Outcome: "resolved"}); err == nil {
		t.Fatal("a session record with no summary should be refused — it would say a " +
			"human was here and nothing about what they found")
	}
}

func TestWriteBackRefusalsCarryTheirFix(t *testing.T) {
	for status, want := range map[int]string{
		http.StatusForbidden:  "investigate scope",
		http.StatusNotFound:   "no longer exists",
		http.StatusBadRequest: "predate",
	} {
		code := status
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.WriteHeader(code)
		}))
		err := NewWriteBackClient(srv.URL, "tok", time.Second).
			RecordSession(&SessionRecord{InvestigationID: 1, Summary: "s"})
		srv.Close()
		if err == nil {
			t.Fatalf("HTTP %d should be an error", code)
		}
		if !strings.Contains(hintOf(err), want) {
			t.Errorf("HTTP %d hint = %q, want it to mention %q", code, hintOf(err), want)
		}
	}
}

// --- summarizing -------------------------------------------------------------

const goodReply = `Here you go:
` + "```json" + `
{
  "outcome": "Resolved",
  "summary": "The CIFS mount had a stale handle. Remounting cleared it.",
  "commands": ["systemctl restart mnt-nas.mount", "  ", "findmnt /mnt/nas"],
  "learning": {
    "learning_type": "solution",
    "title": "Stale CIFS handle after a NAS reboot",
    "description": "The mount survives but IO hangs; a remount is enough.",
    "applies_when": "IO on /mnt/nas hangs while the NAS answers ping",
    "services": ["nas"],
    "category": "storage"
  }
}
` + "```"

func TestParseSummaryReadsAFencedObjectAndNormalisesTheOutcome(t *testing.T) {
	s, ok := ParseSummary(goodReply)
	if !ok {
		t.Fatal("a well-formed fenced reply should parse")
	}
	if s.Outcome != "resolved" {
		t.Errorf("outcome = %q, want the vocabulary's spelling", s.Outcome)
	}
	if len(s.Commands) != 2 {
		t.Errorf("commands = %v, want the blank one dropped", s.Commands)
	}
	if s.Learning == nil || !s.Learning.Valid() {
		t.Fatalf("learning did not survive: %+v", s.Learning)
	}
}

func TestParseSummaryReadsABareObject(t *testing.T) {
	s, ok := ParseSummary(`{"outcome":"mitigated","summary":"capped the log volume","learning":null}`)
	if !ok || s.Outcome != "mitigated" || s.Learning != nil {
		t.Fatalf("bare object not parsed: %+v ok=%v", s, ok)
	}
}

// TestAHalfFilledLearningIsDroppedNotSent: MUTATION GUARD. A learning with no
// applies_when is auto-deprecated server-side — accepted, stored, never
// retrieved. Dropping it here is the difference between "the session seeded
// nothing, and said so" and "the session appeared to seed something".
func TestAHalfFilledLearningIsDroppedNotSent(t *testing.T) {
	s, ok := ParseSummary(`{"outcome":"resolved","summary":"did a thing","learning":
		{"learning_type":"solution","title":"a title","description":"a description"}}`)
	if !ok {
		t.Fatal("the summary itself is fine and should parse")
	}
	if s.Learning != nil {
		t.Error("a learning with no applies_when must not survive parsing — it could " +
			"never be retrieved once stored")
	}
}

func TestParseSummaryRejectsWhatCannotBeStored(t *testing.T) {
	for _, reply := range []string{
		"I think the mount was stale, so I remounted it.", // prose, no object
		"{not json at all}",
		`{"outcome":"resolved"}`, // no summary: nothing to record
		`{"summary":"   "}`,
		"",
	} {
		if _, ok := ParseSummary(reply); ok {
			t.Errorf("should not have parsed: %q", reply)
		}
	}
}

func TestAnUnknownOutcomeBecomesInconclusive(t *testing.T) {
	s, _ := ParseSummary(`{"outcome":"fixed it good","summary":"s"}`)
	if s.Outcome != "inconclusive" {
		t.Errorf("outcome = %q, want inconclusive — a session whose own summarizer "+
			"cannot say what happened is exactly that", s.Outcome)
	}
}

func TestRawTailKeepsTheEndOfTheConversationAndSaysWhatItIs(t *testing.T) {
	msgs := []client.Message{
		{Role: "system", Content: "you are an sre"},
		{Role: "user", Content: "what happened"},
		{Role: "assistant", Content: "the mount is stale"},
		{Role: "user", Content: "remount it"},
	}
	tail := RawTail(msgs, 4000)
	if !strings.Contains(tail, "raw transcript tail") {
		t.Error("the fallback must announce that it is not a summary")
	}
	if strings.Contains(tail, "you are an sre") {
		t.Error("the system prompt is not part of the session")
	}
	if strings.Index(tail, "what happened") > strings.Index(tail, "remount it") {
		t.Error("the tail should read oldest-first")
	}
}

func TestRawTailRespectsItsBudget(t *testing.T) {
	msgs := make([]client.Message, 50)
	for i := range msgs {
		msgs[i] = client.Message{Role: "user", Content: strings.Repeat("x", 500)}
	}
	if got := len(RawTail(msgs, 1000)); got > 1200 {
		t.Errorf("tail is %d chars, want it bounded near 1000", got)
	}
}

func TestSessionExchangesIgnoresPlumbing(t *testing.T) {
	msgs := []client.Message{
		{Role: "system", Content: "s"},
		{Role: "user", Content: "a"},
		{Role: "assistant", Content: "b"},
		{Role: "tool", Content: "c"},
	}
	if got := SessionExchanges(msgs); got != 2 {
		t.Errorf("exchanges = %d, want 2 (system and tool are not exchanges)", got)
	}
}

// TestSummaryPromptDemandsATriggerCondition: the prompt is what makes the
// learning retrievable, so its two load-bearing instructions are pinned. Losing
// either turns write-back into a KB that fills with entries nothing can match.
func TestSummaryPromptDemandsATriggerCondition(t *testing.T) {
	// Both halves, separately: the field in the JSON skeleton the model copies,
	// AND the sentence that says what it is for. A bare word-count check passes
	// when the schema line is deleted and only the prose survives — which is
	// exactly the shape that produces learnings nothing can retrieve.
	if !strings.Contains(SummaryPrompt, `"applies_when": "`) {
		t.Error("the JSON skeleton no longer asks for applies_when, so the model " +
			"has no field to fill in")
	}
	if !strings.Contains(SummaryPrompt, `"applies_when" is required`) {
		t.Error("the prompt no longer explains that a learning without a trigger " +
			"condition is worth nothing")
	}
	if !strings.Contains(SummaryPrompt, "null") || !strings.Contains(SummaryPrompt, "ONLY if") {
		t.Error("the prompt no longer tells the model it may decline to write a " +
			"learning — a learning per session poisons retrieval faster than it helps")
	}
	for _, outcome := range SessionOutcomes {
		if !strings.Contains(SummaryPrompt, outcome) {
			t.Errorf("the prompt does not offer the outcome %q the agent accepts", outcome)
		}
	}
}

// TestADroppedLearningIsReportedNotJustDiscarded: docs/cockpit.md §6 promises
// the operator a warning when the model produced a learning that could never be
// retrieved. Nilling it inside ParseSummary and saying nothing leaves them
// believing the session simply had nothing to teach — a different fact, and a
// wrong one.
func TestADroppedLearningIsReportedNotJustDiscarded(t *testing.T) {
	s, ok := ParseSummary(`{"outcome":"resolved","summary":"did a thing","learning":
		{"learning_type":"solution","title":"a title","description":"a description"}}`)
	if !ok {
		t.Fatal("the summary itself should parse")
	}
	if s.Learning != nil {
		t.Fatal("the learning should still be dropped")
	}
	if s.DroppedLearning == "" {
		t.Fatal("the drop must be reportable, not silent")
	}
	if !strings.Contains(s.DroppedLearning, "applies_when") {
		t.Errorf("the report should name what was missing, got %q", s.DroppedLearning)
	}
	if !strings.Contains(s.DroppedLearning, "a title") {
		t.Errorf("the report should name the learning, got %q", s.DroppedLearning)
	}
}

func TestACompleteLearningReportsNoDrop(t *testing.T) {
	s, _ := ParseSummary(goodReply)
	if s.DroppedLearning != "" {
		t.Errorf("nothing was dropped, but the summary says %q", s.DroppedLearning)
	}
}
