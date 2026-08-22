package tui

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	tea "github.com/charmbracelet/bubbletea"

	"github.com/aachtenberg/cfoperator/cfassist-go/internal/client"
	"github.com/aachtenberg/cfoperator/cfassist-go/internal/config"
	"github.com/aachtenberg/cfoperator/cfassist-go/internal/skills"
	"github.com/aachtenberg/cfoperator/cfassist-go/internal/tools"
)

func newSkillModel(t *testing.T) *model {
	t.Helper()
	cfg := config.Defaults()
	cfg.Skills.Directory = t.TempDir() // no local overlay: the bundled nine
	llm := client.New("ollama", "http://127.0.0.1:1", "llama3.2", 0.7, "")
	return New(cfg, llm, tools.New(cfg), "system prompt", 0, nil, "ollama", nil, "")
}

// A session that ships nine playbooks and lists none of them is the bug.
func TestSkillsAreLoadedIntoTheSession(t *testing.T) {
	if got := len(newSkillModel(t).skills); got < 9 {
		t.Fatalf("session has %d skills, want the 9 built into the binary", got)
	}
}

// Names alone are the guessing this exists to remove.
func TestSkillsListShowsWhatEachOneIsFor(t *testing.T) {
	out := plainText(strings.Join(newSkillModel(t).skillsList(), "\n"))

	if !strings.Contains(out, "investigate-pod") {
		t.Fatalf("listing is missing a bundled skill:\n%s", out)
	}
	if !strings.Contains(out, "crash") && !strings.Contains(out, "pod") {
		t.Errorf("listing shows no description:\n%s", out)
	}
	if !strings.Contains(out, "/skill <name>") {
		t.Errorf("listing should say how to use one:\n%s", out)
	}
	// The MCP keyword tail is matching metadata, not something a human reads.
	if strings.Contains(out, "Keywords:") {
		t.Errorf("keyword tails should be trimmed from the menu:\n%s", out)
	}
}

// What the operator sees and what the model gets are deliberately different:
// the body is thousands of words, and burying the incident under it would make
// the scrollback useless exactly when it matters.
func TestSkillCommandSendsTheBodyButPrintsALine(t *testing.T) {
	m := newSkillModel(t)

	display, prompt := m.skillCommand("investigate-pod immich-kiosk-0")

	shown := plainText(strings.Join(display, "\n"))
	if !strings.Contains(shown, "investigate-pod") || !strings.Contains(shown, "immich-kiosk-0") {
		t.Errorf("the operator should see what ran against what:\n%s", shown)
	}
	if len(shown) > 400 {
		t.Errorf("the playbook body leaked into the scrollback (%d chars)", len(shown))
	}

	if len(prompt) < 500 {
		t.Fatalf("the model got %d chars; the playbook body should be sent in full", len(prompt))
	}
	if !strings.Contains(prompt, "Apply this playbook to: immich-kiosk-0") {
		t.Errorf("target missing from the prompt:\n%s", prompt[:200])
	}
}

func TestSkillCommandWithoutATargetStillRuns(t *testing.T) {
	_, prompt := newSkillModel(t).skillCommand("why-restart")
	if prompt == "" {
		t.Fatal("a skill with no target should still load")
	}
	if strings.Contains(prompt, "## Target") {
		t.Error("no target means no target section")
	}
}

// A half-remembered name should land somewhere useful, not on a bare error.
func TestAnUnknownSkillSuggestsTheNearOnes(t *testing.T) {
	display, prompt := newSkillModel(t).skillCommand("pod")

	if prompt != "" {
		t.Fatal("an unknown skill must not send anything to the model")
	}
	shown := plainText(strings.Join(display, "\n"))
	if !strings.Contains(shown, "investigate-pod") {
		t.Errorf("expected a suggestion for 'pod':\n%s", shown)
	}
}

func TestBareSkillCommandShowsTheList(t *testing.T) {
	display, prompt := newSkillModel(t).skillCommand("")
	if prompt != "" {
		t.Fatal("/skill with no argument must not run anything")
	}
	shown := plainText(strings.Join(display, "\n"))
	if !strings.Contains(shown, "Usage:") || !strings.Contains(shown, "investigate-pod") {
		t.Errorf("bare /skill should teach and list:\n%s", shown)
	}
}

// The overlay is how an operator's own playbook wins, and the listing has to
// say which is which — "why is it not doing what my file says" otherwise.
func TestALocalSkillIsMarkedInTheListing(t *testing.T) {
	dir := t.TempDir()
	writeLocalSkill(t, dir, "check-the-ups", "---\nname: check-the-ups\ndescription: is the UPS on battery\n---\n\nAsk it.")

	cfg := config.Defaults()
	cfg.Skills.Directory = dir
	llm := client.New("ollama", "http://127.0.0.1:1", "llama3.2", 0.7, "")
	m := New(cfg, llm, tools.New(cfg), "system prompt", 0, nil, "ollama", nil, "")

	out := plainText(strings.Join(m.skillsList(), "\n"))
	if !strings.Contains(out, "check-the-ups") || !strings.Contains(out, "(yours)") {
		t.Errorf("a local skill should be listed and marked:\n%s", out)
	}
	if !strings.Contains(out, dir) {
		t.Errorf("the listing should name where local skills come from:\n%s", out)
	}
}

func TestFirstSentenceTrimsTheMatchingMetadata(t *testing.T) {
	got := firstSentence("Investigate a pod. Use when it crashes. Keywords: pod, k8s, OOM.")
	if got != "Investigate a pod." {
		t.Errorf("firstSentence = %q", got)
	}
}

func TestSkillNamesAreOfferedForCompletion(t *testing.T) {
	m := newSkillModel(t)
	names := skills.Names(m.skills)
	if len(names) < 9 {
		t.Fatalf("completion source has %d names", len(names))
	}
	for _, n := range names {
		if n == "" {
			t.Error("an empty completion would insert nothing")
		}
	}
}

func writeLocalSkill(t *testing.T, dir, name, body string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Join(dir, name), 0o755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	if err := os.WriteFile(filepath.Join(dir, name, "SKILL.md"), []byte(body), 0o644); err != nil {
		t.Fatalf("write: %v", err)
	}
}

// The incident finds the operator on whatever terminal they have. A wrapped
// line turns a scannable menu into a paragraph.
func TestSkillsListFitsANarrowTerminal(t *testing.T) {
	m := newSkillModel(t)
	next, _ := m.Update(tea.WindowSizeMsg{Width: 72, Height: 24})
	narrow, ok := next.(*model)
	if !ok {
		t.Fatalf("Update returned %T", next)
	}

	for _, line := range narrow.skillsList() {
		if got := len(plainText(line)); got > 72 {
			t.Errorf("line is %d columns on a 72-column terminal: %q", got, plainText(line))
		}
	}

	// …and the names still survive, which is what the list is for.
	out := plainText(strings.Join(narrow.skillsList(), "\n"))
	if !strings.Contains(out, "why-restart") {
		t.Errorf("truncation ate the names:\n%s", out)
	}
}
