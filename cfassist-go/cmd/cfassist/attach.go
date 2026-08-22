package main

import (
	"errors"
	"fmt"
	"os"
	"strings"
	"sync"
	"time"

	"github.com/aachtenberg/cfoperator/cfassist-go/internal/cfoperator"
	"github.com/aachtenberg/cfoperator/cfassist-go/internal/client"
	"github.com/aachtenberg/cfoperator/cfassist-go/internal/config"
	cfcontext "github.com/aachtenberg/cfoperator/cfassist-go/internal/context"
	"github.com/aachtenberg/cfoperator/cfassist-go/internal/tools"
	"github.com/aachtenberg/cfoperator/cfassist-go/internal/tui"
	"github.com/spf13/cobra"
)

var (
	flagAttachPrint bool
	flagAttachSpawn bool
)

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

The data plane is read-only: investigation/remediation/KB reads are GET-only in
the transport itself. Approving or rejecting a remediation happens in the
console or through the MCP server. The one write an interactive session makes
is to its own credential: it mints a short-lived session token on start and
revokes it on exit (see --session-ttl / --no-session-token).

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
	// Cockpit session token (CFOP-32): interactive sessions carry a credential
	// that dies with them instead of the operator's standing token.
	cmd.Flags().Duration("session-ttl", 4*time.Hour,
		"TTL of the per-session cockpit token (revoked on exit; expiry covers unclean exits)")
	cmd.Flags().Bool("no-session-token", false,
		"Do not mint a per-session cockpit token")
	cmd.Flags().Bool("remediate", false,
		"Request the remediate scope on the session token (your account's ceiling still applies)")
	// Cockpit tier 1 (CFOP-35): run the session on the affected infrastructure
	// instead of on this laptop.
	cmd.Flags().BoolVar(&flagAttachSpawn, "spawn", false,
		"Spawn an ephemeral cockpit on the affected infrastructure and attach to it (admin)")
	// Cockpit tiers 2/3 (CFOP-36): the agent picks the highest runtime the
	// affected host actually has. Forcing one that is not there is an error
	// rather than a quiet downgrade — someone who asks for the container
	// boundary must not silently get a process on the host instead.
	cmd.Flags().String("tier", "auto",
		"Cockpit runtime: auto|pod|container|host|ssh (--spawn only; fails if unavailable)")
	// The agent resolves the affected machine from the remediation rows and the
	// trigger text. That is a heuristic; this is the override for when it picks
	// the wrong box, which the operator can see and it cannot.
	cmd.Flags().String("host", "",
		"Spawn the cockpit on this host instead of the one resolved from the investigation (--spawn only)")
	// Session write-back (CFOP-37). Opt-OUT: the whole point is that what a
	// human works out in a session stops dying with the terminal, and a
	// default-off memory feature is one nobody remembers to turn on.
	cmd.Flags().Bool("no-writeback", false,
		"Do not record what this session concluded on the investigation")
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

	// Scaffold ~/.cfassist BEFORE talking to the agent (CFOP-63).
	//
	// EnsureDirectories is what writes the default config.yaml, and the config
	// is where an operator sets the agent URL. Running it after the fetch —
	// where it used to live — meant that a first run against an unreachable
	// agent died at the fetch and left nothing behind: no file to edit, and no
	// hint that a `cfoperator:` block exists. That is exactly the reported
	// path: install the binary, paste the Slack line, get a connection error,
	// have nowhere to put the URL.
	//
	// --print is excluded on purpose. docs/cockpit.md promises it "makes the
	// API calls and nothing else"; scaffolding a directory tree out of a pipe
	// would break that promise to solve a first-run problem a pipe does not
	// have. Everything that starts something — interactive, one-shot question,
	// --spawn — scaffolds first.
	if !flagAttachPrint {
		if err := config.EnsureDirectories(cfg); err != nil {
			return fmt.Errorf("directories: %w", err)
		}
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

	// --spawn takes over before any of the local work below: the pod fetches
	// its own briefing (with a token minted for it that this laptop never
	// sees), and seeding a local session as well would render the briefing
	// twice and mint a credential nothing needs.
	if flagAttachSpawn {
		return runSpawn(cmd, investigationID, url, token, question)
	}
	// --tier only means anything to the spawn: a plain attach runs the session
	// right here, and there is no ladder to climb. Silently ignoring it would
	// let someone believe they had asked for a container boundary.
	for _, flag := range []string{"tier", "host"} {
		if cmd.Flags().Changed(flag) {
			return fmt.Errorf("--%s only applies to --spawn: "+
				"a plain attach already runs the session on this machine", flag)
		}
	}

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

	llm, activeProvider, err := resolveLLM(cfg)
	if err != nil {
		return err
	}
	if err := llm.CheckConnection(); err != nil {
		return fmt.Errorf("%v\n  hint: Is the LLM server running?", err)
	}

	toolReg := tools.New(cfg)
	// The session gets the same read client the briefing came from, so it can
	// re-check the investigation instead of trusting a snapshot (CFOP-66). No
	// probe first: the fetch above already proved this agent is reachable and
	// that the token reads.
	toolReg.AddCFOperator(api)
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
		cfoperator.AttachGuidance + "\n" + cfoperator.ToolGuidance + "\n\n" + briefing

	// Show the operator the same briefing the model got. They are about to ask
	// questions against it; hiding it would make the session's answers
	// unauditable.
	//
	// This print is the primary-buffer copy: it is what a one-shot question or
	// a redirect to a file sees, and it is what remains on the terminal after
	// an interactive session exits. It is NOT what an interactive operator
	// reads during the session — the TUI runs on the alternate screen, which
	// hides it until exit. The copy that does the work there is the one seeded
	// into the scrollback via tui.Attachment below (CFOP-63).
	fmt.Println(briefing)

	if question != "" {
		return runNonInteractive(cfg, llm, toolReg, systemPrompt, question)
	}

	// What the status bar and the scrollback show for the life of the session.
	// The trigger is the "brief title": it is the alert or condition that
	// started the investigation, which is what an operator recognises.
	facts := cfoperator.InvestigationFacts(attachCtx.Investigation)
	attachment := &tui.Attachment{
		ID:       investigationID,
		Title:    facts.Trigger,
		Briefing: briefing,
	}

	// Cockpit session token (CFOP-32): interactive sessions get a credential
	// bound to this investigation that dies with the session — revoked on
	// clean exit below, expired by TTL on unclean ones. --print and one-shot
	// questions mint nothing: there is no session for a token to die with.
	// Minting is best-effort on purpose: against an agent that predates the
	// TTL mint the briefing is still the product, so we warn and continue.
	// Write-back needs the session's own credential, so the revoke cannot be a
	// plain defer any more: LIFO would fire it before the write. `revoke` is
	// captured here and called explicitly after the session ends, with the
	// deferred copy left only as the crash path.
	var revoke func()
	defer func() {
		if revoke != nil {
			revoke()
		}
	}()
	sessionToken := token

	if noTok, _ := cmd.Flags().GetBool("no-session-token"); !noTok {
		ttl, _ := cmd.Flags().GetDuration("session-ttl")
		scopes := []string{"investigate"}
		if rem, _ := cmd.Flags().GetBool("remediate"); rem {
			scopes = []string{"remediate"}
		}
		mint := cfoperator.NewSessionTokenClient(url, token, 30*time.Second)
		if sess, mintErr := mint.Mint(investigationID, scopes, ttl); mintErr != nil {
			fmt.Fprintf(os.Stderr, "warning: no session token: %v\n", mintErr)
		} else {
			// Point CFOP_API_TOKEN — the variable every client actually reads
			// (ResolveEndpoint, mcp_server) — at the dying credential, so the
			// session's children inherit it INSTEAD of the operator's standing
			// token. Exporting a second variable alongside would leave the
			// standing token on exactly the path this feature exists to shrink.
			// The mint client keeps the standing token on its struct, so the
			// revoke below still authenticates after the swap.
			restore := swapSessionToken(sess.Secret)
			// Same swap for this process's own client — the one the cfoperator
			// tool holds. Leaving it on the standing token would mean the
			// session's own reads outlived the credential minted for them.
			api.SetToken(sess.Secret)
			sessionToken = sess.Secret
			fmt.Printf("session token %s (%s…, scopes %s, TTL %s) — revoked on exit\n",
				sess.Label, sess.Prefix, strings.Join(scopes, ","), ttl)
			var once sync.Once
			revoke = func() {
				once.Do(func() {
					restore()
					api.SetToken(token)
					if revErr := mint.Revoke(sess.ID); revErr != nil {
						fmt.Fprintf(os.Stderr,
							"warning: could not revoke session token %s (it expires in %s anyway): %v\n",
							sess.Prefix, ttl, revErr)
					}
				})
			}
		}
	}

	started := time.Now()
	// The banner line is empty here on purpose: the briefing already opens the
	// scrollback and the status bar carries the investigation id, so a second
	// "cfoperator is at ..." line would be noise on the one screen that has
	// least room for it.
	result, err := tui.Run(cfg, llm, toolReg, systemPrompt, contextCount, cfg.Providers,
		activeProvider, attachment, "")
	if err != nil {
		return err
	}
	config.SaveState(result.Provider, result.Model)

	// Order matters and is therefore explicit: write back with the session's
	// own credential, THEN kill it. A deferred revoke would have run first.
	tier, host := writeBackTarget()
	writeBackSession(cmd, investigationID, url, sessionToken, llm, result.Messages,
		started, tier, host)
	if revoke != nil {
		revoke()
	}
	return nil
}

// swapSessionToken points CFOP_API_TOKEN at the minted session secret and
// returns the restore for the clean-exit defer. Overwrite, not augment: the
// point is that a child inspecting its environment finds the credential that
// dies with the session, and only that one.
func swapSessionToken(secret string) (restore func()) {
	prior, had := os.LookupEnv(cfoperator.EnvAPIToken)
	os.Setenv(cfoperator.EnvAPIToken, secret)
	return func() {
		if had {
			os.Setenv(cfoperator.EnvAPIToken, prior)
		} else {
			os.Unsetenv(cfoperator.EnvAPIToken)
		}
	}
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
