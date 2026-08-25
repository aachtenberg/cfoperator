package tui

import (
	"context"
	"fmt"
	"strings"
	"sync"
	"time"
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
	cancelTurn context.CancelFunc
	// stopping records that a cancel has already gone out for this turn, so a
	// second press is answered instead of swallowed.
	stopping bool
	// turnWG tracks the in-flight turn so the session can wait for it to
	// unwind before its transcript is read.
	turnWG sync.WaitGroup
	// lastTurnCtx is the context handed to the most recent turn, kept so tests
	// can assert what it is scoped to.
	lastTurnCtx context.Context
	// sessionCtx bounds every turn in this session. Cancelled when the TUI
	// quits, so Ctrl+D does not leave a turn running against a session whose
	// token is about to be revoked.
	sessionCtx     context.Context
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
			if m.busy {
				if m.stopping {
					// Nothing left to cancel and the turn is still winding
					// down — a tool call finishing, a request unwinding. Say so
					// rather than absorbing the press, which is the "I pressed
					// stop and nothing happened" this key exists to end.
					m.outputLines = append(m.outputLines,
						dimStyle.Render("  still stopping — ctrl+d quits"))
					m.refreshViewport()
					return m, nil
				}
				if m.cancelTurn != nil {
					m.cancelTurn()
					m.stopping = true
					m.outputLines = append(m.outputLines, dimStyle.Render("  stopping..."))
					m.refreshViewport()
				}
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
			// Stats are left alone on purpose: a cancelled turn measured
			// nothing, and zeroing contextUsed would blank the status bar's
			// context gauge and the last real reading with it.
			m.outputLines = append(m.outputLines, warningStyle.Render("  stopped."))
			m.refreshViewport()
			return m, nil
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
			// Last-4 used to be safe when this slice was user/assistant text.
			// Tool rounds live here now; cutting mid-round 400s every later
			// turn. Snap back to a user text turn so the kept suffix is a
			// complete assistant/tool pairing.
			m.messages = trimToUserBoundary(m.messages, 4)
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
	// from the command table — names, then the argument once the name is in.
	if !isCycling && text != m.lastInput {
		m.lastInput = text
		m.completions = m.completionsFor(text)
		m.completionIdx = 0
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

	// A command is handled here; everything else is a question for the model.
	// The table in commands.go owns what each one does.
	if cmd, args, ok := lookupCommand(text); ok {
		return m, cmd.Run(m, args)
	}

	// Show user message
	m.outputLines = append(m.outputLines,
		"",
		userPromptStyle.Render("> ")+text,
		"",
	)
	m.refreshViewport()

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
	m.stopping = false
	ctx, cancel := context.WithCancel(m.sessionContext())
	m.cancelTurn = cancel
	m.lastTurnCtx = ctx
	m.turnWG.Add(1)
	return m.runConversationCmd(ctx, prompt)
}

// trimToUserBoundary keeps a suffix of at least keep messages, then walks
// back to the nearest user text turn so the cut never lands inside an
// assistant tool_use / role=tool group.
func trimToUserBoundary(msgs []client.Message, keep int) []client.Message {
	if keep < 1 || len(msgs) <= keep {
		return msgs
	}
	cut := len(msgs) - keep
	for cut > 0 && msgs[cut].Role != "user" {
		cut--
	}
	return msgs[cut:]
}

func (m *model) runConversationCmd(ctx context.Context, userInput string) tea.Cmd {
	return func() tea.Msg {
		defer m.turnWG.Done()
		m.messages = append(m.messages, client.Message{Role: "user", Content: userInput})

		out := &tuiOutput{program: m.program, renderer: m.renderer}
		result, msgs := conversation.Run(ctx, m.llm, m.toolReg, out, m.messages, m.systemPrompt, m.cfg.MaxToolIterations)
		// Run returns the session transcript (no synthesized system message),
		// including tool_use/tool_result turns. Keep that rather than the user
		// text plus a final assistant line: dropping the tool round is how a
		// retry after a Claude 400 sent two user messages in a row and then a
		// tool_use with no matching tool_result (messages.2 in the error).
		m.messages = msgs

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
func Run(ctx context.Context, cfg *config.Config, llm *client.LLMClient, toolReg *tools.Registry, systemPrompt string, contextCount int, providers map[string]config.ProviderConfig, activeProvider string, attachment *Attachment, cfoperatorLine string) (RunResult, error) {
	m := New(cfg, llm, toolReg, systemPrompt, contextCount, providers, activeProvider, attachment, cfoperatorLine)

	// Every turn hangs off this, so leaving by any door — Ctrl+D, /exit, a
	// signal — stops work that is still running. Without it a quit during a
	// turn leaves an LLM call and a shell command alive while `attach` goes on
	// to write back the session and revoke the token they are using.
	sessionCtx, endSession := context.WithCancel(ctx)
	defer endSession()
	m.sessionCtx = sessionCtx

	p := tea.NewProgram(m, tea.WithAltScreen())
	m.program = p

	finalModel, err := p.Run()

	// Stop the turn and let it unwind before anyone reads its transcript.
	// p.Run returning does not mean the conversation goroutine has: it appends
	// to m.messages, which is exactly what the caller is about to read.
	endSession()
	m.awaitTurn(turnDrainTimeout)

	if err != nil {
		return RunResult{}, err
	}
	if fm, ok := finalModel.(*model); ok {
		return RunResult{Provider: fm.activeProvider, Model: fm.llm.Model,
			Messages: fm.messages}, nil
	}
	return RunResult{Provider: activeProvider, Model: llm.Model}, nil
}

// turnDrainTimeout bounds the wait for a cancelled turn on the way out. A
// wedged tool should delay an exit, not prevent one.
const turnDrainTimeout = 3 * time.Second

// awaitTurn waits for an in-flight turn to finish, up to d.
func (m *model) awaitTurn(d time.Duration) {
	done := make(chan struct{})
	go func() {
		m.turnWG.Wait()
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(d):
	}
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
	m.stopping = false
	if m.cancelTurn != nil {
		m.cancelTurn()
		m.cancelTurn = nil
	}
}

// sessionContext is the parent every turn hangs off. Tests build models
// directly, so a zero value has to mean "not scoped" rather than panic.
func (m *model) sessionContext() context.Context {
	if m.sessionCtx == nil {
		return context.Background()
	}
	return m.sessionCtx
}

// refreshViewport pushes outputLines into the viewport.
//
// Every branch that appends output has to do this or the line is stored and
// never drawn — which is how the "stopped." acknowledgement shipped invisible.
func (m *model) refreshViewport() {
	if m.ready {
		m.viewport.SetContent(strings.Join(m.outputLines, "\n"))
		m.viewport.GotoBottom()
	}
}
