package tui

import (
	"os"
	"strings"
	"testing"

	"github.com/charmbracelet/bubbles/textarea"
	tea "github.com/charmbracelet/bubbletea"
)

// Ctrl+C is the key every operator already reaches for. It used to reset the
// input box — an input box that, while busy, could not be typed into anyway —
// and leave the turn running with no way to stop it short of quitting the
// session (CFOP-76).
func TestCtrlCStopsARunningTurn(t *testing.T) {
	stopped := false
	m := &model{
		busy:       true,
		cancelTurn: func() { stopped = true },
	}

	m.Update(tea.KeyMsg{Type: tea.KeyCtrlC})

	if !stopped {
		t.Error("Ctrl+C during a turn did not cancel it")
	}
}

// Idle, Ctrl+C keeps its old job. Cancelling a turn that is not running would
// be a no-op at best, and a nil call at worst.
func TestCtrlCWhenIdleDoesNotReachForACancel(t *testing.T) {
	called := false
	// A real model always has an initialised textarea; Ctrl+C reaches it on
	// the idle path.
	m := &model{
		busy:       false,
		cancelTurn: func() { called = true },
		textarea:   textarea.New(),
	}

	m.Update(tea.KeyMsg{Type: tea.KeyCtrlC})

	if called {
		t.Error("Ctrl+C cancelled while idle — there was nothing to stop")
	}
}

// releaseTurn runs when a turn ends so a later Ctrl+C clears the input line
// rather than cancelling a context nobody is waiting on.
func TestReleaseTurnClearsTheCancel(t *testing.T) {
	m := &model{cancelTurn: func() {}}

	m.releaseTurn()

	if m.cancelTurn != nil {
		t.Error("cancelTurn survived the end of its turn")
	}
	m.releaseTurn() // must stay safe when there is nothing to release
}

// The class to protect: a turn is always started cancellably.
//
// Two entry points start turns — a typed question and /skill — and they used to
// set m.busy and build the command separately. A third would eventually be
// added the same way and be uninterruptible, which is exactly how this bug
// existed. startTurn is the only place allowed to raise the flag.
func TestEveryTurnIsStartedThroughStartTurn(t *testing.T) {
	src, err := os.ReadFile("model.go")
	if err != nil {
		t.Fatalf("reading model.go: %v", err)
	}

	lines := strings.Split(string(src), "\n")
	inStartTurn := false
	found := 0

	for i, line := range lines {
		if strings.HasPrefix(line, "func (m *model) startTurn(") {
			inStartTurn = true
		} else if inStartTurn && line == "}" {
			inStartTurn = false
		}

		if strings.TrimSpace(line) == "m.busy = true" {
			found++
			if !inStartTurn {
				t.Errorf("model.go:%d starts a turn outside startTurn — "+
					"it would run with no cancel for Ctrl+C to pull", i+1)
			}
		}
	}

	if found == 0 {
		t.Fatal("found nothing that starts a turn — has the flag been renamed?")
	}
}
