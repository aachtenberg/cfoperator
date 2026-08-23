package main

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/aachtenberg/cfoperator/cfassist-go/internal/cfoperator"
	"github.com/aachtenberg/cfoperator/cfassist-go/internal/client"
	"github.com/spf13/cobra"
)

// --- harness -----------------------------------------------------------------

// captureStderr mirrors captureStdout in attach_test.go. Write-back reports its
// failures on stderr on purpose — they are warnings beside a session that
// worked, not the session's own output — so the tests have to read it.
func captureStderr(t *testing.T, fn func()) string {
	t.Helper()
	orig := os.Stderr
	r, w, err := os.Pipe()
	if err != nil {
		t.Fatalf("pipe: %v", err)
	}
	os.Stderr = w

	done := make(chan string, 1)
	go func() {
		data, _ := io.ReadAll(r)
		done <- string(data)
	}()

	fn()

	w.Close()
	os.Stderr = orig
	return <-done
}

type wbCall struct {
	Path string
	Body map[string]any
}

// wbAgent records every write-back POST. Nothing here may reach a real agent or
// a real model: CI has neither, and a test that silently did would be asserting
// the runner's environment.
func wbAgent(t *testing.T, calls *[]wbCall) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		var body map[string]any
		json.Unmarshal(raw, &body)
		*calls = append(*calls, wbCall{Path: r.URL.Path, Body: body})
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		json.NewEncoder(w).Encode(map[string]any{"id": 77})
	}))
	t.Cleanup(srv.Close)
	return srv
}

// stubSummarizer replaces the LLM turn. Restored by t.Cleanup so one test's
// stub cannot leak into the next.
func stubSummarizer(t *testing.T, s *cfoperator.SessionSummary, err error) {
	t.Helper()
	orig := summarizeSession
	summarizeSession = func(_ context.Context, _ *client.LLMClient, _ []client.Message) (*cfoperator.SessionSummary, error) {
		return s, err
	}
	t.Cleanup(func() { summarizeSession = orig })
}

func wbCmd(t *testing.T, args ...string) *cobra.Command {
	t.Helper()
	cmd := newAttachCmd()
	cmd.SetArgs(append([]string{"1889"}, args...))
	if err := cmd.ParseFlags(args); err != nil {
		t.Fatalf("flags: %v", err)
	}
	return cmd
}

var wbMessages = []client.Message{
	{Role: "system", Content: "you are an sre"},
	{Role: "user", Content: "what happened to the mount"},
	{Role: "assistant", Content: "it is stale; remounting"},
}

// --- the happy path ----------------------------------------------------------

func TestWriteBackStoresTheLearningFirstSoTheSessionCanCiteIt(t *testing.T) {
	var calls []wbCall
	srv := wbAgent(t, &calls)
	stubSummarizer(t, &cfoperator.SessionSummary{
		Outcome: "resolved", Summary: "remounted the stale share",
		Commands: []string{"systemctl restart mnt-nas.mount"},
		Learning: &cfoperator.Learning{
			LearningType: "solution", Title: "stale CIFS handle",
			Description: "remount clears it",
			AppliesWhen: "IO on /mnt/nas hangs while the NAS answers ping",
		},
	}, nil)

	captureStdout(t, func() {
		writeBackSession(wbCmd(t), 1889, srv.URL, "session-token", nil,
			wbMessages, time.Now().Add(-10*time.Minute), "host", "raspberrypi5")
	})

	if len(calls) != 2 {
		t.Fatalf("expected a learning write then a session write, got %d: %+v", len(calls), calls)
	}
	if calls[0].Path != cfoperator.LearningsPath {
		t.Errorf("first write was %s, want the learning — the session record has to "+
			"cite its id, and the other order needs a second write to backfill",
			calls[0].Path)
	}
	if calls[1].Path != "/api/investigations/1889/session" {
		t.Errorf("second write was %s", calls[1].Path)
	}
	if calls[1].Body["learning_id"] != float64(77) {
		t.Errorf("the session did not cite the learning it produced: %+v", calls[1].Body)
	}
	if calls[0].Body["source"] != "cockpit" {
		t.Errorf("the learning is not stamped as cockpit-derived: %+v", calls[0].Body)
	}
	if calls[1].Body["tier"] != "host" || calls[1].Body["host"] != "raspberrypi5" {
		t.Errorf("the session did not record where it ran: %+v", calls[1].Body)
	}
	if d, _ := calls[1].Body["duration_seconds"].(float64); d < 500 {
		t.Errorf("duration_seconds = %v, want the elapsed session", calls[1].Body["duration_seconds"])
	}
}

func TestWriteBackUsesTheSessionsOwnCredential(t *testing.T) {
	var auth string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		auth = r.Header.Get("Authorization")
		w.WriteHeader(http.StatusCreated)
		json.NewEncoder(w).Encode(map[string]any{"id": 1})
	}))
	defer srv.Close()
	stubSummarizer(t, &cfoperator.SessionSummary{Outcome: "resolved", Summary: "s"}, nil)

	captureStdout(t, func() {
		writeBackSession(wbCmd(t), 1889, srv.URL, "the-session-token", nil,
			wbMessages, time.Now(), "pod", "")
	})
	if auth != "Bearer the-session-token" {
		t.Errorf("Authorization = %q, want the credential that dies with the session", auth)
	}
}

// --- a session with nothing to say ------------------------------------------

func TestAnUntouchedSessionRecordsNothing(t *testing.T) {
	var calls []wbCall
	srv := wbAgent(t, &calls)
	stubSummarizer(t, &cfoperator.SessionSummary{Outcome: "resolved", Summary: "s"}, nil)

	// Attached, read the briefing, left: system-only transcript.
	captureStdout(t, func() {
		writeBackSession(wbCmd(t), 1889, srv.URL, "tok", nil,
			[]client.Message{{Role: "system", Content: "you are an sre"}},
			time.Now(), "pod", "")
	})
	if len(calls) != 0 {
		t.Errorf("a session with no exchanges should record nothing, got %+v", calls)
	}
}

func TestASessionWithNoReusableConclusionSeedsNoLearning(t *testing.T) {
	var calls []wbCall
	srv := wbAgent(t, &calls)
	stubSummarizer(t, &cfoperator.SessionSummary{
		Outcome: "no_change", Summary: "looked, it was a false alarm", Learning: nil}, nil)

	captureStdout(t, func() {
		writeBackSession(wbCmd(t), 1889, srv.URL, "tok", nil, wbMessages, time.Now(), "pod", "")
	})
	if len(calls) != 1 || calls[0].Path == cfoperator.LearningsPath {
		t.Fatalf("a session that concluded nothing reusable must not seed a learning: %+v", calls)
	}
	if calls[0].Body["outcome"] != "no_change" {
		t.Errorf("outcome = %v", calls[0].Body["outcome"])
	}
}

// --- degradation -------------------------------------------------------------

// TestAFailedSummaryStoresTheRawTailMarked is the issue's own instruction, and
// the marking is what keeps it honest: a session's only trace must not depend
// on a local model having a good day, and a transcript fragment must never be
// read as a conclusion.
func TestAFailedSummaryStoresTheRawTailMarked(t *testing.T) {
	var calls []wbCall
	srv := wbAgent(t, &calls)
	stubSummarizer(t, nil, io.ErrUnexpectedEOF)

	out := captureStdout(t, func() {
		writeBackSession(wbCmd(t), 1889, srv.URL, "tok", nil, wbMessages, time.Now(), "pod", "")
	})
	_ = out

	if len(calls) != 1 {
		t.Fatalf("the session should still be recorded, got %+v", calls)
	}
	body := calls[0].Body
	if body["degraded"] != true {
		t.Error("a raw tail must be marked degraded, or it reads as a summary")
	}
	if body["outcome"] != "inconclusive" {
		t.Errorf("outcome = %v, want inconclusive", body["outcome"])
	}
	if s, _ := body["summary"].(string); !strings.Contains(s, "raw transcript tail") {
		t.Errorf("summary does not announce what it is: %q", s)
	}
}

// TestAFailedWriteIsReportedNotSwallowed: an operator who believes a session
// was recorded and finds nothing later is worse off than one who was told.
func TestAFailedWriteIsReportedNotSwallowed(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer srv.Close()
	stubSummarizer(t, &cfoperator.SessionSummary{Outcome: "resolved", Summary: "s"}, nil)

	stderr := captureStderr(t, func() {
		writeBackSession(wbCmd(t), 1889, srv.URL, "tok", nil, wbMessages, time.Now(), "pod", "")
	})
	if !strings.Contains(stderr, "NOT recorded") {
		t.Errorf("a failed write must say so; stderr was:\n%s", stderr)
	}
}

// --- the opt-out -------------------------------------------------------------

// TestNoWriteBackIsLoud: the flag has a cost, and the operator should see it in
// the units they just spent rather than discovering the gap weeks later.
func TestNoWriteBackIsLoud(t *testing.T) {
	var calls []wbCall
	srv := wbAgent(t, &calls)
	stubSummarizer(t, &cfoperator.SessionSummary{Outcome: "resolved", Summary: "s"}, nil)

	stderr := captureStderr(t, func() {
		writeBackSession(wbCmd(t, "--no-writeback"), 1889, srv.URL, "tok", nil,
			wbMessages, time.Now(), "pod", "")
	})
	if len(calls) != 0 {
		t.Errorf("--no-writeback wrote anyway: %+v", calls)
	}
	if !strings.Contains(stderr, "no-writeback") || !strings.Contains(stderr, "discarded") {
		t.Errorf("--no-writeback should say what it threw away; stderr was:\n%s", stderr)
	}
}

// --- where the session ran ---------------------------------------------------

func TestWriteBackTargetDefaultsToTheLocalMachine(t *testing.T) {
	os.Unsetenv("CFOP_COCKPIT_TIER")
	os.Unsetenv("CFOP_COCKPIT_HOST")
	tier, host := writeBackTarget()
	if tier != "local" {
		t.Errorf("tier = %q, want local — a plain attach runs on the operator's own "+
			"machine, and 'resolved from a laptop' is a different claim from "+
			"'resolved on the affected node'", tier)
	}
	if host == "" {
		t.Error("host should fall back to this machine's hostname")
	}
}

func TestWriteBackTargetUsesWhatTheCockpitToldIt(t *testing.T) {
	t.Setenv("CFOP_COCKPIT_TIER", "container")
	t.Setenv("CFOP_COCKPIT_HOST", "ubuntu-llm-01")
	tier, host := writeBackTarget()
	if tier != "container" || host != "ubuntu-llm-01" {
		t.Errorf("target = %s/%s, want what the spawn put in the environment", tier, host)
	}
}

// TestTheDroppedLearningWarningReachesTheOperator closes the loop the unit test
// opens: ParseSummary reports the drop, and this is what puts it on their
// screen. The docs promise this exact warning.
func TestTheDroppedLearningWarningReachesTheOperator(t *testing.T) {
	var calls []wbCall
	srv := wbAgent(t, &calls)
	stubSummarizer(t, &cfoperator.SessionSummary{
		Outcome: "resolved", Summary: "s", Learning: nil,
		DroppedLearning: `"stale CIFS handle" is missing applies_when`,
	}, nil)

	stderr := captureStderr(t, func() {
		writeBackSession(wbCmd(t), 1889, srv.URL, "tok", nil, wbMessages, time.Now(), "pod", "")
	})
	if !strings.Contains(stderr, "was not stored") ||
		!strings.Contains(stderr, "applies_when") {
		t.Errorf("the operator was not told the learning was dropped; stderr was:\n%s", stderr)
	}
	if len(calls) != 1 || calls[0].Path == cfoperator.LearningsPath {
		t.Errorf("a dropped learning must not be sent anyway: %+v", calls)
	}
}
