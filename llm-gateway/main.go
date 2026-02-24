// LLM Gateway - OpenAI-compatible proxy with routing, fallback, and observability
//
// Features:
// - OpenAI-compatible /v1/chat/completions endpoint
// - Health-based routing across multiple backends
// - Automatic fallback on errors
// - Prometheus metrics for all requests
// - Simple async job queue
// - K8s-ready health probes

package main

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promhttp"
	"gopkg.in/yaml.v3"
)

var Version = "0.1.0"

// ============================================================================
// Configuration
// ============================================================================

type Config struct {
	Listen   string    `yaml:"listen"`
	Backends []Backend `yaml:"backends"`
	Fallback []string  `yaml:"fallback"` // Backend names in fallback order
}

type Backend struct {
	Name     string `yaml:"name"`
	Provider string `yaml:"provider"` // ollama, openai, anthropic
	URL      string `yaml:"url"`
	Model    string `yaml:"model"`
	APIKey   string `yaml:"api_key"`
	Priority int    `yaml:"priority"` // Lower = preferred
	Enabled  bool   `yaml:"enabled"`
}

func loadConfig(path string) (*Config, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}

	// Expand environment variables
	expanded := os.ExpandEnv(string(data))

	var cfg Config
	if err := yaml.Unmarshal([]byte(expanded), &cfg); err != nil {
		return nil, err
	}

	if cfg.Listen == "" {
		cfg.Listen = ":4000"
	}

	return &cfg, nil
}

// ============================================================================
// Prometheus Metrics
// ============================================================================

var (
	requestsTotal = prometheus.NewCounterVec(
		prometheus.CounterOpts{
			Name: "llm_gateway_requests_total",
			Help: "Total LLM requests",
		},
		[]string{"backend", "model", "status"},
	)

	requestLatency = prometheus.NewHistogramVec(
		prometheus.HistogramOpts{
			Name:    "llm_gateway_request_duration_seconds",
			Help:    "Request latency in seconds",
			Buckets: []float64{0.1, 0.5, 1, 2, 5, 10, 30, 60, 120},
		},
		[]string{"backend", "model"},
	)

	tokensTotal = prometheus.NewCounterVec(
		prometheus.CounterOpts{
			Name: "llm_gateway_tokens_total",
			Help: "Total tokens processed",
		},
		[]string{"backend", "model", "type"}, // type: input, output
	)

	backendHealth = prometheus.NewGaugeVec(
		prometheus.GaugeOpts{
			Name: "llm_gateway_backend_healthy",
			Help: "Backend health status (1=healthy, 0=unhealthy)",
		},
		[]string{"backend"},
	)

	fallbacksTotal = prometheus.NewCounterVec(
		prometheus.CounterOpts{
			Name: "llm_gateway_fallbacks_total",
			Help: "Fallback events",
		},
		[]string{"from", "to"},
	)

	jobsTotal = prometheus.NewCounterVec(
		prometheus.CounterOpts{
			Name: "llm_gateway_jobs_total",
			Help: "Async jobs",
		},
		[]string{"status"}, // queued, running, completed, failed
	)

	jobQueueSize = prometheus.NewGauge(
		prometheus.GaugeOpts{
			Name: "llm_gateway_job_queue_size",
			Help: "Current job queue size",
		},
	)
)

func init() {
	prometheus.MustRegister(requestsTotal)
	prometheus.MustRegister(requestLatency)
	prometheus.MustRegister(tokensTotal)
	prometheus.MustRegister(backendHealth)
	prometheus.MustRegister(fallbacksTotal)
	prometheus.MustRegister(jobsTotal)
	prometheus.MustRegister(jobQueueSize)
}

// ============================================================================
// Backend Health
// ============================================================================

// DiscoveredModel represents a model found on a backend
type DiscoveredModel struct {
	Name       string
	Backend    string
	Provider   string
	Size       int64
	ModifiedAt string
	Details    map[string]interface{}
}

type BackendState struct {
	sync.RWMutex
	healthy    map[string]bool
	lastCheck  map[string]time.Time
	errorCount map[string]int
	models     map[string][]DiscoveredModel // backend name -> models
}

func NewBackendState() *BackendState {
	return &BackendState{
		healthy:    make(map[string]bool),
		lastCheck:  make(map[string]time.Time),
		errorCount: make(map[string]int),
		models:     make(map[string][]DiscoveredModel),
	}
}

func (bs *BackendState) SetHealthy(name string, healthy bool) {
	bs.Lock()
	defer bs.Unlock()
	bs.healthy[name] = healthy
	bs.lastCheck[name] = time.Now()
	if healthy {
		bs.errorCount[name] = 0
		backendHealth.WithLabelValues(name).Set(1)
	} else {
		bs.errorCount[name]++
		backendHealth.WithLabelValues(name).Set(0)
	}
}

func (bs *BackendState) IsHealthy(name string) bool {
	bs.RLock()
	defer bs.RUnlock()
	healthy, exists := bs.healthy[name]
	return exists && healthy
}

func (bs *BackendState) RecordError(name string) {
	bs.Lock()
	defer bs.Unlock()
	bs.errorCount[name]++
	// Mark unhealthy after 3 consecutive errors
	if bs.errorCount[name] >= 3 {
		bs.healthy[name] = false
		backendHealth.WithLabelValues(name).Set(0)
	}
}

func (bs *BackendState) RecordSuccess(name string) {
	bs.Lock()
	defer bs.Unlock()
	bs.errorCount[name] = 0
	bs.healthy[name] = true
	backendHealth.WithLabelValues(name).Set(1)
}

func (bs *BackendState) SetModels(backend string, models []DiscoveredModel) {
	bs.Lock()
	defer bs.Unlock()
	bs.models[backend] = models
}

func (bs *BackendState) GetAllModels() []DiscoveredModel {
	bs.RLock()
	defer bs.RUnlock()
	var all []DiscoveredModel
	for _, models := range bs.models {
		all = append(all, models...)
	}
	return all
}

// ============================================================================
// Job Queue
// ============================================================================

type Job struct {
	ID        string                 `json:"id"`
	Status    string                 `json:"status"` // queued, running, completed, failed
	Request   map[string]interface{} `json:"request,omitempty"`
	Response  map[string]interface{} `json:"response,omitempty"`
	Error     string                 `json:"error,omitempty"`
	CreatedAt time.Time              `json:"created_at"`
	UpdatedAt time.Time              `json:"updated_at"`
}

type JobQueue struct {
	sync.RWMutex
	jobs    map[string]*Job
	pending chan *Job
}

func NewJobQueue(size int) *JobQueue {
	return &JobQueue{
		jobs:    make(map[string]*Job),
		pending: make(chan *Job, size),
	}
}

func (jq *JobQueue) Submit(req map[string]interface{}) *Job {
	id := generateID()
	job := &Job{
		ID:        id,
		Status:    "queued",
		Request:   req,
		CreatedAt: time.Now(),
		UpdatedAt: time.Now(),
	}

	jq.Lock()
	jq.jobs[id] = job
	jq.Unlock()

	jq.pending <- job
	jobsTotal.WithLabelValues("queued").Inc()
	jobQueueSize.Set(float64(len(jq.pending)))

	return job
}

func (jq *JobQueue) Get(id string) *Job {
	jq.RLock()
	defer jq.RUnlock()
	return jq.jobs[id]
}

func (jq *JobQueue) Update(id string, status string, response map[string]interface{}, errMsg string) {
	jq.Lock()
	defer jq.Unlock()
	if job, ok := jq.jobs[id]; ok {
		job.Status = status
		job.Response = response
		job.Error = errMsg
		job.UpdatedAt = time.Now()
		jobsTotal.WithLabelValues(status).Inc()
	}
	jobQueueSize.Set(float64(len(jq.pending)))
}

func generateID() string {
	b := make([]byte, 8)
	rand.Read(b)
	return hex.EncodeToString(b)
}

// ============================================================================
// Gateway
// ============================================================================

type Gateway struct {
	config   *Config
	backends map[string]*Backend
	state    *BackendState
	jobs     *JobQueue
	client   *http.Client
}

func NewGateway(cfg *Config) *Gateway {
	backends := make(map[string]*Backend)
	for i := range cfg.Backends {
		b := &cfg.Backends[i]
		backends[b.Name] = b
	}

	gw := &Gateway{
		config:   cfg,
		backends: backends,
		state:    NewBackendState(),
		jobs:     NewJobQueue(100),
		client:   &http.Client{Timeout: 120 * time.Second},
	}

	// Initialize all backends as healthy
	for name := range backends {
		gw.state.SetHealthy(name, true)
	}

	return gw
}

func (gw *Gateway) SelectBackend(preferredModel string) *Backend {
	// Try fallback order first
	for _, name := range gw.config.Fallback {
		if b, ok := gw.backends[name]; ok && b.Enabled && gw.state.IsHealthy(name) {
			if preferredModel == "" || b.Model == preferredModel {
				return b
			}
		}
	}

	// Fall back to any healthy backend
	for _, name := range gw.config.Fallback {
		if b, ok := gw.backends[name]; ok && b.Enabled && gw.state.IsHealthy(name) {
			return b
		}
	}

	// Last resort: any enabled backend
	for _, b := range gw.backends {
		if b.Enabled {
			return b
		}
	}

	return nil
}

func (gw *Gateway) ProxyRequest(backend *Backend, body []byte) ([]byte, int, error) {
	return gw.ProxyRequestWithProvider(backend, body)
}

// ============================================================================
// HTTP Handlers
// ============================================================================

func (gw *Gateway) handleChatCompletions(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	body, err := io.ReadAll(r.Body)
	if err != nil {
		http.Error(w, "Failed to read request", http.StatusBadRequest)
		return
	}

	// Parse request to get model for routing
	var req map[string]interface{}
	json.Unmarshal(body, &req)
	requestedModel, _ := req["model"].(string)

	// Parse "backend/model" format
	specifiedBackend, actualModel, hasBackendPrefix := parseModelSpec(requestedModel)

	// If backend is explicitly specified, use it directly
	if hasBackendPrefix {
		backend := gw.backends[specifiedBackend]
		if backend == nil || !backend.Enabled {
			http.Error(w, fmt.Sprintf("Backend %s not available", specifiedBackend), http.StatusBadGateway)
			return
		}
		if !gw.state.IsHealthy(specifiedBackend) {
			http.Error(w, fmt.Sprintf("Backend %s is unhealthy", specifiedBackend), http.StatusBadGateway)
			return
		}

		// Update request body with actual model name (without prefix)
		req["model"] = actualModel
		body, _ = json.Marshal(req)

		start := time.Now()
		respBody, statusCode, err := gw.ProxyRequest(backend, body)
		latency := time.Since(start).Seconds()

		requestLatency.WithLabelValues(backend.Name, actualModel).Observe(latency)

		if err != nil || statusCode >= 500 {
			gw.state.RecordError(backend.Name)
			requestsTotal.WithLabelValues(backend.Name, actualModel, "error").Inc()
			http.Error(w, fmt.Sprintf("Backend failed: %v", err), http.StatusBadGateway)
			return
		}

		gw.state.RecordSuccess(backend.Name)
		requestsTotal.WithLabelValues(backend.Name, actualModel, "success").Inc()

		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("X-LLM-Backend", backend.Name)
		w.WriteHeader(statusCode)
		w.Write(respBody)
		return
	}

	// No explicit backend - build ordered list of backends to try
	var backendsToTry []string
	backendSet := make(map[string]bool)

	if requestedModel != "" {
		// Find backends that have this model
		for _, m := range gw.state.GetAllModels() {
			if m.Name == requestedModel && !backendSet[m.Backend] {
				backendsToTry = append(backendsToTry, m.Backend)
				backendSet[m.Backend] = true
			}
		}
	}

	// Add remaining backends from fallback order
	for _, backendName := range gw.config.Fallback {
		if !backendSet[backendName] {
			backendsToTry = append(backendsToTry, backendName)
			backendSet[backendName] = true
		}
	}

	// Try backends in priority order
	var lastErr error
	var lastBackend string

	for _, backendName := range backendsToTry {
		backend := gw.backends[backendName]
		if backend == nil || !backend.Enabled {
			continue
		}

		// Skip unhealthy backends unless it's our last option
		if !gw.state.IsHealthy(backendName) {
			continue
		}

		start := time.Now()
		respBody, statusCode, err := gw.ProxyRequest(backend, body)
		latency := time.Since(start).Seconds()

		requestLatency.WithLabelValues(backend.Name, backend.Model).Observe(latency)

		if err != nil || statusCode >= 500 {
			gw.state.RecordError(backend.Name)
			requestsTotal.WithLabelValues(backend.Name, backend.Model, "error").Inc()
			lastErr = err
			if lastBackend != "" {
				fallbacksTotal.WithLabelValues(lastBackend, backend.Name).Inc()
			}
			lastBackend = backend.Name
			log.Printf("Backend %s failed: %v (status=%d), trying next", backend.Name, err, statusCode)
			continue
		}

		// Success
		gw.state.RecordSuccess(backend.Name)
		requestsTotal.WithLabelValues(backend.Name, backend.Model, "success").Inc()

		// Extract token counts for metrics
		var resp map[string]interface{}
		if json.Unmarshal(respBody, &resp) == nil {
			if usage, ok := resp["usage"].(map[string]interface{}); ok {
				if pt, ok := usage["prompt_tokens"].(float64); ok {
					tokensTotal.WithLabelValues(backend.Name, backend.Model, "input").Add(pt)
				}
				if ct, ok := usage["completion_tokens"].(float64); ok {
					tokensTotal.WithLabelValues(backend.Name, backend.Model, "output").Add(ct)
				}
			}
		}

		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("X-LLM-Backend", backend.Name)
		w.WriteHeader(statusCode)
		w.Write(respBody)
		return
	}

	// All backends failed
	log.Printf("All backends failed, last error: %v", lastErr)
	http.Error(w, fmt.Sprintf("All backends failed: %v", lastErr), http.StatusBadGateway)
}

// parseModelSpec parses "backend/model" format, returns (backend, model, hasPrefix)
func parseModelSpec(spec string) (string, string, bool) {
	if idx := strings.Index(spec, "/"); idx > 0 {
		return spec[:idx], spec[idx+1:], true
	}
	return "", spec, false
}

func (gw *Gateway) handleModels(w http.ResponseWriter, r *http.Request) {
	var models []map[string]interface{}
	seen := make(map[string]bool)

	// First add dynamically discovered models (with backend prefix)
	for _, m := range gw.state.GetAllModels() {
		prefixedName := m.Backend + "/" + m.Name
		if !seen[prefixedName] {
			seen[prefixedName] = true
			models = append(models, map[string]interface{}{
				"id":       prefixedName,
				"object":   "model",
				"owned_by": m.Provider,
				"backend":  m.Backend,
			})
		}
	}

	// Add configured model from backends (with backend prefix)
	for _, b := range gw.backends {
		if b.Enabled && b.Model != "" {
			prefixedName := b.Name + "/" + b.Model
			if !seen[prefixedName] {
				seen[prefixedName] = true
				models = append(models, map[string]interface{}{
					"id":       prefixedName,
					"object":   "model",
					"owned_by": b.Provider,
					"backend":  b.Name,
				})
			}
		}
	}

	// Add static models from providers (with backend prefix)
	for _, b := range gw.backends {
		if b.Enabled {
			provider := GetProvider(b.Provider)
			if provider != nil {
				for _, name := range provider.StaticModels() {
					prefixedName := b.Name + "/" + name
					if !seen[prefixedName] {
						seen[prefixedName] = true
						models = append(models, map[string]interface{}{
							"id":       prefixedName,
							"object":   "model",
							"owned_by": b.Provider,
							"backend":  b.Name,
						})
					}
				}
			}
		}
	}

	resp := map[string]interface{}{
		"object": "list",
		"data":   models,
	}

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(resp)
}

// handleOllamaTags returns models in Ollama /api/tags format for compatibility
// Model names are prefixed with backend: "backend/model" for clear identification
func (gw *Gateway) handleOllamaTags(w http.ResponseWriter, r *http.Request) {
	var models []map[string]interface{}
	seen := make(map[string]bool)

	// Add dynamically discovered models (with backend prefix)
	for _, m := range gw.state.GetAllModels() {
		prefixedName := m.Backend + "/" + m.Name
		if !seen[prefixedName] {
			seen[prefixedName] = true
			details := m.Details
			if details == nil {
				details = map[string]interface{}{
					"format":             "gguf",
					"family":             m.Backend,
					"parameter_size":     "unknown",
					"quantization_level": "unknown",
				}
			}
			models = append(models, map[string]interface{}{
				"name":        prefixedName,
				"modified_at": m.ModifiedAt,
				"size":        m.Size,
				"digest":      "",
				"details":     details,
			})
		}
	}

	// Add configured model from non-Ollama backends (with backend prefix)
	for _, b := range gw.backends {
		if b.Enabled && b.Provider != "ollama" && b.Model != "" {
			prefixedName := b.Name + "/" + b.Model
			if !seen[prefixedName] {
				seen[prefixedName] = true
				models = append(models, map[string]interface{}{
					"name":        prefixedName,
					"modified_at": time.Now().Format(time.RFC3339),
					"size":        0,
					"digest":      "",
					"details": map[string]interface{}{
						"format":             "api",
						"family":             b.Name,
						"parameter_size":     "cloud",
						"quantization_level": "none",
					},
				})
			}
		}
	}

	// Add static models from providers (with backend prefix)
	for _, b := range gw.backends {
		if b.Enabled {
			provider := GetProvider(b.Provider)
			if provider != nil {
				for _, name := range provider.StaticModels() {
					prefixedName := b.Name + "/" + name
					if !seen[prefixedName] {
						seen[prefixedName] = true
						models = append(models, map[string]interface{}{
							"name":        prefixedName,
							"modified_at": time.Now().Format(time.RFC3339),
							"size":        0,
							"digest":      "",
							"details": map[string]interface{}{
								"format":             "api",
								"family":             b.Name,
								"parameter_size":     "cloud",
								"quantization_level": "none",
							},
						})
					}
				}
			}
		}
	}

	resp := map[string]interface{}{
		"models": models,
	}

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(resp)
}

// handleOllamaChat handles native Ollama /api/chat format requests
func (gw *Gateway) handleOllamaChat(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	body, err := io.ReadAll(r.Body)
	if err != nil {
		http.Error(w, "Failed to read request", http.StatusBadRequest)
		return
	}

	// Parse the Ollama request
	var ollamaReq map[string]interface{}
	if err := json.Unmarshal(body, &ollamaReq); err != nil {
		http.Error(w, "Invalid JSON", http.StatusBadRequest)
		return
	}

	requestedModel, _ := ollamaReq["model"].(string)
	if requestedModel == "" {
		http.Error(w, "Model required", http.StatusBadRequest)
		return
	}

	// Parse "backend/model" format
	specifiedBackend, actualModel, hasBackendPrefix := parseModelSpec(requestedModel)

	// Find a backend that has this model
	var targetBackend *Backend

	// If backend is specified explicitly, use it directly
	if hasBackendPrefix {
		if backend, ok := gw.backends[specifiedBackend]; ok && backend.Enabled && gw.state.IsHealthy(specifiedBackend) {
			targetBackend = backend
			requestedModel = actualModel // Use the model name without prefix for the actual request
		} else {
			http.Error(w, fmt.Sprintf("Backend %s not available", specifiedBackend), http.StatusBadGateway)
			return
		}
	} else {
		// No prefix - search across all backends
		// First try backends with dynamically discovered models
		for _, m := range gw.state.GetAllModels() {
			if m.Name == requestedModel {
				if backend, ok := gw.backends[m.Backend]; ok && backend.Enabled && gw.state.IsHealthy(m.Backend) {
					targetBackend = backend
					break
				}
			}
		}

		// Then check statically configured models on backends
		if targetBackend == nil {
			for _, backend := range gw.backends {
				if backend.Enabled && backend.Model == requestedModel && gw.state.IsHealthy(backend.Name) {
					targetBackend = backend
					break
				}
			}
		}

		// Then check static models from provider
		if targetBackend == nil {
			for _, backend := range gw.backends {
				if backend.Enabled && gw.state.IsHealthy(backend.Name) {
					provider := GetProvider(backend.Provider)
					if provider != nil {
						for _, m := range provider.StaticModels() {
							if m == requestedModel {
								targetBackend = backend
								break
							}
						}
					}
				}
				if targetBackend != nil {
					break
				}
			}
		}

		// Fall back to first healthy backend
		if targetBackend == nil {
			for _, backendName := range gw.config.Fallback {
				backend := gw.backends[backendName]
				if backend != nil && backend.Enabled && gw.state.IsHealthy(backendName) {
					targetBackend = backend
					break
				}
			}
		}
	}

	if targetBackend == nil {
		http.Error(w, fmt.Sprintf("No healthy backend available for model %s", requestedModel), http.StatusBadGateway)
		return
	}

	start := time.Now()

	// Use provider interface to handle the request
	respBody, statusCode, err := gw.ProxyOllamaRequestWithProvider(targetBackend, requestedModel, ollamaReq)
	if err != nil {
		log.Printf("Backend %s failed: %v", targetBackend.Name, err)
		gw.state.RecordError(targetBackend.Name)
		http.Error(w, fmt.Sprintf("Backend request failed: %v", err), http.StatusBadGateway)
		return
	}

	latency := time.Since(start).Seconds()
	requestLatency.WithLabelValues(targetBackend.Name, requestedModel).Observe(latency)

	if statusCode >= 400 {
		gw.state.RecordError(targetBackend.Name)
		requestsTotal.WithLabelValues(targetBackend.Name, requestedModel, "error").Inc()
	} else {
		gw.state.RecordSuccess(targetBackend.Name)
		requestsTotal.WithLabelValues(targetBackend.Name, requestedModel, "success").Inc()
	}

	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("X-LLM-Backend", targetBackend.Name)
	w.WriteHeader(statusCode)
	w.Write(respBody)
}

func (gw *Gateway) handleJobSubmit(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	body, err := io.ReadAll(r.Body)
	if err != nil {
		http.Error(w, "Failed to read request", http.StatusBadRequest)
		return
	}

	var req map[string]interface{}
	if err := json.Unmarshal(body, &req); err != nil {
		http.Error(w, "Invalid JSON", http.StatusBadRequest)
		return
	}

	job := gw.jobs.Submit(req)

	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusAccepted)
	json.NewEncoder(w).Encode(job)
}

func (gw *Gateway) handleJobStatus(w http.ResponseWriter, r *http.Request) {
	id := strings.TrimPrefix(r.URL.Path, "/v1/jobs/")
	job := gw.jobs.Get(id)

	if job == nil {
		http.Error(w, "Job not found", http.StatusNotFound)
		return
	}

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(job)
}

func (gw *Gateway) handleHealth(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]interface{}{
		"status":  "ok",
		"version": Version,
	})
}

func (gw *Gateway) handleReady(w http.ResponseWriter, r *http.Request) {
	// Check if at least one backend is healthy
	for name, b := range gw.backends {
		if b.Enabled && gw.state.IsHealthy(name) {
			w.Header().Set("Content-Type", "application/json")
			json.NewEncoder(w).Encode(map[string]string{"status": "ready"})
			return
		}
	}

	w.WriteHeader(http.StatusServiceUnavailable)
	json.NewEncoder(w).Encode(map[string]string{"status": "no healthy backends"})
}

func (gw *Gateway) handleBackends(w http.ResponseWriter, r *http.Request) {
	var backends []map[string]interface{}
	for name, b := range gw.backends {
		backends = append(backends, map[string]interface{}{
			"name":     name,
			"provider": b.Provider,
			"model":    b.Model,
			"enabled":  b.Enabled,
			"healthy":  gw.state.IsHealthy(name),
		})
	}

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(backends)
}

// ============================================================================
// Background Workers
// ============================================================================

func (gw *Gateway) healthChecker(ctx context.Context) {
	ticker := time.NewTicker(30 * time.Second)
	defer ticker.Stop()

	check := func() {
		for name, b := range gw.backends {
			if !b.Enabled {
				continue
			}

			provider := GetProvider(b.Provider)
			if provider == nil {
				provider = GetProvider("openai")
			}

			url := provider.HealthCheckURL(b)
			if url == "" {
				// No health check endpoint, assume healthy
				gw.state.SetHealthy(name, true)
				continue
			}

			ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			req, _ := http.NewRequestWithContext(ctx, "GET", url, nil)
			if b.APIKey != "" {
				req.Header.Set("Authorization", "Bearer "+b.APIKey)
			}

			resp, err := gw.client.Do(req)
			cancel()

			healthy := err == nil && resp != nil && resp.StatusCode == 200

			// Parse response to discover models if provider supports it
			if healthy && provider.SupportsModelDiscovery() && resp != nil {
				body, readErr := io.ReadAll(resp.Body)
				resp.Body.Close()
				if readErr == nil {
					models := provider.ParseModelsResponse(name, body)
					if len(models) > 0 {
						gw.state.SetModels(name, models)
						log.Printf("Discovered %d models from %s", len(models), name)
					}
				}
			} else if resp != nil {
				resp.Body.Close()
			}

			gw.state.SetHealthy(name, healthy)
			log.Printf("Health check %s: healthy=%v", name, healthy)
		}
	}

	// Initial check
	check()

	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			check()
		}
	}
}

func (gw *Gateway) jobWorker(ctx context.Context) {
	for {
		select {
		case <-ctx.Done():
			return
		case job := <-gw.jobs.pending:
			gw.jobs.Update(job.ID, "running", nil, "")

			// Execute the request
			body, _ := json.Marshal(job.Request)

			var lastErr error
			for _, backendName := range gw.config.Fallback {
				backend := gw.backends[backendName]
				if backend == nil || !backend.Enabled || !gw.state.IsHealthy(backendName) {
					continue
				}

				respBody, statusCode, err := gw.ProxyRequest(backend, body)
				if err != nil || statusCode >= 500 {
					lastErr = err
					continue
				}

				var resp map[string]interface{}
				json.Unmarshal(respBody, &resp)
				gw.jobs.Update(job.ID, "completed", resp, "")
				lastErr = nil
				break
			}

			if lastErr != nil {
				gw.jobs.Update(job.ID, "failed", nil, lastErr.Error())
			}
		}
	}
}

// ============================================================================
// Main
// ============================================================================

func main() {
	configPath := os.Getenv("CONFIG_PATH")
	if configPath == "" {
		configPath = "config.yaml"
	}

	cfg, err := loadConfig(configPath)
	if err != nil {
		log.Fatalf("Failed to load config: %v", err)
	}

	gw := NewGateway(cfg)

	// Setup HTTP routes
	mux := http.NewServeMux()

	// OpenAI-compatible endpoints
	mux.HandleFunc("/v1/chat/completions", gw.handleChatCompletions)
	mux.HandleFunc("/v1/models", gw.handleModels)

	// Ollama-compatible endpoints
	mux.HandleFunc("/api/tags", gw.handleOllamaTags)
	mux.HandleFunc("/api/chat", gw.handleOllamaChat)

	// Job queue endpoints
	mux.HandleFunc("/v1/jobs", gw.handleJobSubmit)
	mux.HandleFunc("/v1/jobs/", gw.handleJobStatus)

	// Health/status endpoints
	mux.HandleFunc("/health", gw.handleHealth)
	mux.HandleFunc("/healthz", gw.handleHealth)
	mux.HandleFunc("/ready", gw.handleReady)
	mux.HandleFunc("/readyz", gw.handleReady)
	mux.HandleFunc("/backends", gw.handleBackends)

	// Prometheus metrics
	mux.Handle("/metrics", promhttp.Handler())

	server := &http.Server{
		Addr:    cfg.Listen,
		Handler: mux,
	}

	// Start background workers
	ctx, cancel := context.WithCancel(context.Background())
	go gw.healthChecker(ctx)
	go gw.jobWorker(ctx)

	// Graceful shutdown
	go func() {
		sigCh := make(chan os.Signal, 1)
		signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
		<-sigCh

		log.Println("Shutting down...")
		cancel()

		shutdownCtx, shutdownCancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer shutdownCancel()
		server.Shutdown(shutdownCtx)
	}()

	log.Printf("LLM Gateway v%s listening on %s", Version, cfg.Listen)
	if err := server.ListenAndServe(); err != http.ErrServerClosed {
		log.Fatalf("Server error: %v", err)
	}
}
