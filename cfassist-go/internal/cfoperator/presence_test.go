package cfoperator

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// healthy is the payload a live agent returns from /api/health.
var healthy = map[string]any{
	"status":                "ok",
	"version":               "1.0.8",
	"current_investigation": true,
	"uptime_seconds":        7500.0,
}

// probeServer answers /api/health with health and /api/investigations with
// investigationsStatus, which is how the two halves of a probe are separated:
// health is auth-exempt on the real agent, reads are not.
func probeServer(t *testing.T, health any, investigationsStatus int) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch {
		case r.URL.Path == "/api/health":
			if health == nil {
				w.WriteHeader(http.StatusNotFound)
				return
			}
			body, _ := json.Marshal(health)
			w.Write(body)
		case strings.HasPrefix(r.URL.Path, "/api/investigations"):
			if investigationsStatus != http.StatusOK {
				w.WriteHeader(investigationsStatus)
				w.Write([]byte(`{"error":"unauthorized"}`))
				return
			}
			w.Write([]byte(`{"investigations":[],"count":0}`))
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	t.Cleanup(srv.Close)
	return srv
}

func TestDetectReachableAndReadable(t *testing.T) {
	srv := probeServer(t, healthy, http.StatusOK)

	p := Detect(srv.URL, true, "test-token", 2*time.Second)

	if !p.Reachable || !p.CanRead {
		t.Fatalf("expected reachable+readable, got %+v", p)
	}
	if p.Version != "1.0.8" || !p.Busy {
		t.Errorf("health fields not carried through: %+v", p)
	}
	if p.Uptime != 7500*time.Second {
		t.Errorf("uptime = %v, want 2h05m", p.Uptime)
	}

	prompt := p.PromptSection()
	for _, want := range []string{srv.URL, "`cfoperator` tool", "read-only", "attach"} {
		if !strings.Contains(prompt, want) {
			t.Errorf("prompt missing %q:\n%s", want, prompt)
		}
	}
	if line := p.BannerLine(); !strings.Contains(line, "investigating") {
		t.Errorf("banner should say what it is doing, got %q", line)
	}
}

// A reachable agent with no usable credential is a *different* situation from
// an unreachable one, and the difference has to survive into the prompt: the
// fix is a token, not a restart.
func TestDetectReachableWithoutToken(t *testing.T) {
	srv := probeServer(t, healthy, http.StatusOK)

	p := Detect(srv.URL, false, "", 2*time.Second)

	if !p.Reachable {
		t.Fatalf("health is auth-exempt; expected reachable, got %+v", p)
	}
	if p.CanRead {
		t.Fatal("no token was supplied; reads must not be claimed to work")
	}
	prompt := p.PromptSection()
	if !strings.Contains(prompt, "CFOP_API_TOKEN") {
		t.Errorf("prompt should name the fix:\n%s", prompt)
	}
}

func TestDetectTokenRejected(t *testing.T) {
	srv := probeServer(t, healthy, http.StatusUnauthorized)

	p := Detect(srv.URL, true, "stale-token", 2*time.Second)

	if !p.Reachable || p.CanRead {
		t.Fatalf("expected reachable but unreadable, got %+v", p)
	}
	if !strings.Contains(p.Reason, "token") {
		t.Errorf("reason should point at the token, got %q", p.Reason)
	}
}

// The default address is a well-known local port. Something else answering on
// it must not be announced to the operator as their SRE agent.
func TestDetectRejectsAnUnrelatedService(t *testing.T) {
	srv := probeServer(t, map[string]any{"status": "ok", "version": "9.9"}, http.StatusOK)

	p := Detect(srv.URL, true, "test-token", 2*time.Second)

	if p.Reachable {
		t.Fatalf("a payload without current_investigation is not CFOperator: %+v", p)
	}
	if !strings.Contains(p.Reason, "not CFOperator") {
		t.Errorf("reason should say what was wrong, got %q", p.Reason)
	}
}

func TestPromptSectionWhenConfiguredButUnreachable(t *testing.T) {
	// Port 1 on loopback: nothing listens, and the refusal is immediate.
	p := Detect("http://127.0.0.1:1", true, "", 2*time.Second)

	if p.Reachable {
		t.Fatal("nothing is listening on port 1")
	}
	prompt := p.PromptSection()
	if !strings.Contains(prompt, "did not answer") {
		t.Errorf("an address the operator configured deserves to be named:\n%s", prompt)
	}
	if !strings.Contains(prompt, "do not go looking for a local process") {
		t.Errorf("prompt should head off the ps/systemctl hunt:\n%s", prompt)
	}
	if line := p.BannerLine(); !strings.Contains(line, "not answering") {
		t.Errorf("banner = %q, want it to say the configured address is dead", line)
	}
}

// The regression this whole issue is about: a session on a machine with no
// agent used to read "cfoperator" as a Unix user. The identity paragraph is
// what prevents that, so it is unconditional — including when the probe found
// nothing and when discovery is turned off (zero Presence).
func TestPromptSectionAlwaysExplainsTheWord(t *testing.T) {
	prompt := Presence{}.PromptSection()

	for _, want := range []string{"CFOperator", "not a Unix user", "ps or systemctl"} {
		if !strings.Contains(prompt, want) {
			t.Errorf("identity missing %q:\n%s", want, prompt)
		}
	}
	// An agent nobody configured and nobody is running earns no paragraph.
	if strings.Contains(prompt, "did not answer") || strings.Contains(prompt, "reachable from this machine") {
		t.Errorf("absent-and-unconfigured should stay silent:\n%s", prompt)
	}
	if line := (Presence{}).BannerLine(); line != "" {
		t.Errorf("banner = %q, want empty", line)
	}
}

func TestDetectFromConfigPrefersConfigOverEnvironment(t *testing.T) {
	srv := probeServer(t, healthy, http.StatusOK)
	env := map[string]string{
		EnvAgentURL: "http://127.0.0.1:1",
		EnvAPIToken: "env-token",
	}

	p := DetectFromConfig(srv.URL, "", func(k string) string { return env[k] })

	if !p.Reachable {
		t.Fatalf("config URL should win over the environment: %+v", p)
	}
	if !p.Configured {
		t.Error("a URL from the config file counts as configured")
	}
	if !p.CanRead {
		t.Error("token should have been taken from the environment")
	}
}

func TestShortDuration(t *testing.T) {
	cases := map[time.Duration]string{
		45 * time.Second:  "45s",
		14 * time.Minute:  "14m",
		125 * time.Minute: "2h5m",
		51 * time.Hour:    "2d3h",
	}
	for d, want := range cases {
		if got := shortDuration(d); got != want {
			t.Errorf("shortDuration(%v) = %q, want %q", d, got, want)
		}
	}
}
