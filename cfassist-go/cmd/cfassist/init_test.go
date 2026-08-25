package main

import (
	"bytes"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
)

func TestInitWritesAMissingConfigAndLeavesAnExistingOne(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	root := newTestRoot()

	out := &bytes.Buffer{}
	root.SetOut(out)
	root.SetErr(io.Discard)
	root.SetArgs([]string{"init"})
	if err := root.Execute(); err != nil {
		t.Fatalf("init: %v", err)
	}

	path := filepath.Join(home, ".cfassist", "config.yaml")
	first, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("init did not write %s: %v", path, err)
	}
	if !strings.Contains(string(first), "cfoperator:") {
		t.Error("the file init writes is not the stub template")
	}
	if !strings.Contains(out.String(), "Wrote") {
		t.Errorf("init should say it wrote the file, got %q", out.String())
	}

	if err := os.WriteFile(path, []byte("llm:\n  model: keep-me\n"), 0644); err != nil {
		t.Fatal(err)
	}
	out.Reset()
	root.SetArgs([]string{"init"})
	if err := root.Execute(); err != nil {
		t.Fatalf("second init: %v", err)
	}
	second, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(second, []byte("llm:\n  model: keep-me\n")) {
		t.Errorf("init overwrote an existing config:\n%s", second)
	}
	if !strings.Contains(out.String(), "Already exists") {
		t.Errorf("init should say the file already exists, got %q", out.String())
	}
}

func TestInitIsARegisteredVerb(t *testing.T) {
	root := newTestRoot()
	for _, c := range root.Commands() {
		if c.Name() == "init" {
			return
		}
	}
	t.Fatal("cfassist init must be a registered command — the installer greps `cfassist --help` for `  init ` before calling it")
}

func TestInitLeavesAnInvalidExistingConfigAlone(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	path := filepath.Join(home, ".cfassist", "config.yaml")
	if err := os.MkdirAll(filepath.Dir(path), 0755); err != nil {
		t.Fatal(err)
	}
	bogus := []byte("{{not yaml")
	if err := os.WriteFile(path, bogus, 0644); err != nil {
		t.Fatal(err)
	}

	root := newTestRoot()
	out := &bytes.Buffer{}
	root.SetOut(out)
	root.SetErr(io.Discard)
	root.SetArgs([]string{"init"})
	if err := root.Execute(); err != nil {
		t.Fatalf("init must not fail on an existing corrupt file: %v", err)
	}
	got, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(got, bogus) {
		t.Errorf("init rewrote a corrupt config:\n%s", got)
	}
	if !strings.Contains(out.String(), "Already exists") {
		t.Errorf("got %q", out.String())
	}
}

func TestHelpListsInitInTheShapeTheInstallerGreps(t *testing.T) {
	root := newTestRoot()
	out := &bytes.Buffer{}
	root.SetOut(out)
	root.SetErr(out)
	root.SetArgs([]string{"--help"})
	if err := root.Execute(); err != nil {
		t.Fatal(err)
	}
	if !regexp.MustCompile(`(?m)^  init `).Match(out.Bytes()) {
		t.Errorf("--help does not list init the way the installer greps:\n%s", out)
	}
}
