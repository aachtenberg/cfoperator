// The model-facing half of skills (CFOP-69).
//
// `/skill` is the operator's verb. This is the same capability for the model:
// asked "why does immich-kiosk-0 keep restarting", it can reach for the
// why-restart playbook itself rather than improvising a worse version of
// something the product already knows how to do.
//
// The available skills are named in the tool description, with a line each,
// rather than behind a separate list call. Several supported providers are
// small local models, and a two-step discovery dance (list, then load) is
// exactly the shape they fumble. ~160 tokens buys a model that does not guess.

package tools

import (
	"context"
	"fmt"
	"strings"

	"github.com/aachtenberg/cfoperator/cfassist-go/internal/client"
	"github.com/aachtenberg/cfoperator/cfassist-go/internal/skills"
)

// skillSummaryChars keeps the description compact: one scannable line per
// playbook, not the MCP-facing paragraph with its keyword tail.
const skillSummaryChars = 70

// AddSkills registers the skill tool. Called with the same set the TUI shows,
// so /skills and the model see one list rather than two.
func (r *Registry) AddSkills(all []skills.Skill) {
	if len(all) == 0 {
		return
	}

	names := skills.Names(all)
	r.tools["skill"] = tool{
		schema: client.ToolSchema{
			Type: "function",
			Function: client.ToolSchemaFunction{
				Name: "skill",
				Description: "Load one of CFOperator's investigation playbooks — a written procedure " +
					"for a class of problem, accumulated by this product. Returns the playbook; " +
					"follow it with your own tools. Reach for one when the question matches, " +
					"instead of improvising a shorter version.\n\n" +
					skillCatalogue(all),
				Parameters: map[string]any{
					"type": "object",
					"properties": map[string]any{
						"name": map[string]any{
							"type": "string",
							// An enum, so a plausible-sounding playbook that does not
							// exist cannot be invented and then reported as run.
							"enum":        names,
							"description": "Which playbook to load",
						},
						"target": map[string]any{
							"type": "string",
							"description": "What to apply it to — a pod, host, container or deployment " +
								"name. Optional, but a playbook aimed at nothing usually needs a second turn.",
						},
					},
					"required": []string{"name"},
				},
			},
		},
		execute: func(ctx context.Context, args map[string]any) map[string]any {
			return skillExecute(all, args)
		},
	}
}

func skillExecute(all []skills.Skill, args map[string]any) map[string]any {
	name, _ := args["name"].(string)
	target, _ := args["target"].(string)

	if strings.TrimSpace(name) == "" {
		return map[string]any{
			"error":     "skill needs a name",
			"available": skills.Names(all),
		}
	}

	skill, ok := skills.Find(all, name)
	if !ok {
		// The valid names come back with the error: a model that guessed wrong
		// should be able to fix it on the next turn rather than give up and
		// improvise, which is the behaviour this tool exists to replace.
		return map[string]any{
			"error":     fmt.Sprintf("no playbook called %q", name),
			"available": skills.Names(all),
		}
	}

	return map[string]any{
		"skill":    skill.Name,
		"target":   strings.TrimSpace(target),
		"playbook": skill.Prompt(target),
	}
}

// skillCatalogue renders the one-line-per-playbook block in the tool
// description. Same trimming as the TUI listing, for the same reason: the
// keyword tails are matching metadata for MCP hosts, and here they would be
// context spent on nothing.
func skillCatalogue(all []skills.Skill) string {
	var b strings.Builder
	b.WriteString("Available playbooks:\n")
	for _, s := range all {
		b.WriteString(fmt.Sprintf("- %s: %s\n", s.Name, summarize(s.Description)))
	}
	return strings.TrimRight(b.String(), "\n")
}

func summarize(description string) string {
	if idx := strings.Index(description, ". "); idx > 0 {
		description = description[:idx+1]
	}
	if idx := strings.Index(description, "Keywords:"); idx > 0 {
		description = strings.TrimSpace(description[:idx])
	}
	description = strings.TrimSpace(description)
	if len(description) > skillSummaryChars {
		description = strings.TrimSpace(description[:skillSummaryChars-1]) + "…"
	}
	return description
}
