package dedupe

import (
	"bytes"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
)

// AuthHeader carries the shared secret, as for cfop-tracker and changerecord.
const AuthHeader = "X-CFOP-Token"

// maxBody bounds a request. The open set is small, and the caller sends only
// the payload fields the tiers read.
const maxBody = 8 << 20

// Handler serves the contract in the package doc. secret "" accepts every
// request, which keeps local runs and early deploys usable; main logs it.
func Handler(secret string, log *slog.Logger) http.Handler {
	mux := http.NewServeMux()
	health := func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]any{"ok": true})
	}
	mux.HandleFunc("GET /healthz", health)
	mux.HandleFunc("GET /livez", health)
	mux.HandleFunc("/v1/decide", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			w.Header().Set("Allow", http.MethodPost)
			writeError(w, http.StatusMethodNotAllowed, "POST only")
			return
		}
		if msg := checkAuth(r.Header.Get(AuthHeader), secret); msg != "" {
			writeError(w, http.StatusUnauthorized, msg)
			return
		}
		req, err := decodeRequest(http.MaxBytesReader(w, r.Body, maxBody))
		if err != nil {
			writeError(w, http.StatusBadRequest, err.Error())
			return
		}
		resp := Decide(req)
		log.Info("decided",
			"decision", resp.Decision, "tier", resp.Tier, "row_id", resp.RowID,
			"source", req.Candidate.Source, "dedupe_key", req.Candidate.DedupeKey,
			"open_rows", len(req.OpenRows))
		writeJSON(w, http.StatusOK, resp)
	})
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		writeError(w, http.StatusNotFound, "not found")
	})
	return mux
}

func checkAuth(got, want string) string {
	if want == "" {
		return ""
	}
	if got == "" {
		return "Missing " + AuthHeader + " header"
	}
	if subtle.ConstantTimeCompare([]byte(got), []byte(want)) != 1 {
		return "Invalid " + AuthHeader + " header"
	}
	return ""
}

// decodeRequest reads one Request. Numbers stay json.Number so a payload
// value prints the way Python's str() would print it.
func decodeRequest(body io.Reader) (Request, error) {
	raw, err := io.ReadAll(body)
	if err != nil {
		return Request{}, err
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	// The outer Candidate shadows Request's, so a missing or null candidate
	// is told apart from an empty one. An empty one is a real candidate: the
	// node tier can give it a key.
	var in struct {
		Request
		Candidate *Candidate `json:"candidate"`
	}
	if err := dec.Decode(&in); err != nil {
		return Request{}, errors.New("invalid request body: " + err.Error())
	}
	// Only EOF says the body was one value: More stops at a closing
	// delimiter, so `{...}}` would pass it.
	if _, err := dec.Token(); err != io.EOF {
		return Request{}, errors.New("invalid request body: trailing data")
	}
	if in.Candidate == nil {
		return Request{}, errors.New("invalid request body: no candidate")
	}
	req := in.Request
	req.Candidate = *in.Candidate
	return req, nil
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeError(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]string{"error": msg})
}
