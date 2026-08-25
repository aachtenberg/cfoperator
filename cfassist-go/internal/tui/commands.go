// The command table (CFOP-69).
//
// One place that says what each slash command is called, what it takes, what
// it does and how to complete its argument. /help, tab completion and the
// dispatch in handleSubmit all read it, and the command menu that follows
// (PR B1) will too.
//
// Before this a command was a case in a switch plus a name in a list, and the
// two drifted: /help listed names with no verbs because there was nowhere to
// put one, and a command added to the switch but not the list was invisible to
// tab completion. TestEveryCommandIsDescribed is the guard: a command cannot be
// added here without a summary, so the regression this issue is about cannot
// recur by omission.

package tui

import (
	"fmt"
	"sort"
	"strings"

	tea "github.com/charmbracelet/bubbletea"

	"github.com/aachtenberg/cfoperator/cfassist-go/internal/client"
	"github.com/aachtenberg/cfoperator/cfassist-go/internal/skills"
)

// command is one slash command.
type command struct {
	Name    string // "/skill"
	Args    string // "<name> [target]"; empty when it takes none
	Summary string // one line — what it does, in the operator's words
	// Aliases are other spellings. Slash-prefixed ones ("/quit") match on the
	// first word like a name; bare ones ("quit") only match a whole line, so
	// "help me with this pod" stays a question rather than becoming /help.
	Aliases []string
	// Complete returns argument completions for what has been typed after the
	// name so far. Nil when the command takes no argument.
	Complete func(m *model, prefix string) []string
	// Describe says what one completed argument is, for the menu's right
	// column. Optional — model names have nothing to say about themselves.
	Describe func(m *model, arg string) string
	// Run handles the command. args is whatever followed the name, trimmed.
	Run func(m *model, args string) tea.Cmd
}

// Usage is how the command is written out: name plus its argument shape.
func (c command) Usage() string {
	if c.Args == "" {
		return c.Name
	}
	return c.Name + " " + c.Args
}

// commands is the table, in the order /help shows it: the verbs an operator
// reaches for mid-incident first, housekeeping last.
//
// Populated in init rather than declared: /help's handler renders the table,
// so a literal here would refer to itself and Go would reject the cycle.
var commands []command

func init() {
	commands = []command{
		{
			Name:     "/skill",
			Args:     "<name> [target]",
			Summary:  "Load a playbook into this session, aimed at a pod, host or container",
			Complete: completeSkill,
			Describe: describeSkill,
			Run:      runSkill,
		},
		{
			Name:    "/skills",
			Summary: "The playbooks this session knows, and what each one is for",
			Run:     runSkills,
		},
		{
			Name:    "/tools",
			Summary: "What the model can call right now, and what each tool does",
			Run:     runTools,
		},
		{
			Name:     "/model",
			Args:     "<name>",
			Summary:  "Switch model on the current provider",
			Complete: completeModel,
			Run:      runModel,
		},
		{
			Name:    "/models",
			Summary: "Models the current provider offers",
			Run:     runModels,
		},
		{
			Name:     "/use",
			Args:     "<provider>",
			Summary:  "Switch provider — clears the conversation",
			Complete: completeProvider,
			Describe: describeProvider,
			Run:      runUse,
		},
		{
			Name:    "/providers",
			Summary: "Configured providers, and which one is active",
			Run:     runProviders,
		},
		{
			Name:    "/clear",
			Summary: "Wipe the screen and the conversation; an attachment stays",
			Aliases: []string{"clear"},
			Run:     runClear,
		},
		{
			Name:    "/help",
			Summary: "Commands, tools, skills and keys on one page",
			Aliases: []string{"help"},
			Run:     runHelp,
		},
		{
			Name:    "/exit",
			Summary: "Quit — also ctrl+d, also /quit",
			Aliases: []string{"/quit", "exit", "quit"},
			Run:     func(*model, string) tea.Cmd { return tea.Quit },
		},
	}
}

// lookupCommand resolves a submitted line to a command and its argument.
//
// Names and slash aliases match on the first word, so "/help me" is /help.
// Bare aliases match only the whole line — see command.Aliases.
func lookupCommand(text string) (command, string, bool) {
	word, rest, _ := strings.Cut(strings.TrimSpace(text), " ")
	lower := strings.ToLower(word)
	rest = strings.TrimSpace(rest)

	for _, c := range commands {
		if lower == c.Name {
			return c, rest, true
		}
		for _, a := range c.Aliases {
			if lower != a {
				continue
			}
			if strings.HasPrefix(a, "/") || rest == "" {
				return c, rest, true
			}
		}
	}
	return command{}, "", false
}

// findCommand is lookup by exact name or slash alias, for completion.
func findCommand(name string) (command, bool) {
	for _, c := range commands {
		if name == c.Name {
			return c, true
		}
		for _, a := range c.Aliases {
			if name == a && strings.HasPrefix(a, "/") {
				return c, true
			}
		}
	}
	return command{}, false
}

// completionsFor is what the table offers for what is typed, as the text
// accepting each would leave on the line: command names for a bare prefix,
// and once the name and a space are in, that command's arguments. It is the
// menu's rows without their descriptions (menu.go), kept as a plain list so
// the table can be tested without a terminal.
func (m *model) completionsFor(text string) []string {
	var out []string
	for _, row := range m.menuRows(text) {
		if row.Insert != "" {
			out = append(out, strings.TrimSpace(row.Insert))
		}
	}
	return out
}

// --- completers ---------------------------------------------------------

func completeSkill(m *model, prefix string) []string {
	// Local, embedded and instant — unlike /model, which has to ask the
	// provider — so there is nothing to cache.
	prefix = strings.ToLower(prefix)
	var out []string
	for _, name := range skills.Names(m.skills) {
		if strings.HasPrefix(strings.ToLower(name), prefix) {
			out = append(out, name)
		}
	}
	return out
}

func describeSkill(m *model, name string) string {
	s, ok := skills.Find(m.skills, name)
	if !ok {
		return ""
	}
	detail := firstSentence(s.Description)
	if s.Source == skills.SourceLocal {
		detail += "  (yours)"
	}
	return detail
}

func describeProvider(m *model, name string) string {
	p, ok := m.providers[name]
	if !ok {
		return ""
	}
	return fmt.Sprintf("%s, %s", p.Provider, p.Model)
}

func completeProvider(m *model, prefix string) []string {
	prefix = strings.ToLower(prefix)
	var out []string
	for name := range m.providers {
		if strings.HasPrefix(strings.ToLower(name), prefix) {
			out = append(out, name)
		}
	}
	// A map walk is random; cycling through it in a different order every
	// time is not completion, it is a lottery.
	sort.Strings(out)
	return out
}

// completeModel asks the provider once and remembers the answer per provider,
// because the list is a network round trip and tab is pressed repeatedly.
func completeModel(m *model, prefix string) []string {
	prefix = strings.ToLower(prefix)

	models, ok := m.modelCache[m.activeProvider]
	if !ok && m.llm != nil {
		if fetched, err := m.llm.ListModels(); err == nil {
			sort.Strings(fetched)
			models = fetched
			m.modelCache[m.activeProvider] = fetched
		}
	}

	var out []string
	for _, name := range models {
		if strings.HasPrefix(strings.ToLower(name), prefix) {
			out = append(out, name)
		}
	}
	if len(out) == 0 && len(models) == 0 && m.llm != nil && m.llm.Model != "" {
		// The provider could not be asked: offer the model that is already
		// running, so the operator at least sees its exact spelling.
		out = append(out, m.llm.Model)
	}
	return out
}

// --- handlers -----------------------------------------------------------
//
// Each appends what it has to say and refreshes the viewport. A handler that
// forgets the refresh stores its output and never draws it — that is how the
// "stopped." acknowledgement once shipped invisible.

// runSkill loads a playbook into the session. The prompt goes to the model,
// not to the scrollback: the operator sees a line saying which playbook is
// running against what, and the session then behaves as if they had typed
// the whole thing. With no name it shows the list instead.
func runSkill(m *model, args string) tea.Cmd {
	display, prompt := m.skillCommand(args)
	m.outputLines = append(m.outputLines, display...)
	m.refreshViewport()
	if prompt == "" {
		return nil
	}
	return m.startTurn(prompt)
}

func runSkills(m *model, _ string) tea.Cmd {
	m.outputLines = append(m.outputLines, m.skillsList()...)
	m.refreshViewport()
	return nil
}

func runTools(m *model, _ string) tea.Cmd {
	m.outputLines = append(m.outputLines, "", bannerStyle.Render("Available Tools:"))
	m.outputLines = append(m.outputLines, m.toolRows(m.listWidth())...)
	m.outputLines = append(m.outputLines, "")
	m.refreshViewport()
	return nil
}

func runModel(m *model, args string) tea.Cmd {
	if args == "" {
		m.outputLines = append(m.outputLines,
			dimStyle.Render("  Current model: "+m.llm.Model),
			dimStyle.Render("  Usage: /model <name>"),
		)
	} else {
		oldModel := m.llm.Model
		m.llm.Model = args
		m.outputLines = append(m.outputLines,
			"",
			dimStyle.Render(fmt.Sprintf("  Model switched: %s → %s", oldModel, args)),
		)
	}
	m.refreshViewport()
	return nil
}

func runModels(m *model, _ string) tea.Cmd {
	models, err := m.llm.ListModels()
	if err != nil {
		m.outputLines = append(m.outputLines,
			errorStyle.Render(fmt.Sprintf("Failed to fetch models: %v", err)),
		)
	} else {
		m.outputLines = append(m.outputLines, "", bannerStyle.Render("Available Models:"))
		for _, name := range models {
			marker := "  "
			if name == m.llm.Model {
				marker = dimStyle.Render("* ")
			}
			m.outputLines = append(m.outputLines, marker+toolNameStyle.Render(name))
		}
		m.outputLines = append(m.outputLines,
			"",
			dimStyle.Render("  Switch with: /model <name>"),
		)
	}
	m.refreshViewport()
	return nil
}

func runUse(m *model, name string) tea.Cmd {
	switch {
	case name == "":
		m.outputLines = append(m.outputLines,
			dimStyle.Render("  Usage: /use <provider-name>"),
		)
	case len(m.providers) == 0:
		m.outputLines = append(m.outputLines,
			errorStyle.Render("  No providers configured. Add a providers: block to config.yaml."),
		)
	default:
		if _, ok := m.providers[name]; !ok {
			m.outputLines = append(m.outputLines,
				errorStyle.Render(fmt.Sprintf("  Unknown provider: %s", name)),
				dimStyle.Render(fmt.Sprintf("  Available: %s", strings.Join(completeProvider(m, ""), ", "))),
			)
			m.refreshViewport()
			return nil
		}
		resolved := m.cfg.ResolveProvider(name)
		m.llm = client.New(
			resolved.Provider,
			resolved.URL,
			resolved.Model,
			resolved.Temperature,
			resolved.APIKey,
		)
		m.llm.Name = name
		oldProvider := m.activeProvider
		m.activeProvider = name
		m.cfg.LLM.ContextWindow = resolved.ContextWindow
		m.messages = nil
		m.contextUsed = 0
		m.outputLines = append(m.outputLines, "",
			dimStyle.Render(fmt.Sprintf("  Provider switched: %s → %s (%s)", oldProvider, name, resolved.Model)),
			dimStyle.Render("  Conversation cleared."),
		)
	}
	m.refreshViewport()
	return nil
}

func runProviders(m *model, _ string) tea.Cmd {
	if len(m.providers) == 0 {
		m.outputLines = append(m.outputLines, "",
			dimStyle.Render("  No providers configured. Using single llm: block."),
			dimStyle.Render(fmt.Sprintf("  Provider: %s  Model: %s", m.llm.Provider, m.llm.Model)),
		)
	} else {
		m.outputLines = append(m.outputLines, "", bannerStyle.Render("Configured Providers:"))
		for _, name := range completeProvider(m, "") {
			p := m.providers[name]
			marker := "  "
			if name == m.activeProvider {
				marker = dimStyle.Render("* ")
			}
			m.outputLines = append(m.outputLines,
				fmt.Sprintf("  %s%s  %s",
					marker,
					toolNameStyle.Render(name),
					dimStyle.Render(fmt.Sprintf("(%s, %s)", p.Provider, p.Model)),
				),
			)
		}
		m.outputLines = append(m.outputLines,
			"",
			dimStyle.Render("  Switch with: /use <name>"),
		)
	}
	m.refreshViewport()
	return nil
}

// runClear wipes the screen and the conversation. The briefing does not come
// back — it is still in the system prompt, and re-printing a few hundred
// lines is not what anyone asked for. One line says the session is still
// attached; the box title is the durable record.
func runClear(m *model, _ string) tea.Cmd {
	m.messages = nil
	m.outputLines = nil
	m.appendWelcome(0)
	if m.attachment != nil {
		m.outputLines = append(m.outputLines,
			dimStyle.Render(fmt.Sprintf("  Still attached to investigation #%d.",
				m.attachment.ID)),
			"",
		)
	}
	m.refreshViewport()
	return nil
}

func runHelp(m *model, _ string) tea.Cmd {
	m.outputLines = append(m.outputLines, m.helpLines()...)
	m.refreshViewport()
	return nil
}

// --- /help ---------------------------------------------------------------

// helpNameColumn fits the longest usage ("/skill <name> [target]", 22) with
// room to spare, so descriptions line up rather than stepping. Skill rows use
// their own column; the two happen to agree.
const helpNameColumn = 24

// keys is what the keyboard does, for /help. Only what actually works today
// is listed — a hint for a key that does nothing is worse than no hint.
var keys = [][2]string{
	{"tab", "accept what the menu offers; ↑↓ choose, esc closes it"},
	{"enter", "send"},
	{"ctrl+c", "stop the running turn; when idle, clear the line"},
	{"ctrl+d", "quit"},
	{"pgup / pgdn", "scroll the transcript"},
}

// helpLines is the one page that answers "what can you do": commands, then
// the tools the model can call, then the playbooks, then the keys.
func (m *model) helpLines() []string {
	width := m.listWidth()
	room := width - helpNameColumn - 6

	lines := []string{"", bannerStyle.Render("Commands:")}
	for _, c := range commands {
		lines = append(lines, "  "+toolNameStyle.Render(padRight(c.Usage(), helpNameColumn))+
			"  "+dimStyle.Render(clip(c.Summary, room)))
	}

	lines = append(lines, "",
		bannerStyle.Render("Tools:")+dimStyle.Render("  what the model can call this session"))
	lines = append(lines, m.toolRows(width)...)

	lines = append(lines, "",
		bannerStyle.Render("Skills:")+dimStyle.Render(fmt.Sprintf("  %s", m.skillsSummary())))
	lines = append(lines, m.skillRows(width)...)

	lines = append(lines, "", bannerStyle.Render("Keys:"))
	for _, k := range keys {
		lines = append(lines, "  "+toolNameStyle.Render(padRight(k[0], helpNameColumn))+
			"  "+dimStyle.Render(clip(k[1], room)))
	}
	return append(lines, "")
}

// toolRows is one line per tool the model can call, fitted to width. Tool
// descriptions are written for the model and run to a paragraph; a human
// reading a list wants the first sentence.
func (m *model) toolRows(width int) []string {
	if m.toolReg == nil {
		return []string{dimStyle.Render("  (no tools registered)")}
	}
	room := width - helpNameColumn - 6
	var rows []string
	for _, schema := range m.toolReg.GetSchemas() {
		rows = append(rows, "  "+toolNameStyle.Render(padRight(schema.Function.Name, helpNameColumn))+
			"  "+dimStyle.Render(clip(firstSentence(schema.Function.Description), room)))
	}
	return rows
}

// listWidth is the width lists are fitted to. Before the first size message
// there is no terminal to ask, and 100 columns is a reasonable guess.
func (m *model) listWidth() int {
	if m.width > 0 {
		return m.width
	}
	return 100
}
