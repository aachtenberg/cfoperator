package tui

import (
	"context"
	"fmt"
	"sort"
	"strings"
	"unicode"

	"github.com/charmbracelet/bubbles/textarea"
	"github.com/charmbracelet/bubbles/viewport"
	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/glamour"
	"github.com/charmbracelet/glamour/ansi"
	"github.com/charmbracelet/glamour/styles"
	"github.com/charmbracelet/lipgloss"

	"github.com/aachtenberg/cfoperator/cfassist-go/internal/client"
	"github.com/aachtenberg/cfoperator/cfassist-go/internal/config"
	"github.com/aachtenberg/cfoperator/cfassist-go/internal/conversation"
	"github.com/aachtenberg/cfoperator/cfassist-go/internal/memory"
	"github.com/aachtenberg/cfoperator/cfassist-go/internal/skills"
	"github.com/aachtenberg/cfoperator/cfassist-go/internal/tools"
)

const (
	statusBarHeight = 1
	separatorHeight = 1
	inputAreaHeight = 3
	fixedHeight     = statusBarHeight + separatorHeight + inputAreaHeight
)

// Attachment is the CFOperator investigation a session is attached to.
//
// Nil for a plain `cfassist` session, and every use of it is nil-guarded: the
// plain session must render exactly as it did before attach existed, which is
// what TestPlainSessionStatusBarCarriesNothingExtra holds us to.
//
// It exists because the TUI runs on the alternate screen buffer. `attach`
// prints the briefing before starting the program, and the alt screen makes
// that print invisible for the whole session — so an operator who pasted a
// Slack line had no indication of what they were attached to (CFOP-63). The
// briefing goes into the scrollback and the id into the status bar instead.
type Attachment struct {
	ID       int    // investigation id, e.g. 2242
	Title    string // short human label; the investigation's trigger
	Briefing string // the full rendered briefing, seeded into the scrollback
}

type model struct {
	viewport    viewport.Model
	textarea    textarea.Model
	messages    []client.Message
	outputLines []string
	busy        bool
	// cancelTurn stops the turn currently in flight. Set when one starts,
	// cleared when it ends; nil means there is nothing to stop.
	cancelTurn     context.CancelFunc
	ready          bool
	cfg            *config.Config
	llm            *client.LLMClient
	toolReg        *tools.Registry
	systemPrompt   string
	attachment     *Attachment
	width          int
	height         int
	program        *tea.Program
	renderer       *glamour.TermRenderer
	mdStyle        ansi.StyleConfig
	lastStats      string
	contextUsed    int // last prompt token count (current context usage)
	providers      map[string]config.ProviderConfig
	activeProvider string
	// cfoperatorLine is the presence probe's banner summary, kept on the model
	// so /clear redraws it — the agent is still there after a screen wipe.
	cfoperatorLine string
	// skills are the playbooks this session can load: the nine embedded in the
	// binary, plus anything in the operator's own skills directory.
	skills []skills.Skill
	// Tab completion state
	completions   []string
	completionIdx int
	lastInput     string
	// modelCache holds the list of available models per provider name,
	// populated lazily on first /model tab completion.
	modelCache map[string][]string
}

// slashCommands is the list of available commands for tab completion.
var slashCommands = []string{
	"/clear",
	"/exit",
	"/help",
	"/model",
	"/models",
	"/providers",
	"/quit",
	"/skill",
	"/skills",
	"/tools",
	"/use",
}

// New creates a new TUI model. attachment is nil for a plain session, and
// cfoperatorLine is the presence probe's one-line summary (empty when no agent
// was found) — see cfoperator.Presence.BannerLine.
func New(cfg *config.Config, llm *client.LLMClient, toolReg *tools.Registry, systemPrompt string, contextCount int, providers map[string]config.ProviderConfig, activeProvider string, attachment *Attachment, cfoperatorLine string) *model {
	// Text area for input
	ta := textarea.New()
	ta.Placeholder = "Ask a question..."
	ta.Focus()
	ta.CharLimit = 4096
	ta.ShowLineNumbers = false
	ta.SetHeight(inputAreaHeight)
	ta.FocusedStyle.Base = lipgloss.NewStyle()
	ta.FocusedStyle.CursorLine = lipgloss.NewStyle()
	ta.FocusedStyle.EndOfBuffer = lipgloss.NewStyle()
	ta.FocusedStyle.Prompt = lipgloss.NewStyle().Foreground(lipgloss.Color("#00aa00")).Bold(true)
	ta.BlurredStyle.Base = lipgloss.NewStyle()
	ta.BlurredStyle.CursorLine = lipgloss.NewStyle()
	ta.BlurredStyle.EndOfBuffer = lipgloss.NewStyle()
	ta.BlurredStyle.Prompt = lipgloss.NewStyle().Foreground(lipgloss.Color("#006600"))
	ta.EndOfBufferCharacter = ' '
	ta.SetPromptFunc(3, func(lineIdx int) string {
		if lineIdx == 0 {
			return " > "
		}
		return "   "
	})

	// Glamour renderer for markdown — dark style, no red backgrounds
	mdStyle := styles.DarkStyleConfig
	noColor := ""
	orangeColor := "214" // ANSI 214 = orange
	mdStyle.Code.BackgroundColor = &noColor
	mdStyle.Code.Color = &orangeColor
	mdStyle.CodeBlock.BackgroundColor = &noColor
	mdStyle.Table.BackgroundColor = &noColor
	mdStyle.Document.BackgroundColor = &noColor
	r, _ := glamour.NewTermRenderer(
		glamour.WithStyles(mdStyle),
		glamour.WithWordWrap(120),
	)

	m := &model{
		textarea:       ta,
		messages:       []client.Message{},
		outputLines:    []string{},
		cfg:            cfg,
		llm:            llm,
		toolReg:        toolReg,
		systemPrompt:   systemPrompt,
		attachment:     attachment,
		renderer:       r,
		mdStyle:        mdStyle,
		providers:      providers,
		activeProvider: activeProvider,
		modelCache:     make(map[string][]string),
		cfoperatorLine: cfoperatorLine,
		skills:         skills.Load(cfg.Skills.Directory),
	}

	// Build welcome banner
	m.appendWelcome(contextCount)
	// …then the briefing, if this session is attached to one.
	m.appendBriefing()

	return m
}

// appendBriefing seeds the scrollback with the briefing the model was given.
//
// The whole point: on the alternate screen the operator cannot see anything
// attach printed before the program started, so the copy that matters is this
// one. It goes in as plain text — the briefing already carries its own rules
// and indentation, and running it through the markdown renderer would reflow
// the alignment it uses to stay readable.
func (m *model) appendBriefing() {
	if m.attachment == nil || strings.TrimSpace(m.attachment.Briefing) == "" {
		return
	}
	m.outputLines = append(m.outputLines,
		strings.Split(m.attachment.Briefing, "\n")...,
	)
	m.outputLines = append(m.outputLines,
		"",
		dimStyle.Render("  Attached — this briefing is also in the model's context. "+
			"The status bar keeps the investigation id in view."),
		"",
	)
}

func (m *model) appendWelcome(contextCount int) {
	width := 120
	if m.width > 0 {
		width = m.width
	}

	logo := []string{
		`  _________ ___________ _____     _________  _________.___   ____________________ `,
		`  \_   ___ \\_   _____//  _  \   /   _____/ /   _____/|   | /   _____/\__    ___/ `,
		`  /    \  \/ |    __) /  /_\  \  \_____  \  \_____  \ |   | \_____  \   |    |    `,
		`  \     \____|     \ /    |    \ /        \ /        \|   | /        \  |    |    `,
		`   \______  /\___  / \____|__  //_______  //_______  /|___|/_______  /  |____|    `,
		`          \/     \/          \/         \/         \/              \/              `,
	}

	for _, line := range logo {
		m.outputLines = append(m.outputLines, bannerStyle.Render(line))
	}

	sep := strings.Repeat("─", width)
	info := fmt.Sprintf("  %s", bannerDimStyle.Render("v"+config.Version))
	if contextCount > 0 {
		info += dimStyle.Render(fmt.Sprintf("  (%d context files loaded)", contextCount))
	}
	info += dimStyle.Render("  Type /help for commands")
	m.outputLines = append(m.outputLines, info)
	// What the presence probe found, in the operator's own view. The model was
	// told this; showing it here is what keeps the session's answers about
	// CFOperator auditable instead of magic.
	if m.cfoperatorLine != "" {
		m.outputLines = append(m.outputLines, dimStyle.Render("  "+m.cfoperatorLine))
	}
	m.outputLines = append(m.outputLines,
		separatorStyle.Render(sep),
		"",
	)
}

func (m *model) Init() tea.Cmd {
	return textarea.Blink
}

func (m *model) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	var cmds []tea.Cmd

	switch msg := msg.(type) {
	case tea.KeyMsg:
		switch msg.Type {
		case tea.KeyCtrlD:
			return m, tea.Quit
		case tea.KeyCtrlC:
			// Busy or not, Ctrl+C means "stop what I started". While a turn is
			// running that is the turn; idle, it is the half-typed line.
			if m.busy && m.cancelTurn != nil {
				m.cancelTurn()
				return m, nil
			}
			m.textarea.Reset()
			m.completions = nil
			return m, nil
		case tea.KeyTab:
			if m.busy {
				return m, nil
			}
			return m.handleTabCompletion()
		case tea.KeyEnter:
			if m.busy {
				return m, nil
			}
			m.completions = nil
			return m.handleSubmit()
		case tea.KeyEsc:
			m.completions = nil
			return m, nil
		}

	case tea.WindowSizeMsg:
		m.width = msg.Width
		m.height = msg.Height

		// Recreate markdown renderer at new width
		wrapWidth := m.width - 4
		if wrapWidth < 40 {
			wrapWidth = 40
		}
		if r, err := glamour.NewTermRenderer(
			glamour.WithStyles(m.mdStyle),
			glamour.WithWordWrap(wrapWidth),
		); err == nil {
			m.renderer = r
		}

		vpHeight := m.height - fixedHeight
		if !m.ready {
			m.viewport = viewport.New(m.width, vpHeight)
			m.viewport.SetContent(strings.Join(m.outputLines, "\n"))
			m.ready = true
		} else {
			m.viewport.Width = m.width
			m.viewport.Height = vpHeight
		}
		m.textarea.SetWidth(m.width)

	case appendOutputMsg:
		m.outputLines = append(m.outputLines, msg.text)
		if m.ready {
			m.viewport.SetContent(strings.Join(m.outputLines, "\n"))
			m.viewport.GotoBottom()
		}
		return m, nil

	case llmDoneMsg:
		m.busy = false
		m.releaseTurn()
		r := msg.result
		if r.Cancelled {
			m.outputLines = append(m.outputLines, warningStyle.Render("  stopped."))
		}
		latency := r.Latency.Seconds()
		m.lastStats = fmt.Sprintf("%d↑ %d↓ %.1fs", r.InputTokens, r.OutputTokens, latency)
		if r.ToolCalls > 0 {
			m.lastStats += fmt.Sprintf(" %dt", r.ToolCalls)
		}
		m.contextUsed = r.LastPromptTokens

		// Auto-save and truncate when context exceeds 80%
		ctxLimit := m.cfg.LLM.ContextWindow
		if ctxLimit > 0 && m.contextUsed > ctxLimit*80/100 && len(m.messages) > 4 {
			memory.SaveConversation(m.cfg.Memory.Directory, m.messages)
			// Keep system prompt context + last 4 messages
			m.messages = m.messages[len(m.messages)-4:]
			m.outputLines = append(m.outputLines,
				"",
				warningStyle.Render(fmt.Sprintf("  Context %d/%d tokens (>80%%) — conversation saved and trimmed.",
					m.contextUsed, ctxLimit)),
			)
			if m.ready {
				m.viewport.SetContent(strings.Join(m.outputLines, "\n"))
				m.viewport.GotoBottom()
			}
		}
		return m, nil

	case errMsg:
		m.busy = false
		m.releaseTurn()
		m.outputLines = append(m.outputLines, errorStyle.Render(msg.err.Error()))
		if m.ready {
			m.viewport.SetContent(strings.Join(m.outputLines, "\n"))
			m.viewport.GotoBottom()
		}
		return m, nil
	}

	// Update sub-components
	var cmd tea.Cmd

	// The textarea stays live during a turn. Gating it on m.busy made every
	// keystroke vanish, so a long turn looked like a frozen terminal rather
	// than a busy one (CFOP-76). Submission is still gated, on KeyEnter above.
	m.textarea, cmd = m.textarea.Update(msg)
	cmds = append(cmds, cmd)

	m.viewport, cmd = m.viewport.Update(msg)
	cmds = append(cmds, cmd)

	return m, tea.Batch(cmds...)
}

func (m *model) handleTabCompletion() (tea.Model, tea.Cmd) {
	text := m.textarea.Value()

	// Only complete if input starts with /
	if !strings.HasPrefix(text, "/") {
		m.completions = nil
		return m, nil
	}

	// Check if current text is already one of our completions (user is cycling)
	isCycling := false
	for _, c := range m.completions {
		if text == c {
			isCycling = true
			break
		}
	}

	// If input changed and we're not cycling through completions, rebuild
	if !isCycling && text != m.lastInput {
		m.lastInput = text
		m.completions = nil
		m.completionIdx = 0

		prefix := strings.ToLower(text)
		for _, cmd := range slashCommands {
			if strings.HasPrefix(cmd, prefix) {
				m.completions = append(m.completions, cmd)
			}
		}
		sort.Strings(m.completions)

		// Also add provider names for /use
		if strings.HasPrefix(prefix, "/use ") || prefix == "/use" {
			providerPrefix := ""
			if len(text) > 5 {
				providerPrefix = strings.ToLower(text[5:])
			}
			for name := range m.providers {
				if strings.HasPrefix(strings.ToLower(name), providerPrefix) {
					m.completions = append(m.completions, "/use "+name)
				}
			}
		}

		// Skill names for /skill. Local, embedded and instant — unlike /model,
		// which has to ask the provider — so there is nothing to cache.
		if strings.HasPrefix(prefix, "/skill ") || prefix == "/skill" {
			skillPrefix := ""
			if len(text) > len("/skill ") {
				skillPrefix = strings.ToLower(text[len("/skill "):])
			}
			for _, name := range skills.Names(m.skills) {
				if strings.HasPrefix(strings.ToLower(name), skillPrefix) {
					m.completions = append(m.completions, "/skill "+name)
				}
			}
		}

		// Add model names for /model — fetch from provider on first use, cached per provider
		if strings.HasPrefix(prefix, "/model ") || prefix == "/model" {
			modelPrefix := ""
			if len(text) > 7 {
				modelPrefix = strings.ToLower(text[7:])
			}

			models, ok := m.modelCache[m.activeProvider]
			if !ok {
				if fetched, err := m.llm.ListModels(); err == nil {
					sort.Strings(fetched)
					models = fetched
					m.modelCache[m.activeProvider] = fetched
				}
			}

			if len(models) > 0 {
				for _, name := range models {
					if modelPrefix == "" || strings.HasPrefix(strings.ToLower(name), modelPrefix) {
						m.completions = append(m.completions, "/model "+name)
					}
				}
			} else if m.llm.Model != "" {
				// Fallback: suggest the current model when the provider can't be queried
				m.completions = append(m.completions, "/model "+m.llm.Model)
			}
		}
	}

	if len(m.completions) == 0 {
		return m, nil
	}

	// Cycle through completions
	completion := m.completions[m.completionIdx]
	m.completionIdx = (m.completionIdx + 1) % len(m.completions)

	// Set the completion in textarea
	m.textarea.SetValue(completion)
	// Move cursor to end
	m.textarea.CursorEnd()

	return m, nil
}

func (m *model) handleSubmit() (tea.Model, tea.Cmd) {
	text := strings.TrimSpace(m.textarea.Value())
	if text == "" {
		return m, nil
	}

	m.textarea.Reset()

	// Special commands
	lower := strings.ToLower(text)
	switch lower {
	case "/exit", "/quit", "exit", "quit":
		return m, tea.Quit
	case "/clear", "clear":
		m.messages = nil
		m.outputLines = nil
		m.appendWelcome(0)
		// /clear means "clear the screen", so the briefing does not come back —
		// it is still in the system prompt, and re-printing a few hundred lines
		// is not what anyone asked for. One line says the session is still
		// attached; the status bar is the durable record.
		if m.attachment != nil {
			m.outputLines = append(m.outputLines,
				dimStyle.Render(fmt.Sprintf("  Still attached to investigation #%d.",
					m.attachment.ID)),
				"",
			)
		}
		if m.ready {
			m.viewport.SetContent(strings.Join(m.outputLines, "\n"))
			m.viewport.GotoBottom()
		}
		return m, nil
	case "/tools":
		m.outputLines = append(m.outputLines, "")
		m.outputLines = append(m.outputLines, bannerStyle.Render("Available Tools:"))
		for _, schema := range m.toolReg.GetSchemas() {
			name := schema.Function.Name
			desc := schema.Function.Description
			maxDesc := m.width - 20
			if maxDesc < 80 {
				maxDesc = 80
			}
			if len(desc) > maxDesc {
				desc = desc[:maxDesc] + "..."
			}
			m.outputLines = append(m.outputLines,
				fmt.Sprintf("  %s  %s",
					toolNameStyle.Render(name),
					dimStyle.Render(desc),
				),
			)
		}
		m.outputLines = append(m.outputLines, "")
		if m.ready {
			m.viewport.SetContent(strings.Join(m.outputLines, "\n"))
			m.viewport.GotoBottom()
		}
		return m, nil
	case "/models":
		models, err := m.llm.ListModels()
		if err != nil {
			m.outputLines = append(m.outputLines,
				errorStyle.Render(fmt.Sprintf("Failed to fetch models: %v", err)),
			)
		} else {
			m.outputLines = append(m.outputLines, "")
			m.outputLines = append(m.outputLines, bannerStyle.Render("Available Models:"))
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
		if m.ready {
			m.viewport.SetContent(strings.Join(m.outputLines, "\n"))
			m.viewport.GotoBottom()
		}
		return m, nil
	case "/providers":
		if len(m.providers) == 0 {
			m.outputLines = append(m.outputLines, "",
				dimStyle.Render("  No providers configured. Using single llm: block."),
				dimStyle.Render(fmt.Sprintf("  Provider: %s  Model: %s", m.llm.Provider, m.llm.Model)),
			)
		} else {
			m.outputLines = append(m.outputLines, "", bannerStyle.Render("Configured Providers:"))
			for name, p := range m.providers {
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
		if m.ready {
			m.viewport.SetContent(strings.Join(m.outputLines, "\n"))
			m.viewport.GotoBottom()
		}
		return m, nil
	case "/skills":
		m.outputLines = append(m.outputLines, m.skillsList()...)
		if m.ready {
			m.viewport.SetContent(strings.Join(m.outputLines, "\n"))
			m.viewport.GotoBottom()
		}
		return m, nil
	case "/skill":
		display, _ := m.skillCommand("")
		m.outputLines = append(m.outputLines, display...)
		if m.ready {
			m.viewport.SetContent(strings.Join(m.outputLines, "\n"))
			m.viewport.GotoBottom()
		}
		return m, nil
	case "/help", "help":
		m.outputLines = append(m.outputLines,
			dimStyle.Render("Commands: /clear, /exit, /help, /tools, /models, /model <name>"),
			dimStyle.Render("          /providers, /use <name>"),
			dimStyle.Render(fmt.Sprintf("          /skills, /skill <name> [target]  (%d available)",
				len(m.skills))),
			dimStyle.Render("Tab to autocomplete commands, Ctrl-D to exit, Ctrl-C to cancel."),
		)
		if m.ready {
			m.viewport.SetContent(strings.Join(m.outputLines, "\n"))
			m.viewport.GotoBottom()
		}
		return m, nil
	}

	// /use <name> — switch provider
	if strings.HasPrefix(lower, "/use ") {
		name := strings.TrimSpace(text[5:])
		if name == "" {
			m.outputLines = append(m.outputLines,
				dimStyle.Render("  Usage: /use <provider-name>"),
			)
		} else if len(m.providers) == 0 {
			m.outputLines = append(m.outputLines,
				errorStyle.Render("  No providers configured. Add a providers: block to config.yaml."),
			)
		} else if _, ok := m.providers[name]; !ok {
			m.outputLines = append(m.outputLines,
				errorStyle.Render(fmt.Sprintf("  Unknown provider: %s", name)),
			)
			var names []string
			for n := range m.providers {
				names = append(names, n)
			}
			m.outputLines = append(m.outputLines,
				dimStyle.Render(fmt.Sprintf("  Available: %s", strings.Join(names, ", "))),
			)
		} else {
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
		if m.ready {
			m.viewport.SetContent(strings.Join(m.outputLines, "\n"))
			m.viewport.GotoBottom()
		}
		return m, nil
	}

	// /skill <name> [target] — load a playbook into this session.
	//
	// The prompt goes to the model, not to the scrollback: the operator sees a
	// line saying which playbook is running against what, and the session then
	// behaves as if they had typed the whole thing.
	if strings.HasPrefix(lower, "/skill ") {
		display, prompt := m.skillCommand(text[len("/skill "):])
		m.outputLines = append(m.outputLines, display...)
		if m.ready {
			m.viewport.SetContent(strings.Join(m.outputLines, "\n"))
			m.viewport.GotoBottom()
		}
		if prompt == "" {
			return m, nil
		}
		return m, m.startTurn(prompt)
	}

	// /model <name> — switch model
	if strings.HasPrefix(lower, "/model ") {
		newModel := strings.TrimSpace(text[7:])
		if newModel == "" {
			m.outputLines = append(m.outputLines,
				dimStyle.Render("  Current model: "+m.llm.Model),
				dimStyle.Render("  Usage: /model <name>"),
			)
		} else {
			oldModel := m.llm.Model
			m.llm.Model = newModel
			m.outputLines = append(m.outputLines,
				"",
				dimStyle.Render(fmt.Sprintf("  Model switched: %s → %s", oldModel, newModel)),
			)
		}
		if m.ready {
			m.viewport.SetContent(strings.Join(m.outputLines, "\n"))
			m.viewport.GotoBottom()
		}
		return m, nil
	}

	// Show user message
	m.outputLines = append(m.outputLines,
		"",
		userPromptStyle.Render("> ")+text,
		"",
	)
	if m.ready {
		m.viewport.SetContent(strings.Join(m.outputLines, "\n"))
		m.viewport.GotoBottom()
	}

	// Run conversation in background via tea.Cmd
	return m, m.startTurn(text)
}

// startTurn marks the session busy and hands back the command that runs the
// turn, holding onto the cancel so Ctrl+C has something to pull.
//
// Both entry points — a typed question and /skill — go through here. They used
// to set m.busy and build the command separately, which is how one of them
// would eventually be left uncancellable.
func (m *model) startTurn(prompt string) tea.Cmd {
	m.busy = true
	ctx, cancel := context.WithCancel(context.Background())
	m.cancelTurn = cancel
	return m.runConversationCmd(ctx, prompt)
}

func (m *model) runConversationCmd(ctx context.Context, userInput string) tea.Cmd {
	return func() tea.Msg {
		m.messages = append(m.messages, client.Message{Role: "user", Content: userInput})

		out := &tuiOutput{program: m.program, renderer: m.renderer}
		result, _ := conversation.Run(ctx, m.llm, m.toolReg, out, m.messages, m.systemPrompt, m.cfg.MaxToolIterations)

		if result.Response != "" {
			m.messages = append(m.messages, client.Message{Role: "assistant", Content: result.Response})
		}

		if result.Error != "" {
			return errMsg{err: fmt.Errorf("%s", result.Error)}
		}
		return llmDoneMsg{result: result}
	}
}

// attachSeparator joins the id and the title in the status bar.
const attachSeparator = " · "

// attachSegment renders the status bar's "#<id> · <title>" segment within
// budget columns. Returns "" when nothing meaningful fits, and never returns
// something wider than budget.
//
// The truncation has a priority, because an operator attaching from a phone or
// a Pi console is the case this feature was reported from: the *id* is what
// correlates the session with Slack and the console, so the title is what gets
// cut, and below the width of the id alone the segment vanishes rather than
// printing half an id — "#22" for investigation 2242 is worse than nothing.
//
// The title is flattened to a single printable line here rather than at the
// call site: an investigation's trigger is frequently a multi-line alert body,
// and a newline reaching the status bar would tear the layout apart. Doing it
// here means no constructor of an Attachment can get it wrong.
func attachSegment(a *Attachment, budget int) string {
	if a == nil || budget < 1 {
		return ""
	}

	id := ""
	if a.ID > 0 {
		id = fmt.Sprintf("#%d", a.ID)
	}
	title := flattenToLine(a.Title)

	switch {
	case id == "" && title == "":
		return ""
	case id == "":
		return truncateToWidth(title, budget)
	case title == "":
		if lipgloss.Width(id) > budget {
			return ""
		}
		return id
	}

	full := id + attachSeparator + title
	if lipgloss.Width(full) <= budget {
		return full
	}
	if lipgloss.Width(id) > budget {
		return ""
	}
	if short := truncateToWidth(title, budget-lipgloss.Width(id)-lipgloss.Width(attachSeparator)); short != "" {
		return id + attachSeparator + short
	}
	return id
}

// flattenToLine collapses any run of whitespace to a single space and drops
// non-printable runes, so an alert body cannot inject a newline or an escape
// sequence into the status bar.
func flattenToLine(text string) string {
	var b strings.Builder
	for _, r := range strings.Join(strings.Fields(text), " ") {
		if unicode.IsPrint(r) {
			b.WriteRune(r)
		}
	}
	return b.String()
}

// truncateToWidth cuts text to budget columns, spending one of them on an
// ellipsis so a cut is visible rather than looking like the whole title.
func truncateToWidth(text string, budget int) string {
	if budget <= 0 {
		return ""
	}
	if lipgloss.Width(text) <= budget {
		return text
	}
	out := ""
	for _, r := range text {
		next := out + string(r)
		if lipgloss.Width(next) > budget-1 {
			break
		}
		out = next
	}
	out = strings.TrimRight(out, " ")
	if out == "" {
		return ""
	}
	return out + "…"
}

func (m *model) View() string {
	if !m.ready {
		return "Initializing..."
	}

	// Build status bar — left: provider:model + status, right: stats
	status := "ready"
	if m.busy {
		status = "working..."
	}
	modelDisplay := m.llm.Model
	if m.activeProvider != "" {
		modelDisplay = m.activeProvider + ":" + m.llm.Model
	}
	left := fmt.Sprintf(" %s | %s", modelDisplay, status)

	var rightParts []string
	if m.contextUsed > 0 && m.cfg.LLM.ContextWindow > 0 {
		ctxK := float64(m.contextUsed) / 1000
		maxK := float64(m.cfg.LLM.ContextWindow) / 1000
		rightParts = append(rightParts, fmt.Sprintf("%.1fk/%.0fk ctx", ctxK, maxK))
	}
	if m.lastStats != "" {
		rightParts = append(rightParts, m.lastStats)
	}
	right := strings.Join(rightParts, " | ")
	if right != "" {
		right += " "
	}

	// Pad middle with spaces to push right side to the edge
	// statusStyle has Padding(0,1) which adds 2 chars, so content width is width-2
	contentWidth := m.width - 2

	// The attachment sits between the two existing segments and only ever
	// spends what they leave over, minus the gutters that keep it from butting
	// up against them (one column left, two right). So it cannot push
	// provider:model or the stats off the bar, and the bar cannot outgrow
	// contentWidth and wrap onto a second line — it shortens itself instead,
	// and disappears entirely on a terminal too narrow to hold even the id.
	mid := ""
	if m.attachment != nil {
		mid = attachSegment(m.attachment,
			contentWidth-lipgloss.Width(left)-lipgloss.Width(right)-3)
	}

	gap := contentWidth - lipgloss.Width(left) - lipgloss.Width(right)
	statusText := ""
	if mid == "" {
		if gap < 1 {
			gap = 1
		}
		statusText = left + strings.Repeat(" ", gap) + right
	} else {
		gap -= lipgloss.Width(mid) + 1 // the gutter between left and mid
		if gap < 1 {
			gap = 1
		}
		statusText = left + " " + mid + strings.Repeat(" ", gap) + right
	}
	statusBar := statusStyle.Width(m.width).Render(statusText)

	// Separator
	sep := separatorStyle.Width(m.width).Render(strings.Repeat("─", m.width))

	// Input area with background
	inputContent := m.textarea.View()
	input := inputStyle.Width(m.width).Render(inputContent)

	return lipgloss.JoinVertical(
		lipgloss.Left,
		m.viewport.View(),
		sep,
		statusBar,
		input,
	)
}

// RunResult holds the final TUI state on exit.
type RunResult struct {
	Provider string
	Model    string
	// Messages is the session transcript, returned so `attach` can write back
	// what the session concluded before it is destroyed (CFOP-37). Discarded
	// by every other caller — a plain `cfassist` session has no investigation
	// to attach it to.
	Messages []client.Message
}

// Run starts the TUI application and returns the final provider/model on exit.
// attachment is nil for a plain session and set by `cfassist attach`.
func Run(cfg *config.Config, llm *client.LLMClient, toolReg *tools.Registry, systemPrompt string, contextCount int, providers map[string]config.ProviderConfig, activeProvider string, attachment *Attachment, cfoperatorLine string) (RunResult, error) {
	m := New(cfg, llm, toolReg, systemPrompt, contextCount, providers, activeProvider, attachment, cfoperatorLine)
	p := tea.NewProgram(m, tea.WithAltScreen())
	m.program = p

	finalModel, err := p.Run()
	if err != nil {
		return RunResult{}, err
	}
	if fm, ok := finalModel.(*model); ok {
		return RunResult{Provider: fm.activeProvider, Model: fm.llm.Model,
			Messages: fm.messages}, nil
	}
	return RunResult{Provider: activeProvider, Model: llm.Model}, nil
}

// tuiOutput implements conversation.Output by sending messages to the TUI.
type tuiOutput struct {
	program  *tea.Program
	renderer *glamour.TermRenderer
}

const toolOutputIndent = "    "

func formatToolCallLine(name, detail string) string {
	line := toolOutputIndent + toolNameStyle.Render("-> "+name+":")
	if detail != "" {
		line += " " + detail
	}
	return line
}

func formatToolResultLine(name, detail string, isError bool) string {
	prefix := "<- "
	style := toolSuccessStyle
	if isError {
		prefix = "!! "
		style = toolErrorStyle
	}
	line := toolOutputIndent + style.Render(prefix+name+":")
	if detail != "" {
		line += " " + detail
	}
	return line
}

func (o *tuiOutput) ShowThinking() {
	o.program.Send(appendOutputMsg{text: dimStyle.Render("  thinking...")})
}

func (o *tuiOutput) ClearThinking() {
	// In TUI, thinking just scrolls up naturally — no-op
}

func (o *tuiOutput) ShowToolCall(name string, args map[string]any) {
	var detail string
	switch name {
	case "bash":
		detail, _ = args["command"].(string)
	case "read_file":
		detail, _ = args["path"].(string)
	default:
		detail = fmt.Sprintf("%v", args)
	}
	o.program.Send(appendOutputMsg{text: formatToolCallLine(name, detail)})
}

func (o *tuiOutput) ShowToolResult(name string, result map[string]any) {
	if errMsg, ok := result["error"]; ok {
		o.program.Send(appendOutputMsg{text: formatToolResultLine(name, fmt.Sprintf("%v", errMsg), true)})
		return
	}

	var detail string
	isError := false
	switch name {
	case "bash":
		stdout, _ := result["stdout"].(string)
		exitCode := 0
		if ec, ok := result["exit_code"].(int); ok {
			exitCode = ec
		}
		lines := 0
		if stdout != "" {
			lines = len(strings.Split(stdout, "\n"))
		}
		detail = fmt.Sprintf("%d lines | exit %d", lines, exitCode)
		isError = exitCode != 0
	case "read_file":
		content, _ := result["content"].(string)
		lines := 0
		if content != "" {
			lines = len(strings.Split(content, "\n"))
		}
		detail = fmt.Sprintf("%d lines", lines)
	default:
		detail = "done"
	}
	o.program.Send(appendOutputMsg{text: formatToolResultLine(name, detail, isError)})
}

func (o *tuiOutput) ShowResponse(text string) {
	// Render markdown with glamour
	rendered := text
	if o.renderer != nil {
		if r, err := o.renderer.Render(text); err == nil {
			rendered = strings.TrimSpace(r)
		}
	}
	o.program.Send(appendOutputMsg{text: rendered})
}

func (o *tuiOutput) ShowError(message string, hint string) {
	line := errorStyle.Render(message)
	if hint != "" {
		line += "\n" + dimStyle.Render("  "+hint)
	}
	o.program.Send(appendOutputMsg{text: line})
}

func (o *tuiOutput) ShowWarning(message string) {
	o.program.Send(appendOutputMsg{text: warningStyle.Render(message)})
}

// releaseTurn drops the cancel for a turn that has finished, so a later Ctrl+C
// clears the input line rather than cancelling a context nobody is waiting on.
func (m *model) releaseTurn() {
	if m.cancelTurn != nil {
		m.cancelTurn()
		m.cancelTurn = nil
	}
}
