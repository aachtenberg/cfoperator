package tui

import (
	"fmt"
	"strings"

	"github.com/charmbracelet/bubbles/textarea"
	"github.com/charmbracelet/bubbles/viewport"
	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/glamour"
	"github.com/charmbracelet/glamour/styles"
	"github.com/charmbracelet/lipgloss"

	"github.com/aachtenberg/cfoperator/cfassist-go/internal/client"
	"github.com/aachtenberg/cfoperator/cfassist-go/internal/config"
	"github.com/aachtenberg/cfoperator/cfassist-go/internal/conversation"
	"github.com/aachtenberg/cfoperator/cfassist-go/internal/memory"
	"github.com/aachtenberg/cfoperator/cfassist-go/internal/tools"
)

const (
	statusBarHeight = 1
	separatorHeight = 1
	inputAreaHeight = 3
	fixedHeight     = statusBarHeight + separatorHeight + inputAreaHeight
)

type model struct {
	viewport     viewport.Model
	textarea     textarea.Model
	messages     []client.Message
	outputLines  []string
	busy         bool
	ready        bool
	cfg          *config.Config
	llm          *client.LLMClient
	toolReg      *tools.Registry
	systemPrompt string
	width        int
	height       int
	program      *tea.Program
	renderer     *glamour.TermRenderer
	lastStats    string
	contextUsed  int // last prompt token count (current context usage)
}

// New creates a new TUI model.
func New(cfg *config.Config, llm *client.LLMClient, toolReg *tools.Registry, systemPrompt string, contextCount int) *model {
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
		glamour.WithWordWrap(80),
	)

	m := &model{
		textarea:     ta,
		messages:     []client.Message{},
		outputLines:  []string{},
		cfg:          cfg,
		llm:          llm,
		toolReg:      toolReg,
		systemPrompt: systemPrompt,
		renderer:     r,
	}

	// Build welcome banner
	m.appendWelcome(contextCount)

	return m
}

func (m *model) appendWelcome(contextCount int) {
	sep := strings.Repeat("─", 80)
	welcome := fmt.Sprintf("  %s %s",
		bannerStyle.Render("cfassist"),
		bannerDimStyle.Render("v"+config.Version),
	)
	if contextCount > 0 {
		welcome += dimStyle.Render(fmt.Sprintf("  (%d context files loaded)", contextCount))
	}
	m.outputLines = append(m.outputLines,
		separatorStyle.Render(sep),
		welcome,
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
			m.textarea.Reset()
			return m, nil
		case tea.KeyEnter:
			if m.busy {
				return m, nil
			}
			return m.handleSubmit()
		}

	case tea.WindowSizeMsg:
		m.width = msg.Width
		m.height = msg.Height

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
		r := msg.result
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
		m.outputLines = append(m.outputLines, errorStyle.Render(msg.err.Error()))
		if m.ready {
			m.viewport.SetContent(strings.Join(m.outputLines, "\n"))
			m.viewport.GotoBottom()
		}
		return m, nil
	}

	// Update sub-components
	var cmd tea.Cmd

	if !m.busy {
		m.textarea, cmd = m.textarea.Update(msg)
		cmds = append(cmds, cmd)
	}

	m.viewport, cmd = m.viewport.Update(msg)
	cmds = append(cmds, cmd)

	return m, tea.Batch(cmds...)
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
			if len(desc) > 80 {
				desc = desc[:80] + "..."
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
	case "/help", "help":
		m.outputLines = append(m.outputLines,
			dimStyle.Render("Commands: /clear, /exit, /help, /tools, /models, /model <name>"),
			dimStyle.Render("Ctrl-D to exit, Ctrl-C to cancel input."),
		)
		if m.ready {
			m.viewport.SetContent(strings.Join(m.outputLines, "\n"))
			m.viewport.GotoBottom()
		}
		return m, nil
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

	m.busy = true

	// Run conversation in background via tea.Cmd
	return m, m.runConversationCmd(text)
}

func (m *model) runConversationCmd(userInput string) tea.Cmd {
	return func() tea.Msg {
		m.messages = append(m.messages, client.Message{Role: "user", Content: userInput})

		out := &tuiOutput{program: m.program, renderer: m.renderer}
		result, _ := conversation.Run(m.llm, m.toolReg, out, m.messages, m.systemPrompt, 10)

		if result.Response != "" {
			m.messages = append(m.messages, client.Message{Role: "assistant", Content: result.Response})
		}

		if result.Error != "" {
			return errMsg{err: fmt.Errorf("%s", result.Error)}
		}
		return llmDoneMsg{result: result}
	}
}

func (m *model) View() string {
	if !m.ready {
		return "Initializing..."
	}

	// Build status bar — left: model + status, right: stats
	status := "ready"
	if m.busy {
		status = "working..."
	}
	left := fmt.Sprintf(" %s | %s", m.llm.Model, status)

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
	gap := contentWidth - lipgloss.Width(left) - lipgloss.Width(right)
	if gap < 1 {
		gap = 1
	}
	statusText := left + strings.Repeat(" ", gap) + right
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

// Run starts the TUI application.
func Run(cfg *config.Config, llm *client.LLMClient, toolReg *tools.Registry, systemPrompt string, contextCount int) error {
	m := New(cfg, llm, toolReg, systemPrompt, contextCount)
	p := tea.NewProgram(m, tea.WithAltScreen())
	m.program = p

	_, err := p.Run()
	return err
}

// tuiOutput implements conversation.Output by sending messages to the TUI.
type tuiOutput struct {
	program  *tea.Program
	renderer *glamour.TermRenderer
}

func (o *tuiOutput) ShowThinking() {
	o.program.Send(appendOutputMsg{text: dimStyle.Render("  thinking...")})
}

func (o *tuiOutput) ClearThinking() {
	// In TUI, thinking just scrolls up naturally — no-op
}

func (o *tuiOutput) ShowToolCall(name string, args map[string]any) {
	var line string
	switch name {
	case "bash":
		cmd, _ := args["command"].(string)
		line = toolNameStyle.Render("[tool] bash:") + " " + cmd
	case "read_file":
		path, _ := args["path"].(string)
		line = toolNameStyle.Render("[tool] read_file:") + " " + path
	default:
		line = toolNameStyle.Render(fmt.Sprintf("[tool] %s:", name)) + fmt.Sprintf(" %v", args)
	}
	o.program.Send(appendOutputMsg{text: line})
}

func (o *tuiOutput) ShowToolResult(name string, result map[string]any) {
	if errMsg, ok := result["error"]; ok {
		o.program.Send(appendOutputMsg{text: toolErrorStyle.Render(fmt.Sprintf("[tool] error: %v", errMsg))})
		return
	}

	var line string
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
		if exitCode == 0 {
			line = toolSuccessStyle.Render(fmt.Sprintf("[tool] %d lines | exit %d", lines, exitCode))
		} else {
			line = toolErrorStyle.Render(fmt.Sprintf("[tool] %d lines | exit %d", lines, exitCode))
		}
	case "read_file":
		content, _ := result["content"].(string)
		lines := 0
		if content != "" {
			lines = len(strings.Split(content, "\n"))
		}
		line = toolSuccessStyle.Render(fmt.Sprintf("[tool] %d lines", lines))
	default:
		line = dimStyle.Render("[tool] done")
	}
	o.program.Send(appendOutputMsg{text: line})
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
