package main

import (
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"os/signal"
	"strings"
	"syscall"

	"github.com/aachtenberg/cfoperator/cfassist-go/internal/cfoperator"
	"github.com/aachtenberg/cfoperator/cfassist-go/internal/client"
	"github.com/aachtenberg/cfoperator/cfassist-go/internal/config"
	cfcontext "github.com/aachtenberg/cfoperator/cfassist-go/internal/context"
	"github.com/aachtenberg/cfoperator/cfassist-go/internal/conversation"
	"github.com/aachtenberg/cfoperator/cfassist-go/internal/memory"
	"github.com/aachtenberg/cfoperator/cfassist-go/internal/skills"
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

	// Persistent, not local: `attach` seeds a real session, so it has to honour
	// the same --config/--model/--url/--provider resolution as a plain run. Root
	// still parses them itself, so `cfassist --model m "question"` is unchanged.
	rootCmd.PersistentFlags().StringVar(&flagConfig, "config", "", "Path to config file")
	rootCmd.PersistentFlags().StringVar(&flagModel, "model", "", "Override LLM model")
	rootCmd.PersistentFlags().StringVar(&flagURL, "url", "", "Override LLM endpoint URL")
	rootCmd.PersistentFlags().StringVar(&flagProvider, "provider", "", "Select starting provider by name")
	rootCmd.Flags().BoolVar(&flagVersion, "version", false, "Show version")

	rootCmd.AddCommand(newAttachCmd())
	rootCmd.AddCommand(newInitCmd())

	// Ctrl+C cancels the work rather than only killing the process, so a
	// one-shot or piped run stops on the same key the TUI uses (CFOP-76).
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	// NotifyContext disables the default disposition for the whole process, so
	// without this a phase that never consumes ctx — the blocking stdin read in
	// pipe mode, the startup probe — would swallow Ctrl+C entirely and leave
	// SIGKILL as the only way out. Restoring the default after the first signal
	// keeps the guarantee that pressing it twice always works.
	go func() {
		<-ctx.Done()
		stop()
	}()

	if err := rootCmd.ExecuteContext(ctx); err != nil {
		// Already announced as "stopped." — printing it again as an error
		// would misrepresent a deliberate interrupt.
		if errors.Is(err, errInterrupted) {
			os.Exit(130)
		}
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

	// Resolve provider (CLI flag > saved state > config default) and build the
	// LLM client. Shared with `attach` — see resolveLLM in attach.go.
	llm, activeProvider, err := resolveLLM(cfg)
	if err != nil {
		return err
	}

	// Notice the CFOperator this machine is running next to (CFOP-66), while the
	// LLM connection is being checked. Two round trips to two different
	// services: running them in sequence would put a probe the operator did not
	// ask for in front of the prompt they did.
	presenceCh := make(chan cfoperator.Presence, 1)
	go func() {
		if !cfg.CFOperator.Discover {
			presenceCh <- cfoperator.Presence{}
			return
		}
		presenceCh <- cfoperator.DetectFromConfig(cfg.CFOperator.URL, cfg.CFOperator.Token, os.Getenv)
	}()

	// Check connection. The startup probe fails for the same reasons a chat
	// request does — a rejected key, a model that does not exist — so it asks
	// the error what to suggest rather than assuming a dead server.
	if err := llm.CheckConnection(); err != nil {
		var apiErr *client.APIError
		if errors.As(err, &apiErr) {
			return fmt.Errorf("%v\n  hint: %s", err, apiErr.Hint())
		}
		return fmt.Errorf("%v\n  hint: Is the LLM server running?", err)
	}

	presence := <-presenceCh

	// Create tool registry
	toolReg := tools.New(cfg)
	// The playbooks, for the model as well as for /skill. Loaded here rather
	// than shared with the TUI's copy: it is an embedded read plus a directory
	// scan, and one owner per consumer beats threading state through.
	toolReg.AddSkills(skills.Load(cfg.Skills.Directory))
	// The read tool only exists where there is something to read. On a machine
	// with no agent, a tool that can only fail teaches a model to route around
	// it — and cfassist is an SRE CLI first.
	if presence.Reachable {
		url, token, timeout := cfoperator.ResolveEndpoint(
			cfg.CFOperator.URL, cfg.CFOperator.Token, cfg.CFOperator.Timeout, os.Getenv,
		)
		api := cfoperator.New(url, token, timeout)
		api.URLFrom = cfoperator.AgentURLSource(cfg.CFOperator.URL, os.Getenv)
		toolReg.AddCFOperator(api)
	}

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
	// Unconditional, including when the probe found nothing or was turned off:
	// the identity half says what the *word* means, and on a machine with no
	// agent the right answer is still "no CFOperator is answering here" rather
	// than "there is no such user".
	systemPrompt += "\n\n--- CFOperator ---\n" + presence.PromptSection()

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

		return runNonInteractive(cmd.Context(), cfg, llm, toolReg, systemPrompt, userInput)
	}

	// --- One-shot mode ---
	if question != "" {
		return runNonInteractive(cmd.Context(), cfg, llm, toolReg, systemPrompt, question)
	}

	// --- TUI mode ---
	// nil attachment: a plain session has no investigation, and renders exactly
	// as it did before `attach` existed.
	result, err := tui.Run(cmd.Context(), cfg, llm, toolReg, systemPrompt, contextCount, cfg.Providers,
		activeProvider, nil, presence.BannerLine())
	if err != nil {
		return err
	}
	// Persist last-used provider/model for next session
	config.SaveState(result.Provider, result.Model)
	return nil
}

// errInterrupted marks a run the operator stopped. main turns it into the
// conventional signal exit rather than printing it: `cfassist check && deploy`
// must not treat an abandoned check as a passing one.
var errInterrupted = errors.New("interrupted")

func runNonInteractive(ctx context.Context, cfg *config.Config, llm *client.LLMClient, toolReg *tools.Registry, systemPrompt, question string) error {
	messages := []client.Message{
		{Role: "user", Content: question},
	}

	out := &consoleOutput{}
	result, msgs := conversation.Run(ctx, llm, toolReg, out, messages, systemPrompt, cfg.MaxToolIterations)

	// Save conversation to memory
	if len(msgs) > 0 {
		memory.SaveConversation(cfg.Memory.Directory, msgs)
		memory.Cleanup(cfg.Memory.Directory, cfg.Memory.MaxConversations)
	}

	if result.Cancelled {
		fmt.Fprintln(os.Stderr, "stopped.")
		return errInterrupted
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
