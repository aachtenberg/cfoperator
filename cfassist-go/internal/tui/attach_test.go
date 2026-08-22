package tui

import (
	"regexp"
	"strings"
	"testing"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"

	"github.com/aachtenberg/cfoperator/cfassist-go/internal/client"
	"github.com/aachtenberg/cfoperator/cfassist-go/internal/config"
	"github.com/aachtenberg/cfoperator/cfassist-go/internal/tools"
)

// The session has to say what it is attached to (CFOP-63). The TUI runs on the
// alternate screen buffer, so everything `attach` printed before starting the
// program is invisible until the operator quits — these tests hold the two
// replacements in place: the id in the status bar, the briefing in the
// scrollback, and no trace of either in a plain session.

const (
	testWidth  = 100
	testHeight = 24
)

// newTestModel builds a model at a known size. Sending the WindowSizeMsg is
// what makes it ready — View() renders "Initializing..." until then, so a test
// that skipped it would assert nothing about the status bar.
func newTestModel(t *testing.T, attachment *Attachment, width int) *model {
	t.Helper()
	cfg := config.Defaults()
	llm := client.New("ollama", "http://127.0.0.1:1", "llama3.2", 0.7, "")
	m := New(cfg, llm, tools.New(cfg), "system prompt", 0, nil, "ollama", attachment, "")
	next, _ := m.Update(tea.WindowSizeMsg{Width: width, Height: testHeight})
	ready, ok := next.(*model)
	if !ok {
		t.Fatalf("Update returned %T, want *model", next)
	}
	return ready
}

var ansiPattern = regexp.MustCompile(`\x1b\[[0-9;]*m`)

func plainText(s string) string { return ansiPattern.ReplaceAllString(s, "") }

// statusBarLine returns the rendered status bar, styling stripped. It finds the
// bar by the segment that is always on it rather than by line index, so the
// test does not break when the layout above it changes.
func statusBarLine(t *testing.T, view string) string {
	t.Helper()
	for _, line := range strings.Split(plainText(view), "\n") {
		if strings.Contains(line, "ollama:llama3.2") {
			return line
		}
	}
	t.Fatalf("no status bar in view:\n%s", view)
	return ""
}

// TestPlainSessionStatusBarCarriesNothingExtra is the "renders exactly as
// today" guard that the optional attachment needs. Everything else in this file
// adds things to the bar; this asserts a session with no attachment gets none
// of them.
//
// It pins the *content* of the bar (nothing beyond the existing segments)
// rather than its markup, so restyling does not make it fail while a leaked
// attachment segment does.
func TestPlainSessionStatusBarCarriesNothingExtra(t *testing.T) {
	m := newTestModel(t, nil, testWidth)

	got := strings.TrimSpace(statusBarLine(t, m.View()))
	if got != "ollama:llama3.2 | ready" {
		t.Errorf("plain status bar = %q, want only the provider|status segment", got)
	}

	if strings.Contains(plainText(m.View()), "CFOperator briefing") {
		t.Error("a plain session must not seed a briefing into the scrollback")
	}
}

// TestAttachedSessionShowsTheInvestigationForItsWholeLife covers the reported
// complaint directly: "there is no summary that pops up in the UI to tell me i
// attached - even a number and brief title". The id and title live in the
// status bar, which is redrawn every frame so scrolling cannot lose them, and
// the briefing goes into the scrollback because the alt screen hides the copy
// attach printed before the program started.
func TestAttachedSessionShowsTheInvestigationForItsWholeLife(t *testing.T) {
	m := newTestModel(t, &Attachment{
		ID:       2242,
		Title:    "PodUnschedulable on headless-gpu",
		Briefing: "====\nCFOperator briefing — investigation #2242\n====\nthe node is out of memory",
	}, testWidth)

	bar := statusBarLine(t, m.View())
	for _, want := range []string{"#2242", "PodUnschedulable", "ollama:llama3.2 | ready"} {
		if !strings.Contains(bar, want) {
			t.Errorf("status bar %q is missing %q", bar, want)
		}
	}

	view := plainText(m.View())
	if !strings.Contains(view, "the node is out of memory") {
		t.Errorf("the briefing is not readable inside the alt screen:\n%s", view)
	}
}

// TestAttachmentNeverCostsTheLayoutALine is the gap-math guard. The status bar
// is rendered into a fixed-height layout; a segment wider than the space the
// other segments leave would wrap it onto a second line and push the input area
// off the bottom of the terminal. Comparing against the same model without an
// attachment keeps this honest at widths where the existing segments alone
// already overflow.
func TestAttachmentNeverCostsTheLayoutALine(t *testing.T) {
	att := &Attachment{
		ID:       2242,
		Title:    "PodUnschedulable on headless-gpu — 3 pods pending since 14:02",
		Briefing: "briefing body",
	}

	for width := 20; width <= 140; width += 4 {
		plain := newTestModel(t, nil, width)
		attached := newTestModel(t, att, width)

		if got, want := lipgloss.Height(attached.View()), lipgloss.Height(plain.View()); got != want {
			t.Errorf("width %d: attached view is %d lines, plain is %d — the bar wrapped",
				width, got, want)
		}

		bar := statusBarLine(t, attached.View())
		if !strings.Contains(bar, "ollama:llama3.2") {
			t.Errorf("width %d: the attachment pushed provider:model off the bar: %q", width, bar)
		}
		if lipgloss.Width(bar) > width {
			t.Errorf("width %d: status bar is %d columns wide", width, lipgloss.Width(bar))
		}
	}
}

// TestAttachSegmentTruncation: what survives a narrow terminal is the id.
// Correlating the session with Slack and the console needs the number; the
// title is a convenience. A half-printed id would be worse than none — "#22"
// reads as investigation 22 — so below the width of the id the segment is
// empty.
func TestAttachSegmentTruncation(t *testing.T) {
	att := &Attachment{ID: 2242, Title: "PodUnschedulable on headless-gpu"}

	tests := []struct {
		name   string
		att    *Attachment
		budget int
		want   string
	}{
		{name: "everything fits", att: att, budget: 60, want: "#2242 · PodUnschedulable on headless-gpu"},
		{name: "exactly fits", att: att, budget: 40, want: "#2242 · PodUnschedulable on headless-gpu"},
		{name: "title truncated", att: att, budget: 20, want: "#2242 · PodUnschedu…"},
		{name: "no room for any title", att: att, budget: 8, want: "#2242"},
		{name: "id exactly fits", att: att, budget: 5, want: "#2242"},
		{name: "not even the id", att: att, budget: 4, want: ""},
		{name: "no budget at all", att: att, budget: 0, want: ""},
		{name: "negative budget", att: att, budget: -10, want: ""},
		{name: "no attachment", att: nil, budget: 60, want: ""},
		{name: "id without a title", att: &Attachment{ID: 7}, budget: 60, want: "#7"},
	}

	for _, tt := range tests {
		got := attachSegment(tt.att, tt.budget)
		if got != tt.want {
			t.Errorf("%s: attachSegment(budget=%d) = %q, want %q", tt.name, tt.budget, got, tt.want)
		}
		if tt.budget > 0 && lipgloss.Width(got) > tt.budget {
			t.Errorf("%s: segment %q is %d columns, over budget %d",
				tt.name, got, lipgloss.Width(got), tt.budget)
		}
	}
}

// TestAttachSegmentFlattensTheTitle: an investigation's trigger is often a
// multi-line alert body. A newline reaching the status bar would tear the
// layout apart, so the flattening happens inside the segment rather than being
// left to whoever constructs the Attachment.
func TestAttachSegmentFlattensTheTitle(t *testing.T) {
	got := attachSegment(&Attachment{
		ID:    2242,
		Title: "  PodUnschedulable\n\ton headless-gpu \r\n",
	}, 60)

	if want := "#2242 · PodUnschedulable on headless-gpu"; got != want {
		t.Errorf("attachSegment = %q, want %q", got, want)
	}
	if strings.ContainsAny(got, "\n\r\t") {
		t.Errorf("segment %q still carries a control character", got)
	}
	if strings.Contains(attachSegment(&Attachment{ID: 1, Title: "esc\x1b[31mred"}, 60), "\x1b") {
		t.Error("an escape sequence in the trigger reached the status bar")
	}
}

// TestClearKeepsTheSessionAttached: /clear means "clear the screen", so the
// briefing does not come back — but a cleared screen must not read as an
// unattached session.
func TestClearKeepsTheSessionAttached(t *testing.T) {
	m := newTestModel(t, &Attachment{
		ID: 2242, Title: "PodUnschedulable", Briefing: "briefing body",
	}, testWidth)
	m.textarea.SetValue("/clear")
	next, _ := m.handleSubmit()
	cleared, ok := next.(*model)
	if !ok {
		t.Fatalf("handleSubmit returned %T, want *model", next)
	}

	view := plainText(cleared.View())
	if strings.Contains(view, "briefing body") {
		t.Error("/clear should clear the scrollback, briefing included")
	}
	if !strings.Contains(view, "Still attached to investigation #2242") {
		t.Errorf("a cleared screen must still say it is attached:\n%s", view)
	}
	if !strings.Contains(statusBarLine(t, cleared.View()), "#2242") {
		t.Error("the status bar is the durable record; /clear must not drop it")
	}
}
