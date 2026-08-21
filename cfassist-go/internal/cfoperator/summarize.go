// Distilling a finished cockpit session into something worth keeping (CFOP-37).
//
// Client-side on purpose. cfassist already holds the model connection and the
// whole conversation, so this is one more turn on a warm context rather than a
// transcript shipped across the wire; the server seam that receives the result
// (POST /api/learnings, CFOP-47) is built to take a *structured learning*, not
// a transcript; and a tier-3 session's scrollback is raw shell output typed on
// a production host, which has no business leaving that host by default.
//
// The output is a JSON object or it is nothing. A prose summary would be
// storable but not retrievable, and retrieval is the entire point: a learning
// with no `applies_when` is auto-deprecated by the KB precisely because nothing
// can ever match it.
package cfoperator

import (
	"encoding/json"
	"fmt"
	"regexp"
	"strings"

	"github.com/aachtenberg/cfoperator/cfassist-go/internal/client"
)

// SummaryPrompt asks for exactly one JSON object.
//
// `learning` is explicitly nullable, and the prompt says when to use it. Most
// sessions conclude nothing reusable — someone looked, saw it was fine, and
// left — and a KB that gains a learning per session degrades faster than one
// that gains none. The retrieval corpus is the asset; diluting it to look
// productive is the failure mode this wording exists to prevent.
const SummaryPrompt = `You are closing out an incident session and writing what it should leave behind.

Summarize the conversation above: what was investigated, what was found, what was
done, and whether it worked. Be specific about commands and evidence; a future
reader will have the same alert and none of this context.

Reply with EXACTLY one JSON object and nothing else:
{
  "outcome": "resolved|mitigated|diagnosed|no_change|inconclusive|escalated",
  "summary": "3-6 sentences: what was checked, what was found, what was done, what remains",
  "commands": ["the few commands that actually mattered"],
  "learning": null
}

Set "learning" to an object ONLY if this session concluded something that would
help someone facing a DIFFERENT occurrence of this class of problem. A one-off
fix, a false alarm, or "looked and it was fine" is not a learning — leave it null.
When it is one:
{
  "learning_type": "pattern|solution|root_cause|antipattern|insight",
  "title": "short and specific",
  "description": "what to know, and why",
  "applies_when": "the observable condition that should bring someone back to this",
  "services": ["affected services"],
  "category": "one word"
}

"applies_when" is required and must describe a SYMPTOM someone could notice, not
a restatement of the title. Without it the learning can never be retrieved and
is worth nothing.`

// SessionSummary is the parsed reply.
type SessionSummary struct {
	Outcome  string    `json:"outcome"`
	Summary  string    `json:"summary"`
	Commands []string  `json:"commands"`
	Learning *Learning `json:"learning"`
	// Degraded marks a summary that is really a transcript tail, because the
	// model failed or answered with something unparseable. Recorded rather
	// than discarded — losing the only record of a session is worse — but
	// never presented as if it were a distillation.
	Degraded bool `json:"-"`
	// DroppedLearning is set when the model produced a learning that could
	// never be retrieved and it was discarded here. Reported rather than
	// implied: silently nilling it leaves the operator believing the session
	// simply had nothing to teach, which is a different fact.
	DroppedLearning string `json:"-"`
}

var fencedJSON = regexp.MustCompile("(?s)```(?:json)?\\s*(\\{.*?\\})\\s*```")

// ParseSummary extracts the JSON object from a model reply, fenced or bare.
//
// Returns ok=false rather than an error: every failure here has the same
// answer — keep the raw tail — and distinguishing "no JSON" from "bad JSON"
// would give the caller a choice it does not have.
func ParseSummary(reply string) (*SessionSummary, bool) {
	blob := ""
	if m := fencedJSON.FindStringSubmatch(reply); len(m) == 2 {
		blob = m[1]
	} else if start := strings.Index(reply, "{"); start >= 0 {
		if end := strings.LastIndex(reply, "}"); end > start {
			blob = reply[start : end+1]
		}
	}
	if blob == "" {
		return nil, false
	}

	var s SessionSummary
	if err := json.Unmarshal([]byte(blob), &s); err != nil {
		return nil, false
	}
	if strings.TrimSpace(s.Summary) == "" {
		return nil, false
	}
	s.Outcome = normalizeOutcome(s.Outcome)

	// A learning the model filled in halfway is dropped here rather than sent
	// to be auto-deprecated server-side. Both end with nothing retrievable in
	// the KB; only one of them tells the operator it happened — which is what
	// DroppedLearning carries out to the caller.
	if s.Learning != nil && !s.Learning.Valid() {
		s.DroppedLearning = describeIncompleteLearning(s.Learning)
		s.Learning = nil
	}
	s.Commands = trimList(s.Commands, 20, 300)
	return &s, true
}

// normalizeOutcome maps a model's wording onto the agent's vocabulary. Anything
// unrecognised becomes "inconclusive" — which is true of a session whose own
// summarizer could not say what happened.
func normalizeOutcome(raw string) string {
	got := strings.ToLower(strings.TrimSpace(raw))
	got = strings.ReplaceAll(got, "-", "_")
	got = strings.ReplaceAll(got, " ", "_")
	for _, valid := range SessionOutcomes {
		if got == valid {
			return got
		}
	}
	return "inconclusive"
}

// RawTail is the fallback record: the last few exchanges, marked as such.
//
// "Store the raw tail rather than nothing" is the issue's own instruction, and
// the reason is that a session's only trace should not depend on a local model
// having a good day.
func RawTail(messages []client.Message, maxChars int) string {
	if maxChars <= 0 {
		maxChars = 4000
	}
	var parts []string
	for i := len(messages) - 1; i >= 0; i-- {
		m := messages[i]
		if m.Role == "system" || strings.TrimSpace(m.Content) == "" {
			continue
		}
		parts = append(parts, m.Role+": "+m.Content)
		total := 0
		for _, p := range parts {
			total += len(p)
		}
		if total >= maxChars {
			break
		}
	}
	// Collected newest-first for the budget; rendered oldest-first to read.
	for i, j := 0, len(parts)-1; i < j; i, j = i+1, j-1 {
		parts[i], parts[j] = parts[j], parts[i]
	}
	tail := strings.Join(parts, "\n\n")
	if len(tail) > maxChars {
		tail = tail[len(tail)-maxChars:]
	}
	return "(session summary unavailable — raw transcript tail)\n\n" + tail
}

// Summarize asks the session's own model to distil the conversation.
//
// The transcript is passed as the conversation it already is, with the prompt
// appended as a final user turn: the model has the context loaded, and
// re-stating it as one giant user message would both cost more and read worse.
func Summarize(llm *client.LLMClient, messages []client.Message) (*SessionSummary, error) {
	if len(messages) == 0 {
		return nil, fmt.Errorf("nothing to summarize")
	}
	turn := append(append([]client.Message{}, messages...),
		client.Message{Role: "user", Content: SummaryPrompt})

	// No tools: this turn is a distillation of what already happened, and a
	// model that reached for a tool here would be starting new work in a
	// session the operator has just ended.
	resp, err := llm.Chat(turn, nil)
	if err != nil {
		return nil, err
	}
	summary, ok := ParseSummary(resp.Content)
	if !ok {
		return nil, fmt.Errorf("the model did not return a usable summary")
	}
	return summary, nil
}

// SessionExchanges counts the human/assistant turns, ignoring system and tool
// plumbing — the number an operator recognises as "how long was I in there".
func SessionExchanges(messages []client.Message) int {
	n := 0
	for _, m := range messages {
		if m.Role == "user" || m.Role == "assistant" {
			n++
		}
	}
	return n
}

// describeIncompleteLearning names what was missing, so the warning says which
// half of the model's answer to distrust rather than just that one existed.
func describeIncompleteLearning(l *Learning) string {
	var missing []string
	if strings.TrimSpace(l.Title) == "" {
		missing = append(missing, "title")
	}
	if strings.TrimSpace(l.Description) == "" {
		missing = append(missing, "description")
	}
	if strings.TrimSpace(l.AppliesWhen) == "" {
		missing = append(missing, "applies_when")
	}
	title := strings.TrimSpace(l.Title)
	if title == "" {
		title = "(untitled)"
	}
	return fmt.Sprintf("%q is missing %s", title, strings.Join(missing, " and "))
}

func trimList(items []string, maxItems, maxLen int) []string {
	out := make([]string, 0, len(items))
	for _, item := range items {
		text := strings.TrimSpace(item)
		if text == "" {
			continue
		}
		if len(text) > maxLen {
			text = text[:maxLen]
		}
		out = append(out, text)
		if len(out) >= maxItems {
			break
		}
	}
	return out
}
