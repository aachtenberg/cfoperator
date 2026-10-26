package dedupe

import (
	"strings"
)

// Ported from agent/agent.py: CFOperator._normalize_host, _host_candidates,
// _match_known_host and _collapse_key_for_node_incident (CFOP-71, CFOP-126).

// hostConnectors are the words a model writes between hosts. Matched only as
// whole tokens, so "sandbox-01" and "andrew-node" come through intact.
var hostConnectors = map[string]bool{
	"and": true, "or": true, "&": true, "/": true, "+": true, "|": true,
	"->": true, "→": true, "-": true, "--": true,
}

const hostTokenStrip = "()[]{}<>\"'`.,;:"

// normalizeHost is the bare lowercase hostname in a host field or a
// Prometheus label: an instance label is "raspberrypi4:9100", and the port
// has to come off before anything is compared against a node name.
func normalizeHost(host string) string {
	h := strings.ToLower(pyStrip(host))
	if h == "" {
		return ""
	}
	if i := strings.Index(h, "://"); i >= 0 {
		h = h[i+3:]
	}
	if i := strings.Index(h, "/"); i >= 0 {
		h = h[:i]
	}
	// Strip :port. Never an IPv6 literal here.
	if i := strings.LastIndex(h, ":"); i >= 0 {
		h = h[:i]
	}
	return strings.Trim(h, ".")
}

// hostCandidates is every host-shaped token in a host field or target id, in
// order and de-duplicated. Lexical only: deciding which tokens ARE hosts is
// matchKnownHost's job, against the inventory, because a node can be named
// anything at all.
func hostCandidates(raw string) []string {
	text := pyStrip(raw)
	if text == "" {
		return nil
	}
	var out []string
	seen := map[string]bool{}
	for _, tok := range strings.FieldsFunc(text, func(r rune) bool {
		return r == ',' || r == ';' || pySpace(r)
	}) {
		tok = pyStripChars(tok, hostTokenStrip)
		if tok == "" || hostConnectors[strings.ToLower(tok)] {
			continue
		}
		// "a/b" is two hosts unless it is a URL, which normalizeHost handles.
		parts := []string{tok}
		if !strings.Contains(tok, "://") {
			parts = strings.Split(tok, "/")
		}
		for _, part := range parts {
			h := normalizeHost(pyStripChars(part, hostTokenStrip))
			if h != "" && !seen[h] {
				seen[h] = true
				out = append(out, h)
			}
		}
	}
	return out
}

// matchKnownHost is the registered name token refers to, or "".
//
// Exact first. Otherwise short name against FQDN in either direction, but a
// bare token matches an FQDN only when exactly one registered FQDN has that
// first label: an ambiguous token is an unknown token, and nothing on this
// path guesses. Returns the REGISTERED name, because that is what the
// recovery sweep keys on.
func matchKnownHost(token string, known []string) string {
	token = strings.ToLower(pyStrip(token))
	if token == "" {
		return ""
	}
	names := make([]string, 0, len(known))
	for _, k := range known {
		if n := strings.ToLower(pyStrip(k)); n != "" {
			names = append(names, n)
		}
	}
	for _, n := range names {
		if n == token {
			return token
		}
	}
	if strings.Contains(token, ".") {
		short, _, _ := strings.Cut(token, ".")
		for _, n := range names {
			if n == short {
				return short
			}
		}
		return ""
	}
	var hits []string
	for _, n := range names {
		if first, _, ok := strings.Cut(n, "."); ok && first == token {
			hits = append(hits, n)
		}
	}
	if len(hits) == 1 {
		return hits[0]
	}
	return ""
}

// notReadySet is the agent's _notready_nodes(): lowercased names as a set.
// The service receives a list, so duplicates are removed the way the set
// removed them; matchKnownHost would otherwise count one FQDN twice and call
// it ambiguous.
func notReadySet(names []string) []string {
	seen := map[string]bool{}
	var out []string
	for _, n := range names {
		n = strings.ToLower(n)
		if n != "" && !seen[n] {
			seen[n] = true
			out = append(out, n)
		}
	}
	return out
}

// nodeIncidentKey is the one key every symptom of one dead node folds onto.
func nodeIncidentKey(node string) string {
	return "node-down-" + normalizeHost(node)
}

// collapseKey is the node-incident key for a host field and the registered
// node it names, or "" when no token names a NotReady node. Conditional on
// the node actually being NotReady: a disk-full and a crash-looping pod on a
// healthy host are two problems, not one incident.
func collapseKey(host string, notReady []string) (key, node string) {
	candidates := hostCandidates(host)
	if len(candidates) == 0 || len(notReady) == 0 {
		return "", ""
	}
	for _, tok := range candidates {
		if n := matchKnownHost(tok, notReady); n != "" {
			return nodeIncidentKey(n), n
		}
	}
	return "", ""
}
