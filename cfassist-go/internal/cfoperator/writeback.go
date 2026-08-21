// Cockpit session write-back (CFOP-37): what a human and an agent worked out
// in a session, sent back before the session is destroyed.
//
// This is the half of "compute disposable, state central" that makes the other
// half safe to say. Without it, a cockpit is a terminal that happens to start
// briefed: the pod dies, the container is reaped, the /tmp directory is
// removed, and everything learned in it goes with them. The KB keeps only what
// the autonomous agent found — never what the team found.
//
// A fourth narrow transport rather than a method on Client, for the same reason
// sessiontoken.go and spawn.go are separate: Client's GET-only allowlist is the
// read-only promise of attach's data plane, and its tests fail if that widens.
// This one may POST to exactly two paths and nothing else, so it cannot be bent
// into approving a remediation or minting a standing credential.
//
// Both paths take the session's own `investigate` scope, so the credential that
// writes what the session learned is the credential that dies with it.
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

// LearningsPath is the write seam CFOP-47 built and named this issue as the
// consumer of. Reused rather than re-invented: the KB schema stays private to
// the monolith and token scopes stay the security model.
const LearningsPath = "/api/learnings"

// SessionOutcomes are the verdicts the agent accepts for a session. Anything
// else is a 400, so the vocabulary cannot drift by one client inventing a word.
var SessionOutcomes = []string{
	"resolved", "mitigated", "diagnosed", "no_change", "inconclusive", "escalated",
}

// SessionRecord is what a finished session says about itself.
type SessionRecord struct {
	InvestigationID int      `json:"-"`
	Outcome         string   `json:"outcome"`
	Summary         string   `json:"summary"`
	Commands        []string `json:"commands,omitempty"`
	Tier            string   `json:"tier,omitempty"`
	Host            string   `json:"host,omitempty"`
	DurationSeconds int      `json:"duration_seconds,omitempty"`
	Exchanges       int      `json:"exchanges,omitempty"`
	Model           string   `json:"model,omitempty"`
	LearningID      int      `json:"learning_id,omitempty"`
	// Degraded means the summary is a raw transcript tail because the model
	// could not distil one. Sent so the record can be *shown* as a fragment
	// rather than read as a conclusion.
	Degraded bool `json:"degraded,omitempty"`
}

// Learning is a reusable conclusion, in the shape POST /api/learnings takes.
//
// AppliesWhen is not optional and the server is not the place that discovers
// it: store_learning auto-deprecates a learning with no trigger condition,
// because retrieval can never match one — so a learning without it would be
// accepted, stored, and never seen again. Better to drop it here, loudly.
type Learning struct {
	LearningType string   `json:"learning_type"`
	Title        string   `json:"title"`
	Description  string   `json:"description"`
	AppliesWhen  string   `json:"applies_when"`
	Services     []string `json:"services,omitempty"`
	Tags         []string `json:"tags,omitempty"`
	Category     string   `json:"category,omitempty"`
	Source       string   `json:"source,omitempty"`
}

// Valid reports whether this learning can actually be retrieved once stored.
func (l *Learning) Valid() bool {
	return l != nil &&
		strings.TrimSpace(l.Title) != "" &&
		strings.TrimSpace(l.Description) != "" &&
		strings.TrimSpace(l.AppliesWhen) != ""
}

// WriteBackClient records a finished session against its investigation.
type WriteBackClient struct {
	URL   string
	Token string

	http *http.Client
}

// NewWriteBackClient mirrors New()'s defaults.
func NewWriteBackClient(rawURL, token string, timeout time.Duration) *WriteBackClient {
	if strings.TrimSpace(rawURL) == "" {
		rawURL = DefaultAgentURL
	}
	if timeout <= 0 {
		timeout = 30 * time.Second
	}
	return &WriteBackClient{
		URL:   strings.TrimRight(strings.TrimSpace(rawURL), "/"),
		Token: strings.TrimSpace(token),
		http:  &http.Client{Timeout: timeout},
	}
}

// SetHTTPClient swaps the transport (tests).
func (c *WriteBackClient) SetHTTPClient(h *http.Client) {
	if h != nil {
		c.http = h
	}
}

// SessionPath is the append endpoint for one investigation's sessions.
func SessionPath(investigationID int) string {
	return "/api/investigations/" + strconv.Itoa(investigationID) + "/session"
}

// RecordLearning seeds a reusable conclusion and returns its id.
//
// Called before RecordSession so the session record can cite the learning it
// produced. The other order would need a second write to backfill the id.
func (c *WriteBackClient) RecordLearning(l *Learning) (int, error) {
	if !l.Valid() {
		return 0, newError(
			"a learning needs a title, a description and an applies_when",
			"refusing to store a learning with no trigger condition — it would be "+
				"auto-deprecated and never retrieved")
	}
	payload, _ := json.Marshal(l)
	body, err := c.do(http.MethodPost, LearningsPath, payload)
	if err != nil {
		return 0, err
	}
	var out struct {
		ID int `json:"id"`
	}
	if err := json.Unmarshal(body, &out); err != nil || out.ID == 0 {
		return 0, newError("", "CFOperator returned an unexpected learning response")
	}
	return out.ID, nil
}

// RecordSession appends the session to its investigation.
func (c *WriteBackClient) RecordSession(rec *SessionRecord) error {
	if rec == nil || strings.TrimSpace(rec.Summary) == "" {
		return newError("", "a session record needs a summary")
	}
	payload, _ := json.Marshal(rec)
	_, err := c.do(http.MethodPost, SessionPath(rec.InvestigationID), payload)
	return err
}

// do allows exactly the two write-back endpoints, checked before any socket
// opens — the same guard-in-the-transport pattern as Client.do.
func (c *WriteBackClient) do(method, path string, payload []byte) ([]byte, error) {
	allowed := method == http.MethodPost &&
		(path == LearningsPath || isSessionPath(path))
	if !allowed {
		return nil, newError("", "write-back client refuses %s %s", method, path)
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
		return nil, newError("Is the agent still reachable? The session's own token "+
			"expires with the session.",
			"Cannot reach CFOperator at %s: %v", c.URL, err)
	}
	defer resp.Body.Close()

	body, readErr := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if readErr != nil {
		return nil, newError("", "CFOperator response could not be read: %v", readErr)
	}
	if resp.StatusCode >= 400 {
		return nil, newError(writeBackHint(resp.StatusCode),
			"CFOperator returned HTTP %d for %s: %s",
			resp.StatusCode, path, snippet(body))
	}
	return body, nil
}

// isSessionPath matches /api/investigations/<digits>/session and nothing else —
// not a prefix check, which would also admit
// /api/investigations/1/session/../../auth/tokens.
func isSessionPath(path string) bool {
	const prefix = "/api/investigations/"
	const suffix = "/session"
	if !strings.HasPrefix(path, prefix) || !strings.HasSuffix(path, suffix) {
		return false
	}
	id := path[len(prefix) : len(path)-len(suffix)]
	if id == "" {
		return false
	}
	for _, r := range id {
		if r < '0' || r > '9' {
			return false
		}
	}
	return true
}

// writeBackHint turns the refusals an operator can actually hit into their fix.
func writeBackHint(status int) string {
	switch status {
	case http.StatusUnauthorized, http.StatusForbidden:
		return "the session token may have expired — write-back needs the investigate scope"
	case http.StatusNotFound:
		return "the investigation no longer exists; nothing to attach the session to"
	case http.StatusBadRequest:
		return "the agent rejected the session record; it may predate CFOP-37"
	}
	return fmt.Sprint("")
}
