package conversation

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/aachtenberg/cfoperator/cfassist-go/internal/client"
	"github.com/aachtenberg/cfoperator/cfassist-go/internal/config"
	"github.com/aachtenberg/cfoperator/cfassist-go/internal/tools"
)

// --- Mock Output ---

type mockOutput struct {
	thinkingCount int
	responses     []string
	toolCalls     []string
	errors        []string
	warnings      []string
}

func (o *mockOutput) ShowThinking()  { o.thinkingCount++ }
func (o *mockOutput) ClearThinking() {}
func (o *mockOutput) ShowToolCall(name string, args map[string]any) {
	o.toolCalls = append(o.toolCalls, name)
}
func (o *mockOutput) ShowToolResult(name string, result map[string]any) {}
func (o *mockOutput) ShowResponse(text string)                          { o.responses = append(o.responses, text) }
func (o *mockOutput) ShowError(message string, hint string)             { o.errors = append(o.errors, message) }
func (o *mockOutput) ShowWarning(message string)                        { o.warnings = append(o.warnings, message) }

// --- ParseToolArgs tests ---

func TestParseToolArgsMap(t *testing.T) {
	input := map[string]any{"command": "ls", "timeout": float64(30)}
	result := ParseToolArgs(input)

	if result["command"] != "ls" {
		t.Errorf("command = %v, want %q", result["command"], "ls")
	}
}

func TestParseToolArgsJSONString(t *testing.T) {
	input := `{"command": "hostname", "timeout": 10}`
	result := ParseToolArgs(input)

	if result["command"] != "hostname" {
		t.Errorf("command = %v, want %q", result["command"], "hostname")
	}
}

func TestParseToolArgsInvalidJSON(t *testing.T) {
	input := "not json at all"
	result := ParseToolArgs(input)

	if result["raw"] != "not json at all" {
		t.Errorf("raw = %v, want the original string", result["raw"])
	}
}

func TestParseToolArgsOtherType(t *testing.T) {
	result := ParseToolArgs(42)
	if len(result) != 0 {
		t.Errorf("expected empty map for int, got %v", result)
	}
}

// --- Result struct tests ---

func TestResultFields(t *testing.T) {
	r := Result{
		Response:         "test response",
		ToolCalls:        3,
		InputTokens:      100,
		OutputTokens:     50,
		LastPromptTokens: 100,
		Latency:          2 * time.Second,
		Error:            "",
	}

	if r.Response != "test response" {
		t.Errorf("Response = %q", r.Response)
	}
	if r.ToolCalls != 3 {
		t.Errorf("ToolCalls = %d, want 3", r.ToolCalls)
	}
	if r.Latency != 2*time.Second {
		t.Errorf("Latency = %v", r.Latency)
	}
	if r.LastPromptTokens != 100 {
		t.Errorf("LastPromptTokens = %d, want 100", r.LastPromptTokens)
	}
}

// --- Mock Ollama Server ---

type mockOllamaResponse struct {
	content      string
	toolCall     *client.ToolCall
	toolCalls    []client.ToolCall
	done         bool
	promptTokens int
	evalTokens   int
}

func newMockOllamaServer(t *testing.T, responses []mockOllamaResponse) *httptest.Server {
	t.Helper()
	idx := 0
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/tags" {
			w.WriteHeader(200)
			w.Write([]byte(`{"models":[]}`))
			return
		}

		if r.URL.Path != "/api/chat" {
			w.WriteHeader(404)
			return
		}

		if responses == nil || idx >= len(responses) {
			w.WriteHeader(500)
			w.Write([]byte(`{"error":"mock error"}`))
			return
		}

		resp := responses[idx]
		idx++

		msg := map[string]any{
			"role":    "assistant",
			"content": resp.content,
		}

		if len(resp.toolCalls) > 0 {
			msg["tool_calls"] = resp.toolCalls
		} else if resp.toolCall != nil {
			msg["tool_calls"] = []client.ToolCall{*resp.toolCall}
		}

		body := map[string]any{
			"message":           msg,
			"done":              resp.done,
			"prompt_eval_count": resp.promptTokens,
			"eval_count":        resp.evalTokens,
		}

		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(body)
	}))
}

// --- Integration tests with mock server ---

func TestRunSimpleResponse(t *testing.T) {
	server := newMockOllamaServer(t, []mockOllamaResponse{
		{content: "Hello! I'm here to help.", done: true, promptTokens: 42, evalTokens: 15},
	})
	defer server.Close()

	llm := client.New("ollama", server.URL, "test-model", 0.7, "")
	cfg := config.Defaults()
	cfg.Memory.Directory = t.TempDir()
	toolReg := tools.New(cfg)
	output := &mockOutput{}

	messages := []client.Message{
		{Role: "user", Content: "hello"},
	}

	result, _ := Run(context.Background(), llm, toolReg, output, messages, "You are a test assistant.", 10)

	if result.Error != "" {
		t.Fatalf("unexpected error: %s", result.Error)
	}
	if result.Response != "Hello! I'm here to help." {
		t.Errorf("Response = %q", result.Response)
	}
	if result.InputTokens != 42 {
		t.Errorf("InputTokens = %d, want 42", result.InputTokens)
	}
	if result.OutputTokens != 15 {
		t.Errorf("OutputTokens = %d, want 15", result.OutputTokens)
	}
	if result.LastPromptTokens != 42 {
		t.Errorf("LastPromptTokens = %d, want 42", result.LastPromptTokens)
	}
	if result.Latency <= 0 {
		t.Error("Latency should be positive")
	}
	if len(output.responses) != 1 {
		t.Errorf("expected 1 response shown, got %d", len(output.responses))
	}
	if output.thinkingCount != 1 {
		t.Errorf("expected 1 thinking indicator, got %d", output.thinkingCount)
	}
}

func TestRunWithToolCall(t *testing.T) {
	server := newMockOllamaServer(t, []mockOllamaResponse{
		{
			toolCall: &client.ToolCall{
				Function: client.ToolCallFunction{
					Name:      "bash",
					Arguments: map[string]any{"command": "echo test-output"},
				},
			},
			done: true, promptTokens: 50, evalTokens: 10,
		},
		{content: "The command output was: test-output", done: true, promptTokens: 80, evalTokens: 20},
	})
	defer server.Close()

	llm := client.New("ollama", server.URL, "test-model", 0.7, "")
	cfg := config.Defaults()
	cfg.Memory.Directory = t.TempDir()
	toolReg := tools.New(cfg)
	output := &mockOutput{}

	messages := []client.Message{
		{Role: "user", Content: "run echo test"},
	}

	result, _ := Run(context.Background(), llm, toolReg, output, messages, "You are a test assistant.", 10)

	if result.Error != "" {
		t.Fatalf("unexpected error: %s", result.Error)
	}
	if result.ToolCalls != 1 {
		t.Errorf("ToolCalls = %d, want 1", result.ToolCalls)
	}
	if result.Response != "The command output was: test-output" {
		t.Errorf("Response = %q", result.Response)
	}
	if len(output.toolCalls) != 1 {
		t.Errorf("expected 1 tool call shown, got %d", len(output.toolCalls))
	}
	if result.InputTokens != 130 {
		t.Errorf("InputTokens = %d, want 130", result.InputTokens)
	}
	if result.LastPromptTokens != 80 {
		t.Errorf("LastPromptTokens = %d, want 80", result.LastPromptTokens)
	}
}

func TestRunMaxIterations(t *testing.T) {
	server := newMockOllamaServer(t, []mockOllamaResponse{
		{toolCall: &client.ToolCall{Function: client.ToolCallFunction{Name: "bash", Arguments: map[string]any{"command": "echo 1"}}}, done: true, promptTokens: 10, evalTokens: 5},
		{toolCall: &client.ToolCall{Function: client.ToolCallFunction{Name: "bash", Arguments: map[string]any{"command": "echo 2"}}}, done: true, promptTokens: 10, evalTokens: 5},
		{toolCall: &client.ToolCall{Function: client.ToolCallFunction{Name: "bash", Arguments: map[string]any{"command": "echo 3"}}}, done: true, promptTokens: 10, evalTokens: 5},
	})
	defer server.Close()

	llm := client.New("ollama", server.URL, "test-model", 0.7, "")
	cfg := config.Defaults()
	cfg.Memory.Directory = t.TempDir()
	toolReg := tools.New(cfg)
	output := &mockOutput{}

	messages := []client.Message{
		{Role: "user", Content: "loop forever"},
	}

	result, _ := Run(context.Background(), llm, toolReg, output, messages, "test", 3)

	if result.ToolCalls != 3 {
		t.Errorf("ToolCalls = %d, want 3 (max)", result.ToolCalls)
	}
	if len(output.warnings) != 1 {
		t.Errorf("expected 1 max-iteration warning, got %d", len(output.warnings))
	}
}

func TestRunLLMError(t *testing.T) {
	server := newMockOllamaServer(t, nil)
	defer server.Close()

	llm := client.New("ollama", server.URL, "test-model", 0.7, "")
	cfg := config.Defaults()
	cfg.Memory.Directory = t.TempDir()
	toolReg := tools.New(cfg)
	output := &mockOutput{}

	messages := []client.Message{
		{Role: "user", Content: "hello"},
	}

	result, _ := Run(context.Background(), llm, toolReg, output, messages, "test", 10)

	if result.Error == "" {
		t.Error("expected error from failed LLM")
	}
	if len(output.errors) != 1 {
		t.Errorf("expected 1 error shown, got %d", len(output.errors))
	}
}

func TestRunDefaultMaxIterations(t *testing.T) {
	server := newMockOllamaServer(t, []mockOllamaResponse{
		{content: "ok", done: true, promptTokens: 10, evalTokens: 5},
	})
	defer server.Close()

	llm := client.New("ollama", server.URL, "test-model", 0.7, "")
	cfg := config.Defaults()
	cfg.Memory.Directory = t.TempDir()
	toolReg := tools.New(cfg)
	output := &mockOutput{}

	result, _ := Run(context.Background(), llm, toolReg, output, []client.Message{{Role: "user", Content: "hi"}}, "test", 0)
	if result.Error != "" {
		t.Errorf("unexpected error: %s", result.Error)
	}
}

func TestRunExecutesAllParallelToolCalls(t *testing.T) {
	server := newMockOllamaServer(t, []mockOllamaResponse{
		{
			toolCalls: []client.ToolCall{
				{
					ID: "toolu_a",
					Function: client.ToolCallFunction{
						Name:      "bash",
						Arguments: map[string]any{"command": "echo first"},
					},
				},
				{
					ID: "toolu_b",
					Function: client.ToolCallFunction{
						Name:      "bash",
						Arguments: map[string]any{"command": "echo second"},
					},
				},
			},
			done: true, promptTokens: 40, evalTokens: 12,
		},
		{content: "both commands ran", done: true, promptTokens: 80, evalTokens: 8},
	})
	defer server.Close()

	llm := client.New("ollama", server.URL, "test-model", 0.7, "")
	cfg := config.Defaults()
	cfg.Memory.Directory = t.TempDir()
	toolReg := tools.New(cfg)
	output := &mockOutput{}

	result, msgs := Run(context.Background(), llm, toolReg, output,
		[]client.Message{{Role: "user", Content: "check both"}}, "test", 10)

	if result.Error != "" {
		t.Fatalf("unexpected error: %s", result.Error)
	}
	if result.ToolCalls != 2 {
		t.Errorf("ToolCalls = %d, want 2 (both parallel calls)", result.ToolCalls)
	}
	if len(output.toolCalls) != 2 {
		t.Errorf("shown tool calls = %d, want 2", len(output.toolCalls))
	}
	if result.Response != "both commands ran" {
		t.Errorf("Response = %q", result.Response)
	}

	var toolMsgs int
	var ids []string
	for _, m := range msgs {
		if m.Role == "tool" {
			toolMsgs++
			ids = append(ids, m.ToolCallID)
		}
	}
	if toolMsgs != 2 {
		t.Errorf("tool messages = %d, want 2", toolMsgs)
	}
	wantIDs := map[string]bool{"toolu_a": true, "toolu_b": true}
	for _, id := range ids {
		if !wantIDs[id] {
			t.Errorf("unexpected tool id %q", id)
		}
		delete(wantIDs, id)
	}
	if len(wantIDs) != 0 {
		t.Errorf("missing tool result ids: %v", wantIDs)
	}
}

func TestRunAnthropicSendsOneUserMessageForParallelToolResults(t *testing.T) {
	// The 400 in the session log was Anthropic looking at messages[2] (the
	// assistant tool_use turn after two unmerged user messages, or after a
	// tool_use whose sibling ids had no result) and refusing it. This hits
	// the Anthropic converter through Run, not just toAnthropicMessages.
	var second []byte
	call := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		call++
		if call == 2 {
			second = append([]byte(nil), body...)
		}
		if call == 1 {
			json.NewEncoder(w).Encode(map[string]any{
				"role": "assistant",
				"content": []map[string]any{
					{"type": "tool_use", "id": "toolu_a", "name": "bash", "input": map[string]any{"command": "echo a"}},
					{"type": "tool_use", "id": "toolu_b", "name": "bash", "input": map[string]any{"command": "echo b"}},
				},
				"usage":        map[string]int{"input_tokens": 10, "output_tokens": 20},
				"stop_reason":  "tool_use",
			})
			return
		}
		json.NewEncoder(w).Encode(map[string]any{
			"role":         "assistant",
			"content":      []map[string]any{{"type": "text", "text": "done"}},
			"usage":        map[string]int{"input_tokens": 30, "output_tokens": 4},
			"stop_reason":  "end_turn",
		})
	}))
	defer server.Close()

	llm := client.New("anthropic", server.URL, "claude-sonnet-4-20250514", 0.7, "key")
	cfg := config.Defaults()
	cfg.Memory.Directory = t.TempDir()
	toolReg := tools.New(cfg)
	output := &mockOutput{}

	result, _ := Run(context.Background(), llm, toolReg, output,
		[]client.Message{{Role: "user", Content: "check"}}, "sys", 10)
	if result.Error != "" {
		t.Fatalf("unexpected error: %s", result.Error)
	}
	if call < 2 {
		t.Fatalf("expected a follow-up request after tool results, got %d calls", call)
	}

	var payload struct {
		Messages []struct {
			Role    string          `json:"role"`
			Content json.RawMessage `json:"content"`
		} `json:"messages"`
	}
	if err := json.Unmarshal(second, &payload); err != nil {
		t.Fatalf("second request: %v\n%s", err, second)
	}

	var assistantIdx = -1
	for i, m := range payload.Messages {
		if m.Role == "assistant" && bytes.Contains(m.Content, []byte("tool_use")) {
			assistantIdx = i
			break
		}
	}
	if assistantIdx < 0 {
		t.Fatalf("no assistant tool_use in follow-up:\n%s", second)
	}
	if assistantIdx+1 >= len(payload.Messages) {
		t.Fatal("assistant tool_use is the last message; Anthropic needs tool_result next")
	}
	next := payload.Messages[assistantIdx+1]
	if next.Role != "user" {
		t.Fatalf("message after tool_use has role %q, want user", next.Role)
	}
	var blocks []map[string]any
	if err := json.Unmarshal(next.Content, &blocks); err != nil {
		t.Fatalf("tool_result content should be a block array, got %s: %v", next.Content, err)
	}
	ids := map[string]bool{}
	for _, b := range blocks {
		if b["type"] == "tool_result" {
			id, _ := b["tool_use_id"].(string)
			ids[id] = true
		}
	}
	if !ids["toolu_a"] || !ids["toolu_b"] {
		t.Fatalf("follow-up user message missing tool_results, ids=%v content=%s", ids, next.Content)
	}
	if assistantIdx+2 < len(payload.Messages) && payload.Messages[assistantIdx+2].Role == "user" {
		t.Fatal("tool results were split across consecutive user messages")
	}
}
