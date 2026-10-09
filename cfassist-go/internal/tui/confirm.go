package tui

// The operator prompt for a write-shaped bash command (CFOP-282).
//
// The turn runs in its own goroutine (runConversationCmd), so the gate's
// Asker cannot read the keyboard itself. It posts a confirmRequestMsg to the
// program and blocks on the reply channel; the key loop renders the question,
// takes one key and answers. Ctrl+C while the question is open is a Deny and
// then the usual cancel of the turn.

import (
	"context"
	"fmt"

	tea "github.com/charmbracelet/bubbletea"

	"github.com/aachtenberg/cfoperator/cfassist-go/internal/tools"
)

// confirmRequestMsg carries one question from the gate to the key loop.
type confirmRequestMsg struct {
	command string
	reason  string
	reply   chan<- tools.Decision
}

// confirmCancelMsg withdraws a question whose turn ended before it was
// answered (a timeout, a cancel that was not a key here). Without it the
// question stayed open: the next unrelated key was swallowed, and a `y`
// printed "running" for a command already denied (claude-review on #310).
type confirmCancelMsg struct {
	reply chan<- tools.Decision
}

// askConfirm is the tools.Asker the TUI installs on its registry.
func (m *model) askConfirm(ctx context.Context, command, reason string) tools.Decision {
	reply := make(chan tools.Decision, 1)
	m.program.Send(confirmRequestMsg{command: command, reason: reason, reply: reply})
	select {
	case d := <-reply:
		return d
	case <-ctx.Done():
		m.program.Send(confirmCancelMsg{reply: reply})
		return tools.Deny
	}
}

// withdrawConfirm clears the open question if it is the one that was
// cancelled; a question asked later keeps its turn.
func (m *model) withdrawConfirm(msg confirmCancelMsg) {
	if m.pendingConfirm == nil || m.pendingConfirm.reply != msg.reply {
		return
	}
	m.pendingConfirm = nil
	m.outputLines = append(m.outputLines, dimStyle.Render("    not run; the turn ended before you answered"))
	m.refreshViewport()
}

// confirmPromptLines is what the operator sees above the input.
func confirmPromptLines(command, reason string) []string {
	return []string{
		warningStyle.Render(fmt.Sprintf("  ? this command %s — run it?", reason)),
		"      " + command,
		dimStyle.Render("    y = run it    a = run writes without asking this session    anything else = don't"),
	}
}

// decisionFor maps a key to the operator's answer. Anything that is not an
// explicit yes is a no: Enter, Esc, a stray letter.
func decisionFor(key tea.KeyMsg) tools.Decision {
	switch key.String() {
	case "y", "Y":
		return tools.Allow
	case "a", "A":
		return tools.AllowAll
	}
	return tools.Deny
}

// answerConfirm resolves the open question with one key. Returns true when the
// key was consumed; Ctrl+C is answered (Deny) and then left for the normal
// handler, which cancels the turn.
func (m *model) answerConfirm(key tea.KeyMsg) bool {
	req := m.pendingConfirm
	if req == nil {
		return false
	}
	d := decisionFor(key)
	if key.Type == tea.KeyCtrlC {
		d = tools.Deny
	}
	m.pendingConfirm = nil
	req.reply <- d
	switch d {
	case tools.Allow:
		m.outputLines = append(m.outputLines, dimStyle.Render("    running"))
	case tools.AllowAll:
		m.outputLines = append(m.outputLines, dimStyle.Render("    running; writes will not ask again this session"))
	default:
		m.outputLines = append(m.outputLines, dimStyle.Render("    not run"))
	}
	m.refreshViewport()
	return key.Type != tea.KeyCtrlC
}
