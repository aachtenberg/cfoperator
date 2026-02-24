// Provider interface and implementations for LLM Gateway
// Add new providers by implementing the Provider interface and registering them

package main

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"time"
)

// ============================================================================
// Provider Interface
// ============================================================================

// Provider defines the interface for LLM backend providers.
// Implement this interface to add support for new LLM providers.
type Provider interface {
	// Name returns the provider identifier (e.g., "ollama", "anthropic", "openai")
	Name() string

	// BuildChatRequest transforms an OpenAI-format request into the provider's native format.
	// Returns the URL, request body, and any additional headers needed.
	BuildChatRequest(backend *Backend, model string, openaiReq map[string]interface{}) (url string, body []byte, headers map[string]string, err error)

	// TransformResponse converts the provider's response to OpenAI format.
	// If the provider already returns OpenAI format, this can return the body unchanged.
	TransformResponse(body []byte) []byte

	// BuildOllamaChatRequest transforms an Ollama-format request for this provider.
	// Returns the URL, request body, and any additional headers needed.
	// If useNative is true, the provider should use its native format (for Ollama backends).
	BuildOllamaChatRequest(backend *Backend, model string, ollamaReq map[string]interface{}) (url string, body []byte, headers map[string]string, useNative bool, err error)

	// TransformToOllamaResponse converts the provider's response to Ollama format.
	// Used when responding to /api/chat requests.
	TransformToOllamaResponse(body []byte, model string) []byte

	// HealthCheckURL returns the URL for health checks. Empty string means skip health checks.
	HealthCheckURL(backend *Backend) string

	// SupportsModelDiscovery returns true if this provider supports dynamic model discovery
	SupportsModelDiscovery() bool

	// ParseModelsResponse parses the health check response to extract available models.
	// Only called if SupportsModelDiscovery returns true.
	ParseModelsResponse(backendName string, body []byte) []DiscoveredModel

	// StaticModels returns statically configured models for this provider (e.g., Groq's known models).
	// Used when the provider doesn't support dynamic discovery.
	StaticModels() []string
}

// ============================================================================
// Provider Registry
// ============================================================================

var providerRegistry = make(map[string]Provider)

// RegisterProvider adds a provider to the registry
func RegisterProvider(p Provider) {
	providerRegistry[p.Name()] = p
}

// GetProvider returns the provider for the given name, or nil if not found
func GetProvider(name string) Provider {
	return providerRegistry[name]
}

// GetAllProviders returns all registered providers
func GetAllProviders() map[string]Provider {
	return providerRegistry
}

func init() {
	// Register built-in providers
	RegisterProvider(&OllamaProvider{})
	RegisterProvider(&AnthropicProvider{})
	RegisterProvider(&OpenAIProvider{})
	RegisterProvider(&GroqProvider{})
}

// ============================================================================
// Ollama Provider
// ============================================================================

type OllamaProvider struct{}

func (p *OllamaProvider) Name() string {
	return "ollama"
}

func (p *OllamaProvider) BuildChatRequest(backend *Backend, model string, openaiReq map[string]interface{}) (string, []byte, map[string]string, error) {
	url := strings.TrimRight(backend.URL, "/") + "/api/chat"

	// Transform OpenAI format to Ollama format
	ollamaReq := map[string]interface{}{
		"model":    model,
		"messages": openaiReq["messages"],
		"stream":   false,
	}
	if tools, ok := openaiReq["tools"]; ok {
		ollamaReq["tools"] = tools
	}
	if temp, ok := openaiReq["temperature"]; ok {
		ollamaReq["temperature"] = temp
	}

	body, err := json.Marshal(ollamaReq)
	return url, body, nil, err
}

func (p *OllamaProvider) TransformResponse(body []byte) []byte {
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
				pt, _ := ollamaResp["prompt_eval_count"].(float64)
				ct, _ := ollamaResp["eval_count"].(float64)
				return int(pt + ct)
			}(),
		},
	}

	result, _ := json.Marshal(openaiResp)
	return result
}

func (p *OllamaProvider) BuildOllamaChatRequest(backend *Backend, model string, ollamaReq map[string]interface{}) (string, []byte, map[string]string, bool, error) {
	url := strings.TrimRight(backend.URL, "/") + "/api/chat"
	ollamaReq["model"] = model
	ollamaReq["stream"] = false
	body, err := json.Marshal(ollamaReq)
	return url, body, nil, true, err // useNative=true for Ollama
}

func (p *OllamaProvider) TransformToOllamaResponse(body []byte, model string) []byte {
	// Already in Ollama format
	return body
}

func (p *OllamaProvider) HealthCheckURL(backend *Backend) string {
	return strings.TrimRight(backend.URL, "/") + "/api/tags"
}

func (p *OllamaProvider) SupportsModelDiscovery() bool {
	return true
}

func (p *OllamaProvider) ParseModelsResponse(backendName string, body []byte) []DiscoveredModel {
	var tagsResp struct {
		Models []struct {
			Name       string                 `json:"name"`
			ModifiedAt string                 `json:"modified_at"`
			Size       int64                  `json:"size"`
			Digest     string                 `json:"digest"`
			Details    map[string]interface{} `json:"details"`
		} `json:"models"`
	}

	if err := json.Unmarshal(body, &tagsResp); err != nil {
		return nil
	}

	var models []DiscoveredModel
	for _, m := range tagsResp.Models {
		models = append(models, DiscoveredModel{
			Name:       m.Name,
			Backend:    backendName,
			Provider:   "ollama",
			Size:       m.Size,
			ModifiedAt: m.ModifiedAt,
			Details:    m.Details,
		})
	}
	return models
}

func (p *OllamaProvider) StaticModels() []string {
	return nil // Dynamic discovery only
}

// ============================================================================
// Anthropic Provider
// ============================================================================

type AnthropicProvider struct{}

func (p *AnthropicProvider) Name() string {
	return "anthropic"
}

func (p *AnthropicProvider) BuildChatRequest(backend *Backend, model string, openaiReq map[string]interface{}) (string, []byte, map[string]string, error) {
	url := strings.TrimRight(backend.URL, "/") + "/v1/messages"

	// Transform OpenAI format to Anthropic format
	messages := openaiReq["messages"].([]interface{})
	var systemPrompt string
	var anthropicMsgs []map[string]interface{}

	for _, m := range messages {
		msg := m.(map[string]interface{})
		if msg["role"] == "system" {
			systemPrompt, _ = msg["content"].(string)
		} else {
			anthropicMsgs = append(anthropicMsgs, map[string]interface{}{
				"role":    msg["role"],
				"content": msg["content"],
			})
		}
	}

	anthropicReq := map[string]interface{}{
		"model":      model,
		"max_tokens": 4096,
		"messages":   anthropicMsgs,
	}
	if systemPrompt != "" {
		anthropicReq["system"] = systemPrompt
	}

	// Transform tools to Anthropic format
	if tools, ok := openaiReq["tools"]; ok {
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

	headers := map[string]string{
		"x-api-key":         backend.APIKey,
		"anthropic-version": "2023-06-01",
	}

	body, err := json.Marshal(anthropicReq)
	return url, body, headers, err
}

func (p *AnthropicProvider) TransformResponse(body []byte) []byte {
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
				content, _ = b["text"].(string)
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

func (p *AnthropicProvider) BuildOllamaChatRequest(backend *Backend, model string, ollamaReq map[string]interface{}) (string, []byte, map[string]string, bool, error) {
	// Convert Ollama format to OpenAI format first, then use BuildChatRequest
	openaiReq := map[string]interface{}{
		"model":    model,
		"messages": ollamaReq["messages"],
	}
	if tools, ok := ollamaReq["tools"]; ok {
		openaiReq["tools"] = tools
	}
	url, body, headers, err := p.BuildChatRequest(backend, model, openaiReq)
	return url, body, headers, false, err // useNative=false, need response transformation
}

func (p *AnthropicProvider) TransformToOllamaResponse(body []byte, model string) []byte {
	// First transform to OpenAI format, then to Ollama format
	openaiBody := p.TransformResponse(body)

	var openaiResp map[string]interface{}
	if err := json.Unmarshal(openaiBody, &openaiResp); err != nil {
		return body
	}

	return transformOpenAIToOllama(openaiResp, model)
}

func (p *AnthropicProvider) HealthCheckURL(backend *Backend) string {
	return "" // Anthropic doesn't have a models list endpoint
}

func (p *AnthropicProvider) SupportsModelDiscovery() bool {
	return false
}

func (p *AnthropicProvider) ParseModelsResponse(backendName string, body []byte) []DiscoveredModel {
	return nil
}

func (p *AnthropicProvider) StaticModels() []string {
	return []string{
		"claude-3-5-sonnet-20241022",
		"claude-3-5-haiku-20241022",
		"claude-3-opus-20240229",
		"claude-3-sonnet-20240229",
		"claude-3-haiku-20240307",
	}
}

// ============================================================================
// OpenAI Provider (for OpenAI API and compatible endpoints)
// ============================================================================

type OpenAIProvider struct{}

func (p *OpenAIProvider) Name() string {
	return "openai"
}

func (p *OpenAIProvider) BuildChatRequest(backend *Backend, model string, openaiReq map[string]interface{}) (string, []byte, map[string]string, error) {
	baseURL := strings.TrimRight(backend.URL, "/")
	url := strings.TrimSuffix(baseURL, "/v1") + "/v1/chat/completions"

	openaiReq["model"] = model

	headers := map[string]string{}
	if backend.APIKey != "" {
		headers["Authorization"] = "Bearer " + backend.APIKey
	}

	body, err := json.Marshal(openaiReq)
	return url, body, headers, err
}

func (p *OpenAIProvider) TransformResponse(body []byte) []byte {
	// Already in OpenAI format
	return body
}

func (p *OpenAIProvider) BuildOllamaChatRequest(backend *Backend, model string, ollamaReq map[string]interface{}) (string, []byte, map[string]string, bool, error) {
	// Convert Ollama format to OpenAI format
	openaiReq := map[string]interface{}{
		"model":    model,
		"messages": ollamaReq["messages"],
	}
	if tools, ok := ollamaReq["tools"]; ok {
		openaiReq["tools"] = tools
	}
	url, body, headers, err := p.BuildChatRequest(backend, model, openaiReq)
	return url, body, headers, false, err
}

func (p *OpenAIProvider) TransformToOllamaResponse(body []byte, model string) []byte {
	var openaiResp map[string]interface{}
	if err := json.Unmarshal(body, &openaiResp); err != nil {
		return body
	}
	return transformOpenAIToOllama(openaiResp, model)
}

func (p *OpenAIProvider) HealthCheckURL(backend *Backend) string {
	baseURL := strings.TrimRight(backend.URL, "/")
	return strings.TrimSuffix(baseURL, "/v1") + "/v1/models"
}

func (p *OpenAIProvider) SupportsModelDiscovery() bool {
	return true
}

func (p *OpenAIProvider) ParseModelsResponse(backendName string, body []byte) []DiscoveredModel {
	var resp struct {
		Data []struct {
			ID      string `json:"id"`
			Object  string `json:"object"`
			OwnedBy string `json:"owned_by"`
		} `json:"data"`
	}

	if err := json.Unmarshal(body, &resp); err != nil {
		return nil
	}

	var models []DiscoveredModel
	for _, m := range resp.Data {
		models = append(models, DiscoveredModel{
			Name:     m.ID,
			Backend:  backendName,
			Provider: "openai",
		})
	}
	return models
}

func (p *OpenAIProvider) StaticModels() []string {
	return []string{
		"gpt-4o",
		"gpt-4o-mini",
		"gpt-4-turbo",
		"gpt-4",
		"gpt-3.5-turbo",
	}
}

// ============================================================================
// Groq Provider (OpenAI-compatible but with different URL structure)
// ============================================================================

type GroqProvider struct{}

func (p *GroqProvider) Name() string {
	return "groq"
}

func (p *GroqProvider) BuildChatRequest(backend *Backend, model string, openaiReq map[string]interface{}) (string, []byte, map[string]string, error) {
	baseURL := strings.TrimRight(backend.URL, "/")
	url := baseURL + "/openai/v1/chat/completions"

	openaiReq["model"] = model

	headers := map[string]string{}
	if backend.APIKey != "" {
		headers["Authorization"] = "Bearer " + backend.APIKey
	}

	body, err := json.Marshal(openaiReq)
	return url, body, headers, err
}

func (p *GroqProvider) TransformResponse(body []byte) []byte {
	// Already in OpenAI format
	return body
}

func (p *GroqProvider) BuildOllamaChatRequest(backend *Backend, model string, ollamaReq map[string]interface{}) (string, []byte, map[string]string, bool, error) {
	// Convert Ollama format to OpenAI format
	openaiReq := map[string]interface{}{
		"model":    model,
		"messages": ollamaReq["messages"],
	}
	if tools, ok := ollamaReq["tools"]; ok {
		openaiReq["tools"] = tools
	}
	url, body, headers, err := p.BuildChatRequest(backend, model, openaiReq)
	return url, body, headers, false, err
}

func (p *GroqProvider) TransformToOllamaResponse(body []byte, model string) []byte {
	var openaiResp map[string]interface{}
	if err := json.Unmarshal(body, &openaiResp); err != nil {
		return body
	}
	return transformOpenAIToOllama(openaiResp, model)
}

func (p *GroqProvider) HealthCheckURL(backend *Backend) string {
	baseURL := strings.TrimRight(backend.URL, "/")
	return baseURL + "/openai/v1/models"
}

func (p *GroqProvider) SupportsModelDiscovery() bool {
	return true
}

func (p *GroqProvider) ParseModelsResponse(backendName string, body []byte) []DiscoveredModel {
	var resp struct {
		Data []struct {
			ID      string `json:"id"`
			Object  string `json:"object"`
			OwnedBy string `json:"owned_by"`
		} `json:"data"`
	}

	if err := json.Unmarshal(body, &resp); err != nil {
		return nil
	}

	var models []DiscoveredModel
	for _, m := range resp.Data {
		models = append(models, DiscoveredModel{
			Name:     m.ID,
			Backend:  backendName,
			Provider: "groq",
		})
	}
	return models
}

func (p *GroqProvider) StaticModels() []string {
	return []string{
		"llama-3.3-70b-versatile",
		"llama-3.1-8b-instant",
		"llama3-70b-8192",
		"llama3-8b-8192",
		"mixtral-8x7b-32768",
		"gemma2-9b-it",
	}
}

// ============================================================================
// Helper Functions
// ============================================================================

// transformOpenAIToOllama converts OpenAI response format to Ollama format
func transformOpenAIToOllama(openaiResp map[string]interface{}, model string) []byte {
	choices, _ := openaiResp["choices"].([]interface{})
	if len(choices) == 0 {
		return nil
	}

	choice := choices[0].(map[string]interface{})
	message, _ := choice["message"].(map[string]interface{})
	content, _ := message["content"].(string)
	toolCalls, _ := message["tool_calls"].([]interface{})

	ollamaResp := map[string]interface{}{
		"model":              model,
		"created_at":         time.Now().UTC().Format(time.RFC3339Nano),
		"done":               true,
		"done_reason":        "stop",
		"message": map[string]interface{}{
			"role":    "assistant",
			"content": content,
		},
	}

	// Add tool calls if present
	if len(toolCalls) > 0 {
		ollamaResp["message"].(map[string]interface{})["tool_calls"] = toolCalls
	}

	// Add token counts if available
	if usage, ok := openaiResp["usage"].(map[string]interface{}); ok {
		if pt, ok := usage["prompt_tokens"].(float64); ok {
			ollamaResp["prompt_eval_count"] = int(pt)
		}
		if ct, ok := usage["completion_tokens"].(float64); ok {
			ollamaResp["eval_count"] = int(ct)
		}
	}

	result, _ := json.Marshal(ollamaResp)
	return result
}

// ProxyRequestWithProvider uses the provider interface to make the request
func (gw *Gateway) ProxyRequestWithProvider(backend *Backend, body []byte) ([]byte, int, error) {
	provider := GetProvider(backend.Provider)
	if provider == nil {
		// Fall back to openai provider for unknown providers
		provider = GetProvider("openai")
	}

	// Parse request to get model if specified
	var openaiReq map[string]interface{}
	json.Unmarshal(body, &openaiReq)

	// Use backend model, or fall back to request model
	model := backend.Model
	if model == "" {
		if reqModel, ok := openaiReq["model"].(string); ok && reqModel != "" {
			model = reqModel
		} else {
			// Use first discovered model for this backend
			for _, m := range gw.state.GetAllModels() {
				if m.Backend == backend.Name {
					model = m.Name
					break
				}
			}
		}
	}

	url, reqBody, headers, err := provider.BuildChatRequest(backend, model, openaiReq)
	if err != nil {
		return nil, 0, err
	}

	req, err := http.NewRequest("POST", url, bytes.NewReader(reqBody))
	if err != nil {
		return nil, 0, err
	}

	req.Header.Set("Content-Type", "application/json")
	for k, v := range headers {
		req.Header.Set(k, v)
	}

	resp, err := gw.client.Do(req)
	if err != nil {
		return nil, 0, err
	}
	defer resp.Body.Close()

	respBody, err := readAll(resp.Body)
	if err != nil {
		return nil, resp.StatusCode, err
	}

	// Transform response to OpenAI format
	respBody = provider.TransformResponse(respBody)

	return respBody, resp.StatusCode, nil
}

// ProxyOllamaRequestWithProvider handles native Ollama /api/chat requests
func (gw *Gateway) ProxyOllamaRequestWithProvider(backend *Backend, model string, ollamaReq map[string]interface{}) ([]byte, int, error) {
	provider := GetProvider(backend.Provider)
	if provider == nil {
		provider = GetProvider("openai")
	}

	url, reqBody, headers, useNative, err := provider.BuildOllamaChatRequest(backend, model, ollamaReq)
	if err != nil {
		return nil, 0, err
	}

	req, err := http.NewRequest("POST", url, bytes.NewReader(reqBody))
	if err != nil {
		return nil, 0, err
	}

	req.Header.Set("Content-Type", "application/json")
	for k, v := range headers {
		req.Header.Set(k, v)
	}

	resp, err := gw.client.Do(req)
	if err != nil {
		return nil, 0, err
	}
	defer resp.Body.Close()

	respBody, err := readAll(resp.Body)
	if err != nil {
		return nil, resp.StatusCode, err
	}

	// Transform response to Ollama format if needed
	if !useNative {
		respBody = provider.TransformToOllamaResponse(respBody, model)
	}

	return respBody, resp.StatusCode, nil
}

// readAll is a helper to read response body
func readAll(r io.Reader) ([]byte, error) {
	return io.ReadAll(r)
}
