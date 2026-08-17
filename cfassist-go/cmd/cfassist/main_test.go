package main

import (
	"strings"
	"testing"
)

// The Go binary is the one operators install from releases, so it is the one a
// pasted `cfassist attach 1889` most often lands on. Without the intercept the
// verb falls through to one-shot mode and the LLM answers the literal string
// "attach 1889" — the failure this guards is a confident wrong answer, not a
// crash.
func TestAttachIsIntercepted(t *testing.T) {
	notice, ok := attachNotice([]string{"attach", "1889"})
	if !ok {
		t.Fatal("attach must be intercepted, not passed to the LLM")
	}
	for _, want := range []string{"not available in this build", "1889", "docs/cockpit.md"} {
		if !strings.Contains(notice, want) {
			t.Errorf("notice missing %q:\n%s", want, notice)
		}
	}
}

func TestAttachWithoutAnIDStillExplains(t *testing.T) {
	notice, ok := attachNotice([]string{"attach"})
	if !ok {
		t.Fatal("bare attach must be intercepted too")
	}
	if !strings.Contains(notice, "<investigation-id>") {
		t.Errorf("notice should show the expected argument:\n%s", notice)
	}
}

func TestOrdinaryQuestionsAreNotIntercepted(t *testing.T) {
	// Only the leading token is the verb. A question that merely mentions
	// attaching must still reach the LLM.
	cases := [][]string{
		{},
		{"why", "is", "the", "pod", "down"},
		{"how do I attach a debugger to pid 5"},
		{"tell", "me", "about", "attach"},
	}
	for _, args := range cases {
		if _, ok := attachNotice(args); ok {
			t.Errorf("args %q must not be intercepted", args)
		}
	}
}
