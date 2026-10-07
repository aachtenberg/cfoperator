package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/aachtenberg/cfoperator/cfassist-go/internal/client"
	"github.com/aachtenberg/cfoperator/cfassist-go/internal/config"
	"github.com/aachtenberg/cfoperator/cfassist-go/internal/tools"
)

// A one-shot cfassist whose tool call was never parsed must exit non-zero, so
// `cfassist "restart it" && next-step` stops (CFOP-64). conversation.Run sets
// Result.Error; this guards the step from there to the process status.
func TestRunNonInteractiveFailsWhenToolCallLeaksAsText(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		json.NewEncoder(w).Encode(map[string]any{
			"message": map[string]any{
				"role":    "assistant",
				"content": `bash[ARGS]{"command": "kubectl create job --from=cronjob/reservoir-ingest test-run -n data"}`,
			},
			"done": true,
		})
	}))
	defer srv.Close()

	cfg := config.Defaults()
	cfg.Memory.Directory = t.TempDir()
	llm := client.New("ollama", srv.URL, "test-model", 0.7, "")

	err := runNonInteractive(context.Background(), cfg, llm, tools.New(cfg), "sys", "create the job")
	if err == nil {
		t.Fatal("runNonInteractive returned nil: a tool call that never ran would exit 0")
	}
}
