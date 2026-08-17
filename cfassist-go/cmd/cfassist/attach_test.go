package main

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/aachtenberg/cfoperator/cfassist-go/internal/cfoperator"
	"github.com/spf13/cobra"
)

// newTestRoot rebuilds the command tree the way main() does. Kept in sync by
// construction: both call newAttachCmd, so a verb rename cannot pass here while
// breaking the binary.
func newTestRoot() *cobra.Command {
	root := &cobra.Command{
		Use:           "cfassist [question]",
		Args:          cobra.ArbitraryArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE:          func(cmd *cobra.Command, args []string) error { return nil },
	}
	root.PersistentFlags().StringVar(&flagConfig, "config", "", "")
	root.PersistentFlags().StringVar(&flagModel, "model", "", "")
	root.PersistentFlags().StringVar(&flagURL, "url", "", "")
	root.PersistentFlags().StringVar(&flagProvider, "provider", "", "")
	root.AddCommand(newAttachCmd())
	return root
}

// TestAttachIsARegisteredVerb is half of the Slack↔CLI contract: the other half
// lives in mcp_server/tests/test_mcp_recipe.py, which reads the verb out of this
// source file. Together they assert the command the notification advertises is
// the command the released binary implements.
func TestAttachIsARegisteredVerb(t *testing.T) {
	root := newTestRoot()

	var found *cobra.Command
	for _, c := range root.Commands() {
		if c.Name() == "attach" {
			found = c
			break
		}
	}
	if found == nil {
		t.Fatal("`cfassist attach` is advertised in every Slack/Discord/ntfy notification but is not a registered command")
	}
	if found.Name() != cfoperator.AttachVerb {
		t.Errorf("registered verb %q != cfoperator.AttachVerb %q", found.Name(), cfoperator.AttachVerb)
	}
	if found.Flags().Lookup("print") == nil {
		t.Error("attach must offer --print")
	}
}

// TestAttachRequiresAnInvestigation: bare `cfassist attach` must not fall
// through to something that looks like it worked.
func TestAttachRequiresAnInvestigation(t *testing.T) {
	root := newTestRoot()
	root.SetOut(io.Discard)
	root.SetErr(io.Discard)
	root.SetArgs([]string{"attach"})

	if err := root.Execute(); err == nil {
		t.Fatal("`cfassist attach` with no id should be an error")
	}
}

func TestAttachRejectsABadReference(t *testing.T) {
	root := newTestRoot()
	root.SetOut(io.Discard)
	root.SetErr(io.Discard)
	root.SetArgs([]string{"attach", "release-8"})

	err := root.Execute()
	if err == nil {
		t.Fatal("`attach release-8` should be refused, not silently read as 8")
	}
	if !strings.Contains(err.Error(), "not an investigation id") {
		t.Errorf("error should say what was wrong, got %v", err)
	}
}

// TestRootStillTakesAOneShotQuestion guards the regression the previous attempt
// hit in its own CLI framework: adding a subcommand must not swallow the
// existing `cfassist "why is etcd unhappy"` one-shot form.
func TestRootStillTakesAOneShotQuestion(t *testing.T) {
	root := newTestRoot()
	var got []string
	root.RunE = func(cmd *cobra.Command, args []string) error {
		got = args
		return nil
	}
	root.SetOut(io.Discard)
	root.SetArgs([]string{"--model", "m", "why is etcd unhappy"})

	if err := root.Execute(); err != nil {
		t.Fatalf("one-shot mode broke: %v", err)
	}
	if len(got) != 1 || got[0] != "why is etcd unhappy" {
		t.Errorf("root args = %v, want the question", got)
	}
	if flagModel != "m" {
		t.Errorf("--model should still reach root, got %q", flagModel)
	}
}

// TestAttachInheritsPersistentFlags: attach seeds a real session, so it has to
// honour the same provider/model selection as a plain run.
func TestAttachInheritsPersistentFlags(t *testing.T) {
	root := newTestRoot()
	attach, _, err := root.Find([]string{"attach"})
	if err != nil {
		t.Fatalf("find attach: %v", err)
	}
	for _, name := range []string{"config", "model", "url", "provider"} {
		if attach.InheritedFlags().Lookup(name) == nil {
			t.Errorf("attach should inherit --%s", name)
		}
	}
}

// --- end to end ---------------------------------------------------------------

// TestAttachPrintRendersABriefing drives the real command against a stub agent
// API: parse the ref, make the GETs, render, print, and start nothing.
func TestAttachPrintRendersABriefing(t *testing.T) {
	var methods []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		methods = append(methods, r.Method)
		w.Header().Set("Content-Type", "application/json")
		switch {
		case strings.HasPrefix(r.URL.Path, "/api/investigations/"):
			json.NewEncoder(w).Encode(map[string]any{
				"id":      1889,
				"outcome": "needs_action",
				"trigger": "PodUnschedulable on headless-gpu",
				"host_id": "headless-gpu",
				"findings": map[string]any{
					"response":       "the node is out of memory",
					"recommendation": "raise the limit",
				},
			})
		case r.URL.Path == "/api/remediations":
			json.NewEncoder(w).Encode(map[string]any{
				"remediations": []map[string]any{
					{"id": 42, "investigation_id": 1889, "status": "queued"},
				},
			})
		case r.URL.Path == "/api/kb/search":
			json.NewEncoder(w).Encode(map[string]any{
				"mode":    "fts",
				"results": []map[string]any{{"id": 7, "title": "a learning", "investigation_id": 1889}},
			})
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer srv.Close()

	t.Setenv(cfoperator.EnvAgentURL, srv.URL)
	t.Setenv(cfoperator.EnvAPIToken, "test-token")

	// An empty config file, so the run cannot pick up the developer's own.
	cfgPath := filepath.Join(t.TempDir(), "config.yaml")
	if err := os.WriteFile(cfgPath, []byte("llm:\n  provider: ollama\n"), 0o644); err != nil {
		t.Fatalf("write config: %v", err)
	}

	out := captureStdout(t, func() {
		root := newTestRoot()
		root.SetArgs([]string{"attach", "#1889", "--print", "--config", cfgPath})
		if err := root.Execute(); err != nil {
			t.Fatalf("attach --print failed: %v", err)
		}
	})

	for _, want := range []string{
		"CFOperator briefing",
		"investigation #1889",
		"outcome=needs_action",
		"PodUnschedulable on headless-gpu",
		"the node is out of memory",
		"raise the limit",
		"Linked remediation queue rows (1):",
		"search mode: fts",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("briefing missing %q, got:\n%s", want, out)
		}
	}

	// Read-only, observed from the server side rather than asserted in the client.
	for _, m := range methods {
		if m != http.MethodGet {
			t.Errorf("attach issued a %s; it must only GET", m)
		}
	}
	if len(methods) != 3 {
		t.Errorf("expected 3 GETs (investigation, remediations, kb), got %d: %v", len(methods), methods)
	}
}

// TestAttachPrintReportsAMissingInvestigation: a bad id must fail loudly rather
// than print an empty briefing.
func TestAttachPrintReportsAMissingInvestigation(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusNotFound)
	}))
	defer srv.Close()

	t.Setenv(cfoperator.EnvAgentURL, srv.URL)
	t.Setenv(cfoperator.EnvAPIToken, "test-token")

	cfgPath := filepath.Join(t.TempDir(), "config.yaml")
	os.WriteFile(cfgPath, []byte("llm:\n  provider: ollama\n"), 0o644)

	root := newTestRoot()
	root.SetOut(io.Discard)
	root.SetArgs([]string{"attach", "999999", "--print", "--config", cfgPath})

	if err := root.Execute(); err == nil {
		t.Fatal("a missing investigation should fail, not print an empty briefing")
	}
}

// TestAttachPrintSurfacesTheTokenHint: 401 is the most common first-run failure,
// and the fix (mint a token) has to be in the message.
func TestAttachPrintSurfacesTheTokenHint(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
	}))
	defer srv.Close()

	t.Setenv(cfoperator.EnvAgentURL, srv.URL)
	t.Setenv(cfoperator.EnvAPIToken, "bad-token")

	cfgPath := filepath.Join(t.TempDir(), "config.yaml")
	os.WriteFile(cfgPath, []byte("llm:\n  provider: ollama\n"), 0o644)

	root := newTestRoot()
	root.SetOut(io.Discard)
	root.SetArgs([]string{"attach", "1889", "--print", "--config", cfgPath})

	err := root.Execute()
	if err == nil {
		t.Fatal("a rejected token should be an error")
	}
	if !strings.Contains(err.Error(), "hint:") {
		t.Errorf("401 should carry its fix, got %v", err)
	}
	if !strings.Contains(err.Error(), "admin?tab=tokens") {
		t.Errorf("401 hint should point at the mint page, got %v", err)
	}
}

func captureStdout(t *testing.T, fn func()) string {
	t.Helper()
	orig := os.Stdout
	r, w, err := os.Pipe()
	if err != nil {
		t.Fatalf("pipe: %v", err)
	}
	os.Stdout = w

	done := make(chan string, 1)
	go func() {
		data, _ := io.ReadAll(r)
		done <- string(data)
	}()

	fn()

	w.Close()
	os.Stdout = orig
	return <-done
}
