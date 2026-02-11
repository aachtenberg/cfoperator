package tools

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	"github.com/aachtenberg/cfoperator/cfassist-go/internal/client"
	"github.com/aachtenberg/cfoperator/cfassist-go/internal/config"
	"github.com/aachtenberg/cfoperator/cfassist-go/internal/memory"
)

// Registry holds available tools and their execution functions.
type Registry struct {
	tools map[string]tool
}

type tool struct {
	schema  client.ToolSchema
	execute func(args map[string]any) map[string]any
	timeout int
}

// New creates a ToolRegistry from config.
func New(cfg *config.Config) *Registry {
	r := &Registry{tools: make(map[string]tool)}

	if cfg.Tools.Bash.Enabled {
		bashTimeout := cfg.Tools.Bash.Timeout
		r.tools["bash"] = tool{
			schema: client.ToolSchema{
				Type: "function",
				Function: client.ToolSchemaFunction{
					Name: "bash",
					Description: "Execute a shell command and return stdout, stderr, and exit code. " +
						"Use for checking system state, running diagnostics, reading logs, " +
						"querying APIs, network checks, and any system administration task. " +
						"For commands that need elevated privileges, use 'sudo -n' (non-interactive). " +
						"For package installs, set DEBIAN_FRONTEND=noninteractive.",
					Parameters: map[string]any{
						"type": "object",
						"properties": map[string]any{
							"command": map[string]any{
								"type":        "string",
								"description": "The shell command to execute",
							},
							"timeout": map[string]any{
								"type":        "integer",
								"description": fmt.Sprintf("Timeout in seconds (default %d). Use higher values for long-running commands like package installs.", bashTimeout),
							},
						},
						"required": []string{"command"},
					},
				},
			},
			execute: func(args map[string]any) map[string]any {
				return bashExecute(args, bashTimeout)
			},
			timeout: bashTimeout,
		}
	}

	if cfg.Tools.ReadFile.Enabled {
		maxLines := cfg.Tools.ReadFile.MaxLines
		r.tools["read_file"] = tool{
			schema: client.ToolSchema{
				Type: "function",
				Function: client.ToolSchemaFunction{
					Name: "read_file",
					Description: "Read the contents of a file and return it as text. " +
						"Use for reading configuration files, logs, scripts, or any text file.",
					Parameters: map[string]any{
						"type": "object",
						"properties": map[string]any{
							"path": map[string]any{
								"type":        "string",
								"description": "Absolute or relative path to the file",
							},
							"max_lines": map[string]any{
								"type":        "integer",
								"description": "Maximum number of lines to read (default 500)",
							},
						},
						"required": []string{"path"},
					},
				},
			},
			execute: func(args map[string]any) map[string]any {
				return readFileExecute(args, maxLines)
			},
		}
	}

	// search_memory — search past conversations
	memDir := cfg.Memory.Directory
	r.tools["search_memory"] = tool{
		schema: client.ToolSchema{
			Type: "function",
			Function: client.ToolSchemaFunction{
				Name: "search_memory",
				Description: "Search past conversations by keyword. Use when the user asks about " +
					"previous discussions, wants to recall something, or references a past topic.",
				Parameters: map[string]any{
					"type": "object",
					"properties": map[string]any{
						"query": map[string]any{
							"type":        "string",
							"description": "Keyword or phrase to search for in past conversations",
						},
						"max_results": map[string]any{
							"type":        "integer",
							"description": "Maximum number of matching messages to return (default 10)",
						},
					},
					"required": []string{"query"},
				},
			},
		},
		execute: func(args map[string]any) map[string]any {
			return searchMemoryExecute(args, memDir)
		},
	}

	// list_tools — describe available tools
	r.tools["list_tools"] = tool{
		schema: client.ToolSchema{
			Type: "function",
			Function: client.ToolSchemaFunction{
				Name: "list_tools",
				Description: "List all available tools with their descriptions. Use when the user " +
					"asks what you can do, what tools are available, or your capabilities.",
				Parameters: map[string]any{
					"type":       "object",
					"properties": map[string]any{},
				},
			},
		},
		execute: func(args map[string]any) map[string]any {
			return r.listToolsExecute()
		},
	}

	return r
}

// GetSchemas returns tool schemas in OpenAI function-calling format.
func (r *Registry) GetSchemas() []client.ToolSchema {
	var schemas []client.ToolSchema
	for _, t := range r.tools {
		schemas = append(schemas, t.schema)
	}
	return schemas
}

// Execute runs a tool by name with the given arguments.
func (r *Registry) Execute(name string, args map[string]any) map[string]any {
	t, ok := r.tools[name]
	if !ok {
		return map[string]any{"error": fmt.Sprintf("unknown tool: %s", name)}
	}
	return t.execute(args)
}

func bashExecute(args map[string]any, defaultTimeout int) map[string]any {
	command, _ := args["command"].(string)
	if command == "" {
		return map[string]any{"error": "no command provided"}
	}

	timeout := defaultTimeout
	if t, ok := args["timeout"].(float64); ok && int(t) > 0 {
		timeout = int(t)
	}

	ctx, cancel := context.WithTimeout(context.Background(), time.Duration(timeout)*time.Second)
	defer cancel()

	cmd := exec.CommandContext(ctx, "sh", "-c", command)
	cmd.Env = append(os.Environ(), "DEBIAN_FRONTEND=noninteractive")
	// Prevent commands from hanging on stdin (e.g. sudo password prompt)
	cmd.Stdin = nil
	var stdout, stderr strings.Builder
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr

	err := cmd.Run()
	exitCode := 0
	if err != nil {
		if ctx.Err() == context.DeadlineExceeded {
			return map[string]any{"error": fmt.Sprintf("command timed out after %ds", timeout)}
		}
		if exitErr, ok := err.(*exec.ExitError); ok {
			exitCode = exitErr.ExitCode()
		} else {
			return map[string]any{"error": err.Error()}
		}
	}

	return map[string]any{
		"stdout":    stdout.String(),
		"stderr":    stderr.String(),
		"exit_code": exitCode,
	}
}

func readFileExecute(args map[string]any, defaultMaxLines int) map[string]any {
	pathStr, _ := args["path"].(string)
	if pathStr == "" {
		return map[string]any{"error": "no path provided"}
	}

	maxLines := defaultMaxLines
	if ml, ok := args["max_lines"].(float64); ok {
		maxLines = int(ml)
	}

	// Expand ~
	if strings.HasPrefix(pathStr, "~/") {
		home, _ := os.UserHomeDir()
		pathStr = filepath.Join(home, pathStr[2:])
	}

	info, err := os.Stat(pathStr)
	if err != nil {
		if os.IsNotExist(err) {
			return map[string]any{"error": fmt.Sprintf("file not found: %s", pathStr)}
		}
		return map[string]any{"error": err.Error()}
	}
	if info.IsDir() {
		return map[string]any{"error": fmt.Sprintf("not a file: %s", pathStr)}
	}

	data, err := os.ReadFile(pathStr)
	if err != nil {
		return map[string]any{"error": fmt.Sprintf("failed to read %s: %v", pathStr, err)}
	}

	lines := strings.Split(string(data), "\n")
	totalLines := len(lines)
	truncated := totalLines > maxLines

	if truncated {
		lines = lines[:maxLines]
	}

	result := map[string]any{
		"content": strings.Join(lines, "\n"),
		"lines":   totalLines,
	}
	if truncated {
		result["truncated"] = true
		result["showing"] = maxLines
	}
	return result
}

func searchMemoryExecute(args map[string]any, memDir string) map[string]any {
	query, _ := args["query"].(string)
	if query == "" {
		return map[string]any{"error": "no query provided"}
	}

	maxResults := 10
	if mr, ok := args["max_results"].(float64); ok {
		maxResults = int(mr)
	}

	results := memory.SearchConversations(memDir, query, maxResults)
	if len(results) == 0 {
		return map[string]any{
			"matches": 0,
			"message": fmt.Sprintf("No past conversations found matching '%s'.", query),
		}
	}

	return map[string]any{
		"matches": len(results),
		"results": results,
	}
}

func (r *Registry) listToolsExecute() map[string]any {
	var toolList []map[string]string
	for name, t := range r.tools {
		toolList = append(toolList, map[string]string{
			"name":        name,
			"description": t.schema.Function.Description,
		})
	}
	return map[string]any{
		"tools": toolList,
		"count": len(toolList),
	}
}

// MarshalResult serializes a tool result to JSON string for the LLM.
func MarshalResult(result map[string]any) string {
	b, err := json.Marshal(result)
	if err != nil {
		return fmt.Sprintf(`{"error": "marshal failed: %s"}`, err)
	}
	return string(b)
}
