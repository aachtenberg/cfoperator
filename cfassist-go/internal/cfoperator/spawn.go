// Cockpit spawn (CFOP-35): ask the agent for an ephemeral pod to work the
// incident in, on the affected node.
//
// A third transport rather than a method on Client, for the same reason
// sessiontoken.go is separate: Client's GET-only allowlist is the read-only
// promise of the attach data plane, and its tests fail if that widens. Spawning
// is one POST to one path, so it gets its own allowlist that admits exactly
// that and nothing else — nothing here can be bent into approving a
// remediation or minting a standing credential.
//
// What this client does NOT do is open the terminal. The agent answers with the
// pod's coordinates and the operator's own kubectl attaches: no service
// identity in the system holds pods/attach or pods/exec, and an operator
// spawning a cockpit from a laptop already has cluster credentials. The
// agent-side PTY bridge belongs to the console drawer (CFOP-59), which is where
// that RBAC question gets decided.
package cfoperator

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

// SpawnPath is the one endpoint this transport may call.
const SpawnPath = "/api/cockpit/spawn"

// Placement records where the cockpit landed and why — the unschedulable
// fallback means "the node you asked about" and "the node you got" are
// different often enough that the operator has to be told which happened.
type Placement struct {
	Node string `json:"node"`
	Note string `json:"note"`
}

// Cockpit is a spawned (or already-running) cockpit Job.
type Cockpit struct {
	Status          string    `json:"status"` // "spawned" | "existing"
	JobName         string    `json:"job_name"`
	Namespace       string    `json:"namespace"`
	InvestigationID int       `json:"investigation_id"`
	PodSelector     string    `json:"pod_selector"`
	AttachCommand   string    `json:"attach_command"`
	TTLSeconds      int       `json:"ttl_seconds"`
	TokenPrefix     string    `json:"token_prefix"`
	Placement       Placement `json:"placement"`
}

// SpawnClient asks the agent to launch a cockpit Job.
type SpawnClient struct {
	URL   string
	Token string

	http *http.Client
}

// NewSpawnClient mirrors New()'s defaults, with a longer floor: the spawn call
// creates a Job and mints a token before it answers.
func NewSpawnClient(rawURL, token string, timeout time.Duration) *SpawnClient {
	if strings.TrimSpace(rawURL) == "" {
		rawURL = DefaultAgentURL
	}
	if timeout <= 0 {
		timeout = 60 * time.Second
	}
	return &SpawnClient{
		URL:   strings.TrimRight(strings.TrimSpace(rawURL), "/"),
		Token: strings.TrimSpace(token),
		http:  &http.Client{Timeout: timeout},
	}
}

// SetHTTPClient swaps the transport (tests).
func (c *SpawnClient) SetHTTPClient(h *http.Client) {
	if h != nil {
		c.http = h
	}
}

// Spawn requests a cockpit for an investigation. The session token the pod
// uses is minted server-side and delivered to it through a Kubernetes Secret —
// it is never in this response, and never on this laptop.
func (c *SpawnClient) Spawn(investigationID int, ttl time.Duration) (*Cockpit, error) {
	payload, _ := json.Marshal(map[string]any{
		"investigation_id": investigationID,
		"ttl_seconds":      int(ttl.Seconds()),
	})
	body, err := c.do(http.MethodPost, SpawnPath, payload)
	if err != nil {
		return nil, err
	}
	var cockpit Cockpit
	if err := json.Unmarshal(body, &cockpit); err != nil || cockpit.JobName == "" {
		return nil, newError("", "CFOperator returned an unexpected cockpit response")
	}
	return &cockpit, nil
}

// do allows exactly one method on exactly one path, checked before any socket
// opens — the same guard-in-the-transport pattern as Client.do.
func (c *SpawnClient) do(method, path string, payload []byte) ([]byte, error) {
	if method != http.MethodPost || path != SpawnPath {
		return nil, newError("", "cockpit spawn client refuses %s %s", method, path)
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
		return nil, newError("Is the agent reachable? (--agent-url / CFOP_AGENT_URL)",
			"Cannot reach CFOperator at %s: %v", c.URL, err)
	}
	defer resp.Body.Close()

	body, readErr := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if readErr != nil {
		return nil, newError("", "CFOperator response could not be read: %v", readErr)
	}
	if resp.StatusCode >= 400 {
		return nil, newError(spawnHint(resp.StatusCode),
			"CFOperator returned HTTP %d for %s: %s",
			resp.StatusCode, path, snippet(body))
	}
	return body, nil
}

// spawnHint turns the three refusals an operator actually meets into their fix.
func spawnHint(status int) string {
	switch status {
	case http.StatusForbidden:
		return "spawning a cockpit is admin-only; attach without --spawn still briefs a local session"
	case http.StatusTooManyRequests:
		return "the cockpit concurrency cap is reached — exit an open cockpit, or attach without --spawn"
	case http.StatusServiceUnavailable:
		return "the agent could not mint a session token for the pod; check its auth database"
	}
	return ""
}

func snippet(body []byte) string {
	s := strings.TrimSpace(string(body))
	if len(s) > 200 {
		s = s[:200]
	}
	return fmt.Sprint(s)
}
