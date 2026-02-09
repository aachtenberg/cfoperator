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
