package dedupe

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"testing"
)

// The cases in testdata/ are shared with agent/test_dedupe_parity.py, which
// runs the same files against the Python tiers. A case that fails here and
// passes there (or the reverse) is a parity break.

type caseFile struct {
	About string       `json:"about"`
	Cases []decideCase `json:"cases"`
}

type decideCase struct {
	Name    string   `json:"name"`
	Note    string   `json:"note"`
	Phase4  string   `json:"phase4"`
	Via     string   `json:"via"`
	Request Request  `json:"request"`
	Want    Response `json:"want"`
}

func loadJSON(t *testing.T, path string, into any) {
	t.Helper()
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	dec.DisallowUnknownFields()
	if err := dec.Decode(into); err != nil {
		t.Fatalf("%s: %v", path, err)
	}
}

func TestDecideCases(t *testing.T) {
	files, err := filepath.Glob(filepath.Join("testdata", "decide", "*.json"))
	if err != nil {
		t.Fatal(err)
	}
	// A broken glob must not pass as "every case agreed".
	if len(files) < 8 {
		t.Fatalf("found %d case files under testdata/decide; expected the eight groups", len(files))
	}
	total := 0
	for _, f := range files {
		var cf caseFile
		loadJSON(t, f, &cf)
		if cf.About == "" {
			t.Errorf("%s: every case file says where its cases came from", f)
		}
		for _, c := range cf.Cases {
			total++
			t.Run(filepath.Base(f)+"/"+c.Name, func(t *testing.T) {
				got := Decide(c.Request)
				if got.Reason == "" {
					t.Error("a decision always carries a reason")
				}
				got.Reason = ""
				if !reflect.DeepEqual(got, c.Want) {
					t.Errorf("\n got  %+v\n want %+v", got, c.Want)
				}
			})
		}
	}
	if total < 50 {
		t.Fatalf("ran %d cases; the files hold more than that", total)
	}
}

type functionCases struct {
	About         string `json:"about"`
	NormalizeHost []struct {
		In   *string `json:"in"`
		Want string  `json:"want"`
	} `json:"normalize_host"`
	HostCandidates []struct {
		In   *string  `json:"in"`
		Want []string `json:"want"`
	} `json:"host_candidates"`
	MatchKnownHost []struct {
		Token string   `json:"token"`
		Known []string `json:"known"`
		Want  *string  `json:"want"`
	} `json:"match_known_host"`
	CollapseKey []struct {
		Host     string   `json:"host"`
		NotReady []string `json:"notready"`
		WantKey  *string  `json:"want_key"`
		WantNode *string  `json:"want_node"`
	} `json:"collapse_key"`
	Identifiers []struct {
		In   *string  `json:"in"`
		Want []string `json:"want"`
	} `json:"identifiers"`
	NormalizeTargetID []struct {
		In   any    `json:"in"`
		Want string `json:"want"`
	} `json:"normalize_target_id"`
	TargetsContained []struct {
		A    any  `json:"a"`
		B    any  `json:"b"`
		Want bool `json:"want"`
	} `json:"targets_contained"`
}

func str(p *string) string {
	if p == nil {
		return ""
	}
	return *p
}

func TestFunctionCases(t *testing.T) {
	var fc functionCases
	loadJSON(t, filepath.Join("testdata", "functions.json"), &fc)
	if len(fc.NormalizeHost) == 0 || len(fc.HostCandidates) == 0 || len(fc.MatchKnownHost) == 0 ||
		len(fc.CollapseKey) == 0 || len(fc.Identifiers) == 0 || len(fc.NormalizeTargetID) == 0 ||
		len(fc.TargetsContained) == 0 {
		t.Fatal("a section of functions.json is empty or misnamed")
	}
	for _, c := range fc.NormalizeHost {
		if got := normalizeHost(str(c.In)); got != c.Want {
			t.Errorf("normalizeHost(%q) = %q, want %q", str(c.In), got, c.Want)
		}
	}
	for _, c := range fc.HostCandidates {
		got := hostCandidates(str(c.In))
		if len(got) == 0 && len(c.Want) == 0 {
			continue
		}
		if !reflect.DeepEqual(got, c.Want) {
			t.Errorf("hostCandidates(%q) = %q, want %q", str(c.In), got, c.Want)
		}
	}
	for _, c := range fc.MatchKnownHost {
		if got := matchKnownHost(c.Token, c.Known); got != str(c.Want) {
			t.Errorf("matchKnownHost(%q, %q) = %q, want %q", c.Token, c.Known, got, str(c.Want))
		}
	}
	for _, c := range fc.CollapseKey {
		key, node := collapseKey(c.Host, notReadySet(c.NotReady))
		if key != str(c.WantKey) || node != str(c.WantNode) {
			t.Errorf("collapseKey(%q, %q) = %q, %q; want %q, %q",
				c.Host, c.NotReady, key, node, str(c.WantKey), str(c.WantNode))
		}
	}
	for _, c := range fc.Identifiers {
		var got []string
		for id := range extractIdentifiers(str(c.In)) {
			got = append(got, id)
		}
		sort.Strings(got)
		if len(got) == 0 && len(c.Want) == 0 {
			continue
		}
		if !reflect.DeepEqual(got, c.Want) {
			t.Errorf("extractIdentifiers(%q) = %q, want %q", str(c.In), got, c.Want)
		}
	}
	for _, c := range fc.NormalizeTargetID {
		if got := normalizeTargetID(c.In); got != c.Want {
			t.Errorf("normalizeTargetID(%#v) = %q, want %q", c.In, got, c.Want)
		}
	}
	for _, c := range fc.TargetsContained {
		if got := targetsContained(targetIdentity(c.A), targetIdentity(c.B)); got != c.Want {
			t.Errorf("targetsContained(%v, %v) = %v, want %v", c.A, c.B, got, c.Want)
		}
	}
}
