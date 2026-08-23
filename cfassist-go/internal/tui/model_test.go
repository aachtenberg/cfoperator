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

func TestTrimToUserBoundaryDoesNotSplitAToolRound(t *testing.T) {
	msgs := []client.Message{
		{Role: "user", Content: "first"},
		{Role: "assistant", ToolCalls: []client.ToolCall{{ID: "toolu_a"}}},
		{Role: "tool", ToolCallID: "toolu_a", Content: "a"},
		{Role: "tool", ToolCallID: "toolu_b", Content: "b"},
		{Role: "assistant", Content: "answer"},
		{Role: "user", Content: "next"},
	}
	got := trimToUserBoundary(msgs, 4)
	if got[0].Role != "user" {
		t.Fatalf("kept[0] role = %q, want user (cut landed inside a tool round)", got[0].Role)
	}
	if got[0].Content != "first" {
		t.Fatalf("kept[0] content = %q, want the user turn that opened the tool round", got[0].Content)
	}
	if len(got) != 6 {
		t.Fatalf("len = %d, want the whole transcript once snapped back to the opening user", len(got))
	}
}

func TestTrimToUserBoundaryKeepsASafeSuffix(t *testing.T) {
	msgs := []client.Message{
		{Role: "user", Content: "one"},
		{Role: "assistant", Content: "a1"},
		{Role: "user", Content: "two"},
		{Role: "assistant", Content: "a2"},
		{Role: "user", Content: "three"},
		{Role: "assistant", Content: "a3"},
	}
	got := trimToUserBoundary(msgs, 4)
	if got[0].Role != "user" || got[0].Content != "two" {
		t.Fatalf("suffix starts at %+v, want user two", got[0])
	}
}
