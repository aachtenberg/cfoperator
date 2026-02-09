package config

import (
	"os"
	"path/filepath"
	"testing"
)

func TestDefaults(t *testing.T) {
	cfg := Defaults()

	if cfg.LLM.Provider != "ollama" {
		t.Errorf("default provider = %q, want %q", cfg.LLM.Provider, "ollama")
	}
	if cfg.LLM.URL != "http://localhost:11434" {
		t.Errorf("default URL = %q, want %q", cfg.LLM.URL, "http://localhost:11434")
	}
	if cfg.LLM.Model != "llama3.2" {
		t.Errorf("default model = %q, want %q", cfg.LLM.Model, "llama3.2")
	}
	if cfg.LLM.Temperature != 0.7 {
		t.Errorf("default temperature = %f, want %f", cfg.LLM.Temperature, 0.7)
	}
	if cfg.LLM.ContextWindow != 8192 {
		t.Errorf("default context_window = %d, want %d", cfg.LLM.ContextWindow, 8192)
	}
	if cfg.Tools.Bash.Enabled != true {
		t.Error("default bash.enabled should be true")
	}
	if cfg.Tools.Bash.Timeout != 30 {
		t.Errorf("default bash.timeout = %d, want %d", cfg.Tools.Bash.Timeout, 30)
	}
	if cfg.Tools.ReadFile.MaxLines != 500 {
		t.Errorf("default read_file.max_lines = %d, want %d", cfg.Tools.ReadFile.MaxLines, 500)
	}
	if cfg.Memory.MaxConversations != 50 {
		t.Errorf("default max_conversations = %d, want %d", cfg.Memory.MaxConversations, 50)
	}
	if cfg.SystemPrompt == "" {
		t.Error("default system prompt should not be empty")
	}
}

func TestLoadMissing(t *testing.T) {
	// Loading a non-existent config should return defaults
	cfg, err := Load("/tmp/cfassist-test-nonexistent-12345.yaml")
	if err != nil {
		t.Fatalf("Load non-existent should not error: %v", err)
	}
	if cfg.LLM.Provider != "ollama" {
		t.Errorf("missing config provider = %q, want defaults", cfg.LLM.Provider)
	}
}

func TestLoadYAML(t *testing.T) {
	dir := t.TempDir()
	cfgPath := filepath.Join(dir, "config.yaml")

	yaml := `
llm:
  provider: openai
  url: https://api.example.com
  model: gpt-4o
  temperature: 0.3
  api_key: test-key-123
  context_window: 16384

tools:
  bash:
    enabled: false
    timeout: 60
  read_file:
    enabled: true
    max_lines: 1000

memory:
  directory: /tmp/cfassist-test-mem
  max_conversations: 100
`
	if err := os.WriteFile(cfgPath, []byte(yaml), 0644); err != nil {
		t.Fatal(err)
	}

	cfg, err := Load(cfgPath)
	if err != nil {
		t.Fatalf("Load failed: %v", err)
	}

	if cfg.LLM.Provider != "openai" {
		t.Errorf("provider = %q, want %q", cfg.LLM.Provider, "openai")
	}
	if cfg.LLM.URL != "https://api.example.com" {
		t.Errorf("url = %q, want %q", cfg.LLM.URL, "https://api.example.com")
	}
	if cfg.LLM.Model != "gpt-4o" {
		t.Errorf("model = %q, want %q", cfg.LLM.Model, "gpt-4o")
	}
	if cfg.LLM.Temperature != 0.3 {
		t.Errorf("temperature = %f, want %f", cfg.LLM.Temperature, 0.3)
	}
	if cfg.LLM.APIKey != "test-key-123" {
		t.Errorf("api_key = %q, want %q", cfg.LLM.APIKey, "test-key-123")
	}
	if cfg.LLM.ContextWindow != 16384 {
		t.Errorf("context_window = %d, want %d", cfg.LLM.ContextWindow, 16384)
	}
	if cfg.Tools.Bash.Enabled != false {
		t.Error("bash.enabled should be false")
	}
	if cfg.Tools.Bash.Timeout != 60 {
		t.Errorf("bash.timeout = %d, want %d", cfg.Tools.Bash.Timeout, 60)
	}
	if cfg.Tools.ReadFile.MaxLines != 1000 {
		t.Errorf("read_file.max_lines = %d, want %d", cfg.Tools.ReadFile.MaxLines, 1000)
	}
	if cfg.Memory.MaxConversations != 100 {
		t.Errorf("max_conversations = %d, want %d", cfg.Memory.MaxConversations, 100)
	}
}

func TestLoadEnvExpansion(t *testing.T) {
	dir := t.TempDir()
	cfgPath := filepath.Join(dir, "config.yaml")

	os.Setenv("CFASSIST_TEST_KEY", "secret-from-env")
	defer os.Unsetenv("CFASSIST_TEST_KEY")

	yaml := `
llm:
  provider: openai
  api_key: ${CFASSIST_TEST_KEY}
`
	if err := os.WriteFile(cfgPath, []byte(yaml), 0644); err != nil {
		t.Fatal(err)
	}

	cfg, err := Load(cfgPath)
	if err != nil {
		t.Fatalf("Load failed: %v", err)
	}

	if cfg.LLM.APIKey != "secret-from-env" {
		t.Errorf("api_key = %q, want %q", cfg.LLM.APIKey, "secret-from-env")
	}
}

func TestLoadPartialOverride(t *testing.T) {
	dir := t.TempDir()
	cfgPath := filepath.Join(dir, "config.yaml")

	// Only override model — everything else should stay as defaults
	yaml := `
llm:
  model: custom-model
`
	if err := os.WriteFile(cfgPath, []byte(yaml), 0644); err != nil {
		t.Fatal(err)
	}

	cfg, err := Load(cfgPath)
	if err != nil {
		t.Fatalf("Load failed: %v", err)
	}

	if cfg.LLM.Model != "custom-model" {
		t.Errorf("model = %q, want %q", cfg.LLM.Model, "custom-model")
	}
	// Other fields should retain defaults
	if cfg.LLM.Provider != "ollama" {
		t.Errorf("provider = %q, want default %q", cfg.LLM.Provider, "ollama")
	}
	if cfg.Tools.Bash.Enabled != true {
		t.Error("bash.enabled should default to true")
	}
}

func TestLoadInvalidYAML(t *testing.T) {
	dir := t.TempDir()
	cfgPath := filepath.Join(dir, "config.yaml")

	if err := os.WriteFile(cfgPath, []byte("{{invalid yaml"), 0644); err != nil {
		t.Fatal(err)
	}

	_, err := Load(cfgPath)
	if err == nil {
		t.Error("expected error for invalid YAML")
	}
}

func TestEnsureDirectories(t *testing.T) {
	dir := t.TempDir()
	cfg := Defaults()
	cfg.Context.Directory = filepath.Join(dir, "ctx")
	cfg.Memory.Directory = filepath.Join(dir, "mem")

	if err := EnsureDirectories(cfg); err != nil {
		t.Fatalf("EnsureDirectories failed: %v", err)
	}

	// Check directories were created
	if _, err := os.Stat(cfg.Context.Directory); os.IsNotExist(err) {
		t.Error("context directory was not created")
	}
	if _, err := os.Stat(cfg.Memory.Directory); os.IsNotExist(err) {
		t.Error("memory directory was not created")
	}
}

func TestExpandPath(t *testing.T) {
	home, _ := os.UserHomeDir()

	tests := []struct {
		input string
		want  string
	}{
		{"~/test", filepath.Join(home, "test")},
		{"/absolute/path", "/absolute/path"},
		{"relative/path", "relative/path"},
	}

	for _, tt := range tests {
		got := expandPath(tt.input)
		if got != tt.want {
			t.Errorf("expandPath(%q) = %q, want %q", tt.input, got, tt.want)
		}
	}
}
