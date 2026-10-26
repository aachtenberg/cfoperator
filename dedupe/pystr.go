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
//
// Checked over every code point (the \w, \s and \d classes, and pyLower
// alone and on each side of a capital sigma): built with Go 1.25 (Unicode
// 15.0) they agree with Python 3.12 (15.0) everywhere. The agent runs
// Python 3.11 (Unicode 14.0), and against it they differ only on code
// points 14.0 left unassigned.

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

// pyLower is str.lower(). strings.ToLower maps one rune to one rune; Python
// applies the full mapping, and the two disagree in exactly two places:
// U+0130 lowers to "i" + U+0307 (SpecialCasing.txt), and a capital sigma
// that ends a word lowers to final sigma U+03C2 (the Final_Sigma context).
// Every other rune lowers by the same simple mapping, so a string holding
// neither takes the ToLower path.
func pyLower(s string) string {
	if !strings.ContainsRune(s, 0x130) && !strings.ContainsRune(s, 0x3a3) {
		return strings.ToLower(s)
	}
	rs := []rune(s)
	var b strings.Builder
	b.Grow(len(s) + 1)
	for i, r := range rs {
		switch {
		case r == 0x130:
			b.WriteString("i̇")
		case r == 0x3a3 && finalSigma(rs, i):
			b.WriteRune(0x3c2)
		default:
			b.WriteRune(unicode.ToLower(r))
		}
	}
	return b.String()
}

// finalSigma is CPython's handle_capital_sigma: a cased letter before the
// sigma and none after it, skipping case-ignorable characters both ways.
func finalSigma(rs []rune, i int) bool {
	j := i - 1
	for j >= 0 && caseIgnorable(rs[j]) {
		j--
	}
	if j < 0 || !cased(rs[j]) {
		return false
	}
	j = i + 1
	for j < len(rs) && caseIgnorable(rs[j]) {
		j++
	}
	return j == len(rs) || !cased(rs[j])
}

// cased is Unicode's derived Cased property: Lowercase, Uppercase or Lt.
func cased(r rune) bool {
	return unicode.In(r, unicode.Lu, unicode.Ll, unicode.Lt,
		unicode.Other_Lowercase, unicode.Other_Uppercase)
}

// caseIgnorable is Unicode's derived Case_Ignorable property. The
// Word_Break values it includes (MidLetter, MidNumLet, Single_Quote) are not
// in Go's unicode package, so their members are listed.
func caseIgnorable(r rune) bool {
	switch r {
	case '\'', '.', ':', 0xb7, 0x387, 0x55f, 0x5f4, 0x2018, 0x2019, 0x2024,
		0x2027, 0xfe13, 0xfe52, 0xfe55, 0xff07, 0xff0e, 0xff1a:
		return true
	}
	return unicode.In(r, unicode.Mn, unicode.Me, unicode.Cf, unicode.Lm, unicode.Sk)
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
