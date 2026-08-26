package config

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"

	"gopkg.in/yaml.v3"
)

// defaultSystemPrompt is the built-in SRE prompt. Unset system_prompt in
// config.yaml keeps this; the first-run file stubs it commented so an
// override is a copy rather than a guess.
const defaultSystemPrompt = "You are cfassist, a helpful SRE and systems administration assistant " +
	"running in the user's terminal. You have access to tools for running " +
	"shell commands and reading files. Be concise and practical. Focus on " +
	"diagnosing issues, explaining errors, and suggesting fixes. When you " +
	"need to check something, use your tools rather than guessing."

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

var Version = "0.13.3"

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

// SkillsConfig points at the operator's own playbooks.
//
// The nine that ship with cfassist are embedded in the binary (internal/skills)
// so a fresh install on a machine with no network has them. This directory is
// overlaid on top by name: drop in a SKILL.md to add a playbook, or to replace
// one of ours with your own.
type SkillsConfig struct {
	Directory string `yaml:"directory"`
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
	Bash      BashToolConfig     `yaml:"bash"`
	ReadFile  ReadFileToolConfig `yaml:"read_file"`
	Timescale TimescaleConfig    `yaml:"timescale"`
}

// TimescaleConfig registers the timescale_query tool when Host and Password
// are set. Same sensors DB the agent queries; cfassist reaches it from the
// LAN (the prod NodePort) rather than from inside the cluster.
type TimescaleConfig struct {
	Host     string `yaml:"host"`
	Port     int    `yaml:"port"`
	Database string `yaml:"database"`
	User     string `yaml:"user"`
	Password string `yaml:"password"`
}

// Configured is whether the timescale_query tool should be offered. A host
// with no password is the normal case — most machines cfassist runs on have
// no telemetry DB — and a tool that can only fail teaches the model to grep
// the disk for a binary that does not exist.
func (t TimescaleConfig) Configured() bool {
	return strings.TrimSpace(t.Host) != "" && t.Password != ""
}

// CFOperatorConfig points `cfassist attach` at a CFOperator agent API.
//
// Not a new credential mechanism: leaving both fields empty falls back to the
// CFOP_AGENT_URL / CFOP_API_TOKEN environment variables that mcp_server/client.py
// already reads, so a workstation set up for the MCP server needs nothing here.
// The token is the console's database-backed bearer (minted at
// /admin?tab=tokens); `read` scope is enough, since attach only makes GETs.
type CFOperatorConfig struct {
	URL     string  `yaml:"url"`
	Token   string  `yaml:"token"`
	Timeout float64 `yaml:"timeout"`

	// Discover controls the startup presence probe (CFOP-66): one short GET to
	// /api/health at the resolved address, so a session on a machine that runs
	// CFOperator knows the word names a service it can query rather than a Unix
	// user. Defaults to true; absent from the YAML it stays true, because an
	// unmarshal over Defaults() leaves untouched keys alone.
	Discover bool `yaml:"discover"`
}

type Config struct {
	LLM               LLMConfig                 `yaml:"llm"`
	Providers         map[string]ProviderConfig `yaml:"providers"`
	Context           ContextConfig             `yaml:"context"`
	Skills            SkillsConfig              `yaml:"skills"`
	Memory            MemoryConfig              `yaml:"memory"`
	Tools             ToolsConfig               `yaml:"tools"`
	CFOperator        CFOperatorConfig          `yaml:"cfoperator"`
	SystemPrompt      string                    `yaml:"system_prompt"`
	MaxToolIterations int                       `yaml:"max_tool_iterations"`
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
		Skills: SkillsConfig{
			Directory: filepath.Join(DefaultConfigDir(), "skills"),
		},
		Memory: MemoryConfig{
			Directory:        filepath.Join(DefaultConfigDir(), "memory"),
			MaxConversations: 50,
		},
		Tools: ToolsConfig{
			Bash:     BashToolConfig{Enabled: true, Timeout: 30},
			ReadFile: ReadFileToolConfig{Enabled: true, MaxLines: 500},
			Timescale: TimescaleConfig{
				Port:     5432,
				Database: "sensors",
				User:     "cfoperator_ro",
			},
		},
		CFOperator: CFOperatorConfig{
			Timeout:  30,
			Discover: true,
		},
		MaxToolIterations: 50,
		SystemPrompt:      defaultSystemPrompt,
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
	cfg.Skills.Directory = expandPath(cfg.Skills.Directory)
	cfg.Memory.Directory = expandPath(cfg.Memory.Directory)

	return cfg, nil
}

// EnsureDirectories creates config, context, and memory dirs if they don't exist.
func EnsureDirectories(cfg *Config) error {
	// Skills is created empty on purpose: an operator who wonders where their
	// own playbooks would go finds the answer already on disk.
	dirs := []string{
		DefaultConfigDir(),
		cfg.Context.Directory,
		cfg.Skills.Directory,
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
	const placeholder = "{{SYSTEM_PROMPT}}"
	content := strings.Replace(`# cfassist configuration
# Written on first run. Uncomment a stub to set it; ${ENV} is expanded at load.
# See: https://github.com/aachtenberg/cfoperator

llm:
  default: ollama           # which named provider to start with (/use to switch)
  temperature: 0.7          # shared default; a provider can override
  # api_key: ${OPENAI_API_KEY}  # unused by ollama; set per-provider for remote APIs

  # Legacy single-provider mode (used if the providers block is absent):
  # provider: ollama
  # url: http://localhost:11434
  # model: llama3.2
  # temperature: 0.7
  # context_window: 8192
  # api_key: ${OPENAI_API_KEY}

# Named providers — switch with /use <name> in the TUI
providers:
  ollama:
    provider: ollama
    url: http://localhost:11434
    model: llama3.2
    temperature: 0.7
    context_window: 8192
    # api_key:                  # ollama does not need one
  # groq:
  #   provider: openai
  #   url: https://api.groq.com/openai/v1
  #   model: llama-3.3-70b-versatile
  #   temperature: 0.7
  #   api_key: ${GROQ_API_KEY}
  #   context_window: 131072
  # xai:
  #   provider: openai           # same wire as groq; /v1 is added back after the strip
  #   url: https://api.x.ai/v1
  #   model: grok-4
  #   temperature: 0.7
  #   api_key: ${XAI_API_KEY}
  #   context_window: 131072
  # gemini:
  #   provider: gemini           # openai JSON, but Google serves it with no /v1 segment
  #   url: https://generativelanguage.googleapis.com/v1beta/openai
  #   model: gemini-3.6-flash
  #   temperature: 0.7
  #   api_key: ${GEMINI_API_KEY}
  #   context_window: 1048576
  # deepseek:
  #   provider: openai           # same wire as groq; /v1 is added back after the strip
  #   url: https://api.deepseek.com/v1
  #   model: deepseek-v4-pro
  #   temperature: 0.7
  #   api_key: ${DEEPSEEK_API_KEY}
  #   context_window: 1048576
  # claude:
  #   provider: anthropic
  #   url: https://api.anthropic.com
  #   model: claude-sonnet-4-20250514
  #   temperature: 0.7
  #   api_key: ${ANTHROPIC_API_KEY}
  #   context_window: 200000

context:
  directory: ~/.cfassist/context
  max_tokens: 8000              # cap on files pulled into the session

# Your own playbooks, overlaid by name on the nine built into the binary.
# /skills lists them; /skill <name> [target] loads one into the session.
skills:
  directory: ~/.cfassist/skills

memory:
  directory: ~/.cfassist/memory
  max_conversations: 50         # oldest sessions are dropped past this

tools:
  bash:
    enabled: true
    timeout: 30                 # seconds
  read_file:
    enabled: true
    max_lines: 500
  # timescale:                    # registers timescale_query; same sensors DB as the agent
  #   host: raspberrypi2          # NodePort on the prod cluster, reachable from the LAN
  #   port: 30433
  #   database: sensors
  #   user: cfoperator_ro
  #   password: ${TIMESCALE_RO_PASSWORD}

# CFOperator agent API — used by 'cfassist attach <investigation-id>'.
# url and token are optional: unset, they fall back to CFOP_AGENT_URL and
# CFOP_API_TOKEN, the same variables the MCP server reads. attach is read-only,
# so a token with 'read' scope is enough.
#
# url is the machine RUNNING CFOPERATOR, as reachable from here — a LAN address
# or hostname. It is NOT this workstation: attach exists to be run from
# somewhere else. Use a loopback address only when cfassist runs on the agent
# host itself, or through 'kubectl -n apps port-forward svc/cfoperator
# 8083:8083' on this machine.
cfoperator:
  # url: http://192.168.1.50:8083   # the agent host, not this machine
  # token: ${CFOP_API_TOKEN}
  timeout: 30                       # seconds
  discover: true                    # probe that address at startup so a
                                    # plain session knows a CFOperator is
                                    # there. Set false to skip.

max_tool_iterations: 50             # max tool calls per conversation turn

# Override the default system prompt (unset keeps the built-in SRE prompt):
# system_prompt: |
#   {{SYSTEM_PROMPT}}
`, placeholder, defaultSystemPrompt, 1)
	if strings.Contains(content, "{{") {
		return fmt.Errorf("unsubstituted placeholder in default config template")
	}
	return os.WriteFile(path, []byte(content), 0644)
}
