package tui

import (
	"strings"
	"testing"

	"github.com/charmbracelet/bubbles/textarea"
	tea "github.com/charmbracelet/bubbletea"

	"github.com/aachtenberg/cfoperator/cfassist-go/internal/tools"
)

// pendingModel is a busy model with one question open.
func pendingModel(t *testing.T) (*model, chan tools.Decision) {
	t.Helper()
	ta := textarea.New()
	ta.Focus() // as New() does: an unfocused textarea ignores keys
	m := &model{busy: true, textarea: ta}
	reply := make(chan tools.Decision, 1)
	m.Update(confirmRequestMsg{command: "sudo systemctl restart nginx", reason: "systemctl restart changes service state", reply: reply})
	return m, reply
}

// TestTheQuestionIsRenderedWithTheCommandAndReason checks the prompt lines.
func TestTheQuestionIsRenderedWithTheCommandAndReason(t *testing.T) {
	m, _ := pendingModel(t)
	if m.pendingConfirm == nil {
		t.Fatal("the request was not kept as pending")
	}
	joined := strings.Join(m.outputLines, "\n")
	for _, want := range []string{"sudo systemctl restart nginx", "changes service state", "y = run it", "a = run writes"} {
		if !strings.Contains(joined, want) {
			t.Errorf("prompt lacks %q:\n%s", want, joined)
		}
	}
}

// TestKeysAnswerTheQuestion maps each key to its decision.
func TestKeysAnswerTheQuestion(t *testing.T) {
	cases := []struct {
		key  tea.KeyMsg
		want tools.Decision
	}{
		{tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune{'y'}}, tools.Allow},
		{tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune{'Y'}}, tools.Allow},
		{tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune{'a'}}, tools.AllowAll},
		{tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune{'n'}}, tools.Deny},
		{tea.KeyMsg{Type: tea.KeyEnter}, tools.Deny},
		{tea.KeyMsg{Type: tea.KeyEsc}, tools.Deny},
		{tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune{'q'}}, tools.Deny},
	}
	for _, tc := range cases {
		m, reply := pendingModel(t)
		m.Update(tc.key)
		select {
		case got := <-reply:
			if got != tc.want {
				t.Errorf("%s: got %v, want %v", tc.key.String(), got, tc.want)
			}
		default:
			t.Errorf("%s: no answer was sent", tc.key.String())
		}
		if m.pendingConfirm != nil {
			t.Errorf("%s: the question is still open after an answer", tc.key.String())
		}
	}
}

// TestTheAnswerKeyDoesNotReachTheInputBox checks the key is consumed by the question.
func TestTheAnswerKeyDoesNotReachTheInputBox(t *testing.T) {
	m, _ := pendingModel(t)
	m.Update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune{'y'}})
	if m.textarea.Value() != "" {
		t.Fatalf("the 'y' landed in the input: %q", m.textarea.Value())
	}
}

// TestCtrlCDeniesAndStillCancelsTheTurn checks Ctrl+C answers no and cancels.
func TestCtrlCDeniesAndStillCancelsTheTurn(t *testing.T) {
	m, reply := pendingModel(t)
	cancelled := false
	m.cancelTurn = func() { cancelled = true }
	m.Update(tea.KeyMsg{Type: tea.KeyCtrlC})
	if got := <-reply; got != tools.Deny {
		t.Fatalf("Ctrl+C answered %v, want Deny", got)
	}
	if !cancelled {
		t.Fatal("Ctrl+C with a question open must still cancel the turn")
	}
}

// TestNoQuestionOpenMeansKeysBehaveAsBefore checks answerConfirm is inert with nothing pending.
func TestNoQuestionOpenMeansKeysBehaveAsBefore(t *testing.T) {
	m := &model{busy: false, textarea: textarea.New()}
	if m.answerConfirm(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune{'y'}}) {
		t.Fatal("answerConfirm consumed a key with nothing pending")
	}
}

// TestACancelledTurnWithdrawsTheQuestion: the next key must reach the input
// box, not answer a question whose command was already denied.
func TestACancelledTurnWithdrawsTheQuestion(t *testing.T) {
	m, reply := pendingModel(t)
	m.Update(confirmCancelMsg{reply: reply})
	if m.pendingConfirm != nil {
		t.Fatal("the question is still open after its turn was cancelled")
	}
	m.Update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune{'y'}})
	select {
	case d := <-reply:
		t.Fatalf("a key after the cancel answered the dead question with %v", d)
	default:
	}
	if m.textarea.Value() != "y" {
		t.Fatalf("the key did not reach the input box: %q", m.textarea.Value())
	}
	if joined := strings.Join(m.outputLines, "\n"); !strings.Contains(joined, "turn ended") {
		t.Fatalf("the operator is not told the question was withdrawn:\n%s", joined)
	}
}

// TestACancelForAnotherQuestionIsIgnored: only the cancelled question is
// withdrawn; one asked afterwards keeps its turn.
func TestACancelForAnotherQuestionIsIgnored(t *testing.T) {
	m, _ := pendingModel(t)
	other := make(chan tools.Decision, 1)
	m.Update(confirmCancelMsg{reply: other})
	if m.pendingConfirm == nil {
		t.Fatal("a cancel for a different question withdrew the open one")
	}
}
