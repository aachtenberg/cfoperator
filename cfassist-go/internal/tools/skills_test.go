package tools

import (
	"context"
	"os"
	"strings"
	"testing"

	"github.com/aachtenberg/cfoperator/cfassist-go/internal/config"
	"github.com/aachtenberg/cfoperator/cfassist-go/internal/skills"
)

func newSkillRegistry(t *testing.T) (*Registry, []skills.Skill) {
	t.Helper()
	cfg := config.Defaults()
	cfg.Memory.Directory = os.TempDir()
	all := skills.Load(t.TempDir()) // bundled only
	r := New(cfg)
	r.AddSkills(all)
	return r, all
}

func skillSchema(t *testing.T, r *Registry) (description string, params map[string]any) {
	t.Helper()
	for _, s := range r.GetSchemas() {
		if s.Function.Name == "skill" {
			return s.Function.Description, s.Function.Parameters
		}
	}
	t.Fatal("skill tool not registered")
	return "", nil
}

// A model that has to ask what exists before it can use anything is the same
// guessing problem one layer down — and small local models fumble the two-step
// dance. The names live in the description.
func TestTheSkillToolNamesThePlaybooksUpFront(t *testing.T) {
	r, all := newSkillRegistry(t)
	description, _ := skillSchema(t, r)

	for _, s := range all {
		if !strings.Contains(description, s.Name) {
			t.Errorf("description does not mention %s", s.Name)
		}
	}
	if strings.Contains(description, "Keywords:") {
		t.Error("MCP keyword tails are context spent on nothing")
	}
	// Cheap enough to always carry: this rides in every request.
	if len(description) > 1600 {
		t.Errorf("description is %d chars; it is in every prompt", len(description))
	}
}

// An enum means a plausible-sounding playbook cannot be invented and then
// reported as having been run.
func TestTheNameParameterIsAnEnumOfRealSkills(t *testing.T) {
	r, all := newSkillRegistry(t)
	_, params := skillSchema(t, r)

	props, _ := params["properties"].(map[string]any)
	name, _ := props["name"].(map[string]any)
	enum, ok := name["enum"].([]string)
	if !ok {
		t.Fatalf("name has no enum: %#v", name)
	}
	if len(enum) != len(all) {
		t.Errorf("enum has %d entries, want %d", len(enum), len(all))
	}
	for _, n := range enum {
		if _, found := skills.Find(all, n); !found {
			t.Errorf("enum offers %q, which is not a real skill", n)
		}
	}
}

func TestTheSkillToolReturnsThePlaybook(t *testing.T) {
	r, _ := newSkillRegistry(t)

	res := r.Execute(context.Background(), "skill", map[string]any{"name": "why-restart", "target": "immich-kiosk-0"})

	if res["skill"] != "why-restart" {
		t.Fatalf("result = %+v", res)
	}
	playbook, _ := res["playbook"].(string)
	if len(playbook) < 500 {
		t.Errorf("playbook is %d chars; the body should come back whole", len(playbook))
	}
	if !strings.Contains(playbook, "Apply this playbook to: immich-kiosk-0") {
		t.Error("target did not reach the playbook")
	}
}

func TestTheSkillToolWorksWithoutATarget(t *testing.T) {
	r, _ := newSkillRegistry(t)
	res := r.Execute(context.Background(), "skill", map[string]any{"name": "k3s-cluster-health"})
	if _, bad := res["error"]; bad {
		t.Fatalf("a targetless playbook is legitimate: %+v", res)
	}
	if strings.Contains(res["playbook"].(string), "## Target") {
		t.Error("no target means no target section")
	}
}

// A wrong guess should be fixable on the next turn rather than sending the
// model back to improvising, which is what this tool exists to replace.
func TestAnUnknownPlaybookComesBackWithTheRealNames(t *testing.T) {
	r, all := newSkillRegistry(t)

	res := r.Execute(context.Background(), "skill", map[string]any{"name": "investigate-everything"})

	if _, bad := res["error"]; !bad {
		t.Fatal("an unknown playbook must be an error")
	}
	available, _ := res["available"].([]string)
	if len(available) != len(all) {
		t.Errorf("error listed %d names, want all %d", len(available), len(all))
	}
}

func TestTheSkillToolIsAbsentWhenThereAreNoSkills(t *testing.T) {
	cfg := config.Defaults()
	cfg.Memory.Directory = os.TempDir()
	r := New(cfg)
	r.AddSkills(nil)

	for _, s := range r.GetSchemas() {
		if s.Function.Name == "skill" {
			t.Fatal("a skill tool with no skills can only fail")
		}
	}
}
