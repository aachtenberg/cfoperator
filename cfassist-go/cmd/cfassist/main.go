package main

import (
	"fmt"
	"io"
	"os"
	"strings"

	"github.com/aachtenberg/cfoperator/cfassist-go/internal/client"
	"github.com/aachtenberg/cfoperator/cfassist-go/internal/config"
	cfcontext "github.com/aachtenberg/cfoperator/cfassist-go/internal/context"
	"github.com/aachtenberg/cfoperator/cfassist-go/internal/conversation"
	"github.com/aachtenberg/cfoperator/cfassist-go/internal/memory"
	"github.com/aachtenberg/cfoperator/cfassist-go/internal/tools"
	"github.com/aachtenberg/cfoperator/cfassist-go/internal/tui"
	"github.com/spf13/cobra"
	"golang.org/x/term"
)

var (
	flagConfig   string
	flagModel    string
	flagURL      string
	flagProvider string
	flagVersion  bool
)

func main() {
	rootCmd := &cobra.Command{
		Use:   "cfassist [question]",
		Short: "CLI assistant for SRE and systems administration",
		Long: `cfassist — connect to an LLM (Ollama or OpenAI-compatible) and
ask questions, run tools, and troubleshoot infrastructure.

Run without arguments for interactive TUI mode.
Pass a question for one-shot mode.
Pipe data in for analysis mode.`,
		Args:                  cobra.ArbitraryArgs,
		SilenceUsage:          true,
		SilenceErrors:         true,
		DisableFlagsInUseLine: true,
		RunE:                  run,
	}

	rootCmd.Flags().StringVar(&flagConfig, "config", "", "Path to config file")
	rootCmd.Flags().StringVar(&flagModel, "model", "", "Override LLM model")
	rootCmd.Flags().StringVar(&flagURL, "url", "", "Override LLM endpoint URL")
	rootCmd.Flags().StringVar(&flagProvider, "provider", "", "Select starting provider by name")
	rootCmd.Flags().BoolVar(&flagVersion, "version", false, "Show version")

	if err := rootCmd.Execute(); err != nil {
		fmt.Fprintf(os.Stderr, "Error: %v\n", err)
		os.Exit(1)
	}
}

func run(cmd *cobra.Command, args []string) error {
	if flagVersion {
		fmt.Printf("cfassist %s\n", config.Version)
		return nil
	}

	// Load config
	cfg, err := config.Load(flagConfig)
	if err != nil {
		return fmt.Errorf("config: %w", err)
	}

	if err := config.EnsureDirectories(cfg); err != nil {
		return fmt.Errorf("directories: %w", err)
	}

	// Resolve which provider to use: CLI flag > saved state > config default
	activeProvider := flagProvider
	if activeProvider == "" {
		if saved := config.LoadState(); saved.Provider != "" {
			// Only use saved provider if it still exists in config
			if _, ok := cfg.Providers[saved.Provider]; ok {
				activeProvider = saved.Provider
			}
		}
	}
	if activeProvider == "" {
		activeProvider = cfg.DefaultProviderName()
	}
	resolved := cfg.ResolveProvider(activeProvider)

	// Apply saved model if same provider and no CLI override
	if flagModel == "" {
		if saved := config.LoadState(); saved.Model != "" && saved.Provider == activeProvider {
			resolved.Model = saved.Model
		}
	}
	// CLI overrides always win
	if flagModel != "" {
		resolved.Model = flagModel
	}
	if flagURL != "" {
		resolved.URL = flagURL
	}

	// Create LLM client
	llm := client.New(
		resolved.Provider,
		resolved.URL,
		resolved.Model,
		resolved.Temperature,
		resolved.APIKey,
	)
	// Update cfg.LLM.ContextWindow for context tracking
	cfg.LLM.ContextWindow = resolved.ContextWindow

	// Check connection
	if err := llm.CheckConnection(); err != nil {
		return fmt.Errorf("%v\n  hint: Is the LLM server running?", err)
	}

	// Create tool registry
	toolReg := tools.New(cfg)

	// Load context files
	contextText, contextCount := cfcontext.LoadDirectory(
		cfg.Context.Directory, cfg.Context.MaxTokens*4,
	)

	// Build system prompt with context
	systemPrompt := cfg.SystemPrompt
	if contextText != "" {
		systemPrompt += "\n\n--- Environment Context ---\n" +
			"The following files describe the user's environment. " +
			"Use this information when answering questions.\n\n" +
			contextText
	}

	// Join question args
	question := strings.Join(args, " ")

	// Detect pipe mode
	isPiped := !term.IsTerminal(int(os.Stdin.Fd()))

	// --- Pipe mode ---
	if isPiped {
		pipedData, err := io.ReadAll(os.Stdin)
		if err != nil {
			return fmt.Errorf("reading stdin: %w", err)
		}

		if question == "" {
			question = "Analyze the following input and describe what you see."
		}

		userInput := fmt.Sprintf(
			"The user has piped the following input:\n```\n%s\n```\n\n%s",
			strings.TrimSpace(string(pipedData)), question,
		)

		return runNonInteractive(cfg, llm, toolReg, systemPrompt, userInput)
	}

	// --- One-shot mode ---
	if question != "" {
		return runNonInteractive(cfg, llm, toolReg, systemPrompt, question)
	}

	// --- TUI mode ---
	result, err := tui.Run(cfg, llm, toolReg, systemPrompt, contextCount, cfg.Providers, activeProvider)
	if err != nil {
		return err
	}
	// Persist last-used provider/model for next session
	config.SaveState(result.Provider, result.Model)
	return nil
}

func runNonInteractive(cfg *config.Config, llm *client.LLMClient, toolReg *tools.Registry, systemPrompt, question string) error {
	messages := []client.Message{
		{Role: "user", Content: question},
	}

	out := &consoleOutput{}
	result, msgs := conversation.Run(llm, toolReg, out, messages, systemPrompt, cfg.MaxToolIterations)

	// Save conversation to memory
	if len(msgs) > 0 {
		memory.SaveConversation(cfg.Memory.Directory, msgs)
		memory.Cleanup(cfg.Memory.Directory, cfg.Memory.MaxConversations)
	}

	if result.Error != "" {
		return fmt.Errorf("conversation failed: %s", result.Error)
	}
	return nil
}

// consoleOutput implements conversation.Output for non-TUI mode.
type consoleOutput struct{}

func (o *consoleOutput) ShowThinking() {
	fmt.Print("\033[2m  thinking...\033[0m")
}

func (o *consoleOutput) ClearThinking() {
	fmt.Print("\r\033[K")
}

func (o *consoleOutput) ShowToolCall(name string, args map[string]any) {
	switch name {
	case "bash":
		cmd, _ := args["command"].(string)
		fmt.Printf("\033[2;36m[tool] bash:\033[0m %s\n", cmd)
	case "read_file":
		path, _ := args["path"].(string)
		fmt.Printf("\033[2;36m[tool] read_file:\033[0m %s\n", path)
	default:
		fmt.Printf("\033[2;36m[tool] %s:\033[0m %v\n", name, args)
	}
}

func (o *consoleOutput) ShowToolResult(name string, result map[string]any) {
	if errMsg, ok := result["error"]; ok {
		fmt.Printf("\033[2;31m[tool] error: %v\033[0m\n", errMsg)
		return
	}

	switch name {
	case "bash":
		stdout, _ := result["stdout"].(string)
		exitCode := 0
		if ec, ok := result["exit_code"].(int); ok {
			exitCode = ec
		}
		lines := len(strings.Split(stdout, "\n"))
		if stdout == "" {
			lines = 0
		}
		if exitCode == 0 {
			fmt.Printf("\033[2;32m[tool] %d lines | exit %d\033[0m\n", lines, exitCode)
		} else {
			fmt.Printf("\033[2;31m[tool] %d lines | exit %d\033[0m\n", lines, exitCode)
		}
	case "read_file":
		content, _ := result["content"].(string)
		lines := len(strings.Split(content, "\n"))
		if content == "" {
			lines = 0
		}
		fmt.Printf("\033[2;32m[tool] %d lines\033[0m\n", lines)
	default:
		fmt.Printf("\033[2m[tool] done\033[0m\n")
	}
}

func (o *consoleOutput) ShowResponse(text string) {
	fmt.Println(text)
}

func (o *consoleOutput) ShowError(message string, hint string) {
	fmt.Fprintf(os.Stderr, "\033[1;31m%s\033[0m\n", message)
	if hint != "" {
		fmt.Fprintf(os.Stderr, "\033[2m  %s\033[0m\n", hint)
	}
}

func (o *consoleOutput) ShowWarning(message string) {
	fmt.Fprintf(os.Stderr, "\033[33m%s\033[0m\n", message)
}
