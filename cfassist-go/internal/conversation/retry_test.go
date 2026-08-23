package conversation

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/aachtenberg/cfoperator/cfassist-go/internal/client"
	"github.com/aachtenberg/cfoperator/cfassist-go/internal/config"
	"github.com/aachtenberg/cfoperator/cfassist-go/internal/tools"
)

// The body Groq returns when its parser cannot turn a completion into tool
// calls — the 400 that ended a live session in CFOP-72.
const groqParseFailure = `{"error":{"message":"Parsing failed. The model generated output that could not be parsed. Please adjust your prompt. See 'failed_generation' for more details.","type":"invalid_request_error","code":"tool_use_failed","failed_generation":"<|channel|>commentary"}}`

// --- Output that keeps the hints too ---

type recordingOutput struct {
	mockOutput
	hints []string
}

func (o *recordingOutput) ShowError(message string, hint string) {
	o.errors = append(o.errors, message)
	o.hints = append(o.hints, hint)
}

// --- Scriptable OpenAI-compatible server ---

// reply is one scripted turn: either an HTTP failure, or a completion.
type reply struct {
	status     int    // non-200 to fail this attempt
	body       string // failure body
	retryAfter string // Retry-After header to send with a failure
	content    string // assistant text, when status is 0/200
	toolName   string // when set, the completion is a tool call instead
	toolArgs   string
}

type scriptedServer struct {
	*httptest.Server
	mu       sync.Mutex
	requests int
}

// newScriptedServer replays replies in order. Once the script runs out the last
// entry repeats, so "always fails" is one entry rather than a guess at how many
// attempts the code under test will make.
func newScriptedServer(t *testing.T, replies []reply) *scriptedServer {
	t.Helper()
	s := &scriptedServer{}
	s.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		s.mu.Lock()
		idx := s.requests
		s.requests++
		s.mu.Unlock()

		if idx >= len(replies) {
			idx = len(replies) - 1
		}
		rep := replies[idx]

		if rep.status != 0 && rep.status != http.StatusOK {
			if rep.retryAfter != "" {
				w.Header().Set("Retry-After", rep.retryAfter)
			}
			w.WriteHeader(rep.status)
			w.Write([]byte(rep.body))
			return
		}

		msg := map[string]any{"role": "assistant"}
		if rep.toolName != "" {
			msg["tool_calls"] = []map[string]any{{
				"id":   "call_1",
				"type": "function",
				"function": map[string]any{
					"name":      rep.toolName,
					"arguments": rep.toolArgs,
				},
			}}
		} else {
			msg["content"] = rep.content
		}

		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]any{
			"choices": []map[string]any{{"message": msg}},
			"usage":   map[string]any{"prompt_tokens": 10, "completion_tokens": 5},
		})
	}))
	t.Cleanup(s.Close)
	return s
}

func (s *scriptedServer) count() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.requests
}

// runAgainst wires a turn to the scripted server with backoff removed, since
// the delays are the one thing here not worth waiting for.
func runAgainst(t *testing.T, s *scriptedServer, prompt string) (Result, *recordingOutput) {
	t.Helper()

	saved := retryBackoff
	retryBackoff = 0
	t.Cleanup(func() { retryBackoff = saved })

	llm := client.New("openai", s.URL, "test-model", 0.7, "test-key")
	cfg := config.Defaults()
	cfg.Memory.Directory = t.TempDir()
	output := &recordingOutput{}

	result, _ := Run(llm, tools.New(cfg), output, []client.Message{
		{Role: "user", Content: prompt},
	}, "You are a test assistant.", 10)

	return result, output
}

// The reported failure: a provider refusing to parse the model's own tool call
// used to end the turn. It is nondeterministic, so the next sample usually
// works — the turn should survive it.
func TestParseFailureIsRetriedAndTheTurnSurvives(t *testing.T) {
	s := newScriptedServer(t, []reply{
		{status: http.StatusBadRequest, body: groqParseFailure},
		{content: "The backup completed at 03:14."},
	})

	result, output := runAgainst(t, s, "did the backup succeed?")

	if result.Error != "" {
		t.Fatalf("turn failed after a retryable error: %s", result.Error)
	}
	if result.Response != "The backup completed at 03:14." {
		t.Errorf("Response = %q", result.Response)
	}
	if got := s.count(); got != 2 {
		t.Errorf("made %d requests, want 2 (one failure, one retry)", got)
	}
	if len(output.errors) != 0 {
		t.Errorf("a recovered turn must not show an error: %v", output.errors)
	}
}

// A retry that nobody sees is a lie about latency: the operator is owed the
// difference between one slow model and three attempts.
func TestRetryIsAnnounced(t *testing.T) {
	s := newScriptedServer(t, []reply{
		{status: http.StatusServiceUnavailable, body: `{"error":"upstream unavailable"}`},
		{content: "ok"},
	})

	_, output := runAgainst(t, s, "hello")

	if len(output.warnings) != 1 {
		t.Fatalf("got %d warnings, want exactly 1: %v", len(output.warnings), output.warnings)
	}
	if !strings.Contains(output.warnings[0], "retrying") {
		t.Errorf("warning = %q, want it to say a retry is happening", output.warnings[0])
	}
}

// A turn that never had to retry must stay quiet, or the warning stops meaning
// anything.
func TestCleanTurnWarnsAboutNothing(t *testing.T) {
	s := newScriptedServer(t, []reply{{content: "ok"}})

	_, output := runAgainst(t, s, "hello")

	if len(output.warnings) != 0 {
		t.Errorf("clean turn produced warnings: %v", output.warnings)
	}
	if s.count() != 1 {
		t.Errorf("made %d requests, want 1", s.count())
	}
}

// Retrying is bounded. A provider that is genuinely broken must not hold the
// operator's terminal open indefinitely.
func TestRetryGivesUpAndSaysWhy(t *testing.T) {
	s := newScriptedServer(t, []reply{{status: http.StatusBadRequest, body: groqParseFailure}})

	result, output := runAgainst(t, s, "did the backup succeed?")

	if result.Error == "" {
		t.Fatal("expected the turn to fail once retries were exhausted")
	}
	if got := s.count(); got != maxLLMAttempts {
		t.Errorf("made %d requests, want %d", got, maxLLMAttempts)
	}
	if len(output.hints) != 1 {
		t.Fatalf("got %d hints, want 1: %v", len(output.hints), output.hints)
	}
	// The old hint sent everyone to `curl <url>/api/tags` no matter what broke.
	if strings.Contains(output.hints[0], "/api/tags") {
		t.Errorf("hint names an Ollama endpoint for an OpenAI-compatible provider: %q", output.hints[0])
	}
	if !strings.Contains(output.hints[0], "tool-call output") {
		t.Errorf("hint = %q, want it to name the actual failure", output.hints[0])
	}
}

// A rejected key is not going to be accepted on the second try. Retrying it
// wastes the operator's time and buries the one thing they need to read.
func TestBadKeyIsNotRetried(t *testing.T) {
	s := newScriptedServer(t, []reply{
		{status: http.StatusUnauthorized, body: `{"error":{"message":"Invalid API Key"}}`},
	})

	result, output := runAgainst(t, s, "hello")

	if result.Error == "" {
		t.Fatal("expected the turn to fail")
	}
	if got := s.count(); got != 1 {
		t.Errorf("made %d requests, want 1 — a bad key must not be retried", got)
	}
	if len(output.warnings) != 0 {
		t.Errorf("no retry happened, so nothing should be announced: %v", output.warnings)
	}
	if !strings.Contains(output.hints[0], "api_key") {
		t.Errorf("hint = %q, want it to point at the key", output.hints[0])
	}
}

// Retrying happens inside the tool loop, so it has to leave the accumulated
// messages intact — a retry that resent a half-built conversation would corrupt
// the turn in a way no single-shot test would catch.
func TestRetryInsideToolLoopKeepsTheConversation(t *testing.T) {
	s := newScriptedServer(t, []reply{
		{toolName: "bash", toolArgs: `{"command":"echo backup-ok"}`},
		{status: http.StatusBadRequest, body: groqParseFailure},
		{content: "The command printed backup-ok."},
	})

	result, output := runAgainst(t, s, "check the backup")

	if result.Error != "" {
		t.Fatalf("turn failed: %s", result.Error)
	}
	if result.ToolCalls != 1 {
		t.Errorf("ToolCalls = %d, want 1", result.ToolCalls)
	}
	if result.Response != "The command printed backup-ok." {
		t.Errorf("Response = %q", result.Response)
	}
	if got := s.count(); got != 3 {
		t.Errorf("made %d requests, want 3 (tool call, failed parse, recovery)", got)
	}
	if len(output.warnings) != 1 {
		t.Errorf("got %d warnings, want 1: %v", len(output.warnings), output.warnings)
	}
}

// Retry-After is a number the provider chooses and we sleep on the thread the
// operator is watching. Unbounded, a provider or a proxy asking for an hour
// parks an interactive session with no way out but kill.
func TestOutlandishRetryAfterCannotParkTheSession(t *testing.T) {
	savedCap := maxRetryAfter
	maxRetryAfter = 10 * time.Millisecond
	t.Cleanup(func() { maxRetryAfter = savedCap })

	s := newScriptedServer(t, []reply{
		{status: http.StatusTooManyRequests, body: `{"error":"slow down"}`, retryAfter: "3600"},
		{content: "ok"},
	})

	// Run on its own goroutine: an uncapped delay does not make this assertion
	// fail, it makes the test sit for an hour, which in CI is indistinguishable
	// from a hung suite. Fail fast and say why instead.
	type outcome struct {
		result Result
		output *recordingOutput
	}
	done := make(chan outcome, 1)
	go func() {
		r, o := runAgainst(t, s, "hello")
		done <- outcome{r, o}
	}()

	var result Result
	var output *recordingOutput
	select {
	case got := <-done:
		result, output = got.result, got.output
	case <-time.After(5 * time.Second):
		t.Fatal("still waiting after 5s — the provider's hour was not capped")
	}

	if result.Error != "" {
		t.Fatalf("turn failed: %s", result.Error)
	}
	if len(output.warnings) != 1 {
		t.Fatalf("got %d warnings, want 1: %v", len(output.warnings), output.warnings)
	}
	// Capping is a decision made on the operator's behalf, so it is stated.
	if !strings.Contains(output.warnings[0], "asked for 1h0m0s") {
		t.Errorf("warning = %q, want it to report what the provider asked for", output.warnings[0])
	}
}

// A Retry-After inside the cap is honoured as sent — the provider knows when it
// will answer and we are guessing, so it should not be silently rewritten.
func TestReasonableRetryAfterIsUsedAsSent(t *testing.T) {
	savedCap, savedBackoff := maxRetryAfter, retryBackoff
	maxRetryAfter, retryBackoff = time.Second, 0
	t.Cleanup(func() { maxRetryAfter, retryBackoff = savedCap, savedBackoff })

	s := newScriptedServer(t, []reply{
		{status: http.StatusTooManyRequests, body: `{"error":"slow down"}`, retryAfter: "0.05"},
		{content: "ok"},
	})

	start := time.Now()
	_, output := runAgainst(t, s, "hello")
	elapsed := time.Since(start)

	if elapsed < 50*time.Millisecond {
		t.Errorf("returned in %v — the provider's 50ms was not waited out", elapsed)
	}
	// Nothing was overridden, so nothing to explain.
	if strings.Contains(output.warnings[0], "asked for") {
		t.Errorf("warning = %q, want no cap notice when none was applied", output.warnings[0])
	}
}
