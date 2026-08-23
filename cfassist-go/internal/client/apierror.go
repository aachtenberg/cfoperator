package client

import (
	"fmt"
	"net/http"
	"strconv"
	"strings"
	"time"
)

// maxBodyInMessage caps how much provider prose reaches an error string.
//
// A tool-call parse failure carries the whole completion that would not parse
// in its `failed_generation` field, which can be thousands of characters. That
// belongs in Body for anyone who wants it, not in a line printed to a terminal
// mid-incident.
const maxBodyInMessage = 400

// APIError is what a provider said when it refused a chat request.
//
// It exists so the one decision a caller actually has to make — is sending this
// again worth anything? — comes from a single classifier rather than from
// string-matching provider prose at each call site. StatusCode is 0 when the
// request never got an HTTP response at all.
type APIError struct {
	Provider   string // wire protocol: ollama, openai, or anthropic
	Label      string // the operator's name for it from config ("groq"); Provider when unset
	URL        string
	StatusCode int
	Body       string
	RetryAfter time.Duration // parsed from the Retry-After header; 0 if absent
	Err        error         // the transport failure, when StatusCode is 0
}

// name is what the operator calls this provider. The config key ("groq") is
// what they typed and what /use takes; Provider is the wire protocol underneath
// it, which nobody put in their config.yaml.
func (e *APIError) name() string {
	if e.Label != "" {
		return e.Label
	}
	return e.Provider
}

func (e *APIError) Error() string {
	if e.StatusCode == 0 {
		return fmt.Sprintf("%s request: %v", e.name(), e.Err)
	}
	body := e.Body
	if len(body) > maxBodyInMessage {
		body = body[:maxBodyInMessage] + "… (truncated)"
	}
	return fmt.Sprintf("%s HTTP %d: %s", e.name(), e.StatusCode, body)
}

func (e *APIError) Unwrap() error { return e.Err }

// Retryable reports whether re-sending the identical request has a real chance
// of a different outcome.
func (e *APIError) Retryable() bool {
	switch {
	case e.StatusCode == 0:
		// Never reached the provider: reset connection, DNS blip, timeout.
		return true
	case e.StatusCode == http.StatusRequestTimeout,
		e.StatusCode == http.StatusTooEarly,
		e.StatusCode == http.StatusTooManyRequests:
		return true
	case e.StatusCode >= 500:
		return true
	case e.StatusCode == http.StatusBadRequest:
		return e.IsModelOutputFailure()
	}
	return false
}

// IsModelOutputFailure reports whether a 400 blames the model's own output
// rather than the request we sent.
//
// Groq returns this when its server-side parser cannot turn a completion into
// tool calls, and it is the one 400 worth re-sending: the request was fine, the
// sample was not, and at any temperature above zero the next sample is a
// different sample. It is marked by a `failed_generation` field carrying the
// text that would not parse, under code `tool_use_failed`. Every other 400 says
// we built a bad request, and re-sending an identical bad request only makes
// the operator wait twice for the same refusal.
func (e *APIError) IsModelOutputFailure() bool {
	body := strings.ToLower(e.Body)
	return strings.Contains(body, "failed_generation") ||
		strings.Contains(body, "tool_use_failed")
}

// isContextOverflow reports whether the provider is saying the conversation no
// longer fits. Providers disagree on the status code, so this keys on the body.
func (e *APIError) isContextOverflow() bool {
	body := strings.ToLower(e.Body)
	return strings.Contains(body, "context_length_exceeded") ||
		strings.Contains(body, "context window") ||
		strings.Contains(body, "too many tokens") ||
		strings.Contains(body, "prompt is too long")
}

// Summary is a one-line form naming what went wrong, for a retry notice.
func (e *APIError) Summary() string {
	switch {
	case e.StatusCode == 0:
		return fmt.Sprintf("could not reach %s", e.name())
	case e.IsModelOutputFailure():
		return fmt.Sprintf("%s could not parse the model's tool call", e.name())
	case e.StatusCode == http.StatusTooManyRequests:
		return fmt.Sprintf("%s rate-limited the request", e.name())
	default:
		return fmt.Sprintf("%s returned HTTP %d", e.name(), e.StatusCode)
	}
}

// Hint is the operator-facing next step.
//
// It replaces a single hardcoded string that told everyone to curl an Ollama
// endpoint regardless of provider or failure — advice that was wrong twice over
// against a hosted API refusing a malformed completion.
func (e *APIError) Hint() string {
	switch {
	case e.StatusCode == 0:
		return "Check connection: " + e.probe()
	case e.StatusCode == http.StatusUnauthorized, e.StatusCode == http.StatusForbidden:
		return fmt.Sprintf("The api_key for %s was rejected — check it in ~/.cfassist/config.yaml, or /use another provider.", e.name())
	case e.StatusCode == http.StatusNotFound:
		return fmt.Sprintf("No such model or endpoint — check the model and url for %s in ~/.cfassist/config.yaml.", e.name())
	case e.StatusCode == http.StatusTooManyRequests:
		return "Rate limited. Wait a moment, or /use another provider."
	case e.isContextOverflow():
		return "The conversation no longer fits this model's context — /clear to start fresh, or /use a provider with a larger window."
	case e.IsModelOutputFailure():
		return "The model's tool-call output could not be parsed, and retrying did not help. Try /use another provider, or a model that is steadier at tool calls."
	case e.StatusCode >= 500:
		return fmt.Sprintf("%s is having server trouble and retrying did not help. /use another provider to keep working.", e.name())
	}
	return "Check the provider settings in ~/.cfassist/config.yaml."
}

// probe returns a command that actually tests reachability for this provider —
// each one answers on a different path, and two of the three need a key.
func (e *APIError) probe() string {
	switch e.Provider {
	case "ollama":
		return "curl " + e.URL + "/api/tags"
	case "anthropic":
		// Without anthropic-version the API answers 400, so a probe that omits
		// it sends the operator chasing a second phantom failure.
		return `curl -H "x-api-key: $KEY" -H "anthropic-version: 2023-06-01" ` + e.URL + "/v1/models"
	default:
		return `curl -H "Authorization: Bearer $KEY" ` + e.URL + "/v1/models"
	}
}

// parseRetryAfter reads a Retry-After header.
//
// Only the delay-seconds form is handled, which is what LLM providers send;
// Groq sends fractional seconds, so this parses as a float rather than an int.
// The HTTP-date form yields 0, which callers treat as "no guidance" and fall
// back to their own backoff.
func parseRetryAfter(header string) time.Duration {
	header = strings.TrimSpace(header)
	if header == "" {
		return 0
	}
	seconds, err := strconv.ParseFloat(header, 64)
	if err != nil || seconds <= 0 {
		return 0
	}
	return time.Duration(seconds * float64(time.Second))
}

// newAPIError builds an APIError from a non-200 response the caller has
// already read the body of.
func (c *LLMClient) newAPIError(status int, header http.Header, body string) *APIError {
	return &APIError{
		Provider:   c.Provider,
		Label:      c.Name,
		URL:        c.URL,
		StatusCode: status,
		Body:       body,
		RetryAfter: parseRetryAfter(header.Get("Retry-After")),
	}
}

// newTransportError builds an APIError for a request that never got a response.
func (c *LLMClient) newTransportError(err error) *APIError {
	return &APIError{
		Provider: c.Provider,
		Label:    c.Name,
		URL:      c.URL,
		Err:      err,
	}
}
