package config

import (
	"os"
	"path/filepath"
	"strings"
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
	if cfg.MaxToolIterations != 50 {
		t.Errorf("default max_tool_iterations = %d, want %d", cfg.MaxToolIterations, 50)
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

max_tool_iterations: 25
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
	if cfg.MaxToolIterations != 25 {
		t.Errorf("max_tool_iterations = %d, want %d", cfg.MaxToolIterations, 25)
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

// TestDefaultConfigOffersAReachableAgentExample guards the template an operator
// meets on their first run (CFOP-63).
//
// `attach` runs from wherever the operator is — that is its whole point — so a
// loopback example under `cfoperator:` is wrong everywhere except on the agent
// host itself, and it is wrong in the direction that costs the most time: it
// looks plausible, and produces a connection refused with no clue that the
// address is the problem.
func TestDefaultConfigOffersAReachableAgentExample(t *testing.T) {
	path := filepath.Join(t.TempDir(), "config.yaml")
	if err := writeDefaultConfig(path); err != nil {
		t.Fatalf("writeDefaultConfig: %v", err)
	}
	body, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read back: %v", err)
	}

	section := cfoperatorSection(t, string(body))
	if !strings.Contains(section, "url:") {
		t.Fatal("the cfoperator block no longer shows a url example to copy")
	}
	for _, line := range strings.Split(section, "\n") {
		if !strings.Contains(line, "url:") {
			continue
		}
		for _, loopback := range []string{"127.0.0.1", "localhost", "::1"} {
			if strings.Contains(line, loopback) {
				t.Errorf("the cfoperator.url example points at this machine (%q); "+
					"attach talks to the agent host", strings.TrimSpace(line))
			}
		}
	}
	if !strings.Contains(section, "CFOP_AGENT_URL") {
		t.Error("the block should name CFOP_AGENT_URL — it is the other way to set the address")
	}
}

// cfoperatorSection returns the config template's cfoperator block: everything
// from its heading comment to the next top-level key.
func cfoperatorSection(t *testing.T, content string) string {
	t.Helper()
	start := strings.Index(content, "# CFOperator agent API")
	if start < 0 {
		t.Fatal("the default config no longer mentions the CFOperator agent API; " +
			"a first run would have nothing to point at the agent")
	}
	rest := content[start:]
	if end := strings.Index(rest, "\nmax_tool_iterations"); end > 0 {
		rest = rest[:end]
	}
	return rest
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

func TestLoadProviders(t *testing.T) {
	dir := t.TempDir()
	cfgPath := filepath.Join(dir, "config.yaml")

	yaml := `
llm:
  default: groq
  temperature: 0.7

providers:
  ollama:
    provider: ollama
    url: http://localhost:11434
    model: qwen3:8b
    context_window: 8192
  groq:
    provider: openai
    url: https://api.groq.com/openai/v1
    model: llama-3.3-70b-versatile
    api_key: test-groq-key
    context_window: 131072
  claude:
    provider: anthropic
    url: https://api.anthropic.com
    model: claude-sonnet-4-20250514
    api_key: test-anthropic-key
    context_window: 200000
`
	if err := os.WriteFile(cfgPath, []byte(yaml), 0644); err != nil {
		t.Fatal(err)
	}

	cfg, err := Load(cfgPath)
	if err != nil {
		t.Fatalf("Load failed: %v", err)
	}

	if len(cfg.Providers) != 3 {
		t.Fatalf("expected 3 providers, got %d", len(cfg.Providers))
	}
	if cfg.LLM.Default != "groq" {
		t.Errorf("LLM.Default = %q, want %q", cfg.LLM.Default, "groq")
	}
	if cfg.Providers["claude"].Provider != "anthropic" {
		t.Errorf("claude provider = %q, want %q", cfg.Providers["claude"].Provider, "anthropic")
	}
	if cfg.Providers["groq"].APIKey != "test-groq-key" {
		t.Errorf("groq api_key = %q, want %q", cfg.Providers["groq"].APIKey, "test-groq-key")
	}
}

func TestResolveProviderNamed(t *testing.T) {
	cfg := &Config{
		LLM: LLMConfig{
			Default:     "groq",
			Temperature: 0.7,
		},
		Providers: map[string]ProviderConfig{
			"ollama": {Provider: "ollama", URL: "http://localhost:11434", Model: "qwen3:8b", ContextWindow: 8192},
			"groq":   {Provider: "openai", URL: "https://api.groq.com/openai/v1", Model: "llama-3.3-70b", APIKey: "key123", ContextWindow: 131072},
		},
	}

	// Resolve by explicit name
	resolved := cfg.ResolveProvider("groq")
	if resolved.Provider != "openai" {
		t.Errorf("resolved provider = %q, want %q", resolved.Provider, "openai")
	}
	if resolved.Model != "llama-3.3-70b" {
		t.Errorf("resolved model = %q, want %q", resolved.Model, "llama-3.3-70b")
	}
	if resolved.APIKey != "key123" {
		t.Errorf("resolved api_key = %q, want %q", resolved.APIKey, "key123")
	}
	if resolved.ContextWindow != 131072 {
		t.Errorf("resolved context_window = %d, want %d", resolved.ContextWindow, 131072)
	}
	// Temperature should fall back to top-level
	if resolved.Temperature != 0.7 {
		t.Errorf("resolved temperature = %f, want %f", resolved.Temperature, 0.7)
	}
}

func TestResolveProviderDefault(t *testing.T) {
	cfg := &Config{
		LLM: LLMConfig{
			Default:     "ollama",
			Temperature: 0.5,
		},
		Providers: map[string]ProviderConfig{
			"ollama": {Provider: "ollama", URL: "http://localhost:11434", Model: "llama3.2", ContextWindow: 8192},
		},
	}

	// Empty name should resolve using LLM.Default
	resolved := cfg.ResolveProvider("")
	if resolved.Provider != "ollama" {
		t.Errorf("resolved provider = %q, want %q", resolved.Provider, "ollama")
	}
	if resolved.Model != "llama3.2" {
		t.Errorf("resolved model = %q, want %q", resolved.Model, "llama3.2")
	}
}

func TestResolveProviderFallback(t *testing.T) {
	// No providers block — should fall back to top-level LLM block
	cfg := &Config{
		LLM: LLMConfig{
			Provider:      "ollama",
			URL:           "http://localhost:11434",
			Model:         "llama3.2",
			Temperature:   0.7,
			ContextWindow: 8192,
		},
	}

	resolved := cfg.ResolveProvider("")
	if resolved.Provider != "ollama" {
		t.Errorf("fallback provider = %q, want %q", resolved.Provider, "ollama")
	}
	if resolved.Model != "llama3.2" {
		t.Errorf("fallback model = %q, want %q", resolved.Model, "llama3.2")
	}
}

func TestResolveProviderUnknownName(t *testing.T) {
	cfg := &Config{
		LLM: LLMConfig{
			Provider: "ollama",
			URL:      "http://localhost:11434",
			Model:    "fallback-model",
		},
		Providers: map[string]ProviderConfig{
			"ollama": {Provider: "ollama", URL: "http://localhost:11434", Model: "qwen3:8b"},
		},
	}

	// Unknown name should fall back to top-level LLM block
	resolved := cfg.ResolveProvider("nonexistent")
	if resolved.Model != "fallback-model" {
		t.Errorf("unknown name should fallback, got model = %q", resolved.Model)
	}
}

func TestDefaultProviderName(t *testing.T) {
	// With LLM.Default set
	cfg := &Config{LLM: LLMConfig{Default: "groq"}}
	if name := cfg.DefaultProviderName(); name != "groq" {
		t.Errorf("DefaultProviderName = %q, want %q", name, "groq")
	}

	// Without LLM.Default but with providers
	cfg = &Config{
		Providers: map[string]ProviderConfig{
			"ollama": {Provider: "ollama"},
		},
	}
	name := cfg.DefaultProviderName()
	if name != "ollama" {
		t.Errorf("DefaultProviderName = %q, want %q", name, "ollama")
	}

	// No default, no providers
	cfg = &Config{}
	if name := cfg.DefaultProviderName(); name != "" {
		t.Errorf("DefaultProviderName = %q, want empty", name)
	}
}

func TestProviderTemperatureOverride(t *testing.T) {
	cfg := &Config{
		LLM: LLMConfig{Default: "custom", Temperature: 0.7},
		Providers: map[string]ProviderConfig{
			"custom": {Provider: "openai", URL: "http://example.com", Model: "test", Temperature: 0.3},
		},
	}

	resolved := cfg.ResolveProvider("custom")
	if resolved.Temperature != 0.3 {
		t.Errorf("provider-level temperature = %f, want %f", resolved.Temperature, 0.3)
	}
}

// Discovery is on unless it is turned off, and "absent from the file" is not
// "turned off" — an unmarshal over Defaults() leaves untouched keys alone, and
// that is the only reason a plain bool is safe here (CFOP-66).
func TestCFOperatorDiscoverDefaultsOn(t *testing.T) {
	if !Defaults().CFOperator.Discover {
		t.Error("discovery should default on")
	}

	dir := t.TempDir()
	path := filepath.Join(dir, "config.yaml")
	if err := os.WriteFile(path, []byte("cfoperator:\n  url: http://agent:8083\n"), 0644); err != nil {
		t.Fatalf("write config: %v", err)
	}
	cfg, err := Load(path)
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	if !cfg.CFOperator.Discover {
		t.Error("a cfoperator block that says nothing about discover must not disable it")
	}
	if cfg.CFOperator.URL != "http://agent:8083" {
		t.Errorf("url = %q", cfg.CFOperator.URL)
	}
}

func TestCFOperatorDiscoverCanBeTurnedOff(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "config.yaml")
	if err := os.WriteFile(path, []byte("cfoperator:\n  discover: false\n"), 0644); err != nil {
		t.Fatalf("write config: %v", err)
	}
	cfg, err := Load(path)
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	if cfg.CFOperator.Discover {
		t.Error("discover: false was ignored")
	}
}
