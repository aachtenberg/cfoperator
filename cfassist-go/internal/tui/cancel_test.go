package tui

import (
	"context"
	"github.com/aachtenberg/cfoperator/cfassist-go/internal/client"
	"github.com/aachtenberg/cfoperator/cfassist-go/internal/config"
	"github.com/aachtenberg/cfoperator/cfassist-go/internal/conversation"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

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
	entries, err := os.ReadDir(".")
	if err != nil {
		t.Fatalf("reading package dir: %v", err)
	}

	found := 0
	for _, entry := range entries {
		name := entry.Name()
		if !strings.HasSuffix(name, ".go") || strings.HasSuffix(name, "_test.go") {
			continue
		}
		src, err := os.ReadFile(filepath.Clean(name))
		if err != nil {
			t.Fatalf("reading %s: %v", name, err)
		}

		inStartTurn := false
		for i, line := range strings.Split(string(src), "\n") {
			if strings.HasPrefix(line, "func (m *model) startTurn(") {
				inStartTurn = true
			} else if inStartTurn && line == "}" {
				inStartTurn = false
			}

			// Matched loosely on purpose. Pinning the exact statement let a
			// trailing comment or a rename walk straight past the guard, and
			// scanning only model.go let a new file do the same.
			if !strings.Contains(line, "busy = true") || strings.HasPrefix(strings.TrimSpace(line), "//") {
				continue
			}
			found++
			if !inStartTurn {
				t.Errorf("%s:%d starts a turn outside startTurn — "+
					"it would run with no cancel for Ctrl+C to pull", name, i+1)
			}
		}
	}

	if found == 0 {
		t.Fatal("found nothing that starts a turn — has the flag been renamed?")
	}
}

// newTestModel builds a model far enough along to render: the viewport only
// exists after a size message, and without it output is stored but never drawn.
func newRenderableModel(t *testing.T) *model {
	t.Helper()
	m := &model{
		cfg:      config.Defaults(),
		llm:      client.New("ollama", "http://localhost:11434", "test-model", 0.7, ""),
		textarea: textarea.New(),
	}
	m.Update(tea.WindowSizeMsg{Width: 100, Height: 30})
	return m
}

// The acknowledgement has to reach the screen. It shipped appended to
// outputLines but never pushed into the viewport, so Ctrl+C looked exactly like
// the frozen terminal it was supposed to fix.
func TestStoppingIsVisible(t *testing.T) {
	m := newRenderableModel(t)
	m.busy = true
	m.cancelTurn = func() {}

	m.Update(llmDoneMsg{result: conversation.Result{Cancelled: true}})

	if !strings.Contains(m.View(), "stopped") {
		t.Error("a cancelled turn drew nothing — the operator sees no response to the key")
	}
}

// A cancelled turn measured nothing. Overwriting the readout with its zeros
// blanks the context gauge and loses the last real numbers with it.
func TestCancelDoesNotWipeTheStatusBar(t *testing.T) {
	m := newRenderableModel(t)
	m.busy = true
	m.cancelTurn = func() {}
	m.lastStats = "3100↑ 890↓ 8.2s"
	m.contextUsed = 12400

	m.Update(llmDoneMsg{result: conversation.Result{Cancelled: true}})

	if m.lastStats != "3100↑ 890↓ 8.2s" {
		t.Errorf("lastStats = %q, want the last completed turn's numbers", m.lastStats)
	}
	if m.contextUsed != 12400 {
		t.Errorf("contextUsed = %d, want 12400 — the gauge would vanish", m.contextUsed)
	}
}

// Pressing stop twice must not feel like pressing it into a void. The turn can
// take a moment to unwind, and silence during that moment is the whole
// complaint this work started from.
func TestSecondCtrlCSaysSomething(t *testing.T) {
	m := newRenderableModel(t)
	m.busy = true
	m.cancelTurn = func() {}

	m.Update(tea.KeyMsg{Type: tea.KeyCtrlC})
	before := len(m.outputLines)
	m.Update(tea.KeyMsg{Type: tea.KeyCtrlC})

	if len(m.outputLines) == before {
		t.Fatal("the second press was swallowed")
	}
	if !strings.Contains(m.View(), "still stopping") {
		t.Error("the second press drew nothing")
	}
}

// Leaving by any door stops the work. Ctrl+D and /exit quit without touching
// cancelTurn, so turns hang off the session context to be cancelled on the way
// out — before `attach` writes back the transcript and revokes the token a
// tool call may still be using.
func TestTurnsHangOffTheSessionContext(t *testing.T) {
	sessionCtx, endSession := context.WithCancel(context.Background())
	m := newRenderableModel(t)
	m.sessionCtx = sessionCtx

	var turnCtx context.Context
	m.startTurn("hello")
	turnCtx = m.lastTurnCtx

	endSession()

	select {
	case <-turnCtx.Done():
	case <-time.After(time.Second):
		t.Error("ending the session left the turn's context live")
	}
}
