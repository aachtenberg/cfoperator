package dedupe

import (
	"regexp"
	"sort"
	"strings"
)

// Ported from agent/agent.py: _KUSTOMIZE_HASH_SUFFIX, _normalize_target_id,
// _target_identity, _same_target and _targets_contained (CFOP-453, CFOP-494).

// kustomizeHashSuffix is a kustomize generator suffix: ten characters from
// kustomize's own alphabet, which has no vowels and no 0/1, so "-production"
// or "-ingest" can never be mistaken for one. The optional newline is
// Python's `$`, which also matches before a final "\n".
var kustomizeHashSuffix = regexp.MustCompile(`-[bcdfghkmnpqrstvwxz2456789]{10}(\n?)$`)

// target is one FIX target as the fold compares it: kind, repo and a
// normalized id. Normalized for COMPARISON only; the stored id, the tgt- hash
// and what the executor is shown stay verbatim.
type target struct {
	kind, repo, id string
}

func normalizeTargetID(raw any) string {
	tid := strings.Trim(pyLower(pyStrip(pyStrOr(raw))), "/")
	return kustomizeHashSuffix.ReplaceAllString(tid, "${1}")
}

// targetIdentity is the set of usable targets in a FIX's target list. Empty
// means "this row names nothing matchable", never "matches everything".
func targetIdentity(targets any) map[target]bool {
	out := map[target]bool{}
	list, _ := targets.([]any)
	for _, item := range list {
		t, ok := item.(map[string]any)
		if !ok {
			continue
		}
		kind := pyLower(pyStrip(pyStrOr(t["kind"])))
		tid := normalizeTargetID(t["id"])
		if kind != "" && tid != "" {
			out[target{kind: kind, repo: pyLower(pyStrip(pyStrOr(t["repo"]))), id: tid}] = true
		}
	}
	return out
}

// sameTarget: same kind and repo, and the ids equal or one a path-segment
// suffix of the other. data/eccc-ingest-script is eccc-ingest-script, but
// base/apps/kustomization.yml is not base/monitoring/kustomization.yml.
func sameTarget(a, b target) bool {
	if a.kind != b.kind || a.repo != b.repo {
		return false
	}
	return a.id == b.id || strings.HasSuffix(a.id, "/"+b.id) || strings.HasSuffix(b.id, "/"+a.id)
}

// targetsContained: every target on one side has a match on the other.
// Containment, not intersection: two fixes that each touch something the
// other does not are two fixes.
func targetsContained(a, b map[target]bool) bool {
	if len(a) == 0 || len(b) == 0 {
		return false
	}
	covered := func(xs, ys map[target]bool) bool {
		for x := range xs {
			found := false
			for y := range ys {
				if sameTarget(x, y) {
					found = true
					break
				}
			}
			if !found {
				return false
			}
		}
		return true
	}
	return covered(a, b) || covered(b, a)
}

// targetIDs is the sorted normalized ids, duplicates kept: Python's
// sorted(t[2] for t in targets), which is what the fold names as shared.
func targetIDs(ts map[target]bool) []string {
	out := make([]string, 0, len(ts))
	for t := range ts {
		out = append(out, t.id)
	}
	sort.Strings(out)
	return out
}
