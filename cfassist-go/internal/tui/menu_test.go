package tui

import (
	"fmt"
	"strings"
	"testing"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"

	"github.com/aachtenberg/cfoperator/cfassist-go/internal/config"
)

// The menu is driven by keys, so these tests type. Each keystroke goes
// through Update the way the terminal would send it.

func press(t *testing.T, m *model, key tea.KeyType) *model {
	t.Helper()
	next, _ := m.Update(tea.KeyMsg{Type: key})
	return next.(*model)
}

func typeText(t *testing.T, m *model, text string) *model {
	t.Helper()
	for _, r := range text {
		msg := tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune{r}}
		if r == ' ' {
			msg = tea.KeyMsg{Type: tea.KeySpace, Runes: []rune{' '}}
		}
		next, _ := m.Update(msg)
		m = next.(*model)
	}
	return m
}

func newMenuModel(t *testing.T) *model {
	t.Helper()
	cfg := config.Defaults()
	cfg.Skills.Directory = t.TempDir()
	m := newTestModel(t, nil, 120)
	m.cfg = cfg
	m.providers = map[string]config.ProviderConfig{
		"ollama": {Provider: "ollama", Model: "gemma4:26b"},
		"claude": {Provider: "anthropic", Model: "claude-opus-5"},
	}
	return m
}

// Type "/" and every command is in front of you with a description. Names
// alone were the complaint.
func TestSlashOpensTheMenuWithDescriptions(t *testing.T) {
	m := typeText(t, newMenuModel(t), "/")

	if !m.menu.open {
		t.Fatal("typing / did not open the menu")
	}
	if got, want := len(m.menu.rows), len(commands); got != want {
		t.Errorf("menu has %d rows, want one per command (%d)", got, want)
	}
	view := plainText(m.View())
	for _, c := range commands[:3] {
		if !strings.Contains(view, c.Summary) {
			t.Errorf("menu does not say what %s does:\n%s", c.Name, view)
		}
	}
	if !strings.Contains(view, "more") {
		t.Errorf("%d commands over a %d-row cap should say some are hidden", len(commands), m.menuCap())
	}
}

// Keep typing and it narrows.
func TestMenuFiltersAsYouType(t *testing.T) {
	m := typeText(t, newMenuModel(t), "/sk")

	var labels []string
	for _, r := range m.menu.rows {
		labels = append(labels, r.Label)
	}
	if strings.Join(labels, "|") != "/skill <name> [target]|/skills" {
		t.Errorf("/sk shows %v", labels)
	}
}

// Past the command, the same rows switch to its argument — here the playbooks.
// This is what makes the nine skills discoverable: get as far as "/skill " and
// the rest is shown to you, with what each one is for.
func TestMenuOffersArgumentsAfterTheName(t *testing.T) {
	m := typeText(t, newMenuModel(t), "/skill inv")

	if !m.menu.open {
		t.Fatal("no menu after /skill inv")
	}
	for _, r := range m.menu.rows {
		if !strings.HasPrefix(r.Label, "investigate-") {
			t.Errorf("row %q is not an investigate-* playbook", r.Label)
		}
		if r.Detail == "" {
			t.Errorf("row %q has no description", r.Label)
		}
	}
	if len(m.menu.rows) < 5 {
		t.Errorf("only %d investigate-* rows", len(m.menu.rows))
	}
}

// Tab takes the selected command; one that takes an argument opens it.
func TestTabAcceptsAndOpensTheArgument(t *testing.T) {
	m := typeText(t, newMenuModel(t), "/us")
	m = press(t, m, tea.KeyTab)

	if got := m.textarea.Value(); got != "/use " {
		t.Errorf("after tab the line is %q, want %q", got, "/use ")
	}
	var labels []string
	for _, r := range m.menu.rows {
		labels = append(labels, r.Label+" "+r.Detail)
	}
	if strings.Join(labels, "|") != "claude anthropic, claude-opus-5|ollama ollama, gemma4:26b" {
		t.Errorf("providers offered as %v", labels)
	}
}

// Enter on a command that takes nothing runs it, straight from the menu.
func TestEnterRunsAnArgumentlessCommand(t *testing.T) {
	m := typeText(t, newMenuModel(t), "/he")
	m = press(t, m, tea.KeyEnter)

	if m.textarea.Value() != "" {
		t.Errorf("the line was not sent: %q", m.textarea.Value())
	}
	if m.menu.open {
		t.Error("the menu stayed open after running a command")
	}
	if !strings.Contains(plainText(strings.Join(m.outputLines, "\n")), "Commands:") {
		t.Error("/help did not run")
	}
}

// Enter on an argument the operator has already typed in full sends it rather
// than re-inserting it — the loop where enter only ever adds a space.
func TestEnterOnATypedArgumentSubmits(t *testing.T) {
	m := typeText(t, newMenuModel(t), "/skill investigate-pod")
	if !m.menu.open || len(m.menu.rows) != 1 {
		t.Fatalf("expected one row for the typed playbook, menu=%+v", m.menu)
	}
	m = press(t, m, tea.KeyEnter)

	if !m.busy {
		t.Error("enter on a fully typed /skill did not start the turn")
	}
	if m.textarea.Value() != "" {
		t.Errorf("line still holds %q", m.textarea.Value())
	}
	m.releaseTurn()
}

// ↑↓ move the selection and wrap.
func TestArrowsChooseAndWrap(t *testing.T) {
	m := typeText(t, newMenuModel(t), "/sk")
	m = press(t, m, tea.KeyDown)
	if m.menu.sel != 1 {
		t.Errorf("down: sel = %d", m.menu.sel)
	}
	m = press(t, m, tea.KeyDown)
	if m.menu.sel != 0 {
		t.Errorf("down past the end should wrap: sel = %d", m.menu.sel)
	}
	m = press(t, m, tea.KeyUp)
	if m.menu.sel != 1 {
		t.Errorf("up from the top should wrap: sel = %d", m.menu.sel)
	}
	if m.textarea.Value() != "/sk" {
		t.Errorf("arrows leaked into the text: %q", m.textarea.Value())
	}
}

// Esc shuts the menu for this line; any edit brings it back.
func TestEscClosesUntilTheTextChanges(t *testing.T) {
	m := typeText(t, newMenuModel(t), "/sk")
	m = press(t, m, tea.KeyEsc)
	if m.menu.open {
		t.Fatal("esc did not close the menu")
	}
	m = typeText(t, m, "i")
	if !m.menu.open {
		t.Error("typing after esc should reopen the menu")
	}
	if m.textarea.Value() != "/ski" {
		t.Errorf("line is %q", m.textarea.Value())
	}
}

// The dismissal is for the line as it was, not for that string forever:
// esc on "/sk", backspace to nothing, retype "/sk" — the menu is wanted
// again. (Review of #179 caught this; the first version compared strings.)
func TestRetypingADismissedLineReopensTheMenu(t *testing.T) {
	m := typeText(t, newMenuModel(t), "/sk")
	m = press(t, m, tea.KeyEsc)
	for range "/sk" {
		m = press(t, m, tea.KeyBackspace)
	}
	if m.textarea.Value() != "" {
		t.Fatalf("line is %q after backspacing", m.textarea.Value())
	}
	m = typeText(t, m, "/sk")
	if !m.menu.open {
		t.Error("retyping the dismissed text on the same line left the menu shut")
	}
}

// "?" lists the keys.
func TestQuestionMarkListsTheKeys(t *testing.T) {
	m := typeText(t, newMenuModel(t), "?")
	if !m.menu.open {
		t.Fatal("? did not open the keys")
	}
	view := plainText(m.View())
	for _, k := range keys {
		if !strings.Contains(view, k[0]) {
			t.Errorf("keys list is missing %q", k[0])
		}
	}
	m = press(t, m, tea.KeyEnter)
	if m.menu.open || m.textarea.Value() != "" {
		t.Error("enter on the keys list should close it and clear the line")
	}
}

// A question is a question. Nothing opens for ordinary text.
func TestOrdinaryTextOpensNothing(t *testing.T) {
	m := typeText(t, newMenuModel(t), "why does immich-kiosk-0 keep restarting")
	if m.menu.open {
		t.Error("a question opened the menu")
	}
	if got := lipgloss.Height(m.View()); got != testHeight {
		t.Errorf("view is %d lines on a %d-line terminal", got, testHeight)
	}
}

// The layout is exactly the terminal, whatever is open and whatever size the
// terminal is: menu open or shut, attached or not, busy or idle. A menu that
// pushed the input off the bottom would be the worst version of the thing
// this exists to fix.
func TestChromeAlwaysFitsTheTerminal(t *testing.T) {
	att := &Attachment{ID: 2242, Title: "PodUnschedulable on headless-gpu — 3 pods pending since 14:02"}
	for _, height := range []int{8, 12, 24, 40} {
		for width := 20; width <= 160; width += 10 {
			for _, attached := range []*Attachment{nil, att} {
				for _, busy := range []bool{false, true} {
					m := newTestModel(t, attached, width)
					next, _ := m.Update(tea.WindowSizeMsg{Width: width, Height: height})
					m = next.(*model)
					m.busy = busy
					m = typeText(t, m, "/")

					view := m.View()
					if got := lipgloss.Height(view); got != height {
						t.Errorf("w%d h%d attached=%v busy=%v: view is %d lines with the menu open",
							width, height, attached != nil, busy, got)
					}
					for _, line := range strings.Split(view, "\n") {
						if lipgloss.Width(line) > width {
							t.Errorf("w%d h%d: line is %d wide: %q", width, height, lipgloss.Width(line), plainText(line))
						}
					}
					if !strings.Contains(plainText(view), "llama3.2") {
						t.Errorf("w%d h%d: the footer lost the model", width, height)
					}
				}
			}
		}
	}
}

// The footer sheds in a fixed order: last-turn stats, then hints from the
// right down to "/ commands", then the provider half of provider:model, then
// the gauge, then the last hint. The model name is the floor.
func TestFooterDropOrder(t *testing.T) {
	m := newTestModel(t, nil, 120)
	m.lastStats = "1204↑ 388↓ 6.1s 3t"
	m.contextUsed = 12300
	m.cfg.LLM.ContextWindow = 32000

	at := func(width int) string {
		next, _ := m.Update(tea.WindowSizeMsg{Width: width, Height: 24})
		return strings.TrimSpace(footerLine(t, next.(*model).View()))
	}

	wide := at(120)
	for _, want := range []string{"/ commands", "ollama:llama3.2", "12.3k/32k", "1204↑"} {
		if !strings.Contains(wide, want) {
			t.Errorf("at 120 columns the footer is missing %q: %q", want, wide)
		}
	}
	mid := at(60)
	if strings.Contains(mid, "1204↑") {
		t.Errorf("at 60 columns the stats should be the first to go: %q", mid)
	}
	for _, want := range []string{"/ commands", "ollama:llama3.2", "12.3k/32k"} {
		if !strings.Contains(mid, want) {
			t.Errorf("at 60 columns %q should outlive the stats: %q", want, mid)
		}
	}
	tight := at(40)
	if !strings.Contains(tight, "/ commands") {
		t.Errorf("at 40 columns the first hint should outlive the provider: %q", tight)
	}
	if strings.Contains(tight, "ctrl+d") {
		t.Errorf("at 40 columns the later hints should have gone: %q", tight)
	}
	narrow := at(24)
	if !strings.Contains(narrow, "llama3.2") {
		t.Errorf("at 24 columns the model name must survive: %q", narrow)
	}
	if strings.Contains(narrow, "ollama:") {
		t.Errorf("at 24 columns the provider half should have gone: %q", narrow)
	}
}

// Opening "/" grows the chrome by a menu's worth of rows. The viewport used
// to keep its YOffset, so the extra chrome ate the bottom of the transcript —
// the /providers list the operator had just printed, the thing they opened
// the menu to act on. Stay pinned to that bottom.
func TestOpeningTheMenuKeepsTheLatestTranscript(t *testing.T) {
	m := newMenuModel(t)
	for i := 0; i < 80; i++ {
		m.outputLines = append(m.outputLines, fmt.Sprintf("history %d", i))
	}
	m.textarea.SetValue("/providers")
	next, _ := m.handleSubmit()
	m = next.(*model)

	before := plainText(m.View())
	if !strings.Contains(before, "Configured Providers:") ||
		!strings.Contains(before, "Switch with: /use") {
		t.Fatalf("the listing is not on screen before the menu:\n%s", before)
	}

	m = typeText(t, m, "/")
	if !m.menu.open {
		t.Fatal("typing / did not open the menu")
	}
	after := plainText(m.View())
	if !strings.Contains(after, "Configured Providers:") ||
		!strings.Contains(after, "Switch with: /use") {
		t.Errorf("opening the menu hid the listing the operator just ran:\n%s", after)
	}
	if !strings.Contains(after, "/use") {
		t.Errorf("the menu itself is missing from the view:\n%s", after)
	}
}

// The pin is only for a transcript the operator was already following. A
// scrolled-up view is a place they chose; the menu must not yank it.
func TestOpeningTheMenuDoesNotYankAScrolledTranscript(t *testing.T) {
	m := newMenuModel(t)
	for i := 0; i < 80; i++ {
		m.outputLines = append(m.outputLines, fmt.Sprintf("history %d", i))
	}
	m.refreshViewport()
	m.layout()
	m = press(t, m, tea.KeyPgUp)
	m = press(t, m, tea.KeyPgUp)
	offset := m.viewport.YOffset
	if offset == 0 {
		t.Fatal("pgup did not leave the bottom; the test proves nothing")
	}

	m = typeText(t, m, "/")
	if !m.menu.open {
		t.Fatal("typing / did not open the menu")
	}
	if m.viewport.YOffset != offset {
		t.Errorf("opening the menu moved a scrolled-up transcript from %d to %d",
			offset, m.viewport.YOffset)
	}
}

// A multi-line paste grows the box, and sending shrinks it back.
func TestInputGrowsWithAPasteAndShrinksBack(t *testing.T) {
	m := newMenuModel(t)
	m.textarea.SetValue("line one\nline two\nline three\nline four")
	next, _ := m.Update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune{'!'}})
	m = next.(*model)
	if got := m.textarea.Height(); got != inputMaxLines {
		t.Errorf("box is %d lines for a four-line paste, want the cap %d", got, inputMaxLines)
	}
	if got := lipgloss.Height(m.View()); got != testHeight {
		t.Errorf("view is %d lines with a grown box", got)
	}
	next, _ = m.handleSubmit()
	m = next.(*model)
	if got := m.textarea.Height(); got != 1 {
		t.Errorf("box is still %d lines after sending", got)
	}
	m.releaseTurn()
}
