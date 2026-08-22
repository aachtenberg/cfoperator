package tools

import (
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/aachtenberg/cfoperator/cfassist-go/internal/cfoperator"
	"github.com/aachtenberg/cfoperator/cfassist-go/internal/config"
)

// agentStub answers the handful of agent endpoints the tool touches and
// remembers the methods it was asked for — the read-only claim is worth
// asserting from the outside, not just trusting the transport.
type agentStub struct {
	mu      sync.Mutex
	methods []string
	queries []url.Values
}

func (a *agentStub) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	a.mu.Lock()
	a.methods = append(a.methods, r.Method)
	a.queries = append(a.queries, r.URL.Query())
	a.mu.Unlock()

	w.Header().Set("Content-Type", "application/json")
	switch {
	case r.URL.Path == "/api/health":
		w.Write([]byte(`{"status":"ok","version":"1.0.8","current_investigation":false,"uptime_seconds":60}`))
	case r.URL.Path == "/api/investigations":
		w.Write([]byte(`{"investigations":[{"id":1889,"summary":"` +
			strings.Repeat("x", 900) + `"}],"count":1}`))
	case strings.HasPrefix(r.URL.Path, "/api/investigations/"):
		w.Write([]byte(`{"id":1889,"trigger":"Pod not ready","findings":{"recommendation":"raise the limit"}}`))
	case r.URL.Path == "/api/remediations":
		w.Write([]byte(`{"remediations":[{"id":7,"status":"queued","investigation_id":1889}],"count":1}`))
	case strings.HasPrefix(r.URL.Path, "/api/remediations/"):
		w.Write([]byte(`{"id":7,"status":"queued"}`))
	case r.URL.Path == "/api/kb/search":
		w.Write([]byte(`{"results":[{"id":2024,"learning":"macb NIC hang"}],"mode":"fts"}`))
	default:
		w.WriteHeader(http.StatusNotFound)
	}
}

func newCFOperatorRegistry(t *testing.T) (*Registry, *agentStub) {
	t.Helper()
	stub := &agentStub{}
	srv := httptest.NewServer(stub)
	t.Cleanup(srv.Close)

	cfg := config.Defaults()
	cfg.Memory.Directory = os.TempDir()
	r := New(cfg)
	r.AddCFOperator(cfoperator.New(srv.URL, "test-token", 5*time.Second))
	return r, stub
}

// Absent by default: on a machine with no agent, a tool that can only fail
// teaches a model to route around it.
func TestCFOperatorToolNotRegisteredByDefault(t *testing.T) {
	r := newTestRegistry()
	for _, s := range r.GetSchemas() {
		if s.Function.Name == "cfoperator" {
			t.Fatal("cfoperator tool registered without a detected instance")
		}
	}
	if _, ok := r.Execute("cfoperator", map[string]any{"action": "health"})["error"]; !ok {
		t.Error("calling it anyway should be an unknown-tool error")
	}
}

func TestCFOperatorToolRegistered(t *testing.T) {
	r, _ := newCFOperatorRegistry(t)

	var schema string
	for _, s := range r.GetSchemas() {
		if s.Function.Name == "cfoperator" {
			schema = s.Function.Description
		}
	}
	if schema == "" {
		t.Fatal("cfoperator tool missing from schemas")
	}
	// The description is the only thing steering a model away from the failure
	// this issue is about: hunting for a process instead of asking the service.
	if !strings.Contains(schema, "not ps") {
		t.Errorf("description should redirect away from process hunting: %q", schema)
	}
}

func TestCFOperatorActions(t *testing.T) {
	r, _ := newCFOperatorRegistry(t)

	t.Run("health", func(t *testing.T) {
		res := r.Execute("cfoperator", map[string]any{"action": "health"})
		health, ok := res["health"].(map[string]any)
		if !ok || health["version"] != "1.0.8" {
			t.Fatalf("health = %+v", res)
		}
	})

	t.Run("list_investigations clips long fields", func(t *testing.T) {
		res := r.Execute("cfoperator", map[string]any{"action": "list_investigations"})
		rows, ok := res["investigations"].([]map[string]any)
		if !ok || len(rows) != 1 {
			t.Fatalf("investigations = %+v", res)
		}
		summary, _ := rows[0]["summary"].(string)
		if len(summary) > maxFieldChars+len("… [clipped]") {
			t.Errorf("a 900-char field reached the model unclipped (%d chars)", len(summary))
		}
		if !strings.HasSuffix(summary, "[clipped]") {
			t.Error("clipping should be visible, not silent")
		}
	})

	t.Run("get_investigation returns the briefing", func(t *testing.T) {
		res := r.Execute("cfoperator", map[string]any{"action": "get_investigation", "id": float64(1889)})
		briefing, _ := res["briefing"].(string)
		if !strings.Contains(briefing, "Pod not ready") {
			t.Fatalf("briefing = %q", briefing)
		}
	})

	t.Run("list_remediations", func(t *testing.T) {
		res := r.Execute("cfoperator", map[string]any{"action": "list_remediations", "status": "queued"})
		if res["count"] != 1 {
			t.Fatalf("remediations = %+v", res)
		}
	})

	t.Run("get_remediation", func(t *testing.T) {
		res := r.Execute("cfoperator", map[string]any{"action": "get_remediation", "id": float64(7)})
		row, ok := res["remediation"].(map[string]any)
		if !ok || row["status"] != "queued" {
			t.Fatalf("remediation = %+v", res)
		}
	})

	t.Run("search_knowledge", func(t *testing.T) {
		res := r.Execute("cfoperator", map[string]any{"action": "search_knowledge", "query": "nic"})
		if res["mode"] != "fts" || res["count"] != 1 {
			t.Fatalf("search = %+v", res)
		}
	})
}

// The small defaults above are advisory unless they are also ceilings: a model
// is free to ask for limit: 10000, and neither /api/investigations nor
// /api/remediations caps it server-side. Left unclamped, one hopeful argument
// puts the whole queue in an 8k context.
func TestCFOperatorClampsHopefulLimits(t *testing.T) {
	cases := []struct {
		name string
		args map[string]any
		want string
	}{
		{"investigations", map[string]any{"action": "list_investigations", "limit": float64(10000)}, "50"},
		{"remediations", map[string]any{"action": "list_remediations", "limit": float64(10000)}, "50"},
		{"knowledge", map[string]any{"action": "search_knowledge", "query": "nic", "limit": float64(10000)}, "25"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			r, stub := newCFOperatorRegistry(t)
			r.Execute("cfoperator", tc.args)

			stub.mu.Lock()
			defer stub.mu.Unlock()
			if len(stub.queries) != 1 {
				t.Fatalf("expected one request, got %d", len(stub.queries))
			}
			if got := stub.queries[0].Get("limit"); got != tc.want {
				t.Errorf("agent asked for limit=%s, want %s", got, tc.want)
			}
		})
	}
}

// A sane number survives untouched — the clamp is a ceiling, not a rewrite.
func TestCFOperatorHonoursAReasonableLimit(t *testing.T) {
	r, stub := newCFOperatorRegistry(t)
	r.Execute("cfoperator", map[string]any{"action": "list_investigations", "limit": float64(3)})

	stub.mu.Lock()
	defer stub.mu.Unlock()
	if got := stub.queries[0].Get("limit"); got != "3" {
		t.Errorf("agent asked for limit=%s, want 3", got)
	}
}

func TestCFOperatorBadArguments(t *testing.T) {
	r, _ := newCFOperatorRegistry(t)

	cases := []struct {
		name string
		args map[string]any
	}{
		{"unknown action", map[string]any{"action": "approve_remediation"}},
		{"no action", map[string]any{}},
		{"get_investigation without id", map[string]any{"action": "get_investigation"}},
		{"get_remediation without id", map[string]any{"action": "get_remediation"}},
		{"search without query", map[string]any{"action": "search_knowledge"}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if _, ok := r.Execute("cfoperator", tc.args)["error"]; !ok {
				t.Errorf("expected an error for %v", tc.args)
			}
		})
	}
}

// The tool is a read tool. Its actions are named, and none of them is a write —
// but the guard that matters is that nothing it can do reaches the agent with
// anything but GET.
func TestCFOperatorToolOnlyEverGETs(t *testing.T) {
	r, stub := newCFOperatorRegistry(t)

	for _, args := range []map[string]any{
		{"action": "health"},
		{"action": "list_investigations"},
		{"action": "get_investigation", "id": float64(1889)},
		{"action": "list_remediations"},
		{"action": "get_remediation", "id": float64(7)},
		{"action": "search_knowledge", "query": "nic"},
	} {
		r.Execute("cfoperator", args)
	}

	stub.mu.Lock()
	defer stub.mu.Unlock()
	if len(stub.methods) == 0 {
		t.Fatal("no requests reached the agent")
	}
	for _, m := range stub.methods {
		if m != http.MethodGet {
			t.Fatalf("tool issued a %s", m)
		}
	}
}
