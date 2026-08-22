package skills

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// The bundled set is the reason a fresh install on a bare Pi is useful. An
// empty one would make /skills an empty list that looks like a feature working.
func TestBundledSkillsAreEmbedded(t *testing.T) {
	all := Load("")
	if len(all) < 9 {
		t.Fatalf("got %d bundled skills, want the 9 in skills/", len(all))
	}

	want := map[string]bool{"investigate-pod": false, "why-restart": false, "k3s-cluster-health": false}
	for _, s := range all {
		if _, ok := want[s.Name]; ok {
			want[s.Name] = true
		}
		if s.Source != SourceBundled {
			t.Errorf("%s: source = %q, want bundled", s.Name, s.Source)
		}
		if s.Description == "" || s.Body == "" {
			t.Errorf("%s: empty description or body", s.Name)
		}
	}
	for name, found := range want {
		if !found {
			t.Errorf("bundled set is missing %s", name)
		}
	}
}

func TestSkillsAreSortedByName(t *testing.T) {
	all := Load("")
	for i := 1; i < len(all); i++ {
		if all[i-1].Name > all[i].Name {
			t.Fatalf("not sorted: %s before %s", all[i-1].Name, all[i].Name)
		}
	}
}

// The overlay replaces by name rather than appending: two investigate-pod
// entries in a list is worse than either one alone.
func TestALocalSkillReplacesABundledOne(t *testing.T) {
	dir := t.TempDir()
	writeSkill(t, dir, "investigate-pod", `---
name: investigate-pod
description: my own pod playbook
---

Check the thing I actually care about.`)

	all := Load(dir)

	count := 0
	for _, s := range all {
		if s.Name == "investigate-pod" {
			count++
			if s.Description != "my own pod playbook" {
				t.Errorf("bundled skill won: %q", s.Description)
			}
			if s.Source != SourceLocal {
				t.Errorf("source = %q, want local", s.Source)
			}
		}
	}
	if count != 1 {
		t.Fatalf("investigate-pod appears %d times, want 1", count)
	}
	if len(all) < 9 {
		t.Errorf("the overlay dropped bundled skills: %d left", len(all))
	}
}

func TestALocalSkillCanAddANewOne(t *testing.T) {
	dir := t.TempDir()
	writeSkill(t, dir, "check-the-ups", `---
name: check-the-ups
description: is the UPS on battery
---

Ask the UPS.`)

	if _, ok := Find(Load(dir), "check-the-ups"); !ok {
		t.Fatal("a local-only skill should be loadable")
	}
}

// One malformed playbook must not cost the operator the other eight.
func TestAMalformedSkillIsSkippedNotFatal(t *testing.T) {
	dir := t.TempDir()
	writeSkill(t, dir, "broken", "no frontmatter here at all")
	writeSkill(t, dir, "fine", "---\nname: fine\ndescription: ok\n---\n\nbody")

	all := Load(dir)
	if _, ok := Find(all, "broken"); ok {
		t.Error("a skill without frontmatter should be skipped")
	}
	if _, ok := Find(all, "fine"); !ok {
		t.Error("the good skill next to it should still load")
	}
	if len(all) < 9 {
		t.Error("bundled skills should be unaffected")
	}
}

func TestNameAndDescriptionFallBackToTheDirectory(t *testing.T) {
	dir := t.TempDir()
	writeSkill(t, dir, "nameless", "---\nunrelated: true\n---\n\nbody text")

	s, ok := Find(Load(dir), "nameless")
	if !ok {
		t.Fatal("a skill with frontmatter but no name should use its directory name")
	}
	if !strings.Contains(s.Description, "nameless") {
		t.Errorf("description = %q, want a generated one naming the skill", s.Description)
	}
}

// The target suffix has to match mcp_server/prompts/playbooks.py exactly: the
// same playbook must mean the same thing from cfassist and from an MCP host.
func TestPromptAppendsTheTargetInTheSharedFormat(t *testing.T) {
	s := Skill{Name: "x", Body: "do the thing"}

	if got := s.Prompt(""); got != "do the thing" {
		t.Errorf("no target should leave the body alone, got %q", got)
	}
	want := "do the thing\n\n## Target\n\nApply this playbook to: immich-kiosk-0"
	if got := s.Prompt("  immich-kiosk-0  "); got != want {
		t.Errorf("Prompt() = %q, want %q", got, want)
	}
}

func TestFindIsCaseInsensitiveAndNamesAreListable(t *testing.T) {
	all := Load("")
	if _, ok := Find(all, "INVESTIGATE-POD"); !ok {
		t.Error("operators type what they remember, not what they saw")
	}
	if len(Names(all)) != len(all) {
		t.Error("Names must cover every skill — it feeds tab completion")
	}
}

func TestAMissingLocalDirectoryIsNotAnError(t *testing.T) {
	if len(Load(filepath.Join(t.TempDir(), "nope"))) < 9 {
		t.Error("most machines have no local skills dir; bundled must still load")
	}
}

func writeSkill(t *testing.T, dir, name, content string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Join(dir, name), 0o755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	if err := os.WriteFile(filepath.Join(dir, name, "SKILL.md"), []byte(content), 0o644); err != nil {
		t.Fatalf("write: %v", err)
	}
}
