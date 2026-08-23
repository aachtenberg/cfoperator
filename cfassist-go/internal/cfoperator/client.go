// Package cfoperator is a read-only client for the CFOperator agent API — the
// data source for `cfassist attach`.
//
// `cfassist attach <investigation-id>` runs from the operator's laptop, not from
// inside the cluster, so everything it needs comes over the same HTTP API the
// console and the MCP facade use. Auth is the console's database-backed bearer
// token (auth_api_tokens, minted at /admin?tab=tokens); nothing new is
// introduced here. The environment variable names match mcp_server/client.py so
// a workstation already configured for the MCP server needs no extra setup.
//
// # Read-only is enforced, not merely intended
//
// do refuses any method outside allowedMethods, which contains only GET.
// Approving, rejecting or queueing a remediation is a console/MCP action; an
// attached session must not be able to reach for it even by accident, because
// the whole premise of handing an incident to a terminal agent is that the
// handoff itself changes nothing. The guard lives in the transport rather than
// in each helper, so a later contributor adding a `POST /approve` helper gets a
// failure in their first test run instead of a surprising mutation in
// production.
package cfoperator

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"strings"
	"time"
)

// DefaultAgentURL is where a port-forwarded agent lands by convention:
// kubectl -n apps port-forward svc/cfoperator 8083:8083
const DefaultAgentURL = "http://127.0.0.1:8083"

// Environment variable names, deliberately the same ones mcp_server/client.py
// reads. CFOP_API_TOKEN is no longer a single shared secret (retired
// 2026-08-09, see docs/auth.md) — it is the variable each consumer mounts *its
// own* token as, which is exactly what cfassist is doing.
const (
	EnvAgentURL = "CFOP_AGENT_URL"
	EnvAPIToken = "CFOP_API_TOKEN"
)

// allowedMethods is the read-only guard. Not a convention — do() checks it.
var allowedMethods = map[string]bool{http.MethodGet: true}

// Error is a CFOperator API call that could not be completed.
//
// Carries an optional operator-facing Hint: the CLI prints it under the error,
// because most failures here are configuration (no token, wrong URL, agent not
// port-forwarded) rather than genuine faults.
type Error struct {
	Message string
	Hint    string
}

func (e *Error) Error() string { return e.Message }

func newError(hint, format string, args ...any) *Error {
	return &Error{Message: fmt.Sprintf(format, args...), Hint: hint}
}

// Client is a synchronous, read-only HTTP client for the agent API.
type Client struct {
	URL   string
	Token string

	http *http.Client
	// ctx bounds every request this client makes. Nil means unbounded, which
	// is what the long-lived clients built at startup want.
	ctx context.Context
}

// New builds a client. An empty url falls back to DefaultAgentURL, an empty
// timeout to 30s.
func New(rawURL, token string, timeout time.Duration) *Client {
	if strings.TrimSpace(rawURL) == "" {
		rawURL = DefaultAgentURL
	}
	if timeout <= 0 {
		timeout = 30 * time.Second
	}
	return &Client{
		URL:   strings.TrimRight(strings.TrimSpace(rawURL), "/"),
		Token: strings.TrimSpace(token),
		http:  &http.Client{Timeout: timeout},
	}
}

// WithContext returns a copy of the client whose requests are bound to ctx.
//
// A ctx parameter on every method would be the idiomatic shape, but this client
// has a dozen of them plus multi-request helpers like CollectAttachContext, and
// binding once at the single place that builds a request gets the cancellation
// without a signature cascade through every caller and test. The copy is by
// value and the client holds no locks, so callers keep their own scope.
func (c *Client) WithContext(ctx context.Context) *Client {
	bound := *c
	bound.ctx = ctx
	return &bound
}

// SetHTTPClient swaps the underlying transport. Tests point this at an
// httptest server; it exists so the read-only guard can be exercised without a
// live agent.
func (c *Client) SetHTTPClient(h *http.Client) {
	if h != nil {
		c.http = h
	}
}

// SetToken swaps the bearer this client sends.
//
// For `attach`: the session token is minted after the client (and the tool
// holding it) already exist, and the whole point of that credential is that
// everything the session does afterwards runs under it rather than under the
// operator's standing token. Restoring on exit is the caller's job — it owns
// the ordering (write-back, then revoke).
func (c *Client) SetToken(token string) {
	c.Token = strings.TrimSpace(token)
}

// do performs one request. The method allowlist is checked here, before any
// socket is opened, so a refused method never reaches the network.
func (c *Client) do(method, path string, params url.Values) ([]byte, error) {
	if !allowedMethods[method] {
		return nil, newError(
			"Use the console or the MCP server to act on a remediation.",
			"cfassist attach is read-only; refusing %s %s", method, path,
		)
	}

	full := c.URL + path
	if len(params) > 0 {
		full += "?" + params.Encode()
	}

	ctx := c.ctx
	if ctx == nil {
		ctx = context.Background()
	}
	req, err := http.NewRequestWithContext(ctx, method, full, nil)
	if err != nil {
		return nil, newError("", "bad request URL %s: %v", full, err)
	}
	req.Header.Set("Accept", "application/json")
	if c.Token != "" {
		req.Header.Set("Authorization", "Bearer "+c.Token)
	}

	resp, err := c.http.Do(req)
	if err != nil {
		var netErr net.Error
		if errors.As(err, &netErr) && netErr.Timeout() {
			return nil, newError("", "Timed out talking to CFOperator at %s", c.URL)
		}
		// The hint leads with the two fixes that work anywhere. It used to name
		// only the port-forward, which quietly assumes kubectl and a kubeconfig
		// on this machine — an assumption that fails on exactly the hardware
		// attach is for (a Pi, a phone-tethered laptop), and leaves an operator
		// whose real problem is "the URL points at the wrong host" with nothing
		// to act on. Port-forward stays, third, labelled with what it needs.
		return nil, newError(
			fmt.Sprintf("Is the agent up, and is %s the right address? "+
				"Set the agent host with %s=http://<agent-host>:8083, "+
				"or a cfoperator: block (url/token) in ~/.cfassist/config.yaml. "+
				"With kubectl here, kubectl -n apps port-forward svc/cfoperator 8083:8083 "+
				"makes 127.0.0.1:8083 work instead.", c.URL, EnvAgentURL),
			"Cannot reach CFOperator at %s: %v", c.URL, err,
		)
	}
	defer resp.Body.Close()

	body, readErr := io.ReadAll(io.LimitReader(resp.Body, 8<<20))
	if readErr != nil {
		return nil, newError("", "CFOperator response could not be read: %v", readErr)
	}

	switch {
	case resp.StatusCode == http.StatusUnauthorized || resp.StatusCode == http.StatusForbidden:
		return nil, newError(
			fmt.Sprintf("Mint one at %s/admin?tab=tokens and export %s, "+
				"or set cfoperator.token in ~/.cfassist/config.yaml.", c.URL, EnvAPIToken),
			"CFOperator rejected the API token (HTTP %d)", resp.StatusCode,
		)
	case resp.StatusCode == http.StatusNotFound:
		return nil, newError("", "Not found: %s", path)
	case resp.StatusCode >= 400:
		snippet := string(body)
		if len(snippet) > 200 {
			snippet = snippet[:200]
		}
		return nil, newError("", "CFOperator returned HTTP %d for %s: %s",
			resp.StatusCode, path, snippet)
	}

	return body, nil
}

// getJSON performs a GET and decodes the JSON body into out.
func (c *Client) getJSON(path string, params url.Values, out any) error {
	body, err := c.do(http.MethodGet, path, params)
	if err != nil {
		return err
	}
	if err := json.Unmarshal(body, out); err != nil {
		return newError("", "CFOperator returned a non-JSON response for %s", path)
	}
	return nil
}

// --- reads ------------------------------------------------------------------

// Health returns the agent's /api/health payload as-is.
//
// The one read that works with no credentials: web_auth.py keeps /api/health
// auth-exempt for kubelet probes, which is exactly what makes it the presence
// probe (see presence.go). Returned raw rather than as a struct because the
// caller shape-checks it — a 200 from something else on the port must not pass
// as CFOperator.
func (c *Client) Health() (map[string]any, error) {
	var out map[string]any
	if err := c.getJSON("/api/health", nil, &out); err != nil {
		return nil, err
	}
	return out, nil
}

// ListInvestigations returns recent investigation summary rows, newest first.
//
// Summaries only: the list endpoint carries `outcome` at the top level and no
// findings at all. GetInvestigation is the drill-in.
func (c *Client) ListInvestigations(limit int) ([]map[string]any, error) {
	if limit <= 0 {
		limit = 20
	}
	params := url.Values{}
	params.Set("limit", strconv.Itoa(limit))

	var payload struct {
		Investigations []map[string]any `json:"investigations"`
	}
	if err := c.getJSON("/api/investigations", params, &payload); err != nil {
		return nil, err
	}
	return payload.Investigations, nil
}

// GetInvestigation returns the full investigation detail.
//
// Note the API's shape: the list endpoint returns `outcome` at the top level but
// no findings at all, while this one nests `provider`, `response` and
// `recommendation` under `findings`. Callers that read the top level only see an
// empty report — see briefing.go's InvestigationFacts, which is the single place
// that flattening happens.
func (c *Client) GetInvestigation(id int) (map[string]any, error) {
	var inv map[string]any
	if err := c.getJSON("/api/investigations/"+strconv.Itoa(id), nil, &inv); err != nil {
		return nil, err
	}
	return inv, nil
}

// ListRemediations returns queue rows, optionally filtered by status.
func (c *Client) ListRemediations(status string, limit int) ([]map[string]any, error) {
	if limit <= 0 {
		limit = 200
	}
	params := url.Values{}
	params.Set("limit", strconv.Itoa(limit))
	if status != "" {
		params.Set("status", status)
	}

	var payload struct {
		Remediations []map[string]any `json:"remediations"`
	}
	if err := c.getJSON("/api/remediations", params, &payload); err != nil {
		return nil, err
	}
	return payload.Remediations, nil
}

// GetRemediation returns one queue row in full: payload, result, PR URL.
func (c *Client) GetRemediation(id int) (map[string]any, error) {
	var row map[string]any
	if err := c.getJSON("/api/remediations/"+strconv.Itoa(id), nil, &row); err != nil {
		return nil, err
	}
	return row, nil
}

// RemediationsForInvestigation returns the queue rows linked to one
// investigation, plus whether the scan was truncated.
//
// Filtered client-side: /api/remediations accepts only `status` and `limit`,
// there is no `investigation_id` parameter. Adding one would be a server change
// in service of a client convenience.
//
// The catch is that the queue is ordered newest-first, so a client-side filter
// over the newest N can miss a linked row that is older than the window — and
// an empty result then reads as "nothing was queued for this investigation",
// which is exactly the claim this briefing exists to carry. Being wrong about
// it mid-incident is worse than being vague.
//
// So when the server returns a full page we cannot distinguish "no match" from
// "no match yet"; truncated is true and the caller must say so. The bet that
// the queue is small is fine as a default and unacceptable as a silent one.
// A server-side ?investigation_id= would remove the class entirely.
func (c *Client) RemediationsForInvestigation(id, limit int) (rows []map[string]any, truncated bool, err error) {
	all, err := c.ListRemediations("", limit)
	if err != nil {
		return nil, false, err
	}
	matched := make([]map[string]any, 0, 4)
	for _, row := range all {
		if asInt(row["investigation_id"]) == id {
			matched = append(matched, row)
		}
	}
	// Only ambiguous when the window was actually filled. A short page means
	// we saw the whole queue and an empty result is trustworthy.
	return matched, limit > 0 && len(all) >= limit, nil
}

// SearchKnowledge runs a hybrid (or FTS-fallback) KB search and returns the rows
// alongside the mode the server used.
//
// The two modes return *different row shapes* — the hybrid SQL path selects
// neither `investigation_id` nor `host_id` and adds similarity scores, while the
// FTS fallback returns the full row including `investigation_id`. The mode is
// returned so the caller can say which it got, and the formatter tolerates both.
func (c *Client) SearchKnowledge(query string, limit int) ([]map[string]any, string, error) {
	if limit <= 0 {
		limit = 5
	}
	params := url.Values{}
	params.Set("q", query)
	params.Set("limit", strconv.Itoa(limit))

	var payload struct {
		Results []map[string]any `json:"results"`
		Mode    string           `json:"mode"`
	}
	if err := c.getJSON("/api/kb/search", params, &payload); err != nil {
		return nil, "", err
	}
	return payload.Results, payload.Mode, nil
}

// --- composed read -----------------------------------------------------------

// AttachContext is everything `attach` seeds a session with.
type AttachContext struct {
	Investigation map[string]any
	Remediations  []map[string]any
	Learnings     []map[string]any
	LearningsMode string
	Warnings      []string
	ConsoleURL    string
}

// CollectAttachContext gathers the investigation and its enrichment.
//
// The investigation itself is required — without it there is no briefing and the
// command should fail loudly. The remediation rows and the KB learnings are
// *enrichment*: if either lookup fails the briefing is still worth having, so
// the failure is recorded as a warning the briefing prints rather than an error
// that costs the operator the session. Degrading visibly beats failing totally
// when someone is mid-incident.
func (c *Client) CollectAttachContext(id, learningLimit, remediationLimit int) (*AttachContext, error) {
	inv, err := c.GetInvestigation(id)
	if err != nil {
		return nil, err
	}
	if inv == nil || asInt(inv["id"]) == 0 {
		return nil, newError("", "CFOperator returned no investigation #%d", id)
	}

	ctx := &AttachContext{
		Investigation: inv,
		ConsoleURL:    c.URL,
	}
	invID := asInt(inv["id"])

	remediations, truncated, err := c.RemediationsForInvestigation(invID, remediationLimit)
	if err != nil {
		ctx.Warnings = append(ctx.Warnings,
			"remediation queue unavailable: "+err.Error())
	} else {
		ctx.Remediations = remediations
		if truncated {
			// Same visible-degradation rule as an enrichment failure: the
			// queue is newest-first and we only scanned a full page, so
			// "none found" is not a fact we have. Say that rather than let
			// an empty section imply nothing was ever queued.
			ctx.Warnings = append(ctx.Warnings, fmt.Sprintf(
				"remediation scan truncated at the %d newest queue rows — a linked "+
					"remediation older than that window would not appear here",
				remediationLimit))
		}
	}

	trigger := strings.TrimSpace(asString(inv["trigger"]))
	if trigger != "" {
		if len(trigger) > 400 {
			trigger = trigger[:400]
		}
		learnings, mode, err := c.SearchKnowledge(trigger, learningLimit)
		if err != nil {
			ctx.Warnings = append(ctx.Warnings,
				"knowledge search unavailable: "+err.Error())
		} else {
			ctx.Learnings, ctx.LearningsMode = learnings, mode
		}
	}

	return ctx, nil
}

// ResolveEndpoint resolves (url, token, timeout) for the CFOperator API.
//
// Config file wins over the environment: a config value of ${CFOP_API_TOKEN} has
// already been expanded by config.Load at this point, so a file that opts into
// the variable still reads it, while a file that hardcodes a different endpoint
// is not silently overridden by a stale export.
func ResolveEndpoint(cfgURL, cfgToken string, cfgTimeout float64, lookupEnv func(string) string) (string, string, time.Duration) {
	if lookupEnv == nil {
		lookupEnv = os.Getenv
	}

	resolvedURL := strings.TrimSpace(cfgURL)
	if resolvedURL == "" {
		resolvedURL = strings.TrimSpace(lookupEnv(EnvAgentURL))
	}
	if resolvedURL == "" {
		resolvedURL = DefaultAgentURL
	}

	token := strings.TrimSpace(cfgToken)
	if token == "" {
		token = strings.TrimSpace(lookupEnv(EnvAPIToken))
	}

	timeout := 30 * time.Second
	if cfgTimeout > 0 {
		timeout = time.Duration(cfgTimeout * float64(time.Second))
	}

	return strings.TrimRight(resolvedURL, "/"), token, timeout
}
