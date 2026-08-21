package cfoperator

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"
)

// recorder is a stub agent API that remembers every request that reached it.
// The count matters as much as the responses: the read-only guard's whole point
// is that a refused method never opens a socket.
type recorder struct {
	mu       sync.Mutex
	requests []*http.Request
	handler  func(w http.ResponseWriter, r *http.Request)
}

func (rec *recorder) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	rec.mu.Lock()
	rec.requests = append(rec.requests, r.Clone(r.Context()))
	rec.mu.Unlock()
	if rec.handler != nil {
		rec.handler(w, r)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.Write([]byte(`{}`))
}

func (rec *recorder) count() int {
	rec.mu.Lock()
	defer rec.mu.Unlock()
	return len(rec.requests)
}

func newTestClient(t *testing.T, handler func(w http.ResponseWriter, r *http.Request)) (*Client, *recorder) {
	t.Helper()
	rec := &recorder{handler: handler}
	srv := httptest.NewServer(rec)
	t.Cleanup(srv.Close)
	return New(srv.URL, "test-token", 5*time.Second), rec
}

func jsonHandler(t *testing.T, payload any) func(http.ResponseWriter, *http.Request) {
	t.Helper()
	body, err := json.Marshal(payload)
	if err != nil {
		t.Fatalf("marshal fixture: %v", err)
	}
	return func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.Write(body)
	}
}

// --- the read-only guard -----------------------------------------------------

// TestTransportRefusesNonGET is the guard CFOP-29 turns on. attach must not be
// able to mutate CFOperator state even by accident, so the check lives in the
// transport rather than in each helper.
func TestTransportRefusesNonGET(t *testing.T) {
	c, rec := newTestClient(t, nil)

	for _, method := range []string{
		http.MethodPost, http.MethodPut, http.MethodPatch,
		http.MethodDelete, http.MethodHead, http.MethodOptions,
	} {
		t.Run(method, func(t *testing.T) {
			_, err := c.do(method, "/api/remediations/1/approve", nil)
			if err == nil {
				t.Fatalf("%s was allowed; attach must be read-only", method)
			}
			var apiErr *Error
			if !asError(err, &apiErr) {
				t.Fatalf("expected *Error, got %T", err)
			}
			if !strings.Contains(apiErr.Message, "read-only") {
				t.Errorf("error should say why; got %q", apiErr.Message)
			}
			if apiErr.Hint == "" {
				t.Error("refusal should carry an operator-facing hint")
			}
		})
	}

	// The point of guarding in the transport: nothing was ever sent.
	if n := rec.count(); n != 0 {
		t.Fatalf("refused methods still reached the server %d time(s)", n)
	}
}

// TestAllowedMethodsIsGETOnly guards the class surface, not just one call path.
// A contributor who adds POST to the allowlist to build an approve helper fails
// here, which is the point at which the design decision should be re-argued.
func TestAllowedMethodsIsGETOnly(t *testing.T) {
	if len(allowedMethods) != 1 {
		t.Fatalf("allowedMethods should contain exactly GET, got %v", allowedMethods)
	}
	if !allowedMethods[http.MethodGet] {
		t.Fatalf("GET must be allowed, got %v", allowedMethods)
	}
}

func TestGETIsAllowed(t *testing.T) {
	c, rec := newTestClient(t, jsonHandler(t, map[string]any{"ok": true}))
	if _, err := c.do(http.MethodGet, "/api/investigations/1", nil); err != nil {
		t.Fatalf("GET should be allowed: %v", err)
	}
	if rec.count() != 1 {
		t.Fatalf("expected 1 request, got %d", rec.count())
	}
}

// --- auth and transport behaviour --------------------------------------------

func TestBearerTokenIsSent(t *testing.T) {
	var got string
	c, _ := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		got = r.Header.Get("Authorization")
		w.Write([]byte(`{}`))
	})
	if _, err := c.do(http.MethodGet, "/api/investigations/1", nil); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if got != "Bearer test-token" {
		t.Errorf("Authorization header = %q, want %q", got, "Bearer test-token")
	}
}

func TestUnauthorizedCarriesMintHint(t *testing.T) {
	c, _ := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
	})
	_, err := c.GetInvestigation(1)
	var apiErr *Error
	if !asError(err, &apiErr) {
		t.Fatalf("expected *Error, got %v", err)
	}
	if !strings.Contains(apiErr.Hint, "admin?tab=tokens") {
		t.Errorf("401 hint should point at the token mint page, got %q", apiErr.Hint)
	}
	if !strings.Contains(apiErr.Hint, EnvAPIToken) {
		t.Errorf("401 hint should name %s, got %q", EnvAPIToken, apiErr.Hint)
	}
}

// TestUnreachableAgentHintNamesTheAddressFixes: "cannot reach the agent" is
// most often "the URL points at the wrong host", and the two things that fix
// that are the env var and the config block. The hint used to name only
// `kubectl port-forward`, which silently assumes kubectl and a kubeconfig on
// this machine — an assumption that fails on exactly the hardware attach is for
// (CFOP-63 was reported from a Raspberry Pi) and leaves the operator with
// nothing to act on.
func TestUnreachableAgentHintNamesTheAddressFixes(t *testing.T) {
	// A closed httptest server: an address the OS will refuse, rather than a
	// guessed port that might turn out to be in use on the runner.
	srv := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
	dead := srv.URL
	srv.Close()

	c := New(dead, "test-token", 2*time.Second)
	_, err := c.GetInvestigation(1889)
	var apiErr *Error
	if !asError(err, &apiErr) {
		t.Fatalf("expected *Error, got %v", err)
	}
	for _, want := range []string{EnvAgentURL, "cfoperator:", "config.yaml"} {
		if !strings.Contains(apiErr.Hint, want) {
			t.Errorf("unreachable-agent hint should name %s, got %q", want, apiErr.Hint)
		}
	}
	if !strings.Contains(apiErr.Hint, dead) {
		t.Errorf("the hint should quote the address that failed (%s), got %q", dead, apiErr.Hint)
	}
}

func TestNonJSONResponseIsAnError(t *testing.T) {
	c, _ := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte(`<html>a proxy login page</html>`))
	})
	if _, err := c.GetInvestigation(1); err == nil {
		t.Fatal("HTML response should not parse as an investigation")
	}
}

// --- reads --------------------------------------------------------------------

func TestGetInvestigationKeepsNestedFindings(t *testing.T) {
	c, _ := newTestClient(t, jsonHandler(t, map[string]any{
		"id":      1889,
		"outcome": "needs_action",
		"findings": map[string]any{
			"response":       "the report body",
			"recommendation": "restart it",
			"provider":       "gemma4:26b",
		},
	}))
	inv, err := c.GetInvestigation(1889)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	findings, ok := inv["findings"].(map[string]any)
	if !ok {
		t.Fatalf("findings should survive as a map, got %T", inv["findings"])
	}
	if findings["response"] != "the report body" {
		t.Errorf("nested response lost: %v", findings["response"])
	}
}

// TestRemediationsFilteredClientSide pins the documented workaround: the server
// has no ?investigation_id= filter, so the client asks for the list and matches
// locally. If a server-side filter is ever added this test still passes, but the
// comment in RemediationsForInvestigation is the place to revisit.
func TestRemediationsFilteredClientSide(t *testing.T) {
	var gotQuery string
	c, _ := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		gotQuery = r.URL.RawQuery
		json.NewEncoder(w).Encode(map[string]any{
			"remediations": []map[string]any{
				{"id": 1, "investigation_id": 1889, "status": "queued"},
				{"id": 2, "investigation_id": 1890, "status": "queued"},
				{"id": 3, "investigation_id": 1889, "status": "approved"},
				{"id": 4, "status": "queued"}, // no link at all
			},
		})
	})

	rows, truncated, err := c.RemediationsForInvestigation(1889, 200)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if truncated {
		t.Error("a short page must not be reported as truncated")
	}
	if len(rows) != 2 {
		t.Fatalf("expected 2 linked rows, got %d: %v", len(rows), rows)
	}
	for _, row := range rows {
		if asInt(row["investigation_id"]) != 1889 {
			t.Errorf("row %v is not linked to 1889", row["id"])
		}
	}
	if strings.Contains(gotQuery, "investigation_id") {
		t.Errorf("client sent an unsupported investigation_id filter: %q", gotQuery)
	}
}

// TestRemediationsUnlinkedRowsExcluded guards the zero-value trap: a row with no
// investigation_id must not match investigation 0 or be swept in by a loose
// comparison.
func TestRemediationsUnlinkedRowsExcluded(t *testing.T) {
	c, _ := newTestClient(t, jsonHandler(t, map[string]any{
		"remediations": []map[string]any{{"id": 4, "status": "queued"}},
	}))
	rows, _, err := c.RemediationsForInvestigation(1889, 200)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(rows) != 0 {
		t.Fatalf("unlinked row should not match, got %v", rows)
	}
}

func TestSearchKnowledgeReturnsMode(t *testing.T) {
	for _, mode := range []string{"hybrid", "fts"} {
		t.Run(mode, func(t *testing.T) {
			c, _ := newTestClient(t, jsonHandler(t, map[string]any{
				"mode":    mode,
				"results": []map[string]any{{"id": 7, "title": "a learning"}},
			}))
			rows, got, err := c.SearchKnowledge("etcd timeouts", 5)
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if got != mode {
				t.Errorf("mode = %q, want %q", got, mode)
			}
			if len(rows) != 1 {
				t.Errorf("expected 1 row, got %d", len(rows))
			}
		})
	}
}

// --- composed read ------------------------------------------------------------

// TestCollectDegradesOnEnrichmentFailure is the "mid-incident" contract: losing
// the remediation queue or the KB must cost a warning line, not the briefing.
func TestCollectDegradesOnEnrichmentFailure(t *testing.T) {
	c, _ := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		switch {
		case strings.HasPrefix(r.URL.Path, "/api/investigations/"):
			json.NewEncoder(w).Encode(map[string]any{
				"id": 1889, "trigger": "etcd is unhappy",
			})
		default:
			// Both /api/remediations and /api/kb/search fall over.
			w.WriteHeader(http.StatusInternalServerError)
		}
	})

	ctx, err := c.CollectAttachContext(1889, 5, 200)
	if err != nil {
		t.Fatalf("enrichment failure must not fail the whole attach: %v", err)
	}
	if len(ctx.Warnings) != 2 {
		t.Fatalf("expected a warning for each failed lookup, got %v", ctx.Warnings)
	}
	if asInt(ctx.Investigation["id"]) != 1889 {
		t.Error("the investigation itself should still be present")
	}
}

// TestCollectFailsWithoutInvestigation is the other half: no investigation means
// no briefing, and that must be loud.
func TestCollectFailsWithoutInvestigation(t *testing.T) {
	c, _ := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusNotFound)
	})
	if _, err := c.CollectAttachContext(999999, 5, 200); err == nil {
		t.Fatal("a missing investigation must be an error, not an empty briefing")
	}
}

func TestCollectRejectsEmptyInvestigationBody(t *testing.T) {
	// A 200 with no id — the shape a degraded/partial row would produce.
	c, _ := newTestClient(t, jsonHandler(t, map[string]any{}))
	if _, err := c.CollectAttachContext(1889, 5, 200); err == nil {
		t.Fatal("an investigation with no id must not produce a briefing")
	}
}

func TestCollectSkipsKBSearchWithoutTrigger(t *testing.T) {
	var paths []string
	c, _ := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		paths = append(paths, r.URL.Path)
		switch {
		case strings.HasPrefix(r.URL.Path, "/api/investigations/"):
			json.NewEncoder(w).Encode(map[string]any{"id": 1889})
		default:
			json.NewEncoder(w).Encode(map[string]any{"remediations": []any{}})
		}
	})
	if _, err := c.CollectAttachContext(1889, 5, 200); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	for _, p := range paths {
		if p == "/api/kb/search" {
			t.Error("no trigger text means there is nothing to search for")
		}
	}
}

// --- endpoint resolution ------------------------------------------------------

func TestResolveEndpointPrefersConfigOverEnv(t *testing.T) {
	env := map[string]string{
		EnvAgentURL: "http://from-env:8083",
		EnvAPIToken: "env-token",
	}
	lookup := func(k string) string { return env[k] }

	url, token, timeout := ResolveEndpoint("http://from-config:9000", "cfg-token", 12, lookup)
	if url != "http://from-config:9000" {
		t.Errorf("config url should win, got %q", url)
	}
	if token != "cfg-token" {
		t.Errorf("config token should win, got %q", token)
	}
	if timeout != 12*time.Second {
		t.Errorf("timeout = %v, want 12s", timeout)
	}
}

func TestResolveEndpointFallsBackToEnv(t *testing.T) {
	env := map[string]string{
		EnvAgentURL: "http://from-env:8083/",
		EnvAPIToken: "env-token",
	}
	lookup := func(k string) string { return env[k] }

	url, token, timeout := ResolveEndpoint("", "", 0, lookup)
	if url != "http://from-env:8083" {
		t.Errorf("env url should be used and trimmed, got %q", url)
	}
	if token != "env-token" {
		t.Errorf("env token should be used, got %q", token)
	}
	if timeout != 30*time.Second {
		t.Errorf("default timeout = %v, want 30s", timeout)
	}
}

func TestResolveEndpointDefaultsToLocalPortForward(t *testing.T) {
	url, token, _ := ResolveEndpoint("", "", 0, func(string) string { return "" })
	if url != DefaultAgentURL {
		t.Errorf("url = %q, want %q", url, DefaultAgentURL)
	}
	if token != "" {
		t.Errorf("token should be empty with nothing configured, got %q", token)
	}
}

// asError is errors.As specialised to *Error, kept here so the assertions above
// read as one line.
func asError(err error, target **Error) bool {
	for err != nil {
		if e, ok := err.(*Error); ok {
			*target = e
			return true
		}
		type unwrapper interface{ Unwrap() error }
		u, ok := err.(unwrapper)
		if !ok {
			return false
		}
		err = u.Unwrap()
	}
	return false
}

// TestRemediationsTruncationIsReported guards the lie this briefing could
// otherwise tell. The queue is newest-first, so filtering client-side over a
// full page cannot distinguish "nothing is linked" from "nothing is linked in
// the newest N" — and an empty remediation section reads as the former. When
// the window is full the caller must be told, so the briefing can say so
// instead of asserting a clean queue mid-incident.
func TestRemediationsTruncationIsReported(t *testing.T) {
	full := make([]map[string]any, 3)
	for i := range full {
		full[i] = map[string]any{"id": i + 1, "investigation_id": 9999, "status": "queued"}
	}
	c, _ := newTestClient(t, jsonHandler(t, map[string]any{"remediations": full}))

	rows, truncated, err := c.RemediationsForInvestigation(1889, 3) // limit == page size
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(rows) != 0 {
		t.Fatalf("no row is linked to 1889, got %v", rows)
	}
	if !truncated {
		t.Fatal("a full page with no match must report truncated — otherwise an " +
			"empty section claims nothing was ever queued")
	}
}
