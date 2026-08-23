package tui

import (
	"strings"
	"testing"

	"github.com/aachtenberg/cfoperator/cfassist-go/internal/client"
)

func TestFormatToolCallLine(t *testing.T) {
	line := formatToolCallLine("bash", "kubectl get pods")

	if !strings.HasPrefix(line, toolOutputIndent) {
		t.Fatalf("line = %q, want indented tool output", line)
	}
	if !strings.Contains(line, "-> bash:") {
		t.Fatalf("line = %q, want call indicator", line)
	}
	if !strings.Contains(line, "kubectl get pods") {
		t.Fatalf("line = %q, want tool detail", line)
	}
}

func TestFormatToolResultLine(t *testing.T) {
	tests := []struct {
		name     string
		detail   string
		isError  bool
		contains string
	}{
		{name: "bash", detail: "3 lines | exit 0", contains: "<- bash:"},
		{name: "bash", detail: "permission denied", isError: true, contains: "!! bash:"},
	}

	for _, tt := range tests {
		line := formatToolResultLine(tt.name, tt.detail, tt.isError)

		if !strings.HasPrefix(line, toolOutputIndent) {
			t.Fatalf("line = %q, want indented tool output", line)
		}
		if !strings.Contains(line, tt.contains) {
			t.Fatalf("line = %q, want %q", line, tt.contains)
		}
		if !strings.Contains(line, tt.detail) {
			t.Fatalf("line = %q, want detail %q", line, tt.detail)
		}
	}
}

func TestDropSystemMessages(t *testing.T) {
	in := []client.Message{
		{Role: "system", Content: "you are cfassist"},
		{Role: "user", Content: "hello"},
		{Role: "assistant", Content: "hi"},
		{Role: "tool", ToolCallID: "toolu_a", Content: "ok"},
	}
	out := dropSystemMessages(in)
	if len(out) != 3 {
		t.Fatalf("len = %d, want 3", len(out))
	}
	for _, m := range out {
		if m.Role == "system" {
			t.Fatal("system message should have been dropped")
		}
	}
}
