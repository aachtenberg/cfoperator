package conversation

import (
	"context"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/aachtenberg/cfoperator/cfassist-go/internal/client"
	"github.com/aachtenberg/cfoperator/cfassist-go/internal/config"
	"github.com/aachtenberg/cfoperator/cfassist-go/internal/tools"
)

// promptly is how long a stopped turn gets to notice. Every stall in these
// tests is far longer, so the margin distinguishes "cancelled" from "waited it
// out" without making the suite slow.
const promptly = 3 * time.Second

// runCancellable starts a turn and hands back a stop function plus a channel
// carrying the result. Run goes on its own goroutine because the thing under
// test is whether it comes back at all.
func runCancellable(t *testing.T, s *scriptedServer, prompt string) (context.CancelFunc, <-chan Result, *recordingOutput) {
	t.Helper()

	saved := retryBackoff
	retryBackoff = 0
	t.Cleanup(func() { retryBackoff = saved })

	llm := client.New("openai", s.URL, "test-model", 0.7, "test-key")
	cfg := config.Defaults()
	cfg.Memory.Directory = t.TempDir()

	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)

	output := &recordingOutput{}
	done := make(chan Result, 1)
	go func() {
		result, _ := Run(ctx, llm, tools.New(cfg), output, []client.Message{
			{Role: "user", Content: prompt},
		}, "You are a test assistant.", 10)
		done <- result
	}()

	// output is written on that goroutine; read it only after done fires.
	return cancel, done, output
}

// awaitStop asserts the turn came back because it was stopped, not because the
// stall finished.
func awaitStop(t *testing.T, done <-chan Result, what string) Result {
	t.Helper()
	select {
	case r := <-done:
		if !r.Cancelled {
			t.Errorf("%s: returned but Cancelled is false (Error=%q)", what, r.Error)
		}
		// A deliberate stop is not a failure. Reporting it as one in red
		// teaches operators not to reach for the key.
		if r.Error != "" {
			t.Errorf("%s: a stopped turn reported an error: %q", what, r.Error)
		}
		return r
	case <-time.After(promptly):
		t.Fatalf("%s: still running %v after cancel", what, promptly)
		return Result{}
	}
}

// The reported symptom: a turn in an LLM call could not be stopped, so the
// operator waited out the HTTP timeout — up to two minutes per call, fifty
// times over.
func TestCancelDuringLLMCallStopsTheTurn(t *testing.T) {
	s := newScriptedServer(t, []reply{{delay: time.Minute, content: "too late"}})

	cancel, done, _ := runCancellable(t, s, "did the backup succeed?")
	time.Sleep(50 * time.Millisecond) // let the request get out the door
	cancel()

	awaitStop(t, done, "cancel during LLM call")
}

// Waiting out a backoff is still waiting. Sleeping through the cancel would
// make the key feel broken for up to the cap.
func TestCancelDuringRetryBackoffStopsTheTurn(t *testing.T) {
	savedCap := maxRetryAfter
	maxRetryAfter = 30 * time.Second
	t.Cleanup(func() { maxRetryAfter = savedCap })

	s := newScriptedServer(t, []reply{
		{status: http.StatusTooManyRequests, body: `{"error":"slow down"}`, retryAfter: "30"},
	})

	cancel, done, _ := runCancellable(t, s, "hello")
	time.Sleep(100 * time.Millisecond) // land inside the backoff
	cancel()

	awaitStop(t, done, "cancel during backoff")
}

// A cancelled request surfaces as a transport failure, which Retryable calls
// retryable — right for a dropped connection, wrong here.
//
// Asserting on the request count alone does not catch a missing context check:
// the retry is attempted, but the cancelled context stops it at the transport
// layer before it reaches the server, so the count stays at 1 either way. What
// does reach the operator is the announcement — cfassist telling them it is
// retrying a turn they just stopped. That is the observable difference, so that
// is what this pins.
func TestCancelIsNotAnnouncedAsARetry(t *testing.T) {
	s := newScriptedServer(t, []reply{{delay: time.Minute, content: "too late"}})

	cancel, done, output := runCancellable(t, s, "hello")
	time.Sleep(50 * time.Millisecond)
	cancel()
	awaitStop(t, done, "cancel then check output")

	for _, w := range output.warnings {
		if strings.Contains(w, "retrying") {
			t.Errorf("told the operator %q after they cancelled", w)
		}
	}

	// Give any wrongly-scheduled retry time to arrive before counting.
	time.Sleep(200 * time.Millisecond)
	if got := s.count(); got != 1 {
		t.Errorf("made %d requests after a cancel, want 1", got)
	}
}

// A tool call is the part of a turn most likely to be running when an operator
// gives up on it. This is the case from the report: the model had escalated to
// a full-filesystem grep, which without a shared context runs to its timeout.
func TestCancelDuringToolCallStopsTheTurn(t *testing.T) {
	s := newScriptedServer(t, []reply{
		{toolName: "bash", toolArgs: `{"command":"sleep 60"}`},
		{content: "never reached"},
	})

	cancel, done, _ := runCancellable(t, s, "check the backup")
	time.Sleep(300 * time.Millisecond) // let the command actually start
	cancel()

	awaitStop(t, done, "cancel during tool call")
}

// A turn handed an already-cancelled context must not start work. Checking only
// around the blocking calls would let one more LLM request out first.
func TestAlreadyCancelledTurnDoesNothing(t *testing.T) {
	s := newScriptedServer(t, []reply{{content: "should not be asked"}})

	llm := client.New("openai", s.URL, "test-model", 0.7, "test-key")
	cfg := config.Defaults()
	cfg.Memory.Directory = t.TempDir()

	ctx, cancel := context.WithCancel(context.Background())
	cancel()

	result, _ := Run(ctx, llm, tools.New(cfg), &recordingOutput{}, []client.Message{
		{Role: "user", Content: "hello"},
	}, "You are a test assistant.", 10)

	if !result.Cancelled {
		t.Error("Cancelled is false for a turn that never should have started")
	}
	if got := s.count(); got != 0 {
		t.Errorf("made %d requests on an already-cancelled context, want 0", got)
	}
}

// Cancelling must not cost the operator what the turn already found — the tool
// results are the expensive part and they have already been shown.
func TestCancelKeepsTheWorkAlreadyDone(t *testing.T) {
	s := newScriptedServer(t, []reply{
		{toolName: "bash", toolArgs: `{"command":"echo backup-ok"}`},
		{delay: time.Minute, content: "too late"},
	})

	cancel, done, _ := runCancellable(t, s, "check the backup")
	time.Sleep(300 * time.Millisecond) // first tool call completes, second stalls
	cancel()

	r := awaitStop(t, done, "cancel after a tool call")
	if r.ToolCalls != 1 {
		t.Errorf("ToolCalls = %d, want the completed call to survive the cancel", r.ToolCalls)
	}
}
