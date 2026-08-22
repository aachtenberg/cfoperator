// Package skills puts CFOperator's investigation playbooks in the terminal
// (CFOP-69).
//
// The nine skills/*/SKILL.md files were already registered as MCP prompts for
// *other* hosts — Claude Desktop, Cursor, the Slack bridge — and were
// unreachable from cfassist, the tool an operator actually has in their hands
// during an incident. The product's accumulated know-how was available
// everywhere except the incident.
//
// # Why they are baked into the binary
//
// cfassist is one static file that gets curl'd onto a Pi. Skills that live in
// the repo would be absent there; skills fetched from a connected CFOperator
// would vanish exactly when the agent is the thing that is broken. Embedded,
// they cost 43KB and are always present. ~/.cfassist/skills/ is overlaid on top
// by name, so an operator can add their own or replace one of ours.
//
// # The parsing contract is shared, by hand
//
// mcp_server/prompts/playbooks.py parses the same files for MCP hosts: directory
// name as the fallback name, frontmatter name/description when present, body as
// the text, and the same "## Target" suffix. The two implementations are in
// different languages and different deploy artifacts, so they are kept honest by
// test_skills_bundle.py rather than by a shared import — the same arrangement as
// the attach verb.
package skills

import (
	"embed"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"gopkg.in/yaml.v3"
)

// bundled holds a copy of the repo's skills/ tree. Go embed cannot cross the
// module boundary, so the copy is committed and test_skills_bundle.py fails if
// it drifts — a silently stale playbook is the failure worth guarding.
//
//go:embed all:bundled
var bundled embed.FS

// SourceBundled marks a skill that shipped inside the binary.
const SourceBundled = "bundled"

// SourceLocal marks one loaded from the operator's own directory.
const SourceLocal = "local"

// Skill is one playbook: what it is for, and the text to hand a model.
type Skill struct {
	Name        string
	Description string
	Body        string
	Source      string
}

// Load returns every available skill, sorted by name.
//
// localDir (usually ~/.cfassist/skills) is overlaid on the bundled set by name:
// a local skill called investigate-pod replaces ours rather than appearing
// twice. An unreadable or absent localDir is not an error — most machines have
// none.
func Load(localDir string) []Skill {
	byName := make(map[string]Skill)

	for _, s := range loadEmbedded() {
		byName[s.Name] = s
	}
	for _, s := range loadDir(localDir) {
		byName[s.Name] = s
	}

	out := make([]Skill, 0, len(byName))
	for _, s := range byName {
		out = append(out, s)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out
}

// Find returns one skill by name.
func Find(all []Skill, name string) (Skill, bool) {
	name = strings.TrimSpace(strings.ToLower(name))
	for _, s := range all {
		if strings.ToLower(s.Name) == name {
			return s, true
		}
	}
	return Skill{}, false
}

// Names returns the skill names, for tab completion.
func Names(all []Skill) []string {
	names := make([]string, 0, len(all))
	for _, s := range all {
		names = append(names, s.Name)
	}
	return names
}

// Prompt renders the skill for a session, optionally aimed at something.
//
// The target suffix matches mcp_server/prompts/playbooks.py exactly: the same
// playbook invoked from cfassist and from an MCP host must produce the same
// text, or "run the pod playbook" means two different things depending on where
// you are sitting.
func (s Skill) Prompt(target string) string {
	if strings.TrimSpace(target) == "" {
		return s.Body
	}
	return s.Body + "\n\n## Target\n\nApply this playbook to: " + strings.TrimSpace(target)
}

func loadEmbedded() []Skill {
	entries, err := bundled.ReadDir("bundled")
	if err != nil {
		return nil
	}
	var out []Skill
	for _, e := range entries {
		if !e.IsDir() {
			continue
		}
		data, err := bundled.ReadFile(filepath.Join("bundled", e.Name(), "SKILL.md"))
		if err != nil {
			continue
		}
		if s, ok := parse(e.Name(), string(data), SourceBundled); ok {
			out = append(out, s)
		}
	}
	return out
}

func loadDir(dir string) []Skill {
	if strings.TrimSpace(dir) == "" {
		return nil
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil
	}
	var out []Skill
	for _, e := range entries {
		if !e.IsDir() {
			continue
		}
		data, err := os.ReadFile(filepath.Join(dir, e.Name(), "SKILL.md"))
		if err != nil {
			continue
		}
		if s, ok := parse(e.Name(), string(data), SourceLocal); ok {
			out = append(out, s)
		}
	}
	return out
}

// parse splits SKILL.md into its frontmatter and body.
//
// A file without parseable frontmatter is skipped rather than fatal — same as
// the Python side. One malformed playbook must not cost an operator the other
// eight mid-incident.
func parse(dirName, text, source string) (Skill, bool) {
	if !strings.HasPrefix(text, "---") {
		return Skill{}, false
	}
	parts := strings.SplitN(text, "---", 3)
	if len(parts) < 3 {
		return Skill{}, false
	}

	var meta struct {
		Name        string `yaml:"name"`
		Description string `yaml:"description"`
	}
	if err := yaml.Unmarshal([]byte(parts[1]), &meta); err != nil {
		return Skill{}, false
	}

	body := strings.TrimSpace(parts[2])
	if body == "" {
		return Skill{}, false
	}

	name := strings.TrimSpace(meta.Name)
	if name == "" {
		name = dirName
	}
	description := strings.TrimSpace(meta.Description)
	if description == "" {
		description = "CFOperator playbook: " + name
	}

	return Skill{Name: name, Description: description, Body: body, Source: source}, true
}
