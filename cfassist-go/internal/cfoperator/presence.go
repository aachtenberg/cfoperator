// Notice the CFOperator this session is running next to (CFOP-66).
//
// `attach` proved a session can be told about one investigation. This is the
// weaker but far more common case: an operator ssh'd into a box, typed
// `cfassist`, and asked something about CFOperator without an id in hand.
// Before this, the word had no meaning in a plain session — the model looked
// for a Unix user, a process, a systemd unit, found none, and reported the
// agent was not running while it was answering on :8083 and mid-investigation.
//
// So: one cheap probe at startup, and what it finds goes into the system
// prompt. The point is not the data (the tool fetches that) — it is that the
// session knows the word names a service it can query.

package cfoperator

import (
	"fmt"
	"os"
	"strings"
	"time"
)

// ProbeTimeout bounds the presence probe.
//
// Short on purpose and separate from the API timeout used for real reads: this
// runs on every start, including on a laptop where nothing is listening and
// including against an address that blackholes rather than refuses. A slow
// answer here delays a prompt the operator is waiting on, and the feature is
// worth exactly zero seconds of that.
const ProbeTimeout = 1500 * time.Millisecond

// Presence is what a session knows about the CFOperator around it.
//
// Absent is the normal case — cfassist is an SRE CLI first and most machines it
// runs on have no agent. Everything here degrades to "say nothing extra".
type Presence struct {
	// URL probed, after the config → CFOP_AGENT_URL → default precedence.
	URL string
	// Configured is true when the URL came from the config file or the
	// environment rather than the built-in loopback default. It is the
	// difference between "no agent here" (say nothing) and "the agent you
	// pointed me at is not answering" (say that — it is actionable).
	Configured bool

	Reachable bool
	Version   string
	Busy      bool // an investigation is running right now
	Uptime    time.Duration

	// CanRead is set only when an authenticated read actually succeeded. A
	// token that is absent, expired or under-scoped is a different situation
	// from an unreachable agent, and the model should not conflate them.
	HasToken bool
	CanRead  bool

	// Reason explains the negative cases in operator language: why it is not
	// reachable, or why reads do not work.
	Reason string
}

// Detect probes one address and reports what is there.
//
// Never returns an error: a failed probe *is* a result ("nothing here"), and no
// startup path should fail because a service the user may not even run did not
// answer.
func Detect(rawURL string, configured bool, token string, timeout time.Duration) Presence {
	if timeout <= 0 {
		timeout = ProbeTimeout
	}
	c := New(rawURL, token, timeout)
	p := Presence{URL: c.URL, Configured: configured, HasToken: token != ""}

	health, err := c.Health()
	if err != nil {
		p.Reason = err.Error()
		return p
	}

	// A 200 from *something* on :8083 is not evidence of CFOperator. The
	// default address is a well-known local port and plenty of things answer
	// on it; announcing an unrelated service as the fleet's SRE agent would be
	// worse than saying nothing. current_investigation is the distinctive key
	// — no other health payload carries it.
	if _, ok := health["current_investigation"]; !ok {
		p.Reason = fmt.Sprintf("something is listening at %s but it is not CFOperator", c.URL)
		return p
	}

	p.Reachable = true
	p.Version, _ = health["version"].(string)
	p.Busy, _ = health["current_investigation"].(bool)
	if secs, ok := health["uptime_seconds"].(float64); ok && secs > 0 {
		p.Uptime = time.Duration(secs) * time.Second
	}

	// Health is auth-exempt, so reaching it says nothing about whether reads
	// will work. Ask once, with the cheapest authenticated call there is,
	// rather than letting the model discover the 401 mid-answer.
	if !p.HasToken {
		p.Reason = "no API token configured"
		return p
	}
	if _, err := c.ListInvestigations(1); err != nil {
		p.Reason = err.Error()
		return p
	}
	p.CanRead = true
	return p
}

// DetectFromConfig resolves the endpoint the same way `attach` does and probes
// it. The single entry point for callers that just want "is there one here".
func DetectFromConfig(cfgURL, cfgToken string, lookupEnv func(string) string) Presence {
	if lookupEnv == nil {
		lookupEnv = os.Getenv
	}
	url, token, _ := ResolveEndpoint(cfgURL, cfgToken, 0, lookupEnv)
	configured := strings.TrimSpace(cfgURL) != "" || strings.TrimSpace(lookupEnv(EnvAgentURL)) != ""
	return Detect(url, configured, token, ProbeTimeout)
}

// Identity is the always-on half, and the half that actually fixes the reported
// bug. It costs ~60 tokens and is included even when no agent is found, because
// on a machine without one the right answer is still "no CFOperator is
// answering here" rather than "there is no such user".
const Identity = `CFOperator (also "cfoperator", or "the agent") is the autonomous SRE agent this
CLI belongs to: it investigates alerts, queues remediation proposals for human
approval, and keeps a knowledge base of what it learned. When the operator says
"cfoperator" they mean that service — not a Unix user, not a process to hunt for
with ps or systemctl, and not a database account that happens to share the name.
It is an HTTP service; whether it is running is answered by asking it.`

// ToolGuidance is what an already-connected session needs to hear: the tool
// exists, and it is how you look at CFOperator now rather than at the snapshot.
//
// `attach` uses this instead of a full PromptSection. It has already proved the
// agent is reachable — by fetching the briefing from it — and re-probing to
// announce the same fact would be theatre.
const ToolGuidance = "The `cfoperator` tool reads that instance live: recent investigations, this\n" +
	"investigation in full, the remediation queue, and the knowledge base. Use it\n" +
	"to re-check anything time-sensitive in the briefing below."

// PromptSection renders the CFOperator block appended to the system prompt.
//
// Always includes Identity. Adds the situation only when there is one worth
// stating: a reachable agent, or an explicitly configured one that is not
// answering. An absent agent nobody configured gets no paragraph — the prompt
// is not free, and several supported models are small local ones.
func (p Presence) PromptSection() string {
	var b strings.Builder
	b.WriteString(Identity)

	switch {
	case p.Reachable:
		b.WriteString(fmt.Sprintf("\n\nAn instance is reachable from this machine at %s%s%s. "+
			"Use the `cfoperator` tool to read its investigations, remediation queue and "+
			"knowledge base — that tool is how you check on it, not ps or systemctl.",
			p.URL, versionSuffix(p.Version), stateSuffix(p)))
		if !p.CanRead {
			b.WriteString(fmt.Sprintf("\n\nOnly its health endpoint answers without credentials, "+
				"and reads are currently failing: %s. The `cfoperator` tool will report the same. "+
				"The fix is an API token — minted at %s/admin?tab=tokens, then set as CFOP_API_TOKEN "+
				"or cfoperator.token in ~/.cfassist/config.yaml. Tell the operator that rather than "+
				"working around it.", p.Reason, p.URL))
		}
		b.WriteString("\n\nYour access to it is read-only. Approving, rejecting or queueing a " +
			"remediation happens in the console or through the MCP server — recommend those " +
			"actions, never claim to have taken them. `cfassist attach <investigation-id>` starts " +
			"a session briefed on one investigation.")
	case p.Configured:
		b.WriteString(fmt.Sprintf("\n\nCFOperator is configured at %s but did not answer: %s. "+
			"Report that address as unreachable from here — do not conclude the fleet's agent is "+
			"down, and do not go looking for a local process instead.", p.URL, p.Reason))
	}

	return b.String()
}

// BannerLine is the one-line summary for the TUI banner, so the operator can
// see what the model was told without having to ask it. Empty when there is
// nothing to say.
func (p Presence) BannerLine() string {
	switch {
	case p.Reachable && p.CanRead:
		return fmt.Sprintf("cfoperator%s at %s%s", versionSuffix(p.Version), p.URL, stateSuffix(p))
	case p.Reachable:
		return fmt.Sprintf("cfoperator%s at %s%s · reads unavailable (%s)",
			versionSuffix(p.Version), p.URL, stateSuffix(p), p.Reason)
	case p.Configured:
		return fmt.Sprintf("cfoperator at %s not answering", p.URL)
	}
	return ""
}

// versionSuffix renders what /api/health calls the build: a release is bare
// ("1.1.0", shown as v1.1.0); a main build is its image tag ("main-1a551b7")
// and a source checkout is "dev", neither of which wants a v in front.
func versionSuffix(version string) string {
	if version == "" {
		return ""
	}
	if version[0] >= '0' && version[0] <= '9' {
		return " v" + version
	}
	return " " + version
}

// stateSuffix says what the agent is doing, which is the one live fact the
// health endpoint gives away for free and the first thing an operator asks.
func stateSuffix(p Presence) string {
	state := " · idle"
	if p.Busy {
		state = " · investigating now"
	}
	if p.Uptime > 0 {
		state += fmt.Sprintf(" · up %s", shortDuration(p.Uptime))
	}
	return state
}

// shortDuration renders an uptime the way an operator reads one: 3d4h, 2h11m,
// 14m. time.Duration's own String() gives "51h22m10.5s", which is noise.
func shortDuration(d time.Duration) string {
	switch {
	case d >= 24*time.Hour:
		return fmt.Sprintf("%dd%dh", int(d.Hours())/24, int(d.Hours())%24)
	case d >= time.Hour:
		return fmt.Sprintf("%dh%dm", int(d.Hours()), int(d.Minutes())%60)
	case d >= time.Minute:
		return fmt.Sprintf("%dm", int(d.Minutes()))
	default:
		return fmt.Sprintf("%ds", int(d.Seconds()))
	}
}
