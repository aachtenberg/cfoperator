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
	"bytes"
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

type BackendState struct {
	sync.RWMutex
	healthy    map[string]bool
	lastCheck  map[string]time.Time
	errorCount map[string]int
}

func NewBackendState() *BackendState {
	return &BackendState{
		healthy:    make(map[string]bool),
		lastCheck:  make(map[string]time.Time),
		errorCount: make(map[string]int),
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
	var url string
	var req *http.Request
	var err error

	switch backend.Provider {
	case "ollama":
		url = strings.TrimRight(backend.URL, "/") + "/api/chat"
		// Transform OpenAI format to Ollama format
		var openaiReq map[string]interface{}
		json.Unmarshal(body, &openaiReq)
		ollamaReq := map[string]interface{}{
			"model":    backend.Model,
			"messages": openaiReq["messages"],
			"stream":   false,
		}
		if tools, ok := openaiReq["tools"]; ok {
			ollamaReq["tools"] = tools
		}
		if temp, ok := openaiReq["temperature"]; ok {
			ollamaReq["temperature"] = temp
		}
		body, _ = json.Marshal(ollamaReq)
		req, err = http.NewRequest("POST", url, bytes.NewReader(body))

	case "anthropic":
		url = strings.TrimRight(backend.URL, "/") + "/v1/messages"
		// Transform OpenAI format to Anthropic format
		var openaiReq map[string]interface{}
		json.Unmarshal(body, &openaiReq)

		messages := openaiReq["messages"].([]interface{})
		var systemPrompt string
		var anthropicMsgs []map[string]interface{}
		for _, m := range messages {
			msg := m.(map[string]interface{})
			if msg["role"] == "system" {
				systemPrompt = msg["content"].(string)
			} else {
				anthropicMsgs = append(anthropicMsgs, map[string]interface{}{
					"role":    msg["role"],
					"content": msg["content"],
				})
			}
		}

		anthropicReq := map[string]interface{}{
			"model":      backend.Model,
			"max_tokens": 4096,
			"messages":   anthropicMsgs,
		}
		if systemPrompt != "" {
			anthropicReq["system"] = systemPrompt
		}
		if tools, ok := openaiReq["tools"]; ok {
			// Transform to Anthropic tool format
			var anthropicTools []map[string]interface{}
			for _, t := range tools.([]interface{}) {
				tool := t.(map[string]interface{})
				if fn, ok := tool["function"].(map[string]interface{}); ok {
					anthropicTools = append(anthropicTools, map[string]interface{}{
						"name":         fn["name"],
						"description":  fn["description"],
						"input_schema": fn["parameters"],
					})
				}
			}
			anthropicReq["tools"] = anthropicTools
		}
		body, _ = json.Marshal(anthropicReq)
		req, err = http.NewRequest("POST", url, bytes.NewReader(body))
		if err == nil {
			req.Header.Set("x-api-key", backend.APIKey)
			req.Header.Set("anthropic-version", "2023-06-01")
		}

	default: // openai compatible (groq, etc)
		url = strings.TrimRight(backend.URL, "/")
		url = strings.TrimSuffix(url, "/v1") + "/v1/chat/completions"
		// Inject model
		var openaiReq map[string]interface{}
		json.Unmarshal(body, &openaiReq)
		openaiReq["model"] = backend.Model
		body, _ = json.Marshal(openaiReq)
		req, err = http.NewRequest("POST", url, bytes.NewReader(body))
		if err == nil && backend.APIKey != "" {
			req.Header.Set("Authorization", "Bearer "+backend.APIKey)
		}
	}

	if err != nil {
		return nil, 0, err
	}

	req.Header.Set("Content-Type", "application/json")

	resp, err := gw.client.Do(req)
	if err != nil {
		return nil, 0, err
	}
	defer resp.Body.Close()

	respBody, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, resp.StatusCode, err
	}

	// Transform response back to OpenAI format if needed
	if backend.Provider == "ollama" {
		respBody = gw.transformOllamaResponse(respBody)
	} else if backend.Provider == "anthropic" {
		respBody = gw.transformAnthropicResponse(respBody)
	}

	return respBody, resp.StatusCode, nil
}

func (gw *Gateway) transformOllamaResponse(body []byte) []byte {
	var ollamaResp map[string]interface{}
	if err := json.Unmarshal(body, &ollamaResp); err != nil {
		return body
	}

	msg, _ := ollamaResp["message"].(map[string]interface{})
	content, _ := msg["content"].(string)
	toolCalls, _ := msg["tool_calls"].([]interface{})

	openaiResp := map[string]interface{}{
		"id":      "chatcmpl-" + generateID(),
		"object":  "chat.completion",
		"created": time.Now().Unix(),
		"model":   ollamaResp["model"],
		"choices": []map[string]interface{}{
			{
				"index": 0,
				"message": map[string]interface{}{
					"role":       "assistant",
					"content":    content,
					"tool_calls": toolCalls,
				},
				"finish_reason": "stop",
			},
		},
		"usage": map[string]interface{}{
			"prompt_tokens":     ollamaResp["prompt_eval_count"],
			"completion_tokens": ollamaResp["eval_count"],
			"total_tokens": func() int {
				p, _ := ollamaResp["prompt_eval_count"].(float64)
				c, _ := ollamaResp["eval_count"].(float64)
				return int(p + c)
			}(),
		},
	}

	result, _ := json.Marshal(openaiResp)
	return result
}

func (gw *Gateway) transformAnthropicResponse(body []byte) []byte {
	var anthropicResp map[string]interface{}
	if err := json.Unmarshal(body, &anthropicResp); err != nil {
		return body
	}

	content := ""
	var toolCalls []interface{}

	if contentBlocks, ok := anthropicResp["content"].([]interface{}); ok {
		for _, block := range contentBlocks {
			b := block.(map[string]interface{})
			if b["type"] == "text" {
				content = b["text"].(string)
			} else if b["type"] == "tool_use" {
				toolCalls = append(toolCalls, map[string]interface{}{
					"id":   b["id"],
					"type": "function",
					"function": map[string]interface{}{
						"name":      b["name"],
						"arguments": b["input"],
					},
				})
			}
		}
	}

	usage, _ := anthropicResp["usage"].(map[string]interface{})
	inputTokens, _ := usage["input_tokens"].(float64)
	outputTokens, _ := usage["output_tokens"].(float64)

	openaiResp := map[string]interface{}{
		"id":      anthropicResp["id"],
		"object":  "chat.completion",
		"created": time.Now().Unix(),
		"model":   anthropicResp["model"],
		"choices": []map[string]interface{}{
			{
				"index": 0,
				"message": map[string]interface{}{
					"role":       "assistant",
					"content":    content,
					"tool_calls": toolCalls,
				},
				"finish_reason": anthropicResp["stop_reason"],
			},
		},
		"usage": map[string]interface{}{
			"prompt_tokens":     int(inputTokens),
			"completion_tokens": int(outputTokens),
			"total_tokens":      int(inputTokens + outputTokens),
		},
	}

	result, _ := json.Marshal(openaiResp)
	return result
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

	// Parse request to get model hint (for future model-specific routing)
	var req map[string]interface{}
	json.Unmarshal(body, &req)
	_ = req["model"] // Reserved for model-specific routing

	// Try backends in fallback order
	var lastErr error
	var lastBackend string

	for _, backendName := range gw.config.Fallback {
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

func (gw *Gateway) handleModels(w http.ResponseWriter, r *http.Request) {
	var models []map[string]interface{}
	for _, b := range gw.backends {
		if b.Enabled {
			models = append(models, map[string]interface{}{
				"id":       b.Model,
				"object":   "model",
				"owned_by": b.Provider,
				"backend":  b.Name,
			})
		}
	}

	resp := map[string]interface{}{
		"object": "list",
		"data":   models,
	}

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(resp)
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

			var url string
			switch b.Provider {
			case "ollama":
				url = strings.TrimRight(b.URL, "/") + "/api/tags"
			case "anthropic":
				// Skip health check for Anthropic (no list endpoint)
				gw.state.SetHealthy(name, true)
				continue
			default:
				url = strings.TrimRight(b.URL, "/")
				url = strings.TrimSuffix(url, "/v1") + "/v1/models"
			}

			ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			req, _ := http.NewRequestWithContext(ctx, "GET", url, nil)
			if b.APIKey != "" {
				req.Header.Set("Authorization", "Bearer "+b.APIKey)
			}

			resp, err := gw.client.Do(req)
			cancel()

			healthy := err == nil && resp != nil && resp.StatusCode == 200
			if resp != nil {
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
