// The Palette layout (CFOP-69): transcript, then the input in a rounded box,
// then the menu when one is open, then one footer line.
//
//	╭─ #2242 · PodUnschedulable on headless-gpu ─────────────────────╮
//	│ > /sk                                                          │
//	╰────────────────────────────────────────────────────────────────╯
//	  /skill <name> [target]  Load a playbook into this session…
//	  /skills                 The playbooks this session knows…
//	  ↑↓ choose · tab accept · enter run · esc close   ollama:gemma4:26b · 12.3k/32k
//
// Three fixed lines at rest where the separator, status bar and three-line
// input were five. The box's top border is not decoration: it carries the
// attachment title, on the same id-first truncation the status bar used, and
// it turns amber while a turn is running. The footer's left half is whatever
// hint is true right now; its right half is the model and the context gauge,
// shed in a fixed order when the terminal is narrow.

package tui

import (
	"fmt"
	"strings"

	"github.com/charmbracelet/lipgloss"
)

const (
	footerHeight   = 1
	boxBorderLines = 2
	inputMaxLines  = 3
)

// chromeHeight is every line that is not transcript.
func (m *model) chromeHeight() int {
	return m.textarea.Height() + boxBorderLines + m.menuHeight() + footerHeight
}

// layout gives the viewport whatever the chrome leaves. Called whenever the
// chrome can have changed height — a keystroke, a resize, the menu opening —
// and from View as a backstop, because a stale height here is the input
// pushed off the bottom of the terminal.
//
// When the menu opens the viewport shrinks. Keeping YOffset would clip the
// bottom of the transcript — the /providers (or /models, /skills) list the
// operator just ran, which is why they opened the menu. If they were already
// following the bottom, stay there. A scrolled-up view is left alone.
func (m *model) layout() {
	if !m.ready {
		return
	}
	follow := m.viewport.AtBottom()
	h := m.height - m.chromeHeight()
	if h < 1 {
		h = 1
	}
	m.viewport.Height = h
	m.viewport.Width = m.width
	if follow {
		m.viewport.GotoBottom()
	}
}

// growInput lets the box follow a multi-line paste up to inputMaxLines, and
// shrink back when the line is sent.
func (m *model) growInput() {
	n := m.textarea.LineCount()
	if n < 1 {
		n = 1
	}
	if n > inputMaxLines {
		n = inputMaxLines
	}
	if n != m.textarea.Height() {
		m.textarea.SetHeight(n)
	}
}

// boxView renders the input in its border. The title goes into the top edge
// by rewriting that line — lipgloss draws borders, not titled ones.
func (m *model) boxView() string {
	inner := m.width - 2
	if inner < 4 {
		inner = 4
	}
	border := boxIdleStyle
	if m.busy {
		border = boxBusyStyle
	}

	body := border.Width(inner).Render(m.textarea.View())
	lines := strings.Split(body, "\n")

	// "╭─ " + title + " " …"╮": the title may spend the inner width less
	// the four characters of frame around it.
	if title := attachSegment(m.attachment, inner-4); title != "" {
		// The border style draws a box around whatever it renders, so the
		// fragments of the top edge take its colour and nothing else.
		edge := lipgloss.NewStyle().Foreground(border.GetBorderTopForeground())
		rest := inner - 3 - lipgloss.Width(title)
		if rest < 0 {
			// attachSegment returns "" before it returns a title too wide
			// for its budget, so this does not fire today; it is here so the
			// box cannot widen if that ever changes.
			rest = 0
		}
		lines[0] = edge.Render("╭─ ") + boxTitleStyle.Render(title) +
			edge.Render(" "+strings.Repeat("─", rest)+"╮")
	}
	return strings.Join(lines, "\n")
}

// footerHints is the left half of the footer: only what is true right now.
// A hint that changes with state is one that gets read.
func (m *model) footerHints() []string {
	switch {
	case m.stopping:
		return []string{"stopping…", "ctrl+d quits"}
	case m.busy:
		return []string{"ctrl+c stop", "typing is kept", "ctrl+d quit"}
	}
	if row, ok := m.selectedRow(); ok {
		if row.Insert == "" {
			return []string{"esc close"}
		}
		return []string{"↑↓ choose", "tab accept", "enter run", "esc close"}
	}
	return []string{"/ commands", "? keys", "ctrl+d quit"}
}

// footerStatus is the right half of the footer, as a ladder from everything
// down to the model name alone. Each rung is what a narrower terminal shows:
// last-turn stats go first (they belong to the transcript), then the provider
// half of provider:model, then the context gauge. The model name is the
// floor — below its width the footer is wrong, not narrow.
func (m *model) footerStatus() [][]string {
	modelName := ""
	if m.llm != nil {
		modelName = m.llm.Model
	}
	full := modelName
	if m.activeProvider != "" {
		full = m.activeProvider + ":" + modelName
	}
	gauge := ""
	if m.contextUsed > 0 && m.cfg != nil && m.cfg.LLM.ContextWindow > 0 {
		gauge = fmt.Sprintf("%.1fk/%.0fk",
			float64(m.contextUsed)/1000, float64(m.cfg.LLM.ContextWindow)/1000)
	}

	with := func(parts ...string) []string {
		var out []string
		for _, p := range parts {
			if p != "" {
				out = append(out, p)
			}
		}
		return out
	}
	return [][]string{
		with(full, gauge, m.lastStats),
		with(full, gauge),
		with(modelName, gauge),
		with(modelName),
	}
}

// footerView lays the two halves on one line that never wraps.
func (m *model) footerView() string {
	return fitFooter(m.footerHints(), m.footerStatus(), m.width)
}

const footerSep = " · "

// fitFooter sheds in a fixed order until the line fits: last-turn stats
// first (the ladder's top rung), then hints from the right down to the first
// one — "/ commands" is the hint this issue exists for, so it outlives the
// provider prefix and the gauge — then the provider half of provider:model,
// then the gauge, and only then the last hint. The model name is the floor.
func fitFooter(hints []string, ladder [][]string, width int) string {
	content := width - 4 // two columns of margin each side

	type try struct{ hints, rung int }
	var order []try
	order = append(order, try{len(hints), 0}, try{len(hints), 1})
	for n := len(hints) - 1; n >= 1; n-- {
		order = append(order, try{n, 1})
	}
	keep := 1
	if len(hints) == 0 {
		keep = 0
	}
	order = append(order, try{keep, 2}, try{keep, 3}, try{0, 3})

	for _, o := range order {
		if o.rung >= len(ladder) {
			continue
		}
		left := strings.Join(hints[:o.hints], footerSep)
		right := strings.Join(ladder[o.rung], footerSep)
		gap := content - lipgloss.Width(left) - lipgloss.Width(right)
		if left != "" && right != "" {
			gap -= 2
		}
		if gap >= 0 {
			return renderFooter(left, right, width)
		}
	}
	// Not even the bottom rung fits: keep as much of it as there is room for.
	last := ""
	if len(ladder) > 0 {
		last = strings.Join(ladder[len(ladder)-1], footerSep)
	}
	return renderFooter("", truncateToWidth(last, content), width)
}

func renderFooter(left, right string, width int) string {
	content := width - 4
	gap := content - lipgloss.Width(left) - lipgloss.Width(right)
	if gap < 0 {
		gap = 0
	}
	line := "  " + footerHintStyle.Render(left) + strings.Repeat(" ", gap) +
		footerStatusStyle.Render(right) + "  "
	return padRight(line, width)
}

// View is the four bands, top to bottom. Empty bands (a closed menu) are
// skipped rather than joined, because JoinVertical would draw them as a
// blank line and the input would sit one row too high.
func (m *model) View() string {
	if !m.ready {
		return "Initializing..."
	}
	m.layout()

	bands := []string{m.viewport.View(), m.boxView()}
	bands = append(bands, m.menuView()...)
	bands = append(bands, m.footerView())
	return lipgloss.JoinVertical(lipgloss.Left, bands...)
}
