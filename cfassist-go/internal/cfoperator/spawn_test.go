package cfoperator

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// TestSpawnClientRefusesAnythingButItsOneEndpoint is the guard-in-the-transport
// pattern, third instance: Client is GET-only, SessionTokenClient may touch two
// token paths, and this may POST to exactly one. A spawn client that could POST
// anywhere would be a general-purpose write client with a narrow name.
func TestSpawnClientRefusesAnythingButItsOneEndpoint(t *testing.T) {
	c := NewSpawnClient("http://example.invalid", "tok", time.Second)

	for _, tc := range []struct{ method, path string }{
		{http.MethodPost, "/api/remediations/7/approve"},
		{http.MethodPost, "/api/auth/tokens"},
		{http.MethodDelete, SpawnPath},
		{http.MethodGet, SpawnPath},
		{http.MethodPut, SpawnPath},
	} {
		if _, err := c.do(tc.method, tc.path, nil); err == nil {
			t.Errorf("spawn client allowed %s %s", tc.method, tc.path)
		} else if !strings.Contains(err.Error(), "refuses") {
			t.Errorf("%s %s failed for the wrong reason: %v", tc.method, tc.path, err)
		}
	}
}

func TestSpawnSendsTheInvestigationAndTTL(t *testing.T) {
	var got map[string]any
	var auth string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		auth = r.Header.Get("Authorization")
		json.NewDecoder(r.Body).Decode(&got)
		w.WriteHeader(http.StatusCreated)
		json.NewEncoder(w).Encode(map[string]any{
			"status": "spawned", "job_name": "cfop-cockpit-1889-010203",
			"namespace": "apps", "pod_selector": "cfop-cockpit=1889",
			"placement": map[string]any{"node": "pi4", "note": "pinned to node pi4"},
		})
	}))
	defer srv.Close()

	cockpit, err := NewSpawnClient(srv.URL, "tok", time.Second).Spawn(1889, 4*time.Hour)
	if err != nil {
		t.Fatalf("spawn: %v", err)
	}
	if got["investigation_id"] != float64(1889) {
		t.Errorf("investigation_id = %v, want 1889", got["investigation_id"])
	}
	if got["ttl_seconds"] != float64(14400) {
		t.Errorf("ttl_seconds = %v, want 14400 (the session TTL is the pod deadline)", got["ttl_seconds"])
	}
	if auth != "Bearer tok" {
		t.Errorf("Authorization = %q, want the operator's standing token", auth)
	}
	if cockpit.JobName != "cfop-cockpit-1889-010203" || cockpit.Placement.Node != "pi4" {
		t.Errorf("response not parsed: %+v", cockpit)
	}
}

// TestSpawnResponseWithoutAJobIsAnError: a 200 with an empty body would
// otherwise send the operator into a kubectl attach against "job/".
func TestSpawnResponseWithoutAJobIsAnError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte(`{"status":"spawned"}`))
	}))
	defer srv.Close()

	if _, err := NewSpawnClient(srv.URL, "tok", time.Second).Spawn(1, time.Hour); err == nil {
		t.Fatal("a response with no job name should be an error")
	}
}

// TestSpawnRefusalsCarryTheirFix: 403 (not an admin) and 429 (cap reached) are
// the two an operator actually meets, and both have a next step.
func TestSpawnRefusalsCarryTheirFix(t *testing.T) {
	for status, want := range map[int]string{
		http.StatusForbidden:       "admin",
		http.StatusTooManyRequests: "cap",
	} {
		code := status
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.WriteHeader(code)
		}))
		_, err := NewSpawnClient(srv.URL, "tok", time.Second).Spawn(1, time.Hour)
		srv.Close()
		if err == nil {
			t.Fatalf("HTTP %d should be an error", code)
		}
		apiErr, ok := err.(*Error)
		if !ok || !strings.Contains(apiErr.Hint, want) {
			t.Errorf("HTTP %d hint = %q, want it to mention %q", code, hintOf(err), want)
		}
	}
}

func hintOf(err error) string {
	if e, ok := err.(*Error); ok {
		return e.Hint
	}
	return ""
}
