package memory

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"time"

	"github.com/aachtenberg/cfoperator/cfassist-go/internal/client"
)

// SaveConversation writes messages to a timestamped JSONL file.
func SaveConversation(memoryDir string, messages []client.Message) (string, error) {
	if len(messages) == 0 {
		return "", nil
	}

	if err := os.MkdirAll(memoryDir, 0755); err != nil {
		return "", err
	}

	// Build filename from timestamp + slug of first user message
	ts := time.Now().Format("2006-01-02T15-04-05")
	slug := ""
	for _, m := range messages {
		if m.Role == "user" {
			slug = slugify(m.Content, 40)
			break
		}
	}
	if slug == "" {
		slug = "conversation"
	}

	filename := fmt.Sprintf("%s_%s.jsonl", ts, slug)
	path := filepath.Join(memoryDir, filename)

	f, err := os.Create(path)
	if err != nil {
		return "", err
	}
	defer f.Close()

	enc := json.NewEncoder(f)
	for _, m := range messages {
		if err := enc.Encode(m); err != nil {
			return path, err
		}
	}
	return path, nil
}

// LoadConversation reads messages from a JSONL file.
func LoadConversation(filepath string) ([]client.Message, error) {
	f, err := os.Open(filepath)
	if err != nil {
		return nil, err
	}
	defer f.Close()

	var messages []client.Message
	scanner := bufio.NewScanner(f)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" {
			continue
		}
		var m client.Message
		if err := json.Unmarshal([]byte(line), &m); err != nil {
			continue
		}
		messages = append(messages, m)
	}
	return messages, scanner.Err()
}

// Cleanup removes oldest conversations if over the limit.
func Cleanup(memoryDir string, maxConversations int) error {
	matches, err := filepath.Glob(filepath.Join(memoryDir, "*.jsonl"))
	if err != nil {
		return err
	}

	if len(matches) <= maxConversations {
		return nil
	}

	sort.Strings(matches)
	toDelete := len(matches) - maxConversations
	for i := 0; i < toDelete; i++ {
		os.Remove(matches[i])
	}
	return nil
}

// SearchResult holds a matching conversation snippet.
type SearchResult struct {
	File      string `json:"file"`
	Timestamp string `json:"timestamp"`
	Role      string `json:"role"`
	Content   string `json:"content"`
}

// SearchConversations searches all saved conversations for a keyword.
// Returns matching messages with surrounding context.
func SearchConversations(memoryDir string, query string, maxResults int) []SearchResult {
	if maxResults <= 0 {
		maxResults = 20
	}

	matches, err := filepath.Glob(filepath.Join(memoryDir, "*.jsonl"))
	if err != nil || len(matches) == 0 {
		return nil
	}

	// Search newest first
	sort.Sort(sort.Reverse(sort.StringSlice(matches)))

	queryLower := strings.ToLower(query)
	var results []SearchResult

	for _, path := range matches {
		if len(results) >= maxResults {
			break
		}

		messages, err := LoadConversation(path)
		if err != nil {
			continue
		}

		// Extract timestamp from filename: 2006-01-02T15-04-05_slug.jsonl
		base := filepath.Base(path)
		ts := ""
		if idx := strings.Index(base, "_"); idx > 0 {
			ts = base[:idx]
		}

		for _, m := range messages {
			if m.Role == "system" || m.Role == "tool" {
				continue
			}
			if strings.Contains(strings.ToLower(m.Content), queryLower) {
				content := m.Content
				if len(content) > 300 {
					// Find the match position and show surrounding context
					pos := strings.Index(strings.ToLower(content), queryLower)
					start := pos - 100
					if start < 0 {
						start = 0
					}
					end := pos + len(query) + 200
					if end > len(content) {
						end = len(content)
					}
					content = "..." + content[start:end] + "..."
				}

				results = append(results, SearchResult{
					File:      base,
					Timestamp: ts,
					Role:      m.Role,
					Content:   content,
				})

				if len(results) >= maxResults {
					break
				}
			}
		}
	}

	return results
}

var nonWordRe = regexp.MustCompile(`[^\w\s-]`)
var whitespaceRe = regexp.MustCompile(`[\s_]+`)

func slugify(text string, maxLen int) string {
	s := nonWordRe.ReplaceAllString(text, "")
	s = strings.ToLower(s)
	s = whitespaceRe.ReplaceAllString(s, "-")
	if len(s) > maxLen {
		s = s[:maxLen]
	}
	s = strings.TrimRight(s, "-")
	return s
}
