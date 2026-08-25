package tui

import (
	"fmt"
	"strings"
	"testing"

	tea "github.com/charmbracelet/bubbletea"
)

// The banner used to advertise a version and a file count and never what the
// session could do. It has to count what is loaded and name the key that
// lists each.
func TestWelcomeSaysWhatTheSessionCanDo(t *testing.T) {
	m := newSkillModel(t)
	view := plainText(strings.Join(m.outputLines, "\n"))

	for _, want := range []string{
		fmt.Sprintf("%d tools", len(m.toolReg.GetSchemas())),
		fmt.Sprintf("%d skills", len(m.skills)),
		fmt.Sprintf("%d commands", len(commands)),
		"/ lists commands",
		"? lists keys",
		"/skills lists the playbooks",
		"ollama:llama3.2",
	} {
		if !strings.Contains(view, want) {
			t.Errorf("welcome is missing %q:\n%s", want, view)
		}
	}
	if strings.Contains(view, "Type /help") {
		t.Error("the old one-line hint is still there")
	}
}

// /clear redraws the welcome block; the numbers must not go missing with it.
func TestClearKeepsTheWelcomeNumbers(t *testing.T) {
	m := newSkillModel(t)
	m.contextCount = 3
	m.textarea.SetValue("/clear")
	next, _ := m.handleSubmit()
	view := plainText(strings.Join(next.(*model).outputLines, "\n"))

	for _, want := range []string{"3 context files", fmt.Sprintf("%d skills", len(m.skills))} {
		if !strings.Contains(view, want) {
			t.Errorf("after /clear the welcome lost %q:\n%s", want, view)
		}
	}
}

// Every key the textarea sees, the viewport sees too. With the default
// keymap, typing "b" in a question paged the transcript up — the keys an
// operator is typing are not navigation. Only pgup/pgdn scroll.
func TestTypingDoesNotScrollTheTranscript(t *testing.T) {
	m := newTestModel(t, nil, 100)
	for i := 0; i < 200; i++ {
		m.outputLines = append(m.outputLines, fmt.Sprintf("line %d", i))
	}
	m.refreshViewport()
	m.layout()
	bottom := m.viewport.YOffset
	if bottom == 0 {
		t.Fatal("the transcript is not taller than the viewport; the test proves nothing")
	}

	m = typeText(t, m, "why does the pod bounce, and could u check dmesg for it")
	if m.viewport.YOffset != bottom {
		t.Errorf("typing scrolled the transcript from %d to %d", bottom, m.viewport.YOffset)
	}

	m = press(t, m, tea.KeyPgUp)
	if m.viewport.YOffset >= bottom {
		t.Errorf("pgup did not scroll: offset still %d", m.viewport.YOffset)
	}
}

// Arrows belong to the input and the menu, not the transcript — a choice,
// not an omission of the keymap cut above.
func TestArrowsDoNotScrollTheTranscript(t *testing.T) {
	m := newTestModel(t, nil, 100)
	for i := 0; i < 200; i++ {
		m.outputLines = append(m.outputLines, fmt.Sprintf("line %d", i))
	}
	m.refreshViewport()
	m.layout()
	bottom := m.viewport.YOffset

	m = press(t, m, tea.KeyUp)
	m = press(t, m, tea.KeyUp)
	if m.viewport.YOffset != bottom {
		t.Errorf("up arrow scrolled the transcript from %d to %d", bottom, m.viewport.YOffset)
	}
}
