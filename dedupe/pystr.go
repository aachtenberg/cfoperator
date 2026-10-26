package dedupe

import (
	"encoding/json"
	"strings"
	"unicode"
	"unicode/utf8"
)

// The tiers being ported are Python, and Python's string predicates are
// Unicode-wide where Go's regexp classes are ASCII. These helpers are the
// Python definitions, so a parity case cannot pass in one language and fail
// in the other on a character neither author thought about.

// pySpace is str.isspace(), which is also what re's \s and str.strip() use.
// unicode.IsSpace covers everything except U+001C..U+001F, which Python
// counts as whitespace (bidi class B/S) and Go does not.
func pySpace(r rune) bool {
	return unicode.IsSpace(r) || (r >= 0x1c && r <= 0x1f)
}

// pyWord is re's \w for a str pattern: str.isalnum() or '_'. isalnum is
// letters (L*) plus anything numeric, and every numeric character Python
// knows is in N* or already a letter.
func pyWord(r rune) bool {
	return r == '_' || unicode.IsLetter(r) || unicode.IsNumber(r)
}

// pyStrip is str.strip() with no argument.
func pyStrip(s string) string {
	return strings.TrimFunc(s, pySpace)
}

// pyStripChars is str.strip(chars).
func pyStripChars(s, chars string) string {
	return strings.Trim(s, chars)
}

// truthy is Python truthiness for a value decoded from JSON.
func truthy(v any) bool {
	switch x := v.(type) {
	case nil:
		return false
	case bool:
		return x
	case string:
		return x != ""
	case json.Number:
		f, err := x.Float64()
		return err != nil || f != 0
	case float64:
		return x != 0
	case []any:
		return len(x) > 0
	case map[string]any:
		return len(x) > 0
	}
	return true
}

// pyStrOr is str(v or ""). Strings, numbers and booleans come out as Python
// would print them; a list or a dict comes out empty, which Python would not
// do, but nothing the tiers read is ever one (FIX targets are validated into
// strings before they reach a row).
func pyStrOr(v any) string {
	if !truthy(v) {
		return ""
	}
	switch x := v.(type) {
	case string:
		return x
	case bool:
		return "True"
	case json.Number:
		return x.String()
	}
	return ""
}

// orFirst is `a or b`.
func orFirst(a, b any) any {
	if truthy(a) {
		return a
	}
	return b
}

// firstRunes is s[:n] on a Python str: n code points, not n bytes.
func firstRunes(s string, n int) string {
	if utf8.RuneCountInString(s) <= n {
		return s
	}
	i := 0
	for pos := range s {
		if i == n {
			return s[:pos]
		}
		i++
	}
	return s
}
