package client

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

// Message represents a chat message.
type Message struct {
	Role       string     `json:"role"`
	Content    string     `json:"content,omitempty"`
	ToolCalls  []ToolCall `json:"tool_calls,omitempty"`
	ToolCallID string     `json:"tool_call_id,omitempty"` // for tool result → tool_use_id reference
}

// ToolCall represents a tool call from the LLM.
type ToolCall struct {
	ID       string           `json:"id,omitempty"` // Anthropic tool_use_id
	Function ToolCallFunction `json:"function"`
}

// ToolCallFunction holds the function name and arguments.
type ToolCallFunction struct {
	Name      string         `json:"name"`
	Arguments map[string]any `json:"arguments"`
}

// Response is the parsed LLM response.
type Response struct {
	Content      string
	ToolCalls    []ToolCall
	Role         string
	InputTokens  int
	OutputTokens int
}

// ToolSchema is the OpenAI function-calling schema format.
type ToolSchema struct {
	Type     string             `json:"type"`
	Function ToolSchemaFunction `json:"function"`
}

// ToolSchemaFunction describes a tool's interface.
type ToolSchemaFunction struct {
	Name        string         `json:"name"`
	Description string         `json:"description"`
	Parameters  map[string]any `json:"parameters"`
}

// LLMClient talks to Ollama or OpenAI-compatible APIs.
type LLMClient struct {
	Provider    string
	URL         string
	Model       string
	Temperature float64
	APIKey      string
	HTTPClient  *http.Client
}

// New creates a new LLMClient from config values.
func New(provider, url, model string, temperature float64, apiKey string) *LLMClient {
	return &LLMClient{
		Provider:    provider,
		URL:         strings.TrimRight(url, "/"),
		Model:       model,
		Temperature: temperature,
		APIKey:      apiKey,
		HTTPClient:  &http.Client{Timeout: 120 * time.Second},
	}
}

// Chat sends a non-streaming chat request and returns the full response.
func (c *LLMClient) Chat(messages []Message, tools []ToolSchema) (*Response, error) {
	switch c.Provider {
	case "ollama":
		return c.ollamaChat(messages, tools)
	case "anthropic":
		return c.anthropicChat(messages, tools)
	default:
		return c.openaiChat(messages, tools)
	}
}

// CheckConnection tests if the LLM endpoint is reachable.
func (c *LLMClient) CheckConnection() error {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	if c.Provider == "anthropic" {
		return c.checkAnthropicConnection(ctx)
	}

	var url string
	if c.Provider == "ollama" {
		url = c.URL + "/api/tags"
	} else {
		url = c.URL + "/v1/models"
	}

	req, err := http.NewRequestWithContext(ctx, "GET", url, nil)
	if err != nil {
		return fmt.Errorf("failed to create request: %w", err)
	}

	if c.APIKey != "" {
		req.Header.Set("Authorization", "Bearer "+c.APIKey)
	}

	resp, err := c.HTTPClient.Do(req)
	if err != nil {
		return fmt.Errorf("connection failed: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("HTTP %d from %s", resp.StatusCode, url)
	}
	return nil
}

func (c *LLMClient) checkAnthropicConnection(ctx context.Context) error {
	// Anthropic has no list-models GET endpoint; send a minimal messages request
	payload := []byte(fmt.Sprintf(`{"model":"%s","max_tokens":1,"messages":[{"role":"user","content":"hi"}]}`, c.Model))
	req, err := http.NewRequestWithContext(ctx, "POST", c.URL+"/v1/messages", bytes.NewReader(payload))
	if err != nil {
		return fmt.Errorf("failed to create request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("x-api-key", c.APIKey)
	req.Header.Set("anthropic-version", "2023-06-01")

	resp, err := c.HTTPClient.Do(req)
	if err != nil {
		return fmt.Errorf("connection failed: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("HTTP %d from Anthropic: %s", resp.StatusCode, string(body))
	}
	return nil
}

// ListModels fetches available models from the provider.
func (c *LLMClient) ListModels() ([]string, error) {
	// Anthropic has no list-models API; return known models
	if c.Provider == "anthropic" {
		return []string{
			"claude-sonnet-4-20250514",
			"claude-haiku-4-20250414",
			"claude-opus-4-20250514",
		}, nil
	}

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	var url string
	if c.Provider == "ollama" {
		url = c.URL + "/api/tags"
	} else {
		url = c.URL + "/v1/models"
	}

	req, err := http.NewRequestWithContext(ctx, "GET", url, nil)
	if err != nil {
		return nil, err
	}
	if c.APIKey != "" {
		req.Header.Set("Authorization", "Bearer "+c.APIKey)
	}

	resp, err := c.HTTPClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("HTTP %d", resp.StatusCode)
	}

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, err
	}

	var names []string
	if c.Provider == "ollama" {
		var data struct {
			Models []struct {
				Name string `json:"name"`
			} `json:"models"`
		}
		if err := json.Unmarshal(body, &data); err != nil {
			return nil, err
		}
		for _, m := range data.Models {
			names = append(names, m.Name)
		}
	} else {
		var data struct {
			Data []struct {
				ID string `json:"id"`
			} `json:"data"`
		}
		if err := json.Unmarshal(body, &data); err != nil {
			return nil, err
		}
		for _, m := range data.Data {
			names = append(names, m.ID)
		}
	}

	return names, nil
}

// --- Ollama provider ---

type ollamaChatRequest struct {
	Model    string            `json:"model"`
	Messages []Message         `json:"messages"`
	Stream   bool              `json:"stream"`
	Options  map[string]any    `json:"options,omitempty"`
	Tools    []ToolSchema      `json:"tools,omitempty"`
}

type ollamaChatResponse struct {
	Message        ollamaMessage `json:"message"`
	Done           bool          `json:"done"`
	PromptEvalCount int          `json:"prompt_eval_count"`
	EvalCount      int           `json:"eval_count"`
}

type ollamaMessage struct {
	Role      string     `json:"role"`
	Content   string     `json:"content"`
	ToolCalls []ToolCall `json:"tool_calls,omitempty"`
}

func (c *LLMClient) ollamaChat(messages []Message, tools []ToolSchema) (*Response, error) {
	payload := ollamaChatRequest{
		Model:    c.Model,
		Messages: messages,
		Stream:   false,
		Options:  map[string]any{"temperature": c.Temperature},
	}
	if len(tools) > 0 {
		payload.Tools = tools
	}

	body, err := json.Marshal(payload)
	if err != nil {
		return nil, fmt.Errorf("marshal request: %w", err)
	}

	resp, err := c.HTTPClient.Post(c.URL+"/api/chat", "application/json", bytes.NewReader(body))
	if err != nil {
		return nil, fmt.Errorf("ollama request: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		respBody, _ := io.ReadAll(resp.Body)
		return nil, fmt.Errorf("ollama HTTP %d: %s", resp.StatusCode, string(respBody))
	}

	var result ollamaChatResponse
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return nil, fmt.Errorf("decode response: %w", err)
	}

	return &Response{
		Content:      result.Message.Content,
		ToolCalls:    result.Message.ToolCalls,
		Role:         result.Message.Role,
		InputTokens:  result.PromptEvalCount,
		OutputTokens: result.EvalCount,
	}, nil
}

// --- OpenAI-compatible provider ---

type openaiChatRequest struct {
	Model       string       `json:"model"`
	Messages    []Message    `json:"messages"`
	Stream      bool         `json:"stream"`
	Temperature float64      `json:"temperature"`
	Tools       []ToolSchema `json:"tools,omitempty"`
}

type openaiChatResponse struct {
	Choices []openaiChoice `json:"choices"`
	Usage   openaiUsage    `json:"usage"`
}

type openaiChoice struct {
	Message openaiMessage `json:"message"`
}

type openaiMessage struct {
	Role      string              `json:"role"`
	Content   string              `json:"content"`
	ToolCalls []openaiToolCall    `json:"tool_calls,omitempty"`
}

type openaiToolCall struct {
	Function openaiToolCallFunc `json:"function"`
}

type openaiToolCallFunc struct {
	Name      string `json:"name"`
	Arguments string `json:"arguments"`
}

type openaiUsage struct {
	PromptTokens     int `json:"prompt_tokens"`
	CompletionTokens int `json:"completion_tokens"`
}

func (c *LLMClient) openaiChat(messages []Message, tools []ToolSchema) (*Response, error) {
	payload := openaiChatRequest{
		Model:       c.Model,
		Messages:    messages,
		Stream:      false,
		Temperature: c.Temperature,
	}
	if len(tools) > 0 {
		payload.Tools = tools
	}

	body, err := json.Marshal(payload)
	if err != nil {
		return nil, fmt.Errorf("marshal request: %w", err)
	}

	req, err := http.NewRequest("POST", c.URL+"/v1/chat/completions", bytes.NewReader(body))
	if err != nil {
		return nil, fmt.Errorf("create request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	if c.APIKey != "" {
		req.Header.Set("Authorization", "Bearer "+c.APIKey)
	}

	resp, err := c.HTTPClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("openai request: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		respBody, _ := io.ReadAll(resp.Body)
		return nil, fmt.Errorf("openai HTTP %d: %s", resp.StatusCode, string(respBody))
	}

	var result openaiChatResponse
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return nil, fmt.Errorf("decode response: %w", err)
	}

	if len(result.Choices) == 0 {
		return &Response{Role: "assistant"}, nil
	}

	msg := result.Choices[0].Message

	// Convert OpenAI tool calls to our format
	var toolCalls []ToolCall
	for _, tc := range msg.ToolCalls {
		var args map[string]any
		if err := json.Unmarshal([]byte(tc.Function.Arguments), &args); err != nil {
			args = map[string]any{"raw": tc.Function.Arguments}
		}
		toolCalls = append(toolCalls, ToolCall{
			Function: ToolCallFunction{
				Name:      tc.Function.Name,
				Arguments: args,
			},
		})
	}

	return &Response{
		Content:      msg.Content,
		ToolCalls:    toolCalls,
		Role:         msg.Role,
		InputTokens:  result.Usage.PromptTokens,
		OutputTokens: result.Usage.CompletionTokens,
	}, nil
}

// --- Anthropic provider ---

type anthropicRequest struct {
	Model     string           `json:"model"`
	MaxTokens int              `json:"max_tokens"`
	System    string           `json:"system,omitempty"`
	Messages  []anthropicMsg   `json:"messages"`
	Tools     []anthropicTool  `json:"tools,omitempty"`
}

type anthropicMsg struct {
	Role    string `json:"role"`
	Content any    `json:"content"` // string or []anthropicContentBlock
}

type anthropicContentBlock struct {
	Type      string         `json:"type"`
	Text      string         `json:"text,omitempty"`
	ID        string         `json:"id,omitempty"`
	Name      string         `json:"name,omitempty"`
	Input     map[string]any `json:"input,omitempty"`
	ToolUseID string         `json:"tool_use_id,omitempty"`
	Content   string         `json:"content,omitempty"`
}

type anthropicTool struct {
	Name        string         `json:"name"`
	Description string         `json:"description"`
	InputSchema map[string]any `json:"input_schema"`
}

type anthropicResponse struct {
	Content []struct {
		Type  string         `json:"type"`
		Text  string         `json:"text,omitempty"`
		ID    string         `json:"id,omitempty"`
		Name  string         `json:"name,omitempty"`
		Input map[string]any `json:"input,omitempty"`
	} `json:"content"`
	Role  string `json:"role"`
	Usage struct {
		InputTokens  int `json:"input_tokens"`
		OutputTokens int `json:"output_tokens"`
	} `json:"usage"`
	StopReason string `json:"stop_reason"`
}

func (c *LLMClient) anthropicChat(messages []Message, tools []ToolSchema) (*Response, error) {
	// Extract system prompt from messages
	var systemPrompt string
	var anthropicMsgs []anthropicMsg

	for _, msg := range messages {
		switch msg.Role {
		case "system":
			systemPrompt = msg.Content
		case "user":
			anthropicMsgs = append(anthropicMsgs, anthropicMsg{Role: "user", Content: msg.Content})
		case "assistant":
			if len(msg.ToolCalls) > 0 {
				// Build content blocks for assistant tool_use
				var blocks []anthropicContentBlock
				if msg.Content != "" {
					blocks = append(blocks, anthropicContentBlock{Type: "text", Text: msg.Content})
				}
				for _, tc := range msg.ToolCalls {
					blocks = append(blocks, anthropicContentBlock{
						Type:  "tool_use",
						ID:    tc.ID,
						Name:  tc.Function.Name,
						Input: tc.Function.Arguments,
					})
				}
				anthropicMsgs = append(anthropicMsgs, anthropicMsg{Role: "assistant", Content: blocks})
			} else {
				anthropicMsgs = append(anthropicMsgs, anthropicMsg{Role: "assistant", Content: msg.Content})
			}
		case "tool":
			// Tool results go as user messages with tool_result content blocks
			anthropicMsgs = append(anthropicMsgs, anthropicMsg{
				Role: "user",
				Content: []anthropicContentBlock{{
					Type:      "tool_result",
					ToolUseID: msg.ToolCallID,
					Content:   msg.Content,
				}},
			})
		}
	}

	// Convert tool schemas from OpenAI format to Anthropic format
	var anthropicTools []anthropicTool
	for _, ts := range tools {
		anthropicTools = append(anthropicTools, anthropicTool{
			Name:        ts.Function.Name,
			Description: ts.Function.Description,
			InputSchema: ts.Function.Parameters,
		})
	}

	payload := anthropicRequest{
		Model:     c.Model,
		MaxTokens: 4096,
		System:    systemPrompt,
		Messages:  anthropicMsgs,
	}
	if len(anthropicTools) > 0 {
		payload.Tools = anthropicTools
	}

	body, err := json.Marshal(payload)
	if err != nil {
		return nil, fmt.Errorf("marshal request: %w", err)
	}

	req, err := http.NewRequest("POST", c.URL+"/v1/messages", bytes.NewReader(body))
	if err != nil {
		return nil, fmt.Errorf("create request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("x-api-key", c.APIKey)
	req.Header.Set("anthropic-version", "2023-06-01")

	resp, err := c.HTTPClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("anthropic request: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		respBody, _ := io.ReadAll(resp.Body)
		return nil, fmt.Errorf("anthropic HTTP %d: %s", resp.StatusCode, string(respBody))
	}

	var result anthropicResponse
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return nil, fmt.Errorf("decode response: %w", err)
	}

	// Parse content blocks into our normalized format
	var content string
	var toolCalls []ToolCall
	for _, block := range result.Content {
		switch block.Type {
		case "text":
			content += block.Text
		case "tool_use":
			toolCalls = append(toolCalls, ToolCall{
				ID: block.ID,
				Function: ToolCallFunction{
					Name:      block.Name,
					Arguments: block.Input,
				},
			})
		}
	}

	return &Response{
		Content:      content,
		ToolCalls:    toolCalls,
		Role:         "assistant",
		InputTokens:  result.Usage.InputTokens,
		OutputTokens: result.Usage.OutputTokens,
	}, nil
}

// MarshalContentBlocks is needed for proper JSON serialization of anthropicMsg.Content
// which can be either a string or []anthropicContentBlock.
func (m anthropicMsg) MarshalJSON() ([]byte, error) {
	type alias struct {
		Role    string `json:"role"`
		Content any    `json:"content"`
	}
	switch v := m.Content.(type) {
	case string:
		return json.Marshal(alias{Role: m.Role, Content: v})
	case []anthropicContentBlock:
		return json.Marshal(alias{Role: m.Role, Content: v})
	default:
		return json.Marshal(alias{Role: m.Role, Content: v})
	}
}
