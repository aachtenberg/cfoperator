package cfoperator

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func newTestSessionClient(t *testing.T, handler http.HandlerFunc) (*SessionTokenClient, *httptest.Server) {
	t.Helper()
	srv := httptest.NewServer(handler)
	t.Cleanup(srv.Close)
	return NewSessionTokenClient(srv.URL, "standing-token", time.Second), srv
}

func TestMintPostsBoundTokenRequest(t *testing.T) {
	var got map[string]any
	var auth string
	c, _ := newTestSessionClient(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || r.URL.Path != "/api/auth/tokens" {
			t.Fatalf("unexpected request: %s %s", r.Method, r.URL.Path)
		}
		auth = r.Header.Get("Authorization")
		_ = json.NewDecoder(r.Body).Decode(&got)
		w.WriteHeader(http.StatusCreated)
		_ = json.NewEncoder(w).Encode(map[string]any{
			"id": 7, "token_prefix": "cfop_ab", "label": "cockpit-inv-1889",
			"secret": "cfop_secret",
		})
	})

	tok, err := c.Mint(1889, []string{"investigate"}, 4*time.Hour)
	if err != nil {
		t.Fatalf("mint: %v", err)
	}
	if auth != "Bearer standing-token" {
		t.Fatalf("mint must use the operator's standing credential, got %q", auth)
	}
	// The binding contract: label convention + investigation_id + seconds TTL.
	if got["label"] != "cockpit-inv-1889" {
		t.Fatalf("label = %v", got["label"])
	}
	if got["investigation_id"].(float64) != 1889 {
		t.Fatalf("investigation_id = %v", got["investigation_id"])
	}
	if got["ttl_seconds"].(float64) != (4 * time.Hour).Seconds() {
		t.Fatalf("ttl_seconds = %v", got["ttl_seconds"])
	}
	if tok.ID != 7 || tok.Secret != "cfop_secret" {
		t.Fatalf("token = %+v", tok)
	}
}

func TestMintSurfacesServerRefusal(t *testing.T) {
	// A member requesting remediate above their ceiling gets the server's
	// error verbatim — the ceiling is enforced there, not guessed at here.
	c, _ := newTestSessionClient(t, func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusBadRequest)
		_, _ = w.Write([]byte(`{"error": "role 'member' cannot grant scopes: ['remediate']"}`))
	})
	if _, err := c.Mint(1, []string{"remediate"}, time.Hour); err == nil {
		t.Fatal("expected the ceiling refusal to surface")
	}
}

func TestMintWithoutSecretIsAnError(t *testing.T) {
	// An old agent might answer 200 with an unexpected shape; a session must
	// never think it holds a credential it does not.
	c, _ := newTestSessionClient(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"ok": true}`))
	})
	if _, err := c.Mint(1, []string{"investigate"}, time.Hour); err == nil {
		t.Fatal("expected an error for a token response without a secret")
	}
}

func TestRevokeDeletesByID(t *testing.T) {
	var method, path string
	c, _ := newTestSessionClient(t, func(w http.ResponseWriter, r *http.Request) {
		method, path = r.Method, r.URL.Path
		_, _ = w.Write([]byte(`{"id": 7}`))
	})
	if err := c.Revoke(7); err != nil {
		t.Fatalf("revoke: %v", err)
	}
	if method != http.MethodDelete || path != "/api/auth/tokens/7" {
		t.Fatalf("revoke sent %s %s", method, path)
	}
}

func TestSessionClientRefusesOtherEndpoints(t *testing.T) {
	// The narrow-transport guard: this client must be unusable for anything
	// but its two endpoints — the same discipline as Client's GET-only guard.
	c, srv := newTestSessionClient(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("request reached the network: %s %s", r.Method, r.URL.Path)
	})
	_ = srv
	for _, tc := range []struct{ method, path string }{
		{http.MethodPost, "/api/remediations/1/approve"},
		{http.MethodDelete, "/api/users/1"},
		{http.MethodPut, "/api/auth/tokens"},
		{http.MethodGet, "/api/auth/tokens"},
	} {
		if _, err := c.do(tc.method, tc.path, nil); err == nil {
			t.Fatalf("%s %s was not refused", tc.method, tc.path)
		}
	}
}
