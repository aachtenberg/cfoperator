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
	Role      string     `json:"role"`
	Content   string     `json:"content,omitempty"`
	ToolCalls []ToolCall `json:"tool_calls,omitempty"`
}

// ToolCall represents a tool call from the LLM.
type ToolCall struct {
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
	if c.Provider == "ollama" {
		return c.ollamaChat(messages, tools)
	}
	return c.openaiChat(messages, tools)
}

// CheckConnection tests if the LLM endpoint is reachable.
func (c *LLMClient) CheckConnection() error {
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
