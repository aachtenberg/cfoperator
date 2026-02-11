package config

import (
	"encoding/json"
	"os"
	"path/filepath"
	"regexp"
	"strings"

	"gopkg.in/yaml.v3"
)

// State holds persisted runtime state (e.g. last-used provider/model).
type State struct {
	Provider string `json:"provider,omitempty"`
	Model    string `json:"model,omitempty"`
}

func statePath() string {
	return filepath.Join(DefaultConfigDir(), "state.json")
}

// LoadState reads the persisted state file. Returns zero State if missing.
func LoadState() State {
	data, err := os.ReadFile(statePath())
	if err != nil {
		return State{}
	}
	var s State
	json.Unmarshal(data, &s)
	return s
}

// SaveState writes the current provider/model to the state file.
func SaveState(provider, model string) {
	s := State{Provider: provider, Model: model}
	data, _ := json.Marshal(s)
	os.WriteFile(statePath(), data, 0644)
}

var Version = "0.4.0"

type LLMConfig struct {
	Provider      string  `yaml:"provider"`
	URL           string  `yaml:"url"`
	Model         string  `yaml:"model"`
	Temperature   float64 `yaml:"temperature"`
	APIKey        string  `yaml:"api_key"`
	ContextWindow int     `yaml:"context_window"`
	Default       string  `yaml:"default"`
}

// ProviderConfig defines a named LLM provider in the providers map.
type ProviderConfig struct {
	Provider      string  `yaml:"provider"`
	URL           string  `yaml:"url"`
	Model         string  `yaml:"model"`
	Temperature   float64 `yaml:"temperature"`
	APIKey        string  `yaml:"api_key"`
	ContextWindow int     `yaml:"context_window"`
}

type ContextConfig struct {
	Directory string `yaml:"directory"`
	MaxTokens int    `yaml:"max_tokens"`
}

type MemoryConfig struct {
	Directory        string `yaml:"directory"`
	MaxConversations int    `yaml:"max_conversations"`
}

type BashToolConfig struct {
	Enabled bool `yaml:"enabled"`
	Timeout int  `yaml:"timeout"`
}

type ReadFileToolConfig struct {
	Enabled  bool `yaml:"enabled"`
	MaxLines int  `yaml:"max_lines"`
}

type ToolsConfig struct {
	Bash     BashToolConfig     `yaml:"bash"`
	ReadFile ReadFileToolConfig `yaml:"read_file"`
}

type Config struct {
	LLM          LLMConfig                 `yaml:"llm"`
	Providers    map[string]ProviderConfig  `yaml:"providers"`
	Context      ContextConfig              `yaml:"context"`
	Memory       MemoryConfig               `yaml:"memory"`
	Tools        ToolsConfig                `yaml:"tools"`
	SystemPrompt string                     `yaml:"system_prompt"`
}

// ResolveProvider returns the LLMConfig for a named provider.
// If name is empty, uses LLM.Default. If no providers map exists, returns
// the top-level LLM block (backward compatible).
func (c *Config) ResolveProvider(name string) LLMConfig {
	if name == "" {
		name = c.LLM.Default
	}

	if name != "" && len(c.Providers) > 0 {
		if p, ok := c.Providers[name]; ok {
			llm := LLMConfig{
				Provider:      p.Provider,
				URL:           p.URL,
				Model:         p.Model,
				APIKey:        p.APIKey,
				ContextWindow: p.ContextWindow,
			}
			// Use provider-level temperature, fall back to top-level default
			if p.Temperature != 0 {
				llm.Temperature = p.Temperature
			} else {
				llm.Temperature = c.LLM.Temperature
			}
			return llm
		}
	}

	// Fallback: use the top-level llm block
	return c.LLM
}

// DefaultProviderName returns the name of the active provider.
// Returns LLM.Default if set, otherwise "" (meaning top-level llm block).
func (c *Config) DefaultProviderName() string {
	if c.LLM.Default != "" {
		return c.LLM.Default
	}
	if len(c.Providers) > 0 {
		// Return first provider alphabetically as a fallback
		for name := range c.Providers {
			return name
		}
	}
	return ""
}

func DefaultConfigDir() string {
	home, _ := os.UserHomeDir()
	return filepath.Join(home, ".cfassist")
}

func DefaultConfigPath() string {
	return filepath.Join(DefaultConfigDir(), "config.yaml")
}

func Defaults() *Config {
	return &Config{
		LLM: LLMConfig{
			Provider:      "ollama",
			URL:           "http://localhost:11434",
			Model:         "llama3.2",
			Temperature:   0.7,
			ContextWindow: 8192,
		},
		Context: ContextConfig{
			Directory: filepath.Join(DefaultConfigDir(), "context"),
			MaxTokens: 8000,
		},
		Memory: MemoryConfig{
			Directory:        filepath.Join(DefaultConfigDir(), "memory"),
			MaxConversations: 50,
		},
		Tools: ToolsConfig{
			Bash:     BashToolConfig{Enabled: true, Timeout: 30},
			ReadFile: ReadFileToolConfig{Enabled: true, MaxLines: 500},
		},
		SystemPrompt: "You are cfassist, a helpful SRE and systems administration assistant " +
			"running in the user's terminal. You have access to tools for running " +
			"shell commands and reading files. Be concise and practical. Focus on " +
			"diagnosing issues, explaining errors, and suggesting fixes. When you " +
			"need to check something, use your tools rather than guessing.",
	}
}

// Load reads config from a YAML file, merges with defaults, and expands env vars.
func Load(configPath string) (*Config, error) {
	cfg := Defaults()

	if configPath == "" {
		configPath = DefaultConfigPath()
	}

	data, err := os.ReadFile(configPath)
	if err != nil {
		if os.IsNotExist(err) {
			// No config file — use defaults
			return cfg, nil
		}
		return nil, err
	}

	// Expand ${VAR} references before parsing
	expanded := expandEnvVars(string(data))

	// Parse YAML over defaults
	if err := yaml.Unmarshal([]byte(expanded), cfg); err != nil {
		return nil, err
	}

	// Expand ~ in directory paths
	cfg.Context.Directory = expandPath(cfg.Context.Directory)
	cfg.Memory.Directory = expandPath(cfg.Memory.Directory)

	return cfg, nil
}

// EnsureDirectories creates config, context, and memory dirs if they don't exist.
func EnsureDirectories(cfg *Config) error {
	dirs := []string{
		DefaultConfigDir(),
		cfg.Context.Directory,
		cfg.Memory.Directory,
	}
	for _, d := range dirs {
		if err := os.MkdirAll(d, 0755); err != nil {
			return err
		}
	}

	// Write default config if none exists
	path := DefaultConfigPath()
	if _, err := os.Stat(path); os.IsNotExist(err) {
		return writeDefaultConfig(path)
	}
	return nil
}

func expandEnvVars(s string) string {
	re := regexp.MustCompile(`\$\{([^}]+)\}`)
	return re.ReplaceAllStringFunc(s, func(match string) string {
		varName := match[2 : len(match)-1]
		return os.Getenv(varName)
	})
}

func expandPath(p string) string {
	if strings.HasPrefix(p, "~/") || p == "~" {
		home, _ := os.UserHomeDir()
		p = filepath.Join(home, p[2:])
	}
	return os.ExpandEnv(p)
}

func writeDefaultConfig(path string) error {
	content := `# cfassist configuration
# See: https://github.com/aachtenberg/cfoperator

llm:
  default: ollama      # which provider name to start with
  temperature: 0.7     # shared default

  # Legacy single-provider mode (used if no providers block):
  # provider: ollama
  # url: http://localhost:11434
  # model: llama3.2
  # context_window: 8192

# Named providers — switch with /use <name> in TUI
providers:
  ollama:
    provider: ollama
    url: http://localhost:11434
    model: llama3.2
    context_window: 8192
  # groq:
  #   provider: openai
  #   url: https://api.groq.com/openai/v1
  #   model: llama-3.3-70b-versatile
  #   api_key: ${GROQ_API_KEY}
  #   context_window: 131072
  # claude:
  #   provider: anthropic
  #   url: https://api.anthropic.com
  #   model: claude-sonnet-4-20250514
  #   api_key: ${ANTHROPIC_API_KEY}
  #   context_window: 200000

context:
  directory: ~/.cfassist/context
  max_tokens: 8000

memory:
  directory: ~/.cfassist/memory
  max_conversations: 50

tools:
  bash:
    enabled: true
    timeout: 30
  read_file:
    enabled: true
    max_lines: 500

# Override the default system prompt:
# system_prompt: |
#   You are a custom assistant...
`
	return os.WriteFile(path, []byte(content), 0644)
}
