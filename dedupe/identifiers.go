package dedupe

import (
	"regexp"
	"unicode/utf8"
)

// Ported from agent/agent.py: _IDENT_IP, _IDENT_ENV, _IDENT_ROW and
// CFOperator._extract_remediation_identifiers (CFOP-78).
//
// The hard identifiers a recommendation names: what two differently-worded
// recommendations for one fix reliably share. Only shapes with near-zero
// prose collision: dotted-quad IPs (port kept, since one box runs many
// services), UPPER_SNAKE keys, and <table>_id <n> row references. Hostnames
// and workload names are deliberately absent: a false fold hides a real
// incident, which is worse than a duplicate row.
//
// Python's \b, \d and \W are Unicode-aware and Go's are ASCII-only, so each
// pattern is its body plus explicit boundary classes, and findAll
// walks the text the way re.finditer does. Ported verbatim, "CFOP_TOKEN"
// right after a CJK or accented letter would match here and not in Python.

// nonWord is Python's \W: not a letter, not a number, not '_'.
const nonWord = `[^\p{L}\p{N}_]`

// boundedPattern is one identifier shape: its groups, preceded by a \b and,
// when trailing is set, followed by one. Every shape starts and ends on a
// word character, so \b there means "the neighbour is not one".
type boundedPattern struct {
	atStart *regexp.Regexp // may match at the very start of the text
	inside  *regexp.Regexp // must consume the non-word character before it
}

func newBoundedPattern(groups string, trailing bool) boundedPattern {
	tail := ""
	if trailing {
		tail = `(?:$|` + nonWord + `)`
	}
	return boundedPattern{
		atStart: regexp.MustCompile(`(?:^|` + nonWord + `)` + groups + tail),
		inside:  regexp.MustCompile(nonWord + groups + tail),
	}
}

var (
	// \b(\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?)\b
	identIP = newBoundedPattern(`(\p{Nd}{1,3}(?:\.\p{Nd}{1,3}){3}(?::\p{Nd}+)?)`, true)
	// \b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b
	identEnv = newBoundedPattern(`([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)`, true)
	// \b([A-Za-z][A-Za-z0-9]*_id)\W{0,3}(\d+), with no trailing \b
	identRow = newBoundedPattern(`([A-Za-z][A-Za-z0-9]*_id)`+nonWord+`{0,3}(\p{Nd}+)`, false)
)

// findAll yields each match's submatch strings, like re.finditer over the
// body. Each search restarts where the previous body ended, one character
// back, so the character that ends one identifier can serve as the boundary
// of the next ("10.0.0.1,10.0.0.2" is two).
func (p boundedPattern) findAll(text string) [][]string {
	var out [][]string
	pos := 0
	for pos <= len(text) {
		re, base := p.atStart, 0
		if pos > 0 {
			_, size := utf8.DecodeLastRuneInString(text[:pos])
			re, base = p.inside, pos-size
		}
		loc := re.FindStringSubmatchIndex(text[base:])
		if loc == nil {
			break
		}
		groups := make([]string, 0, len(loc)/2-1)
		for i := 2; i < len(loc); i += 2 {
			if loc[i] < 0 {
				groups = append(groups, "")
				continue
			}
			groups = append(groups, text[base+loc[i]:base+loc[i+1]])
		}
		out = append(out, groups)
		// The body ends at its last group; anything after it is the
		// trailing boundary, which the next match may need.
		end := 0
		for i := 2; i < len(loc); i += 2 {
			if loc[i+1] > end {
				end = loc[i+1]
			}
		}
		pos = base + end
	}
	return out
}

// extractIdentifiers is the set of hard identifiers text names. Empty means
// "names nothing matchable", and callers must treat that as "do not fold",
// never as "matches everything".
func extractIdentifiers(text string) map[string]bool {
	ids := map[string]bool{}
	for _, m := range identIP.findAll(text) {
		ids[m[0]] = true
	}
	for _, m := range identEnv.findAll(text) {
		ids[m[0]] = true
	}
	for _, m := range identRow.findAll(text) {
		ids[pyLower(m[0])+":"+m[1]] = true
	}
	return ids
}
