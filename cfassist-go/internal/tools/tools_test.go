package tools

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/aachtenberg/cfoperator/cfassist-go/internal/config"
)

func newTestRegistry() *Registry {
	cfg := config.Defaults()
	cfg.Memory.Directory = os.TempDir()
	return New(cfg)
}

func TestRegistryHasDefaultTools(t *testing.T) {
	r := newTestRegistry()
	schemas := r.GetSchemas()

	expectedTools := map[string]bool{
		"bash":          false,
		"read_file":     false,
		"search_memory": false,
		"list_tools":    false,
	}

	for _, s := range schemas {
		name := s.Function.Name
		if _, ok := expectedTools[name]; ok {
			expectedTools[name] = true
		}
	}

	for name, found := range expectedTools {
		if !found {
			t.Errorf("tool %q not found in schemas", name)
		}
	}
}

func TestRegistryBashDisabled(t *testing.T) {
	cfg := config.Defaults()
	cfg.Tools.Bash.Enabled = false
	cfg.Memory.Directory = os.TempDir()
	r := New(cfg)

	result := r.Execute("bash", map[string]any{"command": "echo hi"})
	if _, ok := result["error"]; !ok {
		t.Error("disabled bash tool should return error")
	}
}

func TestRegistryReadFileDisabled(t *testing.T) {
	cfg := config.Defaults()
	cfg.Tools.ReadFile.Enabled = false
	cfg.Memory.Directory = os.TempDir()
	r := New(cfg)

	result := r.Execute("read_file", map[string]any{"path": "/etc/hostname"})
	if _, ok := result["error"]; !ok {
		t.Error("disabled read_file tool should return error")
	}
}

func TestExecuteUnknownTool(t *testing.T) {
	r := newTestRegistry()
	result := r.Execute("nonexistent", map[string]any{})
	errMsg, ok := result["error"].(string)
	if !ok {
		t.Fatal("expected error string")
	}
	if errMsg != "unknown tool: nonexistent" {
		t.Errorf("error = %q", errMsg)
	}
}

func TestBashExecuteSimple(t *testing.T) {
	result := bashExecute(map[string]any{"command": "echo hello"}, 30)

	stdout, ok := result["stdout"].(string)
	if !ok {
		t.Fatal("expected stdout string")
	}
	if stdout != "hello\n" {
		t.Errorf("stdout = %q, want %q", stdout, "hello\n")
	}

	exitCode, ok := result["exit_code"].(int)
	if !ok {
		t.Fatal("expected exit_code int")
	}
	if exitCode != 0 {
		t.Errorf("exit_code = %d, want 0", exitCode)
	}
}

func TestBashExecuteEmptyCommand(t *testing.T) {
	result := bashExecute(map[string]any{"command": ""}, 30)
	if _, ok := result["error"]; !ok {
		t.Error("empty command should return error")
	}
}

func TestBashExecuteNoCommand(t *testing.T) {
	result := bashExecute(map[string]any{}, 30)
	if _, ok := result["error"]; !ok {
		t.Error("missing command should return error")
	}
}

func TestBashExecuteNonZeroExit(t *testing.T) {
	result := bashExecute(map[string]any{"command": "exit 42"}, 30)

	exitCode, ok := result["exit_code"].(int)
	if !ok {
		t.Fatal("expected exit_code")
	}
	if exitCode != 42 {
		t.Errorf("exit_code = %d, want 42", exitCode)
	}
}

func TestBashExecuteStderr(t *testing.T) {
	result := bashExecute(map[string]any{"command": "echo error >&2"}, 30)

	stderr, ok := result["stderr"].(string)
	if !ok {
		t.Fatal("expected stderr string")
	}
	if stderr != "error\n" {
		t.Errorf("stderr = %q, want %q", stderr, "error\n")
	}
}

func TestBashExecuteWrapsSSHInBatchMode(t *testing.T) {
	dir := t.TempDir()
	sshPath := filepath.Join(dir, "ssh")
	if err := os.WriteFile(sshPath, []byte("#!/bin/sh\nprintf '%s|' \"$@\"\n"), 0755); err != nil {
		t.Fatalf("write fake ssh: %v", err)
	}

	t.Setenv("PATH", dir+string(os.PathListSeparator)+os.Getenv("PATH"))

	result := bashExecute(map[string]any{"command": "ssh example.com"}, 30)

	stdout, ok := result["stdout"].(string)
	if !ok {
		t.Fatal("expected stdout string")
	}
	if !strings.Contains(stdout, "-o|BatchMode=yes|example.com|") {
		t.Fatalf("stdout = %q, want ssh batch mode args", stdout)
	}
}

func TestBashExecuteTimeout(t *testing.T) {
	result := bashExecute(map[string]any{
		"command": "sleep 10",
		"timeout": float64(1),
	}, 30)

	errMsg, ok := result["error"].(string)
	if !ok {
		t.Fatal("expected error for timed-out command")
	}
	if errMsg == "" {
		t.Error("error should not be empty")
	}
}

func TestReadFileExecute(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "test.txt")
	os.WriteFile(path, []byte("line1\nline2\nline3"), 0644)

	result := readFileExecute(map[string]any{"path": path}, 500)

	content, ok := result["content"].(string)
	if !ok {
		t.Fatal("expected content string")
	}
	if content != "line1\nline2\nline3" {
		t.Errorf("content = %q", content)
	}

	lines, ok := result["lines"].(int)
	if !ok {
		t.Fatal("expected lines int")
	}
	if lines != 3 {
		t.Errorf("lines = %d, want 3", lines)
	}
}

func TestReadFileExecuteTruncated(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "big.txt")

	var content string
	for i := 0; i < 100; i++ {
		content += "line of text\n"
	}
	os.WriteFile(path, []byte(content), 0644)

	result := readFileExecute(map[string]any{"path": path}, 10)

	truncated, ok := result["truncated"].(bool)
	if !ok || !truncated {
		t.Error("expected truncated = true")
	}

	showing, ok := result["showing"].(int)
	if !ok || showing != 10 {
		t.Errorf("showing = %v, want 10", result["showing"])
	}
}

func TestReadFileExecuteNotFound(t *testing.T) {
	result := readFileExecute(map[string]any{"path": "/tmp/nonexistent-12345.txt"}, 500)
	if _, ok := result["error"]; !ok {
		t.Error("expected error for missing file")
	}
}

func TestReadFileExecuteDirectory(t *testing.T) {
	result := readFileExecute(map[string]any{"path": os.TempDir()}, 500)
	if _, ok := result["error"]; !ok {
		t.Error("expected error when reading a directory")
	}
}

func TestReadFileExecuteEmptyPath(t *testing.T) {
	result := readFileExecute(map[string]any{"path": ""}, 500)
	if _, ok := result["error"]; !ok {
		t.Error("expected error for empty path")
	}
}

func TestReadFileExecuteHomeTilde(t *testing.T) {
	// Create a file in home dir to test ~ expansion
	home, _ := os.UserHomeDir()
	testFile := filepath.Join(home, ".cfassist-test-tilde-expansion")
	os.WriteFile(testFile, []byte("test"), 0644)
	defer os.Remove(testFile)

	result := readFileExecute(map[string]any{"path": "~/.cfassist-test-tilde-expansion"}, 500)
	if _, ok := result["error"]; ok {
		t.Errorf("~ expansion should work, got error: %v", result["error"])
	}
}

func TestListToolsExecute(t *testing.T) {
	r := newTestRegistry()
	result := r.listToolsExecute()

	count, ok := result["count"].(int)
	if !ok {
		t.Fatal("expected count int")
	}
	if count < 4 {
		t.Errorf("expected at least 4 tools, got %d", count)
	}

	tools, ok := result["tools"].([]map[string]string)
	if !ok {
		t.Fatal("expected tools slice")
	}
	if len(tools) != count {
		t.Errorf("tools slice length %d != count %d", len(tools), count)
	}

	// Verify each tool has name and description
	for _, tool := range tools {
		if tool["name"] == "" {
			t.Error("tool name should not be empty")
		}
		if tool["description"] == "" {
			t.Error("tool description should not be empty")
		}
	}
}

func TestMarshalResult(t *testing.T) {
	result := map[string]any{
		"stdout":    "hello\n",
		"exit_code": 0,
	}

	s := MarshalResult(result)
	if s == "" {
		t.Error("MarshalResult should not return empty string")
	}
	if s[0] != '{' {
		t.Errorf("MarshalResult should return JSON, got %q", s)
	}
}
