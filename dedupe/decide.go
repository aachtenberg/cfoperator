package dedupe

import (
	"fmt"
	"sort"
	"strings"
)

// Decisions and tiers, as they appear in a Response.
const (
	DecisionNew      = "new"
	DecisionFold     = "fold"
	DecisionSuppress = "suppress"

	TierNode       = "node"
	TierTarget     = "target"
	TierIdentifier = "identifier"
	TierKey        = "key"
)

// foldWindow is how many open rows the fold and the alias lookup read,
// newest first: list_open_remediations(limit=100) and the KB's alias scan.
// The exact-key check reads every open row.
const foldWindow = 100

// absorbedLimit is the agent's [:200] on the summary it records.
const absorbedLimit = 200

// Request is everything one decision needs. See the package doc.
type Request struct {
	Candidate  Candidate  `json:"candidate"`
	OpenRows   []Row      `json:"open_rows"`
	ClosedRows []Row      `json:"closed_rows"`
	Vocabulary Vocabulary `json:"vocabulary"`
}

// Candidate is the row about to be enqueued. Targets and FoldTargets are
// FIX target lists ([{kind, id, repo}]), decoded loosely so a malformed one
// is "names nothing" rather than a rejected request.
type Candidate struct {
	DedupeKey        string `json:"dedupe_key"`
	Recommendation   string `json:"recommendation"`
	Targets          any    `json:"targets"`
	FoldTargets      any    `json:"fold_targets"`
	Host             string `json:"host"`
	Title            string `json:"title"`
	Trigger          string `json:"trigger"`
	RemediationClass string `json:"remediation_class"`
	Source           string `json:"source"`
}

// Row is a remediation row as the knowledge base stores it.
type Row struct {
	ID      int64          `json:"id"`
	Status  string         `json:"status"`
	Payload map[string]any `json:"payload"`
}

// Vocabulary is the known lists subjects are drawn from. Only NotReadyNodes
// is read in phase 1.
type Vocabulary struct {
	NotReadyNodes []string `json:"notready_nodes"`
	Hosts         []string `json:"hosts"`
	Workloads     []string `json:"workloads"`
	Providers     []string `json:"providers"`
}

// Response is the decision and the side effects the agent applies.
type Response struct {
	Decision  string   `json:"decision"`
	RowID     int64    `json:"row_id,omitempty"`
	Tier      string   `json:"tier,omitempty"`
	Reason    string   `json:"reason"`
	Shared    []string `json:"shared,omitempty"`
	DedupeKey string   `json:"dedupe_key,omitempty"`
	Host      string   `json:"host,omitempty"`
	Absorbed  string   `json:"absorbed,omitempty"`
	Alias     string   `json:"alias,omitempty"`
}

func (r Row) open() bool {
	return r.Status != "resolved" && r.Status != "rejected"
}

func (r Row) key() string {
	k, _ := r.Payload["dedupe_key"].(string)
	return k
}

// carriesKey is knowledge_base.payload_carries_dedupe_key: the row's own key,
// or one that folded onto it (CFOP-453).
func (r Row) carriesKey(key string) bool {
	if key == "" {
		return false
	}
	if r.key() == key {
		return true
	}
	aliases, _ := r.Payload["dedupe_aliases"].([]any)
	for _, a := range aliases {
		if s, ok := a.(string); ok && s == key {
			return true
		}
	}
	return false
}

// Decide runs the tiers in the agent's order: node collapse, then the target
// and identifier folds, then the exact key.
func Decide(req Request) Response {
	c := req.Candidate
	var open []Row
	for _, r := range req.OpenRows {
		if r.open() {
			open = append(open, r)
		}
	}
	window := open
	if len(window) > foldWindow {
		window = window[:foldWindow]
	}
	key := c.DedupeKey
	// What a fold records on the surviving row, so a fold is never silent.
	absorbed := firstRunes(pyStrOr(orFirst(c.Trigger, c.Recommendation)), absorbedLimit)

	// CFOP-71: while a node is NotReady, every symptom attributable to it
	// folds onto one node-incident row. The key is rewritten above the other
	// tiers because the symptoms each carry a legitimately different key.
	// The morning summary reads the title when the host is blank (CFOP-130).
	nodeInput := c.Host
	if nodeInput == "" {
		nodeInput = c.Title
	}
	nodeKey, node := collapseKey(nodeInput, notReadySet(req.Vocabulary.NotReadyNodes))
	if nodeKey != "" && nodeKey != key {
		if row, ok := lookupKey(open, window, nodeKey); ok {
			return Response{
				Decision: DecisionFold, RowID: row.ID, Tier: TierNode,
				Reason: fmt.Sprintf("node %s is NotReady; its incident row #%d (%s) covers this",
					node, row.ID, nodeKey),
				Shared: []string{node}, Absorbed: absorbed,
			}
		}
		key = nodeKey
	}

	// CFOP-78 / CFOP-453: the same problem re-found under different wording
	// lands on the open row already covering it. Skipped entirely while the
	// node tier owns the row: with no incident row yet, the first symptom
	// would otherwise be matched against every other open row and the
	// incident row would never be created.
	if nodeKey == "" {
		if r, ok := foldRepeat(c, window, absorbed); ok {
			return r
		}
	}

	// The knowledge base's enqueue check: a row's own key only, not its
	// aliases, across every open row.
	if key != "" {
		for _, row := range open {
			if row.key() == key {
				return Response{
					Decision: DecisionSuppress, RowID: row.ID, Tier: TierKey,
					Reason:    fmt.Sprintf("open row #%d already carries %s", row.ID, key),
					DedupeKey: key,
				}
			}
		}
	}

	resp := Response{Decision: DecisionNew, DedupeKey: key, Reason: "no open row covers it"}
	if nodeKey != "" {
		// The incident row is about the node, so host_id and target.host
		// name it, whatever the model wrote.
		resp.Host = node
		resp.Reason = fmt.Sprintf("node %s is NotReady; this row is its incident (%s)", node, nodeKey)
	}
	return resp
}

// lookupKey is find_open_remediation_by_dedupe_key: a row whose own key it is,
// then a row that carries it as an alias among the newest window.
func lookupKey(open, window []Row, key string) (Row, bool) {
	for _, r := range open {
		if r.key() == key {
			return r, true
		}
	}
	for _, r := range window {
		if r.carriesKey(key) {
			return r, true
		}
	}
	return Row{}, false
}

// foldRepeat is CFOperator._absorb_repeat_remediation. Targets first (the
// FIX names the same object, CFOP-453), then hard identifiers (CFOP-78).
// Containment either way counts; disjoint or empty sets never fold.
func foldRepeat(c Candidate, window []Row, absorbed string) (Response, bool) {
	newIDs := extractIdentifiers(c.Recommendation)
	// CFOP-494: a refused FIX still names its target, as fold_targets.
	newTargets := targetIdentity(orFirst(c.Targets, c.FoldTargets))
	if len(newIDs) == 0 && len(newTargets) == 0 {
		return Response{}, false
	}
	for _, row := range window {
		rowKey := row.key()
		// Exact-key repeats are the knowledge base's to suppress.
		if rowKey != "" && rowKey == c.DedupeKey {
			continue
		}
		var tier string
		var shared []string
		rowTargets := targetIdentity(orFirst(row.Payload["targets"], row.Payload["fold_targets"]))
		if targetsContained(newTargets, rowTargets) {
			tier, shared = TierTarget, targetIDs(newTargets)
		} else if len(newIDs) > 0 {
			rowIDs := extractIdentifiers(pyStrOr(row.Payload["recommendation"]))
			if len(rowIDs) > 0 && (subset(newIDs, rowIDs) || subset(rowIDs, newIDs)) {
				tier, shared = TierIdentifier, intersect(newIDs, rowIDs)
			}
		}
		if tier == "" {
			continue
		}
		resp := Response{
			Decision: DecisionFold, RowID: row.ID, Tier: tier, Shared: shared,
			Reason: fmt.Sprintf("same %s as open row #%d (shared: %s)",
				map[string]string{TierTarget: "FIX target", TierIdentifier: "identifiers"}[tier],
				row.ID, strings.Join(shared, ", ")),
			Absorbed: absorbed,
		}
		// The folded key becomes an alias of the surviving row, so the
		// sweep's loop-break lookup under that key still finds it.
		if c.DedupeKey != "" && c.DedupeKey != rowKey {
			resp.Alias = c.DedupeKey
		}
		return resp, true
	}
	return Response{}, false
}

func subset(a, b map[string]bool) bool {
	for k := range a {
		if !b[k] {
			return false
		}
	}
	return true
}

func intersect(a, b map[string]bool) []string {
	var out []string
	for k := range a {
		if b[k] {
			out = append(out, k)
		}
	}
	sort.Strings(out)
	return out
}
