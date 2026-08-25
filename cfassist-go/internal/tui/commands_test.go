package tui

import (
	"strings"
	"testing"

	tea "github.com/charmbracelet/bubbletea"

	"github.com/aachtenberg/cfoperator/cfassist-go/internal/config"
)

// The table exists so a command cannot ship without saying what it does. This
// is the guard for that: a blank summary, a duplicate spelling or a handler
// left nil fails here rather than surfacing as a mute /help entry.
func TestEveryCommandIsDescribed(t *testing.T) {
	seen := map[string]string{}
	for _, c := range commands {
		if !strings.HasPrefix(c.Name, "/") {
			t.Errorf("%q is not a slash command", c.Name)
		}
		if strings.TrimSpace(c.Summary) == "" {
			t.Errorf("%s has no summary — /help would list a name with no verb", c.Name)
		}
		if c.Run == nil {
			t.Errorf("%s has no handler", c.Name)
		}
		if c.Args != "" && c.Complete == nil {
			t.Errorf("%s takes %s but offers no completion for it", c.Name, c.Args)
		}
		if c.Args == "" && c.Complete != nil {
			t.Errorf("%s completes an argument its usage does not mention", c.Name)
		}
		for _, spelling := range append([]string{c.Name}, c.Aliases...) {
			if other, dup := seen[spelling]; dup {
				t.Errorf("%q is claimed by both %s and %s", spelling, other, c.Name)
			}
			seen[spelling] = c.Name
		}
	}
}

// Names alone were the complaint. /help has to carry every command with its
// argument shape and what it does.
func TestHelpNamesEveryCommandAndWhatItDoes(t *testing.T) {
	m := newSkillModel(t)
	out := plainText(strings.Join(m.helpLines(), "\n"))

	for _, c := range commands {
		if !strings.Contains(out, c.Usage()) {
			t.Errorf("/help does not show %q", c.Usage())
		}
		if !strings.Contains(out, c.Summary) {
			t.Errorf("/help does not say what %s does", c.Name)
		}
	}
}

// "What can you do" is one page: the tools the model can call, the playbooks
// the operator can load, and the keys — not three commands to know about.
func TestHelpIsTheOnePage(t *testing.T) {
	m := newSkillModel(t)
	out := plainText(strings.Join(m.helpLines(), "\n"))

	for _, want := range []string{
		"Commands:", "Tools:", "Skills:", "Keys:",
		"bash", "read_file", // tools
		"investigate-pod", "why-restart", // skills
		"ctrl+c", "ctrl+d", "tab", // keys
	} {
		if !strings.Contains(out, want) {
			t.Errorf("/help is missing %q:\n%s", want, out)
		}
	}
}

// A help page that wraps is a paragraph, not a page. Same terminal the skills
// list is held to.
func TestHelpFitsANarrowTerminal(t *testing.T) {
	m := newSkillModel(t)
	next, _ := m.Update(tea.WindowSizeMsg{Width: 72, Height: 24})
	narrow := next.(*model)

	for _, line := range narrow.helpLines() {
		if got := len([]rune(plainText(line))); got > 72 {
			t.Errorf("line is %d columns on a 72-column terminal: %q", got, plainText(line))
		}
	}
}

// A model with nothing registered — the shape tests build — must still get a
// help page rather than a nil dereference.
func TestHelpSurvivesAnEmptySession(t *testing.T) {
	m := newRenderableModel(t)
	if len(m.helpLines()) == 0 {
		t.Fatal("no help for an empty session")
	}
}

// Slash names match on the first word; bare aliases only on the whole line.
// "help me with this pod" is a question, and turning it into /help would be
// the kind of surprise this table exists to remove.
func TestLookupMatchesWordsAndWholeLinesDifferently(t *testing.T) {
	cases := []struct {
		text string
		want string // command name, or "" for "not a command"
		args string
	}{
		{"/help", "/help", ""},
		{"/HELP", "/help", ""},
		{"/help me", "/help", "me"},
		{"help", "/help", ""},
		{"help me with this pod", "", ""},
		{"/quit", "/exit", ""},
		{"quit", "/exit", ""},
		{"exit now", "", ""},
		{"/skill investigate-pod immich-kiosk-0", "/skill", "investigate-pod immich-kiosk-0"},
		{"/use Ollama-Local", "/use", "Ollama-Local"},
		{"why does immich-kiosk-0 keep restarting", "", ""},
		{"/nonsense", "", ""},
	}
	for _, tc := range cases {
		c, args, ok := lookupCommand(tc.text)
		if tc.want == "" {
			if ok {
				t.Errorf("%q resolved to %s, want a question for the model", tc.text, c.Name)
			}
			continue
		}
		if !ok || c.Name != tc.want {
			t.Errorf("%q resolved to %q, want %s", tc.text, c.Name, tc.want)
			continue
		}
		if args != tc.args {
			t.Errorf("%q: args = %q, want %q", tc.text, args, tc.args)
		}
	}
}

// Tab completion reads the same table, for names and for arguments.
func TestCompletionReadsTheTable(t *testing.T) {
	m := newSkillModel(t)
	m.providers = map[string]config.ProviderConfig{
		"ollama": {}, "claude": {}, "openai": {},
	}

	got := m.completionsFor("/sk")
	if strings.Join(got, " ") != "/skill /skills" {
		t.Errorf("/sk completes to %v", got)
	}

	got = m.completionsFor("/skill inv")
	if len(got) < 5 {
		t.Errorf("/skill inv offers %v, want the investigate-* playbooks", got)
	}
	for _, g := range got {
		if !strings.HasPrefix(g, "/skill investigate-") {
			t.Errorf("%q is not an investigate-* completion", g)
		}
	}

	// The name alone, no space, still offers the argument — "/use<tab>" has
	// always cycled the providers.
	got = m.completionsFor("/use")
	if strings.Join(got, " ") != "/use /use claude /use ollama /use openai" {
		t.Errorf("/use completes to %v", got)
	}

	if got := m.completionsFor("/quit"); len(got) != 1 || got[0] != "/quit" {
		t.Errorf("a slash alias should complete to itself, got %v", got)
	}
	if got := m.completionsFor("hello"); len(got) != 0 {
		t.Errorf("a question offered completions: %v", got)
	}
}

// A command is handled, not sent. It does not start a turn and is not echoed
// as a question.
func TestSubmittingACommandDoesNotReachTheModel(t *testing.T) {
	m := newTestModel(t, nil, testWidth)
	m.textarea.SetValue("/help")

	next, cmd := m.handleSubmit()
	got := next.(*model)

	if got.busy || cmd != nil {
		t.Error("/help started a turn")
	}
	out := plainText(strings.Join(got.outputLines, "\n"))
	if strings.Contains(out, "> /help") {
		t.Error("/help was echoed as a question")
	}
	if !strings.Contains(out, "Commands:") {
		t.Errorf("/help printed nothing:\n%s", out)
	}
}
