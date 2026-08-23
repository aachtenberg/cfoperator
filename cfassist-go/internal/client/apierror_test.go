package client

import (
	"errors"
	"net/http"
	"strings"
	"testing"
	"time"
)

// The body Groq returns when its server-side parser cannot turn a completion
// into tool calls. This is the failure that ended a live session and prompted
// CFOP-72; the marker that makes it recognisable is `failed_generation`.
const groqParseFailureBody = `{"error":{"message":"Parsing failed. The model generated output that could not be parsed. Please adjust your prompt. See 'failed_generation' for more details.","type":"invalid_request_error","code":"tool_use_failed","failed_generation":"<|channel|>commentary to=functions.bash"}}`

func TestRetryableClassification(t *testing.T) {
	cases := []struct {
		name   string
		status int
		body   string
		want   bool
	}{
		{"transport failure", 0, "", true},
		{"request timeout", http.StatusRequestTimeout, "", true},
		{"too early", http.StatusTooEarly, "", true},
		{"rate limited", http.StatusTooManyRequests, "", true},
		{"internal error", http.StatusInternalServerError, "", true},
		{"bad gateway", http.StatusBadGateway, "", true},
		{"unavailable", http.StatusServiceUnavailable, "", true},
		{"gateway timeout", http.StatusGatewayTimeout, "", true},

		{"model output would not parse", http.StatusBadRequest, groqParseFailureBody, true},
		{"tool_use_failed code alone", http.StatusBadRequest, `{"error":{"code":"tool_use_failed"}}`, true},

		{"malformed request", http.StatusBadRequest, `{"error":{"message":"messages: must not be empty"}}`, false},
		{"bad key", http.StatusUnauthorized, `{"error":{"message":"Invalid API Key"}}`, false},
		{"forbidden", http.StatusForbidden, "", false},
		{"no such model", http.StatusNotFound, `{"error":{"message":"model not found"}}`, false},
		{"payload too large", http.StatusRequestEntityTooLarge, "", false},
		{"unprocessable", http.StatusUnprocessableEntity, "", false},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			e := &APIError{Provider: "openai", StatusCode: tc.status, Body: tc.body}
			if tc.status == 0 {
				e.Err = errors.New("connection reset by peer")
			}
			if got := e.Retryable(); got != tc.want {
				t.Errorf("Retryable() = %v, want %v (status %d)", got, tc.want, tc.status)
			}
		})
	}
}

// A generic 400 must stay non-retryable. It means we built a bad request, and
// re-sending an identical bad request only makes the operator wait twice for
// the same refusal — the reason Retryable narrows the 400 case instead of
// treating the whole status as transient.
func TestPlainBadRequestIsNotRetryable(t *testing.T) {
	e := &APIError{Provider: "openai", StatusCode: http.StatusBadRequest,
		Body: `{"error":{"message":"tools[0].function.name: invalid"}}`}
	if e.Retryable() {
		t.Error("a malformed-request 400 must not be retried")
	}
}

// The bug this replaces: one hardcoded hint told every provider to curl an
// Ollama endpoint. This guards the class — a probe suggestion has to name a
// path the provider actually answers on — rather than pinning today's wording.
func TestTransportHintNamesAPathTheProviderAnswers(t *testing.T) {
	cases := []struct {
		provider string
		url      string
		wantPath string
	}{
		{"ollama", "http://localhost:11434", "/api/tags"},
		{"openai", "https://api.groq.com/openai", "/v1/models"},
		{"anthropic", "https://api.anthropic.com", "/v1/models"},
	}

	for _, tc := range cases {
		t.Run(tc.provider, func(t *testing.T) {
			e := &APIError{Provider: tc.provider, URL: tc.url, Err: errors.New("dial tcp: timeout")}
			hint := e.Hint()

			if !strings.Contains(hint, tc.url+tc.wantPath) {
				t.Errorf("hint = %q, want it to probe %q", hint, tc.url+tc.wantPath)
			}
			if tc.provider != "ollama" && strings.Contains(hint, "/api/tags") {
				t.Errorf("hint for %s names the Ollama endpoint: %q", tc.provider, hint)
			}
			if tc.provider != "ollama" && !strings.Contains(hint, "$KEY") {
				t.Errorf("hint for %s omits the auth header the probe needs: %q", tc.provider, hint)
			}
		})
	}
}

// A failure that has nothing to do with reachability must not send the operator
// to a connectivity check — the specific misdirection reported in CFOP-72.
func TestHintDoesNotBlameTheConnectionForOtherFailures(t *testing.T) {
	cases := []struct {
		name       string
		status     int
		body       string
		wantPhrase string
	}{
		{"bad key", http.StatusUnauthorized, `{"error":{"message":"Invalid API Key"}}`, "api_key"},
		{"no such model", http.StatusNotFound, "", "model"},
		{"rate limited", http.StatusTooManyRequests, "", "Rate limited"},
		{"model output", http.StatusBadRequest, groqParseFailureBody, "tool-call output"},
		{"server trouble", http.StatusServiceUnavailable, "", "server trouble"},
		{"context overflow", http.StatusBadRequest, `{"error":{"code":"context_length_exceeded"}}`, "context"},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			e := &APIError{Provider: "openai", URL: "https://api.groq.com/openai",
				StatusCode: tc.status, Body: tc.body}
			hint := e.Hint()

			if strings.Contains(hint, "curl") || strings.Contains(hint, "Check connection") {
				t.Errorf("hint for %s suggests a connectivity check: %q", tc.name, hint)
			}
			if !strings.Contains(hint, tc.wantPhrase) {
				t.Errorf("hint = %q, want it to mention %q", hint, tc.wantPhrase)
			}
		})
	}
}

// Context overflow is reported as a 400 by the OpenAI-compatible providers, so
// it would otherwise fall into the malformed-request bucket and be described as
// a config problem. Retrying it is pointless — the conversation has to shrink.
func TestContextOverflowIsNotRetriedAndSaysSo(t *testing.T) {
	e := &APIError{Provider: "openai", StatusCode: http.StatusBadRequest,
		Body: `{"error":{"message":"Request too large: context_length_exceeded"}}`}

	if e.Retryable() {
		t.Error("context overflow must not be retried — the next attempt is the same size")
	}
	if !strings.Contains(e.Hint(), "/clear") {
		t.Errorf("hint = %q, want it to name the command that fixes this", e.Hint())
	}
}

func TestParseRetryAfter(t *testing.T) {
	cases := []struct {
		header string
		want   time.Duration
	}{
		{"2", 2 * time.Second},
		{"2.5", 2500 * time.Millisecond},
		{" 3 ", 3 * time.Second},
		{"", 0},
		{"0", 0},
		{"-1", 0},
		{"soon", 0},
		{"Wed, 21 Oct 2026 07:28:00 GMT", 0}, // HTTP-date form: no guidance, use our backoff
	}

	for _, tc := range cases {
		if got := parseRetryAfter(tc.header); got != tc.want {
			t.Errorf("parseRetryAfter(%q) = %v, want %v", tc.header, got, tc.want)
		}
	}
}

// A parse failure carries the whole completion that would not parse. It belongs
// in Body for anyone who wants it, not in a line printed to a terminal.
func TestErrorTruncatesBodyButKeepsIt(t *testing.T) {
	long := strings.Repeat("x", 5000)
	e := &APIError{Provider: "openai", StatusCode: http.StatusBadRequest, Body: long}

	msg := e.Error()
	if len(msg) > maxBodyInMessage+200 {
		t.Errorf("Error() is %d chars, want it capped near %d", len(msg), maxBodyInMessage)
	}
	if !strings.Contains(msg, "truncated") {
		t.Errorf("Error() = %q, want it to say the body was cut", msg)
	}
	if e.Body != long {
		t.Error("Body must keep the full text the provider sent")
	}
}

func TestErrorUnwrapsTransportFailure(t *testing.T) {
	inner := errors.New("dial tcp 127.0.0.1:11434: connect: connection refused")
	e := &APIError{Provider: "ollama", URL: "http://localhost:11434", Err: inner}

	if !errors.Is(e, inner) {
		t.Error("errors.Is should reach the transport error")
	}
	if !strings.Contains(e.Error(), "connection refused") {
		t.Errorf("Error() = %q, want it to carry the transport failure", e.Error())
	}
}

func TestSummaryStaysOneLine(t *testing.T) {
	e := &APIError{Provider: "openai", StatusCode: http.StatusBadRequest, Body: groqParseFailureBody}

	summary := e.Summary()
	if strings.Contains(summary, "\n") {
		t.Errorf("Summary() = %q, want a single line", summary)
	}
	if strings.Contains(summary, "failed_generation") {
		t.Errorf("Summary() = %q, want it to omit the raw body", summary)
	}
	if !strings.Contains(summary, "parse") {
		t.Errorf("Summary() = %q, want it to name what failed", summary)
	}
}

// A hint that names "openai" when the operator configured "groq" sends them
// looking for a config key that is not there. The wire protocol stays available
// for choosing the probe path, but never appears in the advice.
func TestHintNamesTheProviderTheOperatorConfigured(t *testing.T) {
	e := &APIError{Provider: "openai", Label: "groq", URL: "https://api.groq.com/openai",
		StatusCode: http.StatusUnauthorized, Body: `{"error":{"message":"Invalid API Key"}}`}

	for what, got := range map[string]string{"Hint": e.Hint(), "Error": e.Error(), "Summary": e.Summary()} {
		if !strings.Contains(got, "groq") {
			t.Errorf("%s() = %q, want it to name the configured provider", what, got)
		}
		if strings.Contains(got, "openai") {
			t.Errorf("%s() = %q, want it to omit the wire protocol", what, got)
		}
	}

	// The probe still has to key off the protocol, not the label.
	transport := &APIError{Provider: "openai", Label: "groq", URL: "https://api.groq.com/openai",
		Err: errors.New("timeout")}
	if !strings.Contains(transport.Hint(), "/v1/models") {
		t.Errorf("probe = %q, want the OpenAI-compatible path", transport.Hint())
	}
}

// Without a label — anything constructing a client without naming it — the
// protocol is still better than an empty string.
func TestUnlabelledErrorFallsBackToProtocol(t *testing.T) {
	e := &APIError{Provider: "ollama", URL: "http://localhost:11434",
		StatusCode: http.StatusInternalServerError}
	if !strings.Contains(e.Summary(), "ollama") {
		t.Errorf("Summary() = %q, want the protocol as a fallback name", e.Summary())
	}
}
