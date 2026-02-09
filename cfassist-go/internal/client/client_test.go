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

	// Test both providers
	for _, provider := range []string{"ollama", "openai"} {
		c := New(provider, server.URL, "model", 0.7, "key")
		_, err := c.Chat([]Message{{Role: "user", Content: "hi"}}, nil)
		if err == nil {
			t.Errorf("%s: expected error for HTTP 400", provider)
		}
	}
}
