// The human-facing half of skills in the TUI (CFOP-69): listing what this
// session can do, and loading one playbook into it.
//
// The command handling is factored out of handleSubmit as pure functions over
// the model's skill set, because the interesting behaviour — what gets shown,
// what gets sent to the model, and the fact that those are deliberately
// different — is worth testing without a terminal.

package tui

import (
	"fmt"
	"strings"

	"github.com/aachtenberg/cfoperator/cfassist-go/internal/skills"
)

// skillsList renders /skills: every playbook, what it is for, and where it came
// from. Descriptions are the point — a list of bare names is the guessing this
// exists to remove.
func (m *model) skillsList() []string {
	if len(m.skills) == 0 {
		return []string{
			"",
			dimStyle.Render("  No skills available. Drop a SKILL.md in " + m.cfg.Skills.Directory + "/<name>/"),
		}
	}

	// The descriptions are written for MCP hosts and run long. This list is read
	// on whatever terminal the incident found the operator on — often 80
	// columns, sometimes a phone-tethered laptop — and a line that wraps turns a
	// scannable menu into a paragraph.
	width := m.width
	if width <= 0 {
		width = 100
	}
	room := width - skillNameColumn - 6

	lines := []string{"", bannerStyle.Render("Skills:")}
	local := 0
	for _, s := range m.skills {
		if s.Source == skills.SourceLocal {
			local++
		}
		lines = append(lines, fmt.Sprintf("  %s  %s%s",
			toolNameStyle.Render(padRight(s.Name, skillNameColumn)),
			dimStyle.Render(clip(firstSentence(s.Description), room)),
			sourceMarker(s.Source),
		))
	}

	summary := fmt.Sprintf("  %d built in", len(m.skills)-local)
	if local > 0 {
		summary += fmt.Sprintf(", %d yours from %s", local, m.cfg.Skills.Directory)
	}
	return append(lines,
		"",
		dimStyle.Render(summary),
		dimStyle.Render("  Load one with: /skill <name> [target]"),
	)
}

// skillCommand handles "/skill <name> [target]".
//
// Returns what to show the operator and what to send the model — which are not
// the same thing on purpose. The playbook body is thousands of words; printing
// it would bury the incident in the scrollback the operator is trying to read,
// while the model needs all of it.
func (m *model) skillCommand(args string) (display []string, prompt string) {
	name, target, _ := strings.Cut(strings.TrimSpace(args), " ")
	name = strings.TrimSpace(name)
	target = strings.TrimSpace(target)

	if name == "" {
		return append([]string{"", dimStyle.Render("  Usage: /skill <name> [target]")},
			m.skillsList()...), ""
	}

	skill, ok := skills.Find(m.skills, name)
	if !ok {
		display = []string{"", errorStyle.Render("  No skill called " + name + ".")}
		if near := nearestSkills(m.skills, name); len(near) > 0 {
			display = append(display, dimStyle.Render("  Did you mean: "+strings.Join(near, ", ")+"?"))
		}
		return append(display, dimStyle.Render("  /skills lists them all.")), ""
	}

	line := "  ▸ " + skill.Name
	if target != "" {
		line += " → " + target
	}
	display = []string{
		"",
		toolNameStyle.Render(line),
		dimStyle.Render("    " + firstSentence(skill.Description)),
		"",
	}
	return display, skill.Prompt(target)
}

// nearestSkills is deliberately crude — substring both ways, nothing fuzzier.
// It exists so a half-remembered name ("pod", "restart") lands somewhere useful
// instead of on a bare error.
func nearestSkills(all []skills.Skill, typed string) []string {
	typed = strings.ToLower(typed)
	var near []string
	for _, s := range all {
		name := strings.ToLower(s.Name)
		if strings.Contains(name, typed) || strings.Contains(typed, name) {
			near = append(near, s.Name)
		}
	}
	if len(near) > 3 {
		near = near[:3]
	}
	return near
}

// firstSentence trims a skill description down to something that fits a list.
// The SKILL.md descriptions carry keyword tails for MCP host matching
// ("Keywords: pod, container, k8s…") that are noise to a human reading a menu.
func firstSentence(description string) string {
	if idx := strings.Index(description, ". "); idx > 0 {
		description = description[:idx+1]
	}
	if idx := strings.Index(description, "Keywords:"); idx > 0 {
		description = strings.TrimSpace(description[:idx])
	}
	return strings.TrimSpace(description)
}

func sourceMarker(source string) string {
	if source == skills.SourceLocal {
		return dimStyle.Render("  (yours)")
	}
	return ""
}

// skillNameColumn fits the longest bundled name (investigate-code-change, 23)
// with a space, so descriptions line up rather than stepping.
const skillNameColumn = 24

// clip shortens to width, marking that it did. Below a floor there is no
// useful truncation left, so the description is dropped entirely rather than
// rendered as three characters and an ellipsis.
func clip(s string, width int) string {
	if width < 12 {
		return ""
	}
	if len(s) <= width {
		return s
	}
	return strings.TrimSpace(s[:width-1]) + "…"
}

func padRight(s string, width int) string {
	if len(s) >= width {
		return s
	}
	return s + strings.Repeat(" ", width-len(s))
}
