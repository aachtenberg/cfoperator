package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// Every entry point that probes the LLM must ask the error what to suggest.
//
// This guards a class, not a line. The startup hint was hardcoded to "Is the
// LLM server running?" in two places; the first fix caught main.go and missed
// attach.go — the path that seeds an investigation session — so a rejected key
// on `cfassist attach` still reported itself as a dead server. Nothing in the
// suite noticed, because the copies are three lines each and neither is
// reachable from a unit test without standing up a whole session.
//
// A third entry point would repeat it. The rule is mechanical, so check it
// mechanically: wherever CheckConnection is called, APIError.Hint must be
// consulted nearby.
func TestEveryConnectionCheckAsksTheErrorForItsHint(t *testing.T) {
	const window = 8 // lines after the call in which the hint must appear

	entries, err := os.ReadDir(".")
	if err != nil {
		t.Fatalf("reading package dir: %v", err)
	}

	checked := 0
	for _, entry := range entries {
		name := entry.Name()
		if !strings.HasSuffix(name, ".go") || strings.HasSuffix(name, "_test.go") {
			continue
		}

		src, err := os.ReadFile(filepath.Clean(name))
		if err != nil {
			t.Fatalf("reading %s: %v", name, err)
		}
		lines := strings.Split(string(src), "\n")

		for i, line := range lines {
			if !strings.Contains(line, "CheckConnection()") {
				continue
			}
			checked++

			end := min(i+window, len(lines))
			if !strings.Contains(strings.Join(lines[i:end], "\n"), "apiErr.Hint()") {
				t.Errorf("%s:%d calls CheckConnection but hand-rolls its hint — "+
					"use APIError.Hint() so the advice matches the failure", name, i+1)
			}
		}
	}

	// A guard that finds nothing to check has stopped guarding anything.
	if checked == 0 {
		t.Fatal("found no CheckConnection call sites — has the probe been renamed?")
	}
}
