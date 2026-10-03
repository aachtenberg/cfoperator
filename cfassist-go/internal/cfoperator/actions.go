// Terminal-session writes against the agent API.
//
// Deliberately NOT part of Client. Client's transport-level GET-only guard is
// the read-only promise of `cfassist attach`, and its tests fail if
// allowedMethods widens. A plain `cfassist` the operator is sitting in is a
// different session: they ask it to close an investigation or send a
// remediation, and a refusal there is the bug. This client can do exactly
// those four POSTs and nothing else, so it cannot be bent into minting a
// token or calling an arbitrary path.
package cfoperator

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"regexp"
	"strings"
	"time"
)

// The four writes a terminal session may make. Same routes console chat and
// the MCP server already call, and the same role gate (admin / remediate).
var (
	remediationActionPath = regexp.MustCompile(`^/api/remediations/(\d+)/(approve|reject|resolve)$`)
	triageActionPath      = regexp.MustCompile(`^/api/investigations/(\d+)/triage$`)
)

// ActionClient posts the operator's verdict. Construct one from a read Client
// with Client.Actions so the address and token cannot drift from the reads.
type ActionClient struct {
	URL   string
	Token string

	http *http.Client
	ctx  context.Context
}

// NewActionClient mirrors New()'s defaults. Prefer Client.Actions.
func NewActionClient(rawURL, token string, timeout time.Duration) *ActionClient {
	if strings.TrimSpace(rawURL) == "" {
		rawURL = DefaultAgentURL
	}
	if timeout <= 0 {
		timeout = 30 * time.Second
	}
	return &ActionClient{
		URL:   strings.TrimRight(strings.TrimSpace(rawURL), "/"),
		Token: strings.TrimSpace(token),
		http: &http.Client{
			Timeout: timeout,
			// A 301/302 would turn this POST into a GET of wherever the
			// redirect points, past the path allowlist. Refuse to follow.
			CheckRedirect: func(*http.Request, []*http.Request) error {
				return http.ErrUseLastResponse
			},
		},
	}
}

// Actions returns the write client for this read client: same address, token
// and deadline. Attach must not call it. The plain session does.
func (c *Client) Actions() *ActionClient {
	timeout := 30 * time.Second
	if c.http != nil && c.http.Timeout > 0 {
		timeout = c.http.Timeout
	}
	a := NewActionClient(c.URL, c.Token, timeout)
	a.ctx = c.ctx
	return a
}

// WithContext bounds the posts to the turn, the same way Client.WithContext
// bounds the reads.
func (c *ActionClient) WithContext(ctx context.Context) *ActionClient {
	bound := *c
	bound.ctx = ctx
	return &bound
}

// SetHTTPClient swaps the transport (tests).
func (c *ActionClient) SetHTTPClient(h *http.Client) {
	if h != nil {
		c.http = h
	}
}

// ApproveRemediation hands the row to the executor (status -> queued). The
// API refuses manual-class rows and rows whose PR is already open; that 409
// comes back as the error.
func (c *ActionClient) ApproveRemediation(id int) (map[string]any, error) {
	return c.postJSON(fmt.Sprintf("/api/remediations/%d/approve", id), nil)
}

// RejectRemediation closes the row as unwanted. note is the audit trail.
func (c *ActionClient) RejectRemediation(id int, note string) (map[string]any, error) {
	note = clipNote(note)
	if note == "" {
		return nil, newError("", "a note is required — it is the only record of why this was rejected")
	}
	return c.postJSON(fmt.Sprintf("/api/remediations/%d/reject", id), map[string]any{"note": note})
}

// ResolveRemediation closes the row as done. It does not execute anything,
// and it does not triage the investigation the row came from.
func (c *ActionClient) ResolveRemediation(id int, note string) (map[string]any, error) {
	note = clipNote(note)
	if note == "" {
		return nil, newError("", "a note is required — it is the only record of why this was closed")
	}
	return c.postJSON(fmt.Sprintf("/api/remediations/%d/resolve", id), map[string]any{"note": note})
}

// TriageInvestigation records the operator's verdict. action is "resolved"
// or "ack". It writes triage_action and leaves the agent's outcome alone.
func (c *ActionClient) TriageInvestigation(id int, action, note string) (map[string]any, error) {
	action = strings.TrimSpace(action)
	if action != "resolved" && action != "ack" {
		return nil, newError("", "triage action must be \"resolved\" or \"ack\", got %q", action)
	}
	note = clipNote(note)
	if note == "" {
		return nil, newError("", "a note is required — it is the only record of why this was triaged")
	}
	return c.postJSON(fmt.Sprintf("/api/investigations/%d/triage", id), map[string]any{
		"action": action,
		"note":   note,
	})
}

func clipNote(note string) string {
	note = strings.TrimSpace(note)
	// Runes, not bytes: the route clips with Python's [:2000], which counts
	// characters, and a byte slice can split a multibyte rune.
	r := []rune(note)
	if len(r) > 2000 {
		note = string(r[:2000])
	}
	return note
}

func (c *ActionClient) postJSON(path string, payload map[string]any) (map[string]any, error) {
	var body []byte
	if payload != nil {
		var err error
		body, err = json.Marshal(payload)
		if err != nil {
			return nil, newError("", "could not encode %s: %v", path, err)
		}
	}
	raw, err := c.do(http.MethodPost, path, body)
	if err != nil {
		return nil, err
	}
	var out map[string]any
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil, newError("", "CFOperator returned a non-JSON response for %s", path)
	}
	return out, nil
}

func (c *ActionClient) do(method, path string, payload []byte) ([]byte, error) {
	if !actionAllowed(method, path) {
		return nil, newError("", "cfassist action client refuses %s %s", method, path)
	}

	var reqBody io.Reader
	if payload != nil {
		reqBody = bytes.NewReader(payload)
	}
	ctx := c.ctx
	if ctx == nil {
		ctx = context.Background()
	}
	req, err := http.NewRequestWithContext(ctx, method, c.URL+path, reqBody)
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

	raw, readErr := io.ReadAll(io.LimitReader(resp.Body, 8<<20))
	if readErr != nil {
		return nil, newError("", "CFOperator response could not be read: %v", readErr)
	}
	switch {
	case resp.StatusCode == http.StatusUnauthorized:
		return nil, newError(
			fmt.Sprintf("Mint a token at %s/admin?tab=tokens and export %s, "+
				"or set cfoperator.token in ~/.cfassist/config.yaml.", c.URL, EnvAPIToken),
			"CFOperator rejected the API token (HTTP %d)", resp.StatusCode,
		)
	case resp.StatusCode == http.StatusForbidden:
		return nil, newError(
			"The token authenticated, but closing and acting need an admin role "+
				"(the remediate scope). A member token cannot do this.",
			"CFOperator refused the action (HTTP %d)", resp.StatusCode,
		)
	case resp.StatusCode >= 300 && resp.StatusCode < 400:
		return nil, newError("", "CFOperator redirected %s (HTTP %d); refusing to follow", path, resp.StatusCode)
	case resp.StatusCode >= 400:
		snippet := string(raw)
		if len(snippet) > 200 {
			snippet = snippet[:200]
		}
		return nil, newError("", "CFOperator returned HTTP %d for %s: %s",
			resp.StatusCode, path, snippet)
	}
	return raw, nil
}

func actionAllowed(method, path string) bool {
	if method != http.MethodPost {
		return false
	}
	return remediationActionPath.MatchString(path) || triageActionPath.MatchString(path)
}
