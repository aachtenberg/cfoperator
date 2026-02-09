package context

import (
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

var supportedExtensions = map[string]bool{
	".md": true, ".txt": true, ".yaml": true, ".yml": true,
	".csv": true, ".json": true, ".conf": true, ".cfg": true,
	".ini": true, ".toml": true,
}

// LoadDirectory reads all context files from a directory.
// Returns the combined text and file count.
func LoadDirectory(directory string, maxChars int) (string, int) {
	if maxChars <= 0 {
		maxChars = 32000
	}

	info, err := os.Stat(directory)
	if err != nil || !info.IsDir() {
		return "", 0
	}

	// Find all supported files
	var files []string
	filepath.Walk(directory, func(path string, info os.FileInfo, err error) error {
		if err != nil || info.IsDir() {
			return nil
		}
		ext := strings.ToLower(filepath.Ext(path))
		if supportedExtensions[ext] {
			files = append(files, path)
		}
		return nil
	})

	sort.Strings(files)

	var parts []string
	charCount := 0
	fileCount := 0

	for _, f := range files {
		data, err := os.ReadFile(f)
		if err != nil {
			continue
		}

		relPath, _ := filepath.Rel(directory, f)
		header := fmt.Sprintf("--- File: %s ---", relPath)
		content := string(data)
		entry := header + "\n" + content + "\n"

		if charCount+len(entry) > maxChars {
			// Try to fit a truncated version
			remaining := maxChars - charCount - len(header) - 20 // room for header + marker
			if remaining > 100 {
				entry = header + "\n" + content[:remaining] + "\n[truncated]\n"
				parts = append(parts, entry)
				fileCount++
			}
			break
		}

		parts = append(parts, entry)
		charCount += len(entry)
		fileCount++
	}

	return strings.Join(parts, "\n"), fileCount
}
