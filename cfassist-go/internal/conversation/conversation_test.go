package conversation

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
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

func TestRunOmitsSynthesizedSystemMessage(t *testing.T) {
	server := newMockOllamaServer(t, []mockOllamaResponse{
		{content: "ok", done: true, promptTokens: 4, evalTokens: 2},
	})
	defer server.Close()

	llm := client.New("ollama", server.URL, "test-model", 0.7, "")
	cfg := config.Defaults()
	cfg.Memory.Directory = t.TempDir()
	_, msgs := Run(context.Background(), llm, tools.New(cfg), &mockOutput{},
		[]client.Message{{Role: "user", Content: "hi"}}, "you are a secret system prompt", 10)
	for _, m := range msgs {
		if m.Role == "system" {
			t.Fatalf("Run leaked the synthesized system message: %+v", m)
		}
		if strings.Contains(m.Content, "secret system prompt") {
			t.Fatalf("system prompt leaked into transcript: %q", m.Content)
		}
	}
}

func TestRunFallbackToolIDsDifferAcrossTurns(t *testing.T) {
	server := newMockOllamaServer(t, []mockOllamaResponse{
		{toolCall: &client.ToolCall{Function: client.ToolCallFunction{Name: "bash", Arguments: map[string]any{"command": "echo a"}}}, done: true, promptTokens: 4, evalTokens: 2},
		{content: "a", done: true, promptTokens: 6, evalTokens: 1},
		{toolCall: &client.ToolCall{Function: client.ToolCallFunction{Name: "bash", Arguments: map[string]any{"command": "echo b"}}}, done: true, promptTokens: 4, evalTokens: 2},
		{content: "b", done: true, promptTokens: 6, evalTokens: 1},
	})
	defer server.Close()

	llm := client.New("ollama", server.URL, "test-model", 0.7, "")
	cfg := config.Defaults()
	cfg.Memory.Directory = t.TempDir()
	toolReg := tools.New(cfg)

	_, first := Run(context.Background(), llm, toolReg, &mockOutput{},
		[]client.Message{{Role: "user", Content: "a"}}, "sys", 10)
	_, second := Run(context.Background(), llm, toolReg, &mockOutput{},
		[]client.Message{{Role: "user", Content: "b"}}, "sys", 10)

	idOf := func(msgs []client.Message) string {
		t.Helper()
		for _, m := range msgs {
			if m.Role == "tool" {
				return m.ToolCallID
			}
		}
		t.Fatal("no tool message")
		return ""
	}
	id1, id2 := idOf(first), idOf(second)
	if id1 == "" || id2 == "" {
		t.Fatal("expected synthesized tool ids")
	}
	if id1 == id2 {
		t.Fatalf("fallback ids collided across Run calls: %q", id1)
	}
}

func TestRunKeepsToolResultsWhenCancelledMidRound(t *testing.T) {
	server := newMockOllamaServer(t, []mockOllamaResponse{
		{
			toolCalls: []client.ToolCall{
				{ID: "toolu_a", Function: client.ToolCallFunction{Name: "bash", Arguments: map[string]any{"command": "echo first"}}},
				{ID: "toolu_b", Function: client.ToolCallFunction{Name: "bash", Arguments: map[string]any{"command": "echo second"}}},
			},
			done: true, promptTokens: 10, evalTokens: 5,
		},
	})
	defer server.Close()

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	afterTool = func() { cancel() }
	t.Cleanup(func() { afterTool = nil })

	llm := client.New("ollama", server.URL, "test-model", 0.7, "")
	cfg := config.Defaults()
	cfg.Memory.Directory = t.TempDir()
	result, msgs := Run(ctx, llm, tools.New(cfg), &mockOutput{},
		[]client.Message{{Role: "user", Content: "check both"}}, "sys", 10)

	if !result.Cancelled {
		t.Fatal("expected the turn to be marked cancelled")
	}
	var toolMsgs []client.Message
	for _, m := range msgs {
		if m.Role == "tool" {
			toolMsgs = append(toolMsgs, m)
		}
	}
	if len(toolMsgs) != 2 {
		t.Fatalf("tool messages = %d, want 2 (one per tool_use even after cancel)", len(toolMsgs))
	}
	if toolMsgs[0].ToolCallID != "toolu_a" || toolMsgs[1].ToolCallID != "toolu_b" {
		t.Fatalf("tool ids = %q, %q", toolMsgs[0].ToolCallID, toolMsgs[1].ToolCallID)
	}
	if !toolMsgs[1].IsError || !strings.Contains(toolMsgs[1].Content, "cancelled") {
		t.Fatalf("second result should be the cancelled filler, got %+v", toolMsgs[1])
	}
}

// --- Announced-step nudge ---

func TestAnnouncesStep(t *testing.T) {
	cases := []struct {
		text string
		want bool
	}{
		// The screenshot that prompted this: narration, then a promise, then stop.
		{"The logs show successful executions.\n\nI'll check the Kubernetes events in the `data` namespace.", true},
		{"Let me look at the pod logs.", true},
		{"Found nothing yet. Next, I will run kubectl describe.", true},
		{"I’ll query the events now.", true},
		{"I'm going to inspect the node.", true},

		{"", false},
		{"The job is healthy; no action needed.", false},
		{"Let me know if you want me to dig further.", false},
		{"I'll keep an eye on it.", false},
		{"Should I check the events next?", false},
		{"If you want, I'll check the events next.", false},
		// Earlier paragraphs are reasoning; only the close decides.
		{"I'll check the logs.\n\nThe logs were clean, so the failure was transient.", false},
	}
	for _, c := range cases {
		if got := announcesStep(c.text); got != c.want {
			t.Errorf("announcesStep(%q) = %v, want %v", c.text, got, c.want)
		}
	}
}

func newNudgeRun(t *testing.T, responses []mockOllamaResponse) (Result, []client.Message, *mockOutput) {
	t.Helper()
	server := newMockOllamaServer(t, responses)
	t.Cleanup(server.Close)
	llm := client.New("ollama", server.URL, "test-model", 0.7, "")
	cfg := config.Defaults()
	cfg.Memory.Directory = t.TempDir()
	output := &mockOutput{}
	result, msgs := Run(context.Background(), llm, tools.New(cfg), output,
		[]client.Message{{Role: "user", Content: "why did the job fail?"}}, "sys", 10)
	return result, msgs, output
}

func bashCall(cmd string) *client.ToolCall {
	return &client.ToolCall{Function: client.ToolCallFunction{Name: "bash", Arguments: map[string]any{"command": cmd}}}
}

// The turn carries on after an announced step instead of handing the operator
// a promise and a prompt.
func TestRunNudgesAnnouncedStep(t *testing.T) {
	result, msgs, output := newNudgeRun(t, []mockOllamaResponse{
		{toolCall: bashCall("echo logs"), done: true},
		{content: "Logs look fine.\n\nI'll check the events.", done: true},
		{toolCall: bashCall("echo events"), done: true},
		{content: "No failing events; the failure was transient.", done: true},
	})
	if result.Error != "" {
		t.Fatalf("unexpected error: %s", result.Error)
	}
	if result.ToolCalls != 2 {
		t.Errorf("ToolCalls = %d, want 2 — the announced step never ran", result.ToolCalls)
	}
	if result.Response != "No failing events; the failure was transient." {
		t.Errorf("Response = %q", result.Response)
	}
	if len(output.warnings) != 1 {
		t.Errorf("warnings = %v, want one nudge notice", output.warnings)
	}
	nudges := 0
	for _, m := range msgs {
		if m.Role == "user" && m.Content == AnnouncedStepNudge {
			nudges++
		}
	}
	if nudges != 1 {
		t.Errorf("transcript holds %d nudges, want 1", nudges)
	}
}

// An announcement on the final allowed iteration is the answer. Nudging would
// continue past the loop and return an empty Response plus the iteration-limit
// warning, even though the reply was already shown.
func TestRunDoesNotNudgeOnTheLastIteration(t *testing.T) {
	server := newMockOllamaServer(t, []mockOllamaResponse{
		{content: "I'll check the events.", done: true},
	})
	t.Cleanup(server.Close)
	llm := client.New("ollama", server.URL, "test-model", 0.7, "")
	cfg := config.Defaults()
	cfg.Memory.Directory = t.TempDir()
	output := &mockOutput{}
	result, msgs := Run(context.Background(), llm, tools.New(cfg), output,
		[]client.Message{{Role: "user", Content: "why did the job fail?"}}, "sys", 1)

	if result.Response != "I'll check the events." {
		t.Errorf("Response = %q, want the announcement kept as the answer", result.Response)
	}
	if len(output.warnings) != 0 {
		t.Errorf("warnings = %v, want none", output.warnings)
	}
	for _, m := range msgs {
		if m.Content == AnnouncedStepNudge {
			t.Errorf("transcript contains a nudge on the last iteration")
		}
	}
}

// A model that narrates again after being nudged is answering; nudging forever
// on a heuristic would burn the iteration budget for nothing.
func TestRunNudgesAtMostOnceInARow(t *testing.T) {
	result, _, output := newNudgeRun(t, []mockOllamaResponse{
		{content: "I'll check the events.", done: true},
		{content: "Let me look at the events.", done: true},
	})
	if result.Response != "Let me look at the events." {
		t.Errorf("Response = %q, want the second reply taken as final", result.Response)
	}
	if len(output.warnings) != 1 {
		t.Errorf("warnings = %v, want exactly one nudge", output.warnings)
	}
}

// After a tool round the model has acted, so a later narrated step is a new
// one and earns its own nudge.
func TestRunNudgeResetsAfterToolRound(t *testing.T) {
	result, _, output := newNudgeRun(t, []mockOllamaResponse{
		{content: "I'll check the logs.", done: true},
		{toolCall: bashCall("echo logs"), done: true},
		{content: "I'll check the events.", done: true},
		{toolCall: bashCall("echo events"), done: true},
		{content: "Done: transient upstream timeout.", done: true},
	})
	if result.ToolCalls != 2 || result.Response != "Done: transient upstream timeout." {
		t.Errorf("ToolCalls = %d, Response = %q", result.ToolCalls, result.Response)
	}
	if len(output.warnings) != 2 {
		t.Errorf("warnings = %v, want two nudges", output.warnings)
	}
}

// The exact string cfassist rendered as an answer on 2026-08-21 (CFOP-64):
// Ministral's tool-call wire format, which Ollama did not parse, so the
// kubectl create job it describes never ran.
const leakedMinistralCall = `bash[ARGS]{"command": "kubectl create job --from=cronjob/reservoir-ingest test-run -n data"}`

func TestLeakedToolCall(t *testing.T) {
	cases := []struct {
		text string
		want bool
	}{
		{leakedMinistralCall, true},
		{"[TOOL_CALLS]" + leakedMinistralCall, true},
		{`[TOOL_CALLS][{"name": "bash", "arguments": {"command": "ls"}}]`, true},
		{"Creating the job now.\n\n" + leakedMinistralCall, true},
		{`<tool_call>{"name": "bash", "arguments": {"command": "ls"}}</tool_call>`, true},
		{`<|python_tag|>{"name": "bash", "parameters": {"command": "ls"}}`, true},
		{`<|python_tag|>brave_search.call(query="ollama tool parsing")`, true},

		{"", false},
		{"The job ran and completed in 12s.", false},
		{"Run `kubectl create job --from=cronjob/reservoir-ingest test-run -n data` to retry.", false},
		{"args := map[string]any{\"command\": \"ls\"}", false},
		{"See [ARGS] in the docs.", false},
		// An answer that only mentions a marker is still an answer.
		{"The string [TOOL_CALLS] identifies a Mistral tool-call marker.", false},
		{"Hermes wraps calls in <tool_call> tags, which Ollama strips.", false},
		{"Llama emits <|python_tag|> before a call.", false},
	}
	for _, c := range cases {
		if _, got := leakedToolCall(c.text); got != c.want {
			t.Errorf("leakedToolCall(%q) = %v, want %v", c.text, got, c.want)
		}
	}
}

func TestLeakedToolCallQuotesFromTheMarkerAndCaps(t *testing.T) {
	call, _ := leakedToolCall("Creating the job now.\n\n" + leakedMinistralCall)
	if call != leakedMinistralCall {
		t.Errorf("call = %q, want the prose before the marker dropped", call)
	}
	long, _ := leakedToolCall(`bash[ARGS]{"command": "` + strings.Repeat("x", 1000) + `"}`)
	if n := len([]rune(long)); n != maxLeakedCallShown+1 {
		t.Errorf("quoted call is %d runes, want capped at %d plus an ellipsis", n, maxLeakedCallShown)
	}
}

// An unparsed tool call must never read as an answer: no response, an error
// that says nothing ran, a failed Result (so `cfassist -p … && next` stops),
// and a transcript that tells the next turn the call did not execute.
func TestRunSurfacesLeakedToolCallAsNothingRan(t *testing.T) {
	result, msgs, output := newNudgeRun(t, []mockOllamaResponse{
		{content: leakedMinistralCall, done: true},
	})
	if len(output.responses) != 0 {
		t.Errorf("responses = %q, want none — the raw call was rendered as an answer", output.responses)
	}
	if len(output.errors) != 1 || !strings.Contains(output.errors[0], "Nothing ran") ||
		!strings.Contains(output.errors[0], "reservoir-ingest") {
		t.Errorf("errors = %q, want one saying nothing ran and quoting the call", output.errors)
	}
	if result.Error == "" || result.Response != "" || result.ToolCalls != 0 {
		t.Errorf("result = %+v, want an error, no response, no tool calls", result)
	}
	if len(output.warnings) != 0 {
		t.Errorf("warnings = %v, want no announced-step nudge", output.warnings)
	}
	last := msgs[len(msgs)-1]
	if last.Role != "assistant" || !strings.Contains(last.Content, "did not run") {
		t.Errorf("last transcript message = %+v, want an assistant note that the call did not run", last)
	}
	if strings.Contains(last.Content, "[ARGS]") {
		t.Errorf("transcript note quotes the raw call, which the model may copy: %q", last.Content)
	}
}

// Calls the provider parsed earlier in the turn did run. Saying "nothing ran"
// after them could send an operator to repeat a mutation that happened.
func TestRunLeakedToolCallAfterRealCallSaysOnlyThatOneFailed(t *testing.T) {
	result, _, output := newNudgeRun(t, []mockOllamaResponse{
		{toolCall: bashCall("echo first"), done: true},
		{content: leakedMinistralCall, done: true},
	})
	if result.ToolCalls != 1 || result.Error == "" {
		t.Errorf("result = %+v, want one tool call and an error", result)
	}
	if len(output.errors) != 1 || strings.Contains(output.errors[0], "Nothing ran") ||
		!strings.Contains(output.errors[0], "the 1 before it did") {
		t.Errorf("errors = %q, want only the leaked call reported as not run", output.errors)
	}
}

// Structured tool calls still run when the content also carries a marker; the
// guard is for replies where the provider lifted nothing.
func TestRunIgnoresMarkerWhenToolCallsParsed(t *testing.T) {
	result, _, output := newNudgeRun(t, []mockOllamaResponse{
		{content: "[TOOL_CALLS]", toolCall: bashCall("echo ok"), done: true},
		{content: "All good.", done: true},
	})
	if result.Error != "" || result.ToolCalls != 1 || result.Response != "All good." {
		t.Errorf("result = %+v, errors = %v", result, output.errors)
	}
}
