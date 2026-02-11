package client

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestNewClient(t *testing.T) {
	c := New("ollama", "http://localhost:11434", "llama3.2", 0.7, "")

	if c.Provider != "ollama" {
		t.Errorf("Provider = %q, want %q", c.Provider, "ollama")
	}
	if c.URL != "http://localhost:11434" {
		t.Errorf("URL = %q, want %q", c.URL, "http://localhost:11434")
	}
	if c.Model != "llama3.2" {
		t.Errorf("Model = %q, want %q", c.Model, "llama3.2")
	}
	if c.Temperature != 0.7 {
		t.Errorf("Temperature = %f, want %f", c.Temperature, 0.7)
	}
}

func TestNewClientTrimsTrailingSlash(t *testing.T) {
	c := New("ollama", "http://localhost:11434/", "model", 0.7, "")
	if c.URL != "http://localhost:11434" {
		t.Errorf("URL should have trailing slash trimmed, got %q", c.URL)
	}
}

func TestNewClientTrimsTrailingV1(t *testing.T) {
	c := New("openai", "https://api.groq.com/openai/v1", "model", 0.7, "key")
	if c.URL != "https://api.groq.com/openai" {
		t.Errorf("URL should have /v1 trimmed, got %q", c.URL)
	}
	// Also with trailing slash
	c2 := New("openai", "https://api.groq.com/openai/v1/", "model", 0.7, "key")
	if c2.URL != "https://api.groq.com/openai" {
		t.Errorf("URL should have /v1/ trimmed, got %q", c2.URL)
	}
}

func TestCheckConnectionOllama(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/tags" {
			t.Errorf("unexpected path: %s", r.URL.Path)
		}
		w.WriteHeader(http.StatusOK)
		json.NewEncoder(w).Encode(map[string]any{"models": []any{}})
	}))
	defer server.Close()

	c := New("ollama", server.URL, "test", 0.7, "")
	if err := c.CheckConnection(); err != nil {
		t.Errorf("CheckConnection failed: %v", err)
	}
}

func TestCheckConnectionOpenAI(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/models" {
			t.Errorf("unexpected path: %s", r.URL.Path)
		}
		// Verify auth header
		if auth := r.Header.Get("Authorization"); auth != "Bearer test-key" {
			t.Errorf("expected auth header, got %q", auth)
		}
		w.WriteHeader(http.StatusOK)
		json.NewEncoder(w).Encode(map[string]any{"data": []any{}})
	}))
	defer server.Close()

	c := New("openai", server.URL, "gpt-4o", 0.7, "test-key")
	if err := c.CheckConnection(); err != nil {
		t.Errorf("CheckConnection failed: %v", err)
	}
}

func TestCheckConnectionFailure(t *testing.T) {
	c := New("ollama", "http://127.0.0.1:1", "model", 0.7, "")
	err := c.CheckConnection()
	if err == nil {
		t.Error("expected connection error")
	}
}

func TestCheckConnectionHTTPError(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer server.Close()

	c := New("ollama", server.URL, "model", 0.7, "")
	err := c.CheckConnection()
	if err == nil {
		t.Error("expected error for HTTP 500")
	}
}

func TestListModelsOllama(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/tags" {
			t.Errorf("unexpected path: %s", r.URL.Path)
		}
		json.NewEncoder(w).Encode(map[string]any{
			"models": []map[string]any{
				{"name": "llama3.2"},
				{"name": "qwen2.5"},
				{"name": "glm-4"},
			},
		})
	}))
	defer server.Close()

	c := New("ollama", server.URL, "llama3.2", 0.7, "")
	models, err := c.ListModels()
	if err != nil {
		t.Fatalf("ListModels failed: %v", err)
	}
	if len(models) != 3 {
		t.Fatalf("expected 3 models, got %d", len(models))
	}
	if models[0] != "llama3.2" {
		t.Errorf("models[0] = %q, want %q", models[0], "llama3.2")
	}
}

func TestListModelsOpenAI(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/models" {
			t.Errorf("unexpected path: %s", r.URL.Path)
		}
		json.NewEncoder(w).Encode(map[string]any{
			"data": []map[string]any{
				{"id": "gpt-4o"},
				{"id": "gpt-3.5-turbo"},
			},
		})
	}))
	defer server.Close()

	c := New("openai", server.URL, "gpt-4o", 0.7, "key")
	models, err := c.ListModels()
	if err != nil {
		t.Fatalf("ListModels failed: %v", err)
	}
	if len(models) != 2 {
		t.Fatalf("expected 2 models, got %d", len(models))
	}
	if models[0] != "gpt-4o" {
		t.Errorf("models[0] = %q, want %q", models[0], "gpt-4o")
	}
}

func TestListModelsError(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer server.Close()

	c := New("ollama", server.URL, "model", 0.7, "")
	_, err := c.ListModels()
	if err == nil {
		t.Error("expected error for HTTP 500")
	}
}

func TestOllamaChat(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/chat" {
			t.Errorf("unexpected path: %s", r.URL.Path)
			return
		}

		// Verify request body
		var req ollamaChatRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			t.Errorf("failed to decode request: %v", err)
			return
		}

		if req.Model != "llama3.2" {
			t.Errorf("request model = %q, want %q", req.Model, "llama3.2")
		}
		if req.Stream != false {
			t.Error("stream should be false")
		}

		json.NewEncoder(w).Encode(ollamaChatResponse{
			Message: ollamaMessage{
				Role:    "assistant",
				Content: "Hello! I'm here to help.",
			},
			Done:            true,
			PromptEvalCount: 42,
			EvalCount:       15,
		})
	}))
	defer server.Close()

	c := New("ollama", server.URL, "llama3.2", 0.7, "")
	messages := []Message{
		{Role: "user", Content: "hello"},
	}

	resp, err := c.Chat(messages, nil)
	if err != nil {
		t.Fatalf("Chat failed: %v", err)
	}

	if resp.Content != "Hello! I'm here to help." {
		t.Errorf("Content = %q", resp.Content)
	}
	if resp.Role != "assistant" {
		t.Errorf("Role = %q, want %q", resp.Role, "assistant")
	}
	if resp.InputTokens != 42 {
		t.Errorf("InputTokens = %d, want %d", resp.InputTokens, 42)
	}
	if resp.OutputTokens != 15 {
		t.Errorf("OutputTokens = %d, want %d", resp.OutputTokens, 15)
	}
}

func TestOllamaChatWithToolCall(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		json.NewEncoder(w).Encode(ollamaChatResponse{
			Message: ollamaMessage{
				Role: "assistant",
				ToolCalls: []ToolCall{
					{
						Function: ToolCallFunction{
							Name:      "bash",
							Arguments: map[string]any{"command": "hostname"},
						},
					},
				},
			},
			Done:            true,
			PromptEvalCount: 50,
			EvalCount:       10,
		})
	}))
	defer server.Close()

	c := New("ollama", server.URL, "model", 0.7, "")
	resp, err := c.Chat([]Message{{Role: "user", Content: "hostname?"}}, nil)
	if err != nil {
		t.Fatalf("Chat failed: %v", err)
	}

	if len(resp.ToolCalls) != 1 {
		t.Fatalf("expected 1 tool call, got %d", len(resp.ToolCalls))
	}
	if resp.ToolCalls[0].Function.Name != "bash" {
		t.Errorf("tool call name = %q, want %q", resp.ToolCalls[0].Function.Name, "bash")
	}
}

func TestOpenAIChat(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/chat/completions" {
			t.Errorf("unexpected path: %s", r.URL.Path)
			return
		}

		if auth := r.Header.Get("Authorization"); auth != "Bearer test-key" {
			t.Errorf("auth header = %q", auth)
		}

		json.NewEncoder(w).Encode(openaiChatResponse{
			Choices: []openaiChoice{
				{
					Message: openaiMessage{
						Role:    "assistant",
						Content: "Hello from OpenAI!",
					},
				},
			},
			Usage: openaiUsage{
				PromptTokens:     30,
				CompletionTokens: 10,
			},
		})
	}))
	defer server.Close()

	c := New("openai", server.URL, "gpt-4o", 0.5, "test-key")
	resp, err := c.Chat([]Message{{Role: "user", Content: "hi"}}, nil)
	if err != nil {
		t.Fatalf("Chat failed: %v", err)
	}

	if resp.Content != "Hello from OpenAI!" {
		t.Errorf("Content = %q", resp.Content)
	}
	if resp.InputTokens != 30 {
		t.Errorf("InputTokens = %d, want %d", resp.InputTokens, 30)
	}
}

func TestOpenAIChatWithToolCall(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		json.NewEncoder(w).Encode(openaiChatResponse{
			Choices: []openaiChoice{
				{
					Message: openaiMessage{
						Role: "assistant",
						ToolCalls: []openaiToolCall{
							{
								Function: openaiToolCallFunc{
									Name:      "bash",
									Arguments: `{"command": "ls"}`,
								},
							},
						},
					},
				},
			},
			Usage: openaiUsage{PromptTokens: 20, CompletionTokens: 5},
		})
	}))
	defer server.Close()

	c := New("openai", server.URL, "gpt-4o", 0.7, "key")
	resp, err := c.Chat([]Message{{Role: "user", Content: "list files"}}, nil)
	if err != nil {
		t.Fatalf("Chat failed: %v", err)
	}

	if len(resp.ToolCalls) != 1 {
		t.Fatalf("expected 1 tool call, got %d", len(resp.ToolCalls))
	}
	if resp.ToolCalls[0].Function.Name != "bash" {
		t.Errorf("tool name = %q", resp.ToolCalls[0].Function.Name)
	}
	cmd, ok := resp.ToolCalls[0].Function.Arguments["command"].(string)
	if !ok || cmd != "ls" {
		t.Errorf("tool args command = %v", resp.ToolCalls[0].Function.Arguments["command"])
	}
}

func TestOpenAIChatEmptyChoices(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		json.NewEncoder(w).Encode(openaiChatResponse{
			Choices: []openaiChoice{},
			Usage:   openaiUsage{},
		})
	}))
	defer server.Close()

	c := New("openai", server.URL, "model", 0.7, "key")
	resp, err := c.Chat([]Message{{Role: "user", Content: "hi"}}, nil)
	if err != nil {
		t.Fatalf("Chat failed: %v", err)
	}
	if resp.Role != "assistant" {
		t.Errorf("empty choices should return assistant role, got %q", resp.Role)
	}
}

func TestChatHTTPError(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusBadRequest)
		w.Write([]byte("bad request"))
	}))
	defer server.Close()

	// Test all providers
	for _, provider := range []string{"ollama", "openai", "anthropic"} {
		c := New(provider, server.URL, "model", 0.7, "key")
		_, err := c.Chat([]Message{{Role: "user", Content: "hi"}}, nil)
		if err == nil {
			t.Errorf("%s: expected error for HTTP 400", provider)
		}
	}
}

func TestAnthropicChat(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/messages" {
			t.Errorf("unexpected path: %s", r.URL.Path)
			return
		}

		// Verify Anthropic auth headers
		if apiKey := r.Header.Get("x-api-key"); apiKey != "test-key" {
			t.Errorf("x-api-key = %q, want %q", apiKey, "test-key")
		}
		if version := r.Header.Get("anthropic-version"); version != "2023-06-01" {
			t.Errorf("anthropic-version = %q, want %q", version, "2023-06-01")
		}

		// Verify system prompt extracted from messages
		var req anthropicRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			t.Errorf("decode request: %v", err)
			return
		}
		if req.System != "You are helpful." {
			t.Errorf("system = %q, want %q", req.System, "You are helpful.")
		}
		if req.Model != "claude-sonnet-4-20250514" {
			t.Errorf("model = %q, want %q", req.Model, "claude-sonnet-4-20250514")
		}

		json.NewEncoder(w).Encode(anthropicResponse{
			Content: []struct {
				Type  string         `json:"type"`
				Text  string         `json:"text,omitempty"`
				ID    string         `json:"id,omitempty"`
				Name  string         `json:"name,omitempty"`
				Input map[string]any `json:"input,omitempty"`
			}{
				{Type: "text", Text: "Hello from Claude!"},
			},
			Role: "assistant",
			Usage: struct {
				InputTokens  int `json:"input_tokens"`
				OutputTokens int `json:"output_tokens"`
			}{InputTokens: 25, OutputTokens: 8},
			StopReason: "end_turn",
		})
	}))
	defer server.Close()

	c := New("anthropic", server.URL, "claude-sonnet-4-20250514", 0.7, "test-key")
	messages := []Message{
		{Role: "system", Content: "You are helpful."},
		{Role: "user", Content: "hello"},
	}

	resp, err := c.Chat(messages, nil)
	if err != nil {
		t.Fatalf("Chat failed: %v", err)
	}

	if resp.Content != "Hello from Claude!" {
		t.Errorf("Content = %q, want %q", resp.Content, "Hello from Claude!")
	}
	if resp.Role != "assistant" {
		t.Errorf("Role = %q, want %q", resp.Role, "assistant")
	}
	if resp.InputTokens != 25 {
		t.Errorf("InputTokens = %d, want %d", resp.InputTokens, 25)
	}
	if resp.OutputTokens != 8 {
		t.Errorf("OutputTokens = %d, want %d", resp.OutputTokens, 8)
	}
}

func TestAnthropicChatWithToolCall(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		json.NewEncoder(w).Encode(anthropicResponse{
			Content: []struct {
				Type  string         `json:"type"`
				Text  string         `json:"text,omitempty"`
				ID    string         `json:"id,omitempty"`
				Name  string         `json:"name,omitempty"`
				Input map[string]any `json:"input,omitempty"`
			}{
				{Type: "text", Text: "Let me check that."},
				{Type: "tool_use", ID: "toolu_abc123", Name: "bash", Input: map[string]any{"command": "hostname"}},
			},
			Role: "assistant",
			Usage: struct {
				InputTokens  int `json:"input_tokens"`
				OutputTokens int `json:"output_tokens"`
			}{InputTokens: 30, OutputTokens: 15},
			StopReason: "tool_use",
		})
	}))
	defer server.Close()

	c := New("anthropic", server.URL, "claude-sonnet-4-20250514", 0.7, "key")
	resp, err := c.Chat([]Message{{Role: "user", Content: "hostname?"}}, nil)
	if err != nil {
		t.Fatalf("Chat failed: %v", err)
	}

	if resp.Content != "Let me check that." {
		t.Errorf("Content = %q", resp.Content)
	}
	if len(resp.ToolCalls) != 1 {
		t.Fatalf("expected 1 tool call, got %d", len(resp.ToolCalls))
	}
	tc := resp.ToolCalls[0]
	if tc.ID != "toolu_abc123" {
		t.Errorf("tool call ID = %q, want %q", tc.ID, "toolu_abc123")
	}
	if tc.Function.Name != "bash" {
		t.Errorf("tool call name = %q, want %q", tc.Function.Name, "bash")
	}
	cmd, ok := tc.Function.Arguments["command"].(string)
	if !ok || cmd != "hostname" {
		t.Errorf("tool args command = %v", tc.Function.Arguments["command"])
	}
}

func TestCheckConnectionAnthropic(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/messages" {
			t.Errorf("unexpected path: %s", r.URL.Path)
		}
		if r.Method != "POST" {
			t.Errorf("unexpected method: %s", r.Method)
		}
		if apiKey := r.Header.Get("x-api-key"); apiKey != "test-key" {
			t.Errorf("x-api-key = %q, want %q", apiKey, "test-key")
		}
		w.WriteHeader(http.StatusOK)
		json.NewEncoder(w).Encode(map[string]any{
			"content": []map[string]any{{"type": "text", "text": "hi"}},
			"role":    "assistant",
		})
	}))
	defer server.Close()

	c := New("anthropic", server.URL, "claude-sonnet-4-20250514", 0.7, "test-key")
	if err := c.CheckConnection(); err != nil {
		t.Errorf("CheckConnection failed: %v", err)
	}
}

func TestListModelsAnthropic(t *testing.T) {
	c := New("anthropic", "https://api.anthropic.com", "claude-sonnet-4-20250514", 0.7, "key")
	models, err := c.ListModels()
	if err != nil {
		t.Fatalf("ListModels failed: %v", err)
	}
	if len(models) != 3 {
		t.Fatalf("expected 3 models, got %d", len(models))
	}
	// Should include known Claude models
	found := false
	for _, m := range models {
		if m == "claude-sonnet-4-20250514" {
			found = true
		}
	}
	if !found {
		t.Errorf("expected claude-sonnet-4-20250514 in models list: %v", models)
	}
}

func TestAnthropicChatToolSchemaConversion(t *testing.T) {
	var receivedReq anthropicRequest

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		json.NewDecoder(r.Body).Decode(&receivedReq)
		json.NewEncoder(w).Encode(anthropicResponse{
			Content: []struct {
				Type  string         `json:"type"`
				Text  string         `json:"text,omitempty"`
				ID    string         `json:"id,omitempty"`
				Name  string         `json:"name,omitempty"`
				Input map[string]any `json:"input,omitempty"`
			}{
				{Type: "text", Text: "ok"},
			},
			Role: "assistant",
		})
	}))
	defer server.Close()

	c := New("anthropic", server.URL, "claude-sonnet-4-20250514", 0.7, "key")
	tools := []ToolSchema{
		{
			Type: "function",
			Function: ToolSchemaFunction{
				Name:        "bash",
				Description: "Run a shell command",
				Parameters: map[string]any{
					"type": "object",
					"properties": map[string]any{
						"command": map[string]any{"type": "string"},
					},
					"required": []string{"command"},
				},
			},
		},
	}

	_, err := c.Chat([]Message{{Role: "user", Content: "test"}}, tools)
	if err != nil {
		t.Fatalf("Chat failed: %v", err)
	}

	// Verify tool schema was converted to Anthropic format
	if len(receivedReq.Tools) != 1 {
		t.Fatalf("expected 1 tool, got %d", len(receivedReq.Tools))
	}
	tool := receivedReq.Tools[0]
	if tool.Name != "bash" {
		t.Errorf("tool name = %q, want %q", tool.Name, "bash")
	}
	if tool.Description != "Run a shell command" {
		t.Errorf("tool description = %q", tool.Description)
	}
	if tool.InputSchema == nil {
		t.Error("tool input_schema should not be nil")
	}
}
