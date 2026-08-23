package conversation

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/aachtenberg/cfoperator/cfassist-go/internal/client"
	"github.com/aachtenberg/cfoperator/cfassist-go/internal/tools"
)

// Result holds the outcome of a conversation turn.
type Result struct {
	Response         string
	ToolCalls        int
	InputTokens      int
	OutputTokens     int
	LastPromptTokens int // tokens in the last prompt (= current context usage)
	Latency          time.Duration
	Error            string

	// Cancelled marks a turn the operator stopped. It is not an Error: the
	// tool calls already made and shown are real work, and reporting a
	// deliberate interrupt in red teaches operators not to use it.
	Cancelled bool
}

// Output is called during a conversation to report what's happening.
type Output interface {
	ShowThinking()
	ClearThinking()
	ShowToolCall(name string, args map[string]any)
	ShowToolResult(name string, result map[string]any)
	ShowResponse(text string)
	ShowError(message string, hint string)
	ShowWarning(message string)
}

// Run executes a conversation turn with tool-calling loop.
// Uses non-streaming for reliable tool call parsing with Ollama.
func Run(
	ctx context.Context,
	llm *client.LLMClient,
	toolReg *tools.Registry,
	output Output,
	messages []client.Message,
	systemPrompt string,
	maxIterations int,
) (Result, []client.Message) {
	if maxIterations <= 0 {
		maxIterations = 10
	}

	// Prepend system message
	fullMessages := make([]client.Message, 0, len(messages)+1)
	fullMessages = append(fullMessages, client.Message{Role: "system", Content: systemPrompt})
	fullMessages = append(fullMessages, messages...)

	toolSchemas := toolReg.GetSchemas()
	result := Result{}
	start := time.Now()

	for i := 0; i < maxIterations; i++ {
		// Checked at the top of every iteration, not only around the calls that
		// block: a turn that has been told to stop should not start a new round
		// of work no matter how quickly the last one returned.
		if ctx.Err() != nil {
			return cancelled(&result, start), fullMessages
		}

		output.ShowThinking()

		resp, err := chatWithRetry(ctx, llm, output, fullMessages, toolSchemas)
		output.ClearThinking()

		if err != nil {
			if ctx.Err() != nil {
				return cancelled(&result, start), fullMessages
			}
			output.ShowError(fmt.Sprintf("LLM request failed: %v", err), hintFor(err))
			result.Error = err.Error()
			result.Latency = time.Since(start)
			return result, fullMessages
		}

		result.InputTokens += resp.InputTokens
		result.OutputTokens += resp.OutputTokens
		result.LastPromptTokens = resp.InputTokens

		// Handle tool calls
		if len(resp.ToolCalls) > 0 {
			tc := resp.ToolCalls[0]
			toolName := tc.Function.Name
			toolArgs := tc.Function.Arguments

			output.ShowToolCall(toolName, toolArgs)
			toolResult := toolReg.Execute(ctx, toolName, toolArgs)
			output.ShowToolResult(toolName, toolResult)
			result.ToolCalls++

			// Append assistant message with tool call
			assistantMsg := client.Message{
				Role:      "assistant",
				ToolCalls: resp.ToolCalls,
			}
			if resp.Content != "" {
				assistantMsg.Content = resp.Content
			}
			fullMessages = append(fullMessages, assistantMsg)

			// Append tool result
			fullMessages = append(fullMessages, client.Message{
				Role:       "tool",
				Content:    tools.MarshalResult(toolResult),
				ToolCallID: tc.ID,
			})
			continue
		}

		// No tool calls — final response
		text := resp.Content
		if text != "" {
			output.ShowResponse(text)
		}
		result.Response = text
		result.Latency = time.Since(start)
		return result, fullMessages
	}

	// Max iterations reached
	output.ShowWarning(fmt.Sprintf("Reached maximum tool iterations (%d).", maxIterations))
	result.Latency = time.Since(start)
	return result, fullMessages
}

// maxLLMAttempts bounds a single Chat call, not the whole turn: each iteration
// of the tool loop gets its own budget, so a long investigation is not rationed
// by an earlier hiccup.
const maxLLMAttempts = 3

// retryBackoff is the base delay between attempts, doubling each time. Tests
// set it to zero; a human is waiting at a prompt, so the real values are short.
var retryBackoff = 500 * time.Millisecond

// maxRetryAfter caps how long a provider may park an interactive session.
//
// Retry-After is a number we did not choose, slept on the thread the operator
// is watching. A provider — or a proxy in front of one — asking for an hour
// would freeze the TUI with no way out but kill. We still make the attempt it
// asked for, just not on its schedule, and the warning says so.
var maxRetryAfter = 15 * time.Second

// chatWithRetry re-sends a request that failed in a way a second sample could
// fix — most often a provider refusing to parse the model's own tool call,
// which is nondeterministic and usually gone on the next try.
//
// Retries are announced rather than swallowed. An operator watching a turn take
// nine seconds is owed the difference between one slow model and three attempts,
// and a silent retry loop is how a degraded provider stays invisible until it
// fails outright.
func chatWithRetry(
	ctx context.Context,
	llm *client.LLMClient,
	output Output,
	messages []client.Message,
	toolSchemas []client.ToolSchema,
) (*client.Response, error) {
	for attempt := 1; ; attempt++ {
		resp, err := llm.Chat(ctx, messages, toolSchemas)
		if err == nil {
			return resp, nil
		}

		// A cancelled request surfaces as a transport failure, which Retryable
		// calls retryable — correctly, for a dropped connection. Here it would
		// mean answering the operator's stop with two more requests, so the
		// context is asked first and wins.
		if ctx.Err() != nil {
			return nil, err
		}

		var apiErr *client.APIError
		if !errors.As(err, &apiErr) || !apiErr.Retryable() || attempt >= maxLLMAttempts {
			return nil, err
		}

		// The provider's own guidance wins over our backoff when it sends any —
		// on a 429 it knows when it will answer and we are guessing.
		delay, asked := apiErr.RetryAfter, apiErr.RetryAfter
		if delay <= 0 {
			delay = retryBackoff << (attempt - 1)
		}
		if delay > maxRetryAfter {
			delay = maxRetryAfter
		}

		waited := ""
		if asked > delay {
			waited = fmt.Sprintf(" (asked for %s, waiting %s)", asked, delay)
		}

		output.ClearThinking()
		output.ShowWarning(fmt.Sprintf("%s%s — retrying (%d of %d)",
			apiErr.Summary(), waited, attempt+1, maxLLMAttempts))

		// Waiting out a backoff is still waiting. Sleeping through a cancel
		// would make Ctrl+C feel broken for up to the cap.
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-time.After(delay):
		}
		output.ShowThinking()
	}
}

// cancelled finalises a turn the operator stopped, keeping whatever the turn
// already produced.
func cancelled(result *Result, start time.Time) Result {
	result.Cancelled = true
	result.Latency = time.Since(start)
	return *result
}

// hintFor asks the provider error what an operator should do about it, falling
// back to a generic line for errors raised before a request was ever built.
func hintFor(err error) string {
	var apiErr *client.APIError
	if errors.As(err, &apiErr) {
		return apiErr.Hint()
	}
	return "Check the provider settings in ~/.cfassist/config.yaml."
}

// ParseToolArgs handles the case where arguments might be a JSON string.
func ParseToolArgs(args any) map[string]any {
	switch v := args.(type) {
	case map[string]any:
		return v
	case string:
		var m map[string]any
		if err := json.Unmarshal([]byte(v), &m); err != nil {
			return map[string]any{"raw": v}
		}
		return m
	default:
		return map[string]any{}
	}
}
