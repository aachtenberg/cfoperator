package cfoperator

import (
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestActionClientRefusesAnythingButTheFourPosts(t *testing.T) {
	var hits int
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hits++
		w.WriteHeader(http.StatusOK)
	}))
	t.Cleanup(srv.Close)

	c := NewActionClient(srv.URL, "tok", time.Second)
	for _, call := range []struct{ method, path string }{
		{http.MethodGet, "/api/remediations/1/approve"},
		{http.MethodPost, "/api/auth/tokens"},
		{http.MethodPost, "/api/remediations"},
		{http.MethodPost, "/api/remediations/1/reclassify"},
		{http.MethodDelete, "/api/remediations/1"},
		{http.MethodPost, "/api/investigations/1/triage/extra"},
		{http.MethodPost, "/api/remediations/1/approve/../x"},
		{http.MethodPost, "/api/remediations/nope/approve"},
	} {
		if _, err := c.do(call.method, call.path, nil); err == nil {
			t.Errorf("%s %s was allowed", call.method, call.path)
		}
	}
	if hits != 0 {
		t.Fatalf("a refused call reached the agent %d times", hits)
	}
}

func TestActionClientPostsTheFourWrites(t *testing.T) {
	var gotMethod, gotPath, gotBody string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotMethod = r.Method
		gotPath = r.URL.Path
		b, _ := io.ReadAll(r.Body)
		gotBody = string(b)
		if r.Header.Get("Authorization") != "Bearer tok" {
			t.Errorf("auth = %q", r.Header.Get("Authorization"))
		}
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"id":1,"status":"ok"}`))
	}))
	t.Cleanup(srv.Close)
	c := NewActionClient(srv.URL, "tok", time.Second)

	if _, err := c.ApproveRemediation(7); err != nil {
		t.Fatal(err)
	}
	if gotMethod != http.MethodPost || gotPath != "/api/remediations/7/approve" || gotBody != "" {
		t.Fatalf("approve = %s %s %q", gotMethod, gotPath, gotBody)
	}

	if _, err := c.RejectRemediation(7, "won't fix"); err != nil {
		t.Fatal(err)
	}
	if gotPath != "/api/remediations/7/reject" || !strings.Contains(gotBody, "won't fix") {
		t.Fatalf("reject = %s %q", gotPath, gotBody)
	}

	if _, err := c.ResolveRemediation(7, "fixed by hand"); err != nil {
		t.Fatal(err)
	}
	if gotPath != "/api/remediations/7/resolve" || !strings.Contains(gotBody, "fixed by hand") {
		t.Fatalf("resolve = %s %q", gotPath, gotBody)
	}

	if _, err := c.TriageInvestigation(12, "ack", "seen it"); err != nil {
		t.Fatal(err)
	}
	if gotPath != "/api/investigations/12/triage" || !strings.Contains(gotBody, `"action":"ack"`) {
		t.Fatalf("triage = %s %q", gotPath, gotBody)
	}
}

func TestActionClientRequiresANote(t *testing.T) {
	c := NewActionClient("http://127.0.0.1:1", "tok", time.Second)
	if _, err := c.RejectRemediation(1, "  "); err == nil {
		t.Fatal("reject with a blank note should fail before any dial")
	}
	if _, err := c.ResolveRemediation(1, ""); err == nil {
		t.Fatal("resolve with no note should fail")
	}
	if _, err := c.TriageInvestigation(1, "resolved", ""); err == nil {
		t.Fatal("triage with no note should fail")
	}
	if _, err := c.TriageInvestigation(1, "suppress", "no"); err == nil {
		t.Fatal("triage should refuse a verdict the route does not accept")
	}
}

func TestActionClientDoesNotFollowRedirects(t *testing.T) {
	var hits []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hits = append(hits, r.URL.Path)
		if r.URL.Path == "/api/remediations/1/approve" {
			http.Redirect(w, r, "/api/auth/tokens", http.StatusFound)
			return
		}
		t.Errorf("followed the redirect to %s", r.URL.Path)
	}))
	t.Cleanup(srv.Close)

	c := NewActionClient(srv.URL, "tok", time.Second)
	if _, err := c.ApproveRemediation(1); err == nil {
		t.Fatal("a redirect must not count as success")
	}
	if len(hits) != 1 || hits[0] != "/api/remediations/1/approve" {
		t.Fatalf("hits = %v, want only the original POST", hits)
	}
}
