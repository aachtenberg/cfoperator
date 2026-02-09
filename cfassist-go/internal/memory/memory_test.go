package memory

import (
	"fmt"
	"os"
	"path/filepath"
	"testing"

	"github.com/aachtenberg/cfoperator/cfassist-go/internal/client"
)

func TestSaveAndLoadConversation(t *testing.T) {
	dir := t.TempDir()

	messages := []client.Message{
		{Role: "user", Content: "what is my hostname?"},
		{Role: "assistant", Content: "Let me check that for you."},
		{Role: "user", Content: "thanks"},
	}

	path, err := SaveConversation(dir, messages)
	if err != nil {
		t.Fatalf("SaveConversation failed: %v", err)
	}
	if path == "" {
		t.Fatal("SaveConversation returned empty path")
	}

	// Verify file exists
	if _, err := os.Stat(path); os.IsNotExist(err) {
		t.Fatalf("saved file does not exist: %s", path)
	}

	// Load it back
	loaded, err := LoadConversation(path)
	if err != nil {
		t.Fatalf("LoadConversation failed: %v", err)
	}

	if len(loaded) != len(messages) {
		t.Fatalf("loaded %d messages, want %d", len(loaded), len(messages))
	}

	for i, m := range loaded {
		if m.Role != messages[i].Role {
			t.Errorf("message[%d].Role = %q, want %q", i, m.Role, messages[i].Role)
		}
		if m.Content != messages[i].Content {
			t.Errorf("message[%d].Content = %q, want %q", i, m.Content, messages[i].Content)
		}
	}
}

func TestSaveEmptyConversation(t *testing.T) {
	dir := t.TempDir()

	path, err := SaveConversation(dir, nil)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if path != "" {
		t.Errorf("expected empty path for empty messages, got %q", path)
	}
}

func TestSaveCreatesDirectory(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "nested", "dir")

	messages := []client.Message{
		{Role: "user", Content: "hello"},
	}

	path, err := SaveConversation(dir, messages)
	if err != nil {
		t.Fatalf("SaveConversation failed: %v", err)
	}
	if _, err := os.Stat(path); os.IsNotExist(err) {
		t.Fatal("file should have been created in nested dir")
	}
}

func TestLoadNonexistent(t *testing.T) {
	_, err := LoadConversation("/tmp/nonexistent-cfassist-test-12345.jsonl")
	if err == nil {
		t.Error("expected error loading nonexistent file")
	}
}

func TestCleanup(t *testing.T) {
	dir := t.TempDir()

	// Create 5 conversation files with unique content so slugs differ
	for i := 0; i < 5; i++ {
		messages := []client.Message{
			{Role: "user", Content: fmt.Sprintf("unique conversation topic number %d here", i)},
		}
		if _, err := SaveConversation(dir, messages); err != nil {
			t.Fatal(err)
		}
	}

	// Verify 5 files exist
	matches, _ := filepath.Glob(filepath.Join(dir, "*.jsonl"))
	if len(matches) != 5 {
		t.Fatalf("expected 5 files, got %d", len(matches))
	}

	// Cleanup to max 3
	if err := Cleanup(dir, 3); err != nil {
		t.Fatalf("Cleanup failed: %v", err)
	}

	matches, _ = filepath.Glob(filepath.Join(dir, "*.jsonl"))
	if len(matches) != 3 {
		t.Errorf("after cleanup expected 3 files, got %d", len(matches))
	}
}

func TestCleanupNoOp(t *testing.T) {
	dir := t.TempDir()

	// Create 2 files with unique content, cleanup with limit 10 — should keep all
	for i := 0; i < 2; i++ {
		SaveConversation(dir, []client.Message{{Role: "user", Content: fmt.Sprintf("distinct message %d", i)}})
	}

	if err := Cleanup(dir, 10); err != nil {
		t.Fatalf("Cleanup failed: %v", err)
	}

	matches, _ := filepath.Glob(filepath.Join(dir, "*.jsonl"))
	if len(matches) != 2 {
		t.Errorf("expected 2 files (no deletion), got %d", len(matches))
	}
}

func TestSearchConversations(t *testing.T) {
	dir := t.TempDir()

	messages := []client.Message{
		{Role: "user", Content: "tell me about kubernetes pods"},
		{Role: "assistant", Content: "Kubernetes pods are the smallest deployable units."},
		{Role: "user", Content: "how about docker containers?"},
		{Role: "assistant", Content: "Docker containers package applications with dependencies."},
	}

	if _, err := SaveConversation(dir, messages); err != nil {
		t.Fatal(err)
	}

	// Search for "kubernetes"
	results := SearchConversations(dir, "kubernetes", 10)
	if len(results) == 0 {
		t.Fatal("expected search results for 'kubernetes'")
	}

	// Both user and assistant messages mention kubernetes
	found := false
	for _, r := range results {
		if r.Role == "user" || r.Role == "assistant" {
			found = true
		}
	}
	if !found {
		t.Error("expected user or assistant role in results")
	}
}

func TestSearchCaseInsensitive(t *testing.T) {
	dir := t.TempDir()

	messages := []client.Message{
		{Role: "user", Content: "What about PROMETHEUS?"},
	}
	SaveConversation(dir, messages)

	results := SearchConversations(dir, "prometheus", 10)
	if len(results) == 0 {
		t.Error("case-insensitive search should find 'PROMETHEUS' with query 'prometheus'")
	}
}

func TestSearchNoResults(t *testing.T) {
	dir := t.TempDir()

	messages := []client.Message{
		{Role: "user", Content: "hello world"},
	}
	SaveConversation(dir, messages)

	results := SearchConversations(dir, "nonexistent-xyz-query", 10)
	if len(results) != 0 {
		t.Errorf("expected 0 results, got %d", len(results))
	}
}

func TestSearchMaxResults(t *testing.T) {
	dir := t.TempDir()

	// Save a conversation with many matching messages
	var messages []client.Message
	for i := 0; i < 20; i++ {
		messages = append(messages, client.Message{Role: "user", Content: "test message with keyword"})
	}
	SaveConversation(dir, messages)

	results := SearchConversations(dir, "keyword", 5)
	if len(results) > 5 {
		t.Errorf("expected max 5 results, got %d", len(results))
	}
}

func TestSearchSkipsSystemAndToolMessages(t *testing.T) {
	dir := t.TempDir()

	messages := []client.Message{
		{Role: "system", Content: "You are an assistant with searchterm"},
		{Role: "tool", Content: "searchterm result"},
		{Role: "user", Content: "no match here"},
	}
	SaveConversation(dir, messages)

	results := SearchConversations(dir, "searchterm", 10)
	if len(results) != 0 {
		t.Errorf("system and tool messages should be skipped, got %d results", len(results))
	}
}

func TestSearchEmptyDir(t *testing.T) {
	dir := t.TempDir()
	results := SearchConversations(dir, "anything", 10)
	if results != nil {
		t.Errorf("expected nil for empty dir, got %v", results)
	}
}

func TestSlugify(t *testing.T) {
	tests := []struct {
		input  string
		maxLen int
		want   string
	}{
		{"Hello World", 40, "hello-world"},
		{"What is my hostname?", 20, "what-is-my-hostname"},
		{"test!!!", 40, "test"},
		{"", 40, ""},
		{"a very long sentence that exceeds the limit", 10, "a-very-lon"},
	}

	for _, tt := range tests {
		got := slugify(tt.input, tt.maxLen)
		if got != tt.want {
			t.Errorf("slugify(%q, %d) = %q, want %q", tt.input, tt.maxLen, got, tt.want)
		}
	}
}
