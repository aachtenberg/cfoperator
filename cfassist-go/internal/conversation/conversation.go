package conversation

import (
	"encoding/json"
	"fmt"
	"time"

	"github.com/aachtenberg/cfoperator/cfassist-go/internal/client"
	"github.com/aachtenberg/cfoperator/cfassist-go/internal/tools"
)

// Result holds the outcome of a conversation turn.
type Result struct {
	Response     string
	ToolCalls    int
	InputTokens  int
	OutputTokens int
	Latency      time.Duration
	Error        string
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
		output.ShowThinking()

		resp, err := llm.Chat(fullMessages, toolSchemas)
		output.ClearThinking()

		if err != nil {
			output.ShowError(
				fmt.Sprintf("LLM request failed: %v", err),
				fmt.Sprintf("Check connection: curl %s/api/tags", llm.URL),
			)
			result.Error = err.Error()
			result.Latency = time.Since(start)
			return result, fullMessages
		}

		result.InputTokens += resp.InputTokens
		result.OutputTokens += resp.OutputTokens

		// Handle tool calls
		if len(resp.ToolCalls) > 0 {
			tc := resp.ToolCalls[0]
			toolName := tc.Function.Name
			toolArgs := tc.Function.Arguments

			output.ShowToolCall(toolName, toolArgs)
			toolResult := toolReg.Execute(toolName, toolArgs)
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
				Role:    "tool",
				Content: tools.MarshalResult(toolResult),
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
