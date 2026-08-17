package main

import (
	"errors"
	"fmt"
	"os"
	"strings"

	"github.com/aachtenberg/cfoperator/cfassist-go/internal/cfoperator"
	"github.com/aachtenberg/cfoperator/cfassist-go/internal/client"
	"github.com/aachtenberg/cfoperator/cfassist-go/internal/config"
	cfcontext "github.com/aachtenberg/cfoperator/cfassist-go/internal/context"
	"github.com/aachtenberg/cfoperator/cfassist-go/internal/tools"
	"github.com/aachtenberg/cfoperator/cfassist-go/internal/tui"
	"github.com/spf13/cobra"
)

var flagAttachPrint bool

// newAttachCmd builds `cfassist attach <investigation-id> [question]`.
//
// This is the verb every Slack, Discord and ntfy notification advertises (see
// event_runtime/notifications.py). It has to exist in the *released binary*,
// because that binary is what an operator has on the laptop where they paste
// the line — which is why mcp_server/tests/test_mcp_recipe.py asserts the
// command the notification prints is the command this file registers.
func newAttachCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "attach <investigation-id> [question]",
		Short: "Brief this session on a CFOperator investigation",
		Long: `Fetch a CFOperator investigation — its trigger, operator triage notes,
findings, linked remediation queue rows and related knowledge base learnings —
render a briefing, and start a session already seeded with it.

The investigation reference is what people actually paste: 1889, #1889, or a
console URL ending in the id.

With --print the briefing is written to stdout and nothing is started, so it can
be piped into whatever agent the operator actually drives.

Pass a trailing question to get a one-shot answer against the briefing instead
of an interactive session.

Read-only: attach makes GET requests and nothing else. Approving or rejecting a
remediation happens in the console or through the MCP server.

The CFOperator address is --agent-url (or CFOP_AGENT_URL / cfoperator.url in the
config). The global --url flag is the *LLM* endpoint and does not point here —
"attach --url http://127.0.0.1:8083" would send prompts to the agent as if it
were a model server, which is a natural thing to type against a port-forward.`,
		Args:         cobra.MinimumNArgs(1),
		SilenceUsage: true,
		RunE:         runAttach,
	}
	// Distinct from the global --url on purpose. The two addresses are
	// different services and conflating them fails confusingly rather than
	// loudly: the LLM client would just get HTTP errors from the agent.
	cmd.Flags().String("agent-url", "",
		"CFOperator agent URL (default: CFOP_AGENT_URL, or cfoperator.url from config)")
	cmd.Flags().BoolVar(&flagAttachPrint, "print", false,
		"Render the briefing to stdout and exit without starting a session")
	return cmd
}

func runAttach(cmd *cobra.Command, args []string) error {
	investigationID, err := cfoperator.ParseInvestigationRef(args[0])
	if err != nil {
		return err
	}
	question := strings.TrimSpace(strings.Join(args[1:], " "))

	cfg, err := config.Load(flagConfig)
	if err != nil {
		return fmt.Errorf("config: %w", err)
	}

	// --- fetch and render ---------------------------------------------------

	// An explicit --agent-url beats the config file, which already beats the
	// environment (ResolveEndpoint's own precedence). Passed as the config
	// value rather than as a fourth source so there is one precedence rule.
	agentURL := cfg.CFOperator.URL
	if flagAgentURL, _ := cmd.Flags().GetString("agent-url"); strings.TrimSpace(flagAgentURL) != "" {
		agentURL = strings.TrimSpace(flagAgentURL)
	}

	url, token, timeout := cfoperator.ResolveEndpoint(
		agentURL, cfg.CFOperator.Token, cfg.CFOperator.Timeout, os.Getenv,
	)
	api := cfoperator.New(url, token, timeout)

	attachCtx, err := api.CollectAttachContext(investigationID, 5, 200)
	if err != nil {
		return formatAPIError(err)
	}
	briefing := cfoperator.BuildBriefing(attachCtx, 4000)

	// --print is deliberately checked before any LLM setup: the briefing is the
	// product, and cfassist is only the first vehicle for it. An operator piping
	// this into another agent should not need a reachable LLM to get it.
	if flagAttachPrint {
		fmt.Println(briefing)
		return nil
	}

	// --- seed a session -----------------------------------------------------

	if err := config.EnsureDirectories(cfg); err != nil {
		return fmt.Errorf("directories: %w", err)
	}

	llm, activeProvider, err := resolveLLM(cfg)
	if err != nil {
		return err
	}
	if err := llm.CheckConnection(); err != nil {
		return fmt.Errorf("%v\n  hint: Is the LLM server running?", err)
	}

	toolReg := tools.New(cfg)
	contextText, contextCount := cfcontext.LoadDirectory(
		cfg.Context.Directory, cfg.Context.MaxTokens*4,
	)

	systemPrompt := cfg.SystemPrompt
	if contextText != "" {
		systemPrompt += "\n\n--- Environment Context ---\n" +
			"The following files describe the user's environment. " +
			"Use this information when answering questions.\n\n" +
			contextText
	}
	// The guidance goes above the briefing so the model reads "this is a
	// snapshot, and you cannot act on CFOperator from here" before it reads
	// anything that might tempt it to do either.
	systemPrompt += "\n\n--- CFOperator Investigation ---\n" +
		cfoperator.AttachGuidance + "\n" + briefing

	// Show the operator the same briefing the model got. They are about to ask
	// questions against it; hiding it would make the session's answers
	// unauditable.
	fmt.Println(briefing)

	if question != "" {
		return runNonInteractive(cfg, llm, toolReg, systemPrompt, question)
	}

	result, err := tui.Run(cfg, llm, toolReg, systemPrompt, contextCount, cfg.Providers, activeProvider)
	if err != nil {
		return err
	}
	config.SaveState(result.Provider, result.Model)
	return nil
}

// formatAPIError renders a CFOperator error with its operator-facing hint on a
// second line. Most failures here are configuration — no token, wrong URL, agent
// not port-forwarded — and the hint is the actual fix.
func formatAPIError(err error) error {
	var apiErr *cfoperator.Error
	if errors.As(err, &apiErr) && apiErr.Hint != "" {
		return fmt.Errorf("%s\n  hint: %s", apiErr.Message, apiErr.Hint)
	}
	return err
}

// resolveLLM builds the LLM client from config plus the persisted/flag-selected
// provider. Shared by the root command and attach so a seeded session honours
// the same --provider/--model/--url resolution as a plain one.
func resolveLLM(cfg *config.Config) (*client.LLMClient, string, error) {
	activeProvider := flagProvider
	if activeProvider == "" {
		if saved := config.LoadState(); saved.Provider != "" {
			if _, ok := cfg.Providers[saved.Provider]; ok {
				activeProvider = saved.Provider
			}
		}
	}
	if activeProvider == "" {
		activeProvider = cfg.DefaultProviderName()
	}
	resolved := cfg.ResolveProvider(activeProvider)

	if flagModel == "" {
		if saved := config.LoadState(); saved.Model != "" && saved.Provider == activeProvider {
			resolved.Model = saved.Model
		}
	}
	if flagModel != "" {
		resolved.Model = flagModel
	}
	if flagURL != "" {
		resolved.URL = flagURL
	}

	llm := client.New(
		resolved.Provider,
		resolved.URL,
		resolved.Model,
		resolved.Temperature,
		resolved.APIKey,
	)
	cfg.LLM.ContextWindow = resolved.ContextWindow
	return llm, activeProvider, nil
}
