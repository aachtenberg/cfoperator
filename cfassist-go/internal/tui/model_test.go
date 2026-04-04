package tui

import (
	"strings"
	"testing"
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
