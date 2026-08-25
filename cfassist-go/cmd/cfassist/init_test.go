package main

import (
	"bytes"
	"io"
	"os"
	"path/filepath"
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
	t.Fatal("cfassist init must be a registered command — the installer probes `help init` before calling it")
}
