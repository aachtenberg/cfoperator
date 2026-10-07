package tools

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/aachtenberg/cfoperator/cfassist-go/internal/config"
)

// recordingAsker answers with a fixed decision and remembers what it was asked.
type recordingAsker struct {
	decision Decision
	asked    []string
}

func (a *recordingAsker) ask(ctx context.Context, command, reason string) Decision {
	a.asked = append(a.asked, command+" :: "+reason)
	return a.decision
}

func gatedRegistry(t *testing.T, decision Decision) (*Registry, *recordingAsker) {
	t.Helper()
	cfg := config.Defaults()
	cfg.Memory.Directory = t.TempDir()
	r := New(cfg)
	a := &recordingAsker{decision: decision}
	r.ConfirmBashWrites(a.ask)
	return r, a
}

func toolNames(r *Registry) map[string]bool {
	names := map[string]bool{}
	for _, s := range r.GetSchemas() {
		names[s.Function.Name] = true
	}
	return names
}

func TestWithoutBashRemovesTheToolEverywhereTheModelCouldSeeIt(t *testing.T) {
	r := newTestRegistry()
	r.WithoutBash()
	if toolNames(r)["bash"] {
		t.Fatal("bash is still offered in the schemas")
	}
	res := r.Execute(context.Background(), "bash", map[string]any{"command": "echo hi"})
	if _, ok := res["error"]; !ok {
		t.Fatalf("bash still executes after WithoutBash: %v", res)
	}
	listed := r.Execute(context.Background(), "list_tools", nil)
	for _, entry := range listed["tools"].([]map[string]string) {
		if entry["name"] == "bash" {
			t.Fatal("list_tools still names bash")
		}
	}
	if !toolNames(r)["read_file"] {
		t.Fatal("read_file should survive; only the shell goes")
	}
}

func TestAReadRunsWithoutAsking(t *testing.T) {
	r, a := gatedRegistry(t, Deny)
	res := r.Execute(context.Background(), "bash", map[string]any{"command": "echo hello"})
	if out, _ := res["stdout"].(string); !strings.Contains(out, "hello") {
		t.Fatalf("read did not run: %v", res)
	}
	if len(a.asked) != 0 {
		t.Fatalf("a read must not prompt: %v", a.asked)
	}
}

func TestADeclinedWriteDoesNotRunAndSaysSo(t *testing.T) {
	r, a := gatedRegistry(t, Deny)
	marker := filepath.Join(t.TempDir(), "touched")
	res := r.Execute(context.Background(), "bash", map[string]any{"command": "touch " + marker})
	if _, err := os.Stat(marker); err == nil {
		t.Fatal("the declined command ran: the marker file exists")
	}
	msg, _ := res["error"].(string)
	if !strings.Contains(msg, "not run") || !strings.Contains(msg, "changes the filesystem") || !strings.Contains(msg, "Do not retry") {
		t.Fatalf("the model must be told it did not run, and why: %v", res)
	}
	if len(a.asked) != 1 || !strings.Contains(a.asked[0], "touch "+marker) {
		t.Fatalf("the operator was asked %v", a.asked)
	}
}

func TestAnAllowedWriteRuns(t *testing.T) {
	r, _ := gatedRegistry(t, Allow)
	marker := filepath.Join(t.TempDir(), "touched")
	r.Execute(context.Background(), "bash", map[string]any{"command": "touch " + marker})
	if _, err := os.Stat(marker); err != nil {
		t.Fatal("the allowed command did not run")
	}
}

func TestAllowAllStopsAskingForTheSession(t *testing.T) {
	r, a := gatedRegistry(t, AllowAll)
	dir := t.TempDir()
	r.Execute(context.Background(), "bash", map[string]any{"command": "touch " + filepath.Join(dir, "one")})
	r.Execute(context.Background(), "bash", map[string]any{"command": "touch " + filepath.Join(dir, "two")})
	if len(a.asked) != 1 {
		t.Fatalf("asked %d times; 'all' should have been asked once", len(a.asked))
	}
	if _, err := os.Stat(filepath.Join(dir, "two")); err != nil {
		t.Fatal("the second write did not run")
	}
}

func TestNoAskerMeansNoOperatorAndTheWriteIsRefused(t *testing.T) {
	cfg := config.Defaults()
	cfg.Memory.Directory = t.TempDir()
	r := New(cfg) // gate installed, nobody to ask
	marker := filepath.Join(t.TempDir(), "touched")
	res := r.Execute(context.Background(), "bash", map[string]any{"command": "touch " + marker})
	if _, err := os.Stat(marker); err == nil {
		t.Fatal("ran with no operator to confirm")
	}
	if msg, _ := res["error"].(string); !strings.Contains(msg, "no operator") {
		t.Fatalf("got %v", res)
	}
}

func TestTheLatestAskerWinsAndNothingIsWrappedTwice(t *testing.T) {
	r, first := gatedRegistry(t, Deny)
	second := &recordingAsker{decision: Allow}
	r.ConfirmBashWrites(second.ask)
	marker := filepath.Join(t.TempDir(), "touched")
	r.Execute(context.Background(), "bash", map[string]any{"command": "touch " + marker})
	if len(first.asked) != 0 || len(second.asked) != 1 {
		t.Fatalf("first asked %d, second asked %d; want 0 and 1", len(first.asked), len(second.asked))
	}
	if _, err := os.Stat(marker); err != nil {
		t.Fatal("the second asker allowed it and it did not run")
	}
}

func TestConfirmWritesOffRestoresTheOldBehaviour(t *testing.T) {
	off := false
	cfg := config.Defaults()
	cfg.Memory.Directory = t.TempDir()
	cfg.Tools.Bash.ConfirmWrites = &off
	r := New(cfg)
	a := &recordingAsker{decision: Deny}
	r.ConfirmBashWrites(a.ask)
	marker := filepath.Join(t.TempDir(), "touched")
	r.Execute(context.Background(), "bash", map[string]any{"command": "touch " + marker})
	if _, err := os.Stat(marker); err != nil {
		t.Fatal("confirm_writes: false must run writes unasked, as before")
	}
	if len(a.asked) != 0 {
		t.Fatal("confirm_writes: false must not prompt")
	}
}

func TestTheGatedToolTellsTheModelAboutTheGate(t *testing.T) {
	r := newTestRegistry()
	for _, s := range r.GetSchemas() {
		if s.Function.Name == "bash" && !strings.Contains(s.Function.Description, "shown to the operator first") {
			t.Fatal("the bash description must say writes are confirmed, so the model expects the error")
		}
	}
}
