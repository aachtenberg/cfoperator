package main

import (
	"bytes"
	"context"
	"strings"
	"testing"
	"time"

	"github.com/aachtenberg/cfoperator/cfassist-go/internal/config"
	"github.com/aachtenberg/cfoperator/cfassist-go/internal/tools"
)

// registry is a default registry with a scratch memory directory.
func registry(t *testing.T) *tools.Registry {
	t.Helper()
	cfg := config.Defaults()
	cfg.Memory.Directory = t.TempDir()
	return tools.New(cfg)
}

// hasBash reports whether the registry offers bash.
func hasBash(reg *tools.Registry) bool {
	for _, s := range reg.GetSchemas() {
		if s.Function.Name == "bash" {
			return true
		}
	}
	return false
}

// TestPipedInputGetsNoShell checks a pipe removes bash.
func TestPipedInputGetsNoShell(t *testing.T) {
	reg := registry(t)
	shellPolicy(reg, true, nil)
	if hasBash(reg) {
		t.Fatal("a pipe must not offer bash: the content did not come from the operator")
	}
}

// TestATerminalKeepsBashBehindTheGate checks a terminal keeps bash and a write asks.
func TestATerminalKeepsBashBehindTheGate(t *testing.T) {
	reg := registry(t)
	asked := 0
	shellPolicy(reg, false, func(ctx context.Context, command, reason string) tools.Decision {
		asked++
		return tools.Deny
	})
	if !hasBash(reg) {
		t.Fatal("a terminal session keeps its shell")
	}
	res := reg.Execute(context.Background(), "bash", map[string]any{"command": "rm -rf /tmp/never"})
	if asked != 1 {
		t.Fatalf("the write was not put to the operator (asked %d)", asked)
	}
	if msg, _ := res["error"].(string); !strings.Contains(msg, "not run") {
		t.Fatalf("declined write must report not run: %v", res)
	}
}

// TestTerminalAskReadsOneLine maps each typed line to its decision.
func TestTerminalAskReadsOneLine(t *testing.T) {
	cases := map[string]tools.Decision{
		"y\n": tools.Allow, "yes\n": tools.Allow, "Y\n": tools.Allow,
		"a\n": tools.AllowAll, "all\n": tools.AllowAll,
		"n\n": tools.Deny, "\n": tools.Deny, "whatever\n": tools.Deny,
	}
	for input, want := range cases {
		var out bytes.Buffer
		ask := terminalAsk(strings.NewReader(input), &out)
		got := ask(context.Background(), "systemctl restart nginx", "systemctl restart changes service state")
		if got != want {
			t.Errorf("input %q: got %v, want %v", input, got, want)
		}
		if !strings.Contains(out.String(), "systemctl restart nginx") || !strings.Contains(out.String(), "changes service state") {
			t.Errorf("the question must show the command and the reason:\n%s", out.String())
		}
	}
}

// TestTerminalAskHonoursACancelledTurn returns Deny when the context is cancelled.
func TestTerminalAskHonoursACancelledTurn(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	// A reader that never delivers a line: the answer must come from ctx.
	block, _ := newBlockingReader()
	done := make(chan tools.Decision, 1)
	go func() { done <- terminalAsk(block, &bytes.Buffer{})(ctx, "reboot", "reboot takes the host down") }()
	select {
	case got := <-done:
		if got != tools.Deny {
			t.Fatalf("cancelled turn answered %v, want Deny", got)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("terminalAsk did not return on a cancelled context")
	}
}

type blockingReader struct{ ch chan struct{} }

// newBlockingReader is a reader that never delivers a line until released.
func newBlockingReader() (*blockingReader, func()) {
	r := &blockingReader{ch: make(chan struct{})}
	return r, func() { close(r.ch) }
}

func (r *blockingReader) Read(p []byte) (int, error) {
	<-r.ch
	return 0, nil
}
