// Cockpit session tokens (CFOP-32): a credential minted at attach time that
// dies with the session — revoked on clean exit, expired by TTL regardless.
//
// Deliberately NOT part of Client: Client's transport-level GET-only guard is
// the read-only promise of attach's data plane, and its tests fail if
// allowedMethods widens. Minting a session credential is a different, equally
// narrow contract, so it gets its own transport with its own allowlist: only
// POST /api/auth/tokens and DELETE /api/auth/tokens/<id> — nothing here can be
// bent into approving a remediation.
package cfoperator

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"strings"
	"time"
)

// SessionToken is a minted cockpit credential. Secret is the only copy that
// will ever exist in cleartext — the store keeps a digest.
type SessionToken struct {
	ID     int    `json:"id"`
	Prefix string `json:"token_prefix"`
	Label  string `json:"label"`
	Secret string `json:"secret"`
}

// SessionTokenClient mints and revokes cockpit session tokens using the
// operator's standing credential. Same endpoint/auth conventions as Client.
type SessionTokenClient struct {
	URL   string
	Token string

	http *http.Client
}

// NewSessionTokenClient mirrors New()'s defaults.
func NewSessionTokenClient(rawURL, token string, timeout time.Duration) *SessionTokenClient {
	if strings.TrimSpace(rawURL) == "" {
		rawURL = DefaultAgentURL
	}
	if timeout <= 0 {
		timeout = 30 * time.Second
	}
	return &SessionTokenClient{
		URL:   strings.TrimRight(strings.TrimSpace(rawURL), "/"),
		Token: strings.TrimSpace(token),
		http:  &http.Client{Timeout: timeout},
	}
}

// SetHTTPClient swaps the transport (tests).
func (c *SessionTokenClient) SetHTTPClient(h *http.Client) {
	if h != nil {
		c.http = h
	}
}

// Mint creates a token labeled cockpit-inv-<id>, bound to the investigation in
// the server's audit log, expiring after ttl.
func (c *SessionTokenClient) Mint(investigationID int, scopes []string, ttl time.Duration) (*SessionToken, error) {
	payload, _ := json.Marshal(map[string]any{
		"label":            fmt.Sprintf("cockpit-inv-%d", investigationID),
		"scopes":           scopes,
		"ttl_seconds":      int(ttl.Seconds()),
		"investigation_id": investigationID,
	})
	body, err := c.do(http.MethodPost, "/api/auth/tokens", payload)
	if err != nil {
		return nil, err
	}
	var tok SessionToken
	if err := json.Unmarshal(body, &tok); err != nil || tok.Secret == "" {
		return nil, newError("", "CFOperator returned an unexpected token response")
	}
	return &tok, nil
}

// Revoke kills a minted token; the deferred call on session exit.
func (c *SessionTokenClient) Revoke(tokenID int) error {
	_, err := c.do(http.MethodDelete, "/api/auth/tokens/"+strconv.Itoa(tokenID), nil)
	return err
}

// do allows exactly the two session-token endpoints, checked before any socket
// opens — the same guard-in-the-transport pattern as Client.do.
func (c *SessionTokenClient) do(method, path string, payload []byte) ([]byte, error) {
	allowed := (method == http.MethodPost && path == "/api/auth/tokens") ||
		(method == http.MethodDelete && strings.HasPrefix(path, "/api/auth/tokens/"))
	if !allowed {
		return nil, newError("", "session-token client refuses %s %s", method, path)
	}

	var reqBody io.Reader
	if payload != nil {
		reqBody = bytes.NewReader(payload)
	}
	req, err := http.NewRequest(method, c.URL+path, reqBody)
	if err != nil {
		return nil, newError("", "bad request URL %s%s: %v", c.URL, path, err)
	}
	req.Header.Set("Accept", "application/json")
	if payload != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if c.Token != "" {
		req.Header.Set("Authorization", "Bearer "+c.Token)
	}

	resp, err := c.http.Do(req)
	if err != nil {
		return nil, newError("", "Cannot reach CFOperator at %s: %v", c.URL, err)
	}
	defer resp.Body.Close()

	body, readErr := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if readErr != nil {
		return nil, newError("", "CFOperator response could not be read: %v", readErr)
	}
	if resp.StatusCode >= 400 {
		snippet := string(body)
		if len(snippet) > 200 {
			snippet = snippet[:200]
		}
		return nil, newError("", "CFOperator returned HTTP %d for %s: %s",
			resp.StatusCode, path, snippet)
	}
	return body, nil
}
