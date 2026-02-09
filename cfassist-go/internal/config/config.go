package config

import (
	"os"
	"path/filepath"
	"regexp"
	"strings"

	"gopkg.in/yaml.v3"
)

var Version = "0.2.0"

type LLMConfig struct {
	Provider    string  `yaml:"provider"`
	URL         string  `yaml:"url"`
	Model       string  `yaml:"model"`
	Temperature float64 `yaml:"temperature"`
	APIKey      string  `yaml:"api_key"`
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
	LLM          LLMConfig     `yaml:"llm"`
	Context      ContextConfig `yaml:"context"`
	Memory       MemoryConfig  `yaml:"memory"`
	Tools        ToolsConfig   `yaml:"tools"`
	SystemPrompt string        `yaml:"system_prompt"`
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
			Provider:    "ollama",
			URL:         "http://localhost:11434",
			Model:       "llama3.2",
			Temperature: 0.7,
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
  provider: ollama
  url: http://localhost:11434
  model: llama3.2
  temperature: 0.7

  # OpenAI-compatible provider example:
  # provider: openai
  # url: https://api.openai.com/v1
  # model: gpt-4o
  # api_key: ${OPENAI_API_KEY}

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
