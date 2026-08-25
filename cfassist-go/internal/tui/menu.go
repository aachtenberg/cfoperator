// The menu (CFOP-69, "Palette"): what opens under the cursor when the line
// starts with "/" or is "?".
//
// One widget, three kinds of row. Type "/" and it lists commands with a
// description each, narrowing as you type; once the command is in, the same
// rows switch to its argument — the nine playbooks after "/skill ", the
// providers after "/use ", the models after "/model ". "?" lists the keys. The
// point is the second kind: an operator who gets as far as "/sk" is shown the
// playbooks without knowing they exist, which is the discoverability the
// issue asked for — the skills engine made them reachable, this makes them
// visible.
//
// It is data plus a few keys. Rendering and the space it takes are in
// layout.go; the rows come from the command table in commands.go.

package tui

import (
	"strconv"
	"strings"

	tea "github.com/charmbracelet/bubbletea"
)

// menuRow is one line of the menu.
type menuRow struct {
	Label  string // left column: a command's usage, a skill's name, a key
	Detail string // right column: what it does
	Insert string // what accepting it puts on the line; "" for rows that only inform
	Submit bool   // accepting it runs it — a command that takes no argument
}

// menuState is the open menu. The zero value is closed.
type menuState struct {
	open bool
	rows []menuRow
	sel  int
	// text is the input the rows were built for, so a redraw for the same
	// text keeps the selection and a changed text resets it.
	text string
}

// menuCapRows is the most rows shown at once. Past it the menu says how many
// it is hiding; on a short terminal the cap is lower still (see menuCap) so
// at least half the transcript stays visible.
const menuCapRows = 7

// menuRows builds the rows for what is typed. Nil means no menu.
func (m *model) menuRows(text string) []menuRow {
	if text == "?" {
		rows := make([]menuRow, 0, len(keys))
		for _, k := range keys {
			rows = append(rows, menuRow{Label: k[0], Detail: k[1]})
		}
		return rows
	}
	if !strings.HasPrefix(text, "/") {
		return nil
	}

	word, rest, hasSpace := strings.Cut(text, " ")
	lower := strings.ToLower(word)

	if !hasSpace {
		var rows []menuRow
		for _, c := range commands {
			if !strings.HasPrefix(c.Name, lower) {
				continue
			}
			insert := c.Name
			if c.Args != "" {
				// A trailing space so accepting the command opens its argument.
				insert += " "
			}
			rows = append(rows, menuRow{
				Label:  c.Usage(),
				Detail: c.Summary,
				Insert: insert,
				Submit: c.Args == "",
			})
		}
		return rows
	}

	c, ok := findCommand(lower)
	if !ok || c.Complete == nil {
		return nil
	}
	var rows []menuRow
	for _, arg := range c.Complete(m, strings.TrimSpace(rest)) {
		detail := ""
		if c.Describe != nil {
			detail = c.Describe(m, arg)
		}
		rows = append(rows, menuRow{
			Label:  arg,
			Detail: detail,
			Insert: c.Name + " " + arg + " ",
		})
	}
	return rows
}

// syncMenu rebuilds the menu for the current line. Called after every
// keystroke reaches the textarea, so the menu is a function of the text
// rather than a mode the operator has to enter and leave.
func (m *model) syncMenu() {
	text := m.textarea.Value()
	if text == m.menuDismissedFor {
		m.menu = menuState{}
		return
	}
	// The dismissal is for one line, and the line has changed: forget it now,
	// or backspacing to nothing and retyping the same text would find the
	// menu still shut.
	m.menuDismissedFor = ""
	if strings.Contains(text, "\n") {
		m.menu = menuState{}
		return
	}
	rows := m.menuRows(text)
	if len(rows) == 0 {
		m.menu = menuState{}
		return
	}
	sel := 0
	if m.menu.open && text == m.menu.text && m.menu.sel < len(rows) {
		sel = m.menu.sel
	}
	m.menu = menuState{open: true, rows: rows, sel: sel, text: text}
}

// closeMenu shuts the menu for now; it reopens as soon as the text changes.
func (m *model) closeMenu() {
	m.menu = menuState{}
}

// dismissMenu shuts the menu for this exact text — esc — so it does not pop
// straight back on the next redraw. Any edit to the line lifts it.
func (m *model) dismissMenu() {
	m.menuDismissedFor = m.textarea.Value()
	m.menu = menuState{}
}

// selectedRow is the highlighted row, if the menu is open. syncMenu keeps
// the selection in range; this is the one place that relies on it.
func (m *model) selectedRow() (menuRow, bool) {
	if !m.menu.open || m.menu.sel < 0 || m.menu.sel >= len(m.menu.rows) {
		return menuRow{}, false
	}
	return m.menu.rows[m.menu.sel], true
}

// acceptMenu puts the selected row on the line. For a command that takes an
// argument that is the name plus a space, which is what opens the argument
// rows; for an argument it is the whole command, ready to send.
func (m *model) acceptMenu() {
	row, ok := m.selectedRow()
	if !ok || row.Insert == "" {
		return
	}
	m.textarea.SetValue(row.Insert)
	m.textarea.CursorEnd()
	m.menuDismissedFor = ""
	m.syncMenu()
}

// menuKey handles a key while the menu is open. Reports whether it took the
// key; anything it did not take falls through to the textarea as usual, so
// typing keeps narrowing the rows.
func (m *model) menuKey(msg tea.KeyMsg) (bool, tea.Cmd) {
	n := len(m.menu.rows)
	switch msg.Type {
	case tea.KeyUp:
		m.menu.sel = (m.menu.sel + n - 1) % n
		return true, nil
	case tea.KeyDown:
		m.menu.sel = (m.menu.sel + 1) % n
		return true, nil
	case tea.KeyEsc:
		m.dismissMenu()
		return true, nil
	case tea.KeyTab:
		if !m.busy {
			m.acceptMenu()
		}
		return true, nil
	case tea.KeyEnter:
		if m.busy {
			return true, nil
		}
		row, ok := m.selectedRow()
		if !ok {
			return false, nil
		}
		if row.Insert == "" {
			// The keys list: enter is "thanks, got it".
			m.textarea.Reset()
			m.closeMenu()
			return true, nil
		}
		typed := strings.TrimSpace(m.textarea.Value())
		if row.Submit || typed == strings.TrimSpace(row.Insert) {
			// Either the row runs as it is, or the operator has already typed
			// exactly what accepting would insert — a second enter that only
			// re-inserted the same text would be a loop.
			m.textarea.SetValue(row.Insert)
			m.closeMenu()
			_, cmd := m.handleSubmit()
			return true, cmd
		}
		m.acceptMenu()
		return true, nil
	}
	return false, nil
}

// menuCap is how many rows fit: the fixed cap, or fewer on a short terminal
// so the transcript keeps at least half the screen.
func (m *model) menuCap() int {
	c := menuCapRows
	if m.height > 0 && m.height/3 < c {
		c = m.height / 3
	}
	if c < 1 {
		c = 1
	}
	return c
}

// menuWindow is the slice of rows on screen, scrolled so the selection is
// visible, and how many rows are hidden below it.
func (m *model) menuWindow() (rows []menuRow, hidden int) {
	if !m.menu.open {
		return nil, 0
	}
	c := m.menuCap()
	start := 0
	if m.menu.sel >= c {
		start = m.menu.sel - c + 1
	}
	end := start + c
	if end > len(m.menu.rows) {
		end = len(m.menu.rows)
	}
	return m.menu.rows[start:end], len(m.menu.rows) - end
}

// menuHeight is the lines the menu takes: its visible rows plus the "n more"
// line when it is hiding some.
func (m *model) menuHeight() int {
	rows, hidden := m.menuWindow()
	if len(rows) == 0 {
		return 0
	}
	if hidden > 0 {
		return len(rows) + 1
	}
	return len(rows)
}

// menuView renders the open menu, one line per row, every line exactly width
// columns. Descriptions clip on the same floor the lists use, and vanish
// before the labels do — the labels are what tab completion showed before
// the menu existed, so a narrow terminal is never worse off than it was.
func (m *model) menuView() []string {
	rows, hidden := m.menuWindow()
	if len(rows) == 0 {
		return nil
	}
	width := m.listWidth()

	col := 0
	for _, r := range m.menu.rows {
		if w := len([]rune(r.Label)); w > col {
			col = w
		}
	}
	col += 2
	if col > helpNameColumn+6 {
		col = helpNameColumn + 6
	}
	// The column can never be wider than the terminal leaves for it, or a
	// padded label would widen every line of the frame.
	if col > width-6 {
		col = width - 6
	}
	if col < 4 {
		col = 4
	}
	room := width - col - 6

	// Which row of the window is selected.
	selInWindow := -1
	if c := m.menuCap(); m.menu.sel >= c {
		selInWindow = c - 1
	} else {
		selInWindow = m.menu.sel
	}

	var out []string
	for i, r := range rows {
		label := truncateToWidth(r.Label, col-1)
		text := "  " + padRight(label, col) + "  " + clip(r.Detail, room)
		text = truncateToWidth(text, width)
		if i == selInWindow && r.Insert != "" {
			out = append(out, menuSelectedStyle.Width(width).Render(text))
			continue
		}
		line := "  " + menuLabelStyle.Render(padRight(label, col)) + "  " +
			dimStyle.Render(clip(r.Detail, room))
		out = append(out, padRight(line, width))
	}
	if hidden > 0 {
		more := truncateToWidth("  … "+strconv.Itoa(hidden)+" more — keep typing", width)
		out = append(out, padRight(dimStyle.Render(more), width))
	}
	return out
}
