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
			"status": "spawned", "tier": "pod", "job_name": "cfop-cockpit-1889-010203",
			"namespace": "apps", "pod_selector": "cfop-cockpit=1889",
			"attach_argv": []string{"kubectl", "attach", "-it", "-n", "apps",
				"job/cfop-cockpit-1889-010203"},
			"placement": map[string]any{"node": "pi4", "note": "pinned to node pi4"},
		})
	}))
	defer srv.Close()

	cockpit, err := NewSpawnClient(srv.URL, "tok", time.Second).Spawn(1889, 4*time.Hour, TierAuto, "")
	if err != nil {
		t.Fatalf("spawn: %v", err)
	}
	if got["investigation_id"] != float64(1889) {
		t.Errorf("investigation_id = %v, want 1889", got["investigation_id"])
	}
	if got["ttl_seconds"] != float64(14400) {
		t.Errorf("ttl_seconds = %v, want 14400 (the session TTL is the pod deadline)", got["ttl_seconds"])
	}
	if got["tier"] != TierAuto {
		t.Errorf("tier = %v, want %q — the server picks the rung, the client asks for auto",
			got["tier"], TierAuto)
	}
	if got["host"] != "" {
		t.Errorf("host = %v, want empty — the agent resolves it unless told otherwise",
			got["host"])
	}
	if auth != "Bearer tok" {
		t.Errorf("Authorization = %q, want the operator's standing token", auth)
	}
	if cockpit.JobName != "cfop-cockpit-1889-010203" || cockpit.Placement.Node != "pi4" {
		t.Errorf("response not parsed: %+v", cockpit)
	}
}

// TestSpawnResponseWithoutAnAttachIsAnError: a 200 with no attach argv would
// otherwise send the operator into exec.Command against an empty slice, which
// panics. The check is on the argv rather than on a name because the name
// differs by tier and a session nothing can attach to is useless whatever it
// is called.
func TestSpawnResponseWithoutAnAttachIsAnError(t *testing.T) {
	for _, body := range []string{
		`{"status":"spawned"}`,
		`{"status":"spawned","job_name":"cfop-cockpit-1","attach_command":"kubectl attach x"}`,
	} {
		payload := body
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.Write([]byte(payload))
		}))
		_, err := NewSpawnClient(srv.URL, "tok", time.Second).Spawn(1, time.Hour, TierAuto, "")
		srv.Close()
		if err == nil {
			t.Fatalf("a response with no attach argv should be an error (body %s)", payload)
		}
	}
}

// TestSpawnDefaultsTheTierForAnOlderAgent: an agent from before the ladder
// answers without a tier field, and everything it can spawn is a pod. Without
// the default the client would skip the readiness wait and attach to a Pending
// pod — a regression visible only against a not-yet-upgraded agent.
func TestSpawnDefaultsTheTierForAnOlderAgent(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		json.NewEncoder(w).Encode(map[string]any{
			"status": "spawned", "job_name": "cfop-cockpit-1-010203", "namespace": "apps",
			"attach_argv": []string{"kubectl", "attach", "-it", "-n", "apps", "job/x"},
		})
	}))
	defer srv.Close()

	cockpit, err := NewSpawnClient(srv.URL, "tok", time.Second).Spawn(1, time.Hour, TierAuto, "")
	if err != nil {
		t.Fatalf("spawn: %v", err)
	}
	if cockpit.Tier != TierPod {
		t.Errorf("tier = %q, want %q for an agent that does not send one", cockpit.Tier, TierPod)
	}
}

// TestCockpitWhereReadsAsALocation: the operator is told where their session
// landed, and "apps/cfop-cockpit-9" and "raspberrypi5:cfop-cockpit-9" are
// different enough places that the message has to distinguish them.
func TestCockpitWhereReadsAsALocation(t *testing.T) {
	pod := &Cockpit{Tier: TierPod, Namespace: "apps", JobName: "cfop-cockpit-9-01"}
	if got := pod.Where(); got != "apps/cfop-cockpit-9-01" {
		t.Errorf("pod Where() = %q", got)
	}
	host := &Cockpit{Tier: TierHost, Host: "raspberrypi5", SessionName: "cfop-cockpit-9"}
	if got := host.Where(); got != "raspberrypi5:cfop-cockpit-9" {
		t.Errorf("host Where() = %q", got)
	}
}

// TestSpawnRefusalsCarryTheirFix: 403 (not an admin) and 429 (cap reached) are
// the two an operator actually meets, and both have a next step.
func TestSpawnRefusalsCarryTheirFix(t *testing.T) {
	for status, want := range map[int]string{
		http.StatusForbidden:       "admin",
		http.StatusTooManyRequests: "cap",
		http.StatusConflict:        "--tier",
	} {
		code := status
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.WriteHeader(code)
		}))
		_, err := NewSpawnClient(srv.URL, "tok", time.Second).Spawn(1, time.Hour, TierAuto, "")
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

// TestSpawnSendsAnExplicitHostOverride: the agent resolves the affected machine
// from remediation rows and trigger text, which is a heuristic. The operator
// staring at the incident can see what it cannot, so the override has to reach
// the server rather than being resolved (or ignored) here.
func TestSpawnSendsAnExplicitHostOverride(t *testing.T) {
	var got map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		json.NewDecoder(r.Body).Decode(&got)
		w.WriteHeader(http.StatusCreated)
		json.NewEncoder(w).Encode(map[string]any{
			"status": "spawned", "tier": TierSSH, "session_name": "cfop-cockpit-1",
			"host":        "raspberrypi5",
			"attach_argv": []string{"ssh", "-t", "sre@10.0.0.15", "/tmp/cfop-cockpit-1/run"},
		})
	}))
	defer srv.Close()

	if _, err := NewSpawnClient(srv.URL, "tok", time.Second).
		Spawn(1, time.Hour, TierAuto, " raspberrypi5 "); err != nil {
		t.Fatalf("spawn: %v", err)
	}
	if got["host"] != "raspberrypi5" {
		t.Errorf("host = %v, want the trimmed override", got["host"])
	}
}
