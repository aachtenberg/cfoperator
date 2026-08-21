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
	"time"

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

// --- cockpit session token (CFOP-32) -----------------------------------------

func TestSwapSessionTokenIsWhatAChildActuallyReads(t *testing.T) {
	// The env var children inherit and every client reads is CFOP_API_TOKEN.
	// The swap must put the minted secret THERE — a side-channel variable
	// would leave the standing laptop token on exactly the path the session
	// credential exists to shrink.
	t.Setenv(cfoperator.EnvAPIToken, "standing-laptop-token")

	restore := swapSessionToken("cfop_minted_session_secret")
	if got := os.Getenv(cfoperator.EnvAPIToken); got != "cfop_minted_session_secret" {
		t.Fatalf("child-visible %s = %q, want the minted secret", cfoperator.EnvAPIToken, got)
	}

	restore()
	if got := os.Getenv(cfoperator.EnvAPIToken); got != "standing-laptop-token" {
		t.Fatalf("after restore %s = %q, want the standing token back", cfoperator.EnvAPIToken, got)
	}
}

func TestSwapSessionTokenRestoresAnUnsetEnvToUnset(t *testing.T) {
	// A laptop configured via config.yaml has no env token at all; restore
	// must not invent one.
	os.Unsetenv(cfoperator.EnvAPIToken)
	restore := swapSessionToken("cfop_minted")
	restore()
	if _, present := os.LookupEnv(cfoperator.EnvAPIToken); present {
		t.Fatalf("%s should be unset again after restore", cfoperator.EnvAPIToken)
	}
}

// --- cockpit spawn (CFOP-35) --------------------------------------------------

// stubKubectl replaces both kubectl hooks and returns the recorded argv of the
// interactive call. Nothing in these tests may shell out: CI has no cluster,
// and a test that silently ran the real kubectl would be asserting the runner's
// environment rather than this code.
func stubKubectl(t *testing.T, phase string) *[][]string {
	t.Helper()
	calls := &[][]string{}
	origCapture, origInteractive := runKubectlCapture, runKubectlInteractive
	origInterval, origTimeout := spawnPollInterval, spawnReadyTimeout
	spawnReadyTimeout = 50 * time.Millisecond
	runKubectlCapture = func(args ...string) (string, error) {
		*calls = append(*calls, append([]string{"capture"}, args...))
		return phase, nil
	}
	runKubectlInteractive = func(args ...string) error {
		*calls = append(*calls, append([]string{"interactive"}, args...))
		return nil
	}
	spawnPollInterval = time.Millisecond
	t.Cleanup(func() {
		runKubectlCapture, runKubectlInteractive = origCapture, origInteractive
		spawnPollInterval, spawnReadyTimeout = origInterval, origTimeout
		flagAttachSpawn, flagAttachPrint = false, false
	})
	return calls
}

func spawnStubAgent(t *testing.T, seen *[]*http.Request) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		*seen = append(*seen, r)
		if r.Method != http.MethodPost || r.URL.Path != cfoperator.SpawnPath {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		json.NewEncoder(w).Encode(map[string]any{
			"status":           "spawned",
			"job_name":         "cfop-cockpit-1889-010203",
			"namespace":        "apps",
			"investigation_id": 1889,
			"pod_selector":     "cfop-cockpit=1889",
			"attach_command":   "kubectl attach -it -n apps job/cfop-cockpit-1889-010203",
			"ttl_seconds":      14400,
			"token_prefix":     "cfop_abcd",
			"placement":        map[string]any{"node": "raspberrypi4", "note": "pinned to node raspberrypi4"},
		})
	}))
	t.Cleanup(srv.Close)
	return srv
}

// TestSpawnRoutesThroughTheEndpointThenTheOperatorsKubectl is the whole of
// tier 1 in one assertion: the agent creates the pod, and the *local* kubectl
// opens the terminal. If this ever became an agent-side attach, some service
// account would need pods/attach — the decision CFOP-59 exists to make.
func TestSpawnRoutesThroughTheEndpointThenTheOperatorsKubectl(t *testing.T) {
	var seen []*http.Request
	srv := spawnStubAgent(t, &seen)
	calls := stubKubectl(t, "Running")

	t.Setenv(cfoperator.EnvAgentURL, srv.URL)
	t.Setenv(cfoperator.EnvAPIToken, "test-token")
	cfgPath := filepath.Join(t.TempDir(), "config.yaml")
	os.WriteFile(cfgPath, []byte("llm:\n  provider: ollama\n"), 0o644)

	root := newTestRoot()
	root.SetOut(io.Discard)
	root.SetArgs([]string{"attach", "1889", "--spawn", "--config", cfgPath})
	captureStdout(t, func() {
		if err := root.Execute(); err != nil {
			t.Fatalf("attach --spawn failed: %v", err)
		}
	})

	if len(seen) != 1 {
		t.Fatalf("expected exactly one agent call, got %d", len(seen))
	}
	if seen[0].Method != http.MethodPost || seen[0].URL.Path != cfoperator.SpawnPath {
		t.Errorf("spawn called %s %s, want POST %s",
			seen[0].Method, seen[0].URL.Path, cfoperator.SpawnPath)
	}

	var attach []string
	for _, c := range *calls {
		if c[0] == "interactive" {
			attach = c[1:]
		}
	}
	if attach == nil {
		t.Fatal("no interactive kubectl call: nothing attached the operator to the cockpit")
	}
	want := []string{"attach", "-it", "-n", "apps", "job/cfop-cockpit-1889-010203"}
	if strings.Join(attach, " ") != strings.Join(want, " ") {
		t.Errorf("attach argv = %v, want %v", attach, want)
	}
	if kubectlBinary != "kubectl" {
		t.Errorf("the cockpit TTY must be the operator's own kubectl, got %q", kubectlBinary)
	}
}

// TestSpawnWaitsForThevPodBeforeAttaching: attaching to a Pending pod fails,
// and the pod is Pending for as long as the image pull takes on a Pi.
func TestSpawnWaitsForThePodBeforeAttaching(t *testing.T) {
	var seen []*http.Request
	srv := spawnStubAgent(t, &seen)
	calls := stubKubectl(t, "Running")

	t.Setenv(cfoperator.EnvAgentURL, srv.URL)
	t.Setenv(cfoperator.EnvAPIToken, "test-token")
	cfgPath := filepath.Join(t.TempDir(), "config.yaml")
	os.WriteFile(cfgPath, []byte("llm:\n  provider: ollama\n"), 0o644)

	root := newTestRoot()
	root.SetOut(io.Discard)
	root.SetArgs([]string{"attach", "1889", "--spawn", "--config", cfgPath})
	captureStdout(t, func() {
		if err := root.Execute(); err != nil {
			t.Fatalf("attach --spawn failed: %v", err)
		}
	})

	if len(*calls) < 2 || (*calls)[0][0] != "capture" {
		t.Fatalf("expected a readiness poll before the attach, got %v", *calls)
	}
	if (*calls)[len(*calls)-1][0] != "interactive" {
		t.Errorf("the attach must come last, got %v", *calls)
	}
}

// TestSpawnRefusesPrint: --print renders the briefing here and starts nothing;
// --spawn starts a pod and renders nothing here. Silently honouring one would
// leave an orphaned Job or an unexplained no-op.
func TestSpawnRefusesPrint(t *testing.T) {
	stubKubectl(t, "Running")
	cfgPath := filepath.Join(t.TempDir(), "config.yaml")
	os.WriteFile(cfgPath, []byte("llm:\n  provider: ollama\n"), 0o644)

	root := newTestRoot()
	root.SetOut(io.Discard)
	root.SetErr(io.Discard)
	root.SetArgs([]string{"attach", "1889", "--spawn", "--print", "--config", cfgPath})

	err := root.Execute()
	if err == nil {
		t.Fatal("--spawn --print should be refused, not silently resolved")
	}
	if !strings.Contains(err.Error(), "--print") {
		t.Errorf("the error should name the conflicting flags, got %v", err)
	}
}

func TestSpawnRefusesATrailingQuestion(t *testing.T) {
	stubKubectl(t, "Running")
	cfgPath := filepath.Join(t.TempDir(), "config.yaml")
	os.WriteFile(cfgPath, []byte("llm:\n  provider: ollama\n"), 0o644)

	root := newTestRoot()
	root.SetOut(io.Discard)
	root.SetErr(io.Discard)
	root.SetArgs([]string{"attach", "1889", "did it recover?", "--spawn", "--config", cfgPath})

	if err := root.Execute(); err == nil {
		t.Fatal("`--spawn <question>` should be refused: a one-shot does not need a pod")
	}
}

func TestSpawnIsARegisteredFlag(t *testing.T) {
	root := newTestRoot()
	attach, _, err := root.Find([]string{"attach"})
	if err != nil {
		t.Fatalf("find attach: %v", err)
	}
	if attach.Flags().Lookup("spawn") == nil {
		t.Error("attach must offer --spawn (docs/cockpit.md documents it as tier 1)")
	}
}

// --- the readiness wait must key on THIS Job ---------------------------------

// selectorOf pulls the value of `-l` out of a kubectl argv.
func selectorOf(args []string) string {
	for i, a := range args {
		if a == "-l" && i+1 < len(args) {
			return args[i+1]
		}
	}
	return ""
}

func stubPhases(t *testing.T, phase func(selector string, call int) string) {
	t.Helper()
	origCapture, origInterval, origTimeout := runKubectlCapture, spawnPollInterval, spawnReadyTimeout
	spawnPollInterval = time.Millisecond
	// The real deadline is three minutes (an image pull onto a Pi). A test that
	// regresses into never seeing Running must fail in a second, not stall the
	// suite for three minutes before saying so — which is exactly what the
	// mutation check of this guard did.
	spawnReadyTimeout = 50 * time.Millisecond
	calls := 0
	runKubectlCapture = func(args ...string) (string, error) {
		calls++
		return phase(selectorOf(args), calls), nil
	}
	t.Cleanup(func() {
		runKubectlCapture, spawnPollInterval, spawnReadyTimeout = origCapture, origInterval, origTimeout
	})
}

// TestWaitForCockpitIgnoresAnEarlierCockpitForTheSameInvestigation covers the
// ordinary re-run path rather than an edge case: spawn, work, exit — the pod is
// Succeeded but the Job lingers for ttlSecondsAfterFinished — then spawn again.
// Selecting on the per-investigation label matches that Succeeded pod next to
// the new Pending one, and the wait concludes the cockpit ended before it
// started. Kubernetes labels Job pods `job-name`, and the attach already
// addresses `job/<name>`; the wait has to agree with it.
//
// The two-pod fixture is the point: a single-pod one passes on the bug.
func TestWaitForCockpitIgnoresAnEarlierCockpitForTheSameInvestigation(t *testing.T) {
	const jobSelector = "job-name=cfop-cockpit-1889-142233"
	stubPhases(t, func(selector string, call int) string {
		switch selector {
		case "cfop-cockpit=1889":
			// The finished cockpit's pod, still labelled for the investigation.
			return "Succeeded Pending"
		case jobSelector:
			if call > 1 {
				return "Running" // the image pull finishes between polls
			}
			return "Pending"
		}
		return ""
	})

	cockpit := &cfoperator.Cockpit{
		Namespace:   "apps",
		JobName:     "cfop-cockpit-1889-142233",
		PodSelector: "cfop-cockpit=1889",
	}
	if err := waitForCockpit(cockpit); err != nil {
		t.Fatalf("wait bailed on a pod belonging to an earlier cockpit: %v", err)
	}
	if got := selectorOf(cockpitPhaseArgs(cockpit)); got != jobSelector {
		t.Errorf("wait selects on %q; it must select on this Job (%q)", got, jobSelector)
	}
}

// TestWaitForCockpitStillReportsThisJobsPodDying: narrowing the selector must
// not cost the real failure signal.
func TestWaitForCockpitStillReportsThisJobsPodDying(t *testing.T) {
	stubPhases(t, func(string, int) string { return "Failed" })

	err := waitForCockpit(&cfoperator.Cockpit{
		Namespace: "apps", JobName: "cfop-cockpit-1889-142233",
		PodSelector: "cfop-cockpit=1889",
	})
	if err == nil {
		t.Fatal("a pod that died must be reported, not waited on until the deadline")
	}
	if !strings.Contains(err.Error(), "ended before") {
		t.Errorf("unhelpful error for a dead pod: %v", err)
	}
}

// TestWaitForCockpitPrefersARunningPodOverATerminalOne: one Job can show both
// phases when its pod was replaced (eviction, lost node), and a live pod to
// attach to beats a dead one to report.
func TestWaitForCockpitPrefersARunningPodOverATerminalOne(t *testing.T) {
	stubPhases(t, func(string, int) string { return "Failed Running" })

	if err := waitForCockpit(&cfoperator.Cockpit{
		Namespace: "apps", JobName: "j", PodSelector: "cfop-cockpit=1889",
	}); err != nil {
		t.Fatalf("a Running pod should be attached to: %v", err)
	}
}
