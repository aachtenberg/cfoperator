package dedupe

import (
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func serve(t *testing.T, secret string) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(Handler(secret, slog.New(slog.NewTextHandler(io.Discard, nil))))
	t.Cleanup(srv.Close)
	return srv
}

func post(t *testing.T, url, token, body string) (*http.Response, map[string]any) {
	t.Helper()
	req, err := http.NewRequest(http.MethodPost, url, strings.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	if token != "" {
		req.Header.Set(AuthHeader, token)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var out map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		t.Fatalf("response is not JSON: %v", err)
	}
	return resp, out
}

// One real case over the wire: the live fold onto #362 (investigation 4886),
// so the contract's field names are checked against what the agent will send.
func liveFoldRequest(t *testing.T) string {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join("testdata", "decide", "freshet_dns_360_365.json"))
	if err != nil {
		t.Fatal(err)
	}
	var cf struct {
		Cases []struct {
			Name    string          `json:"name"`
			Request json.RawMessage `json:"request"`
		} `json:"cases"`
	}
	if err := json.Unmarshal(raw, &cf); err != nil {
		t.Fatal(err)
	}
	for _, c := range cf.Cases {
		if strings.HasPrefix(c.Name, "investigation 4886") {
			return string(c.Request)
		}
	}
	t.Fatal("the 4886 case is gone from freshet_dns_360_365.json")
	return ""
}

func TestDecideOverHTTP(t *testing.T) {
	srv := serve(t, "")
	resp, out := post(t, srv.URL+"/v1/decide", "", liveFoldRequest(t))
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status %d: %v", resp.StatusCode, out)
	}
	want := map[string]any{
		"decision": "fold", "row_id": float64(362), "tier": "target",
		"alias": "inv-dispatch-raspberrypi5-fix-freshet-dashboard-dns",
	}
	for k, v := range want {
		if out[k] != v {
			t.Errorf("%s = %#v, want %#v", k, out[k], v)
		}
	}
	for _, k := range []string{"reason", "shared", "absorbed"} {
		if _, ok := out[k]; !ok {
			t.Errorf("response has no %q", k)
		}
	}
	if _, ok := out["host"]; ok {
		t.Error("a fold names no host; the field is omitted")
	}
}

func TestSharedSecret(t *testing.T) {
	srv := serve(t, "s3cret")
	body := `{"candidate": {"dedupe_key": "k"}}`
	for _, tc := range []struct {
		token string
		code  int
	}{
		{"", http.StatusUnauthorized},
		{"wrong", http.StatusUnauthorized},
		{"s3cret", http.StatusOK},
	} {
		resp, out := post(t, srv.URL+"/v1/decide", tc.token, body)
		if resp.StatusCode != tc.code {
			t.Errorf("token %q: status %d, want %d (%v)", tc.token, resp.StatusCode, tc.code, out)
		}
	}
	// Health stays open, so a probe needs no secret.
	for _, path := range []string{"/healthz", "/livez"} {
		resp, err := http.Get(srv.URL + path)
		if err != nil {
			t.Fatal(err)
		}
		resp.Body.Close()
		if resp.StatusCode != http.StatusOK {
			t.Errorf("%s: status %d", path, resp.StatusCode)
		}
	}
}

func TestNoSecretAcceptsUnauthenticated(t *testing.T) {
	srv := serve(t, "")
	resp, out := post(t, srv.URL+"/v1/decide", "", `{"candidate": {"dedupe_key": "k"}}`)
	if resp.StatusCode != http.StatusOK || out["decision"] != "new" || out["dedupe_key"] != "k" {
		t.Fatalf("status %d, body %v", resp.StatusCode, out)
	}
}

// An empty candidate is still a candidate; only a missing one is refused.
func TestEmptyCandidateIsNew(t *testing.T) {
	srv := serve(t, "")
	resp, out := post(t, srv.URL+"/v1/decide", "", `{"candidate": {}}`)
	if resp.StatusCode != http.StatusOK || out["decision"] != "new" {
		t.Fatalf("status %d, body %v", resp.StatusCode, out)
	}
}

func TestBadRequests(t *testing.T) {
	srv := serve(t, "")
	for name, body := range map[string]string{
		"not json":         `{"candidate":`,
		"trailing data":    `{"candidate": {}} {}`,
		"trailing brace":   `{"candidate": {"dedupe_key": "k"}}}`,
		"trailing bracket": `{"candidate": {"dedupe_key": "k"}}]`,
		"no candidate":     `{"open_rows": []}`,
		"null candidate":   `{"candidate": null}`,
		"null body":        `null`,
		"empty body":       ``,
		"wrong type":       `{"candidate": {"host": 5}}`,
		"too large":        `{"candidate": {"recommendation": "` + strings.Repeat("x", maxBody) + `"}}`,
	} {
		resp, out := post(t, srv.URL+"/v1/decide", "", body)
		if resp.StatusCode != http.StatusBadRequest || out["error"] == "" {
			t.Errorf("%s: status %d, body %v", name, resp.StatusCode, out)
		}
	}
	resp, err := http.Get(srv.URL + "/v1/decide")
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusMethodNotAllowed {
		t.Errorf("GET /v1/decide: status %d", resp.StatusCode)
	}
	resp, err = http.Get(srv.URL + "/v1/nothing")
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusNotFound {
		t.Errorf("unknown path: status %d", resp.StatusCode)
	}
}
