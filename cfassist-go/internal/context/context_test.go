package context

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestLoadDirectoryEmpty(t *testing.T) {
	dir := t.TempDir()

	text, count := LoadDirectory(dir, 32000)
	if text != "" {
		t.Errorf("expected empty text for empty dir, got %q", text)
	}
	if count != 0 {
		t.Errorf("expected 0 files, got %d", count)
	}
}

func TestLoadDirectoryNonexistent(t *testing.T) {
	text, count := LoadDirectory("/tmp/nonexistent-cfassist-ctx-12345", 32000)
	if text != "" || count != 0 {
		t.Error("nonexistent dir should return empty")
	}
}

func TestLoadDirectorySingleFile(t *testing.T) {
	dir := t.TempDir()
	os.WriteFile(filepath.Join(dir, "infra.md"), []byte("# My Infrastructure\nHost: pi3"), 0644)

	text, count := LoadDirectory(dir, 32000)
	if count != 1 {
		t.Errorf("expected 1 file, got %d", count)
	}
	if !strings.Contains(text, "infra.md") {
		t.Error("output should contain filename")
	}
	if !strings.Contains(text, "My Infrastructure") {
		t.Error("output should contain file content")
	}
}

func TestLoadDirectoryMultipleFiles(t *testing.T) {
	dir := t.TempDir()
	os.WriteFile(filepath.Join(dir, "a.txt"), []byte("File A content"), 0644)
	os.WriteFile(filepath.Join(dir, "b.yaml"), []byte("key: value"), 0644)
	os.WriteFile(filepath.Join(dir, "c.json"), []byte(`{"test": true}`), 0644)

	text, count := LoadDirectory(dir, 32000)
	if count != 3 {
		t.Errorf("expected 3 files, got %d", count)
	}
	if !strings.Contains(text, "a.txt") {
		t.Error("should contain a.txt")
	}
	if !strings.Contains(text, "b.yaml") {
		t.Error("should contain b.yaml")
	}
	if !strings.Contains(text, "c.json") {
		t.Error("should contain c.json")
	}
}

func TestLoadDirectoryIgnoresUnsupported(t *testing.T) {
	dir := t.TempDir()
	os.WriteFile(filepath.Join(dir, "script.py"), []byte("print('hi')"), 0644)
	os.WriteFile(filepath.Join(dir, "binary.exe"), []byte{0, 1, 2}, 0644)
	os.WriteFile(filepath.Join(dir, "notes.md"), []byte("# Notes"), 0644)

	text, count := LoadDirectory(dir, 32000)
	if count != 1 {
		t.Errorf("expected 1 file (only .md), got %d", count)
	}
	if !strings.Contains(text, "notes.md") {
		t.Error("should contain notes.md")
	}
	if strings.Contains(text, "script.py") {
		t.Error("should NOT contain script.py")
	}
}

func TestLoadDirectoryTruncation(t *testing.T) {
	dir := t.TempDir()

	// Create a large file
	bigContent := strings.Repeat("x", 5000)
	os.WriteFile(filepath.Join(dir, "big.txt"), []byte(bigContent), 0644)
	os.WriteFile(filepath.Join(dir, "small.txt"), []byte("small"), 0644)

	// Set maxChars to 1000 — should truncate big file
	text, count := LoadDirectory(dir, 1000)
	if count == 0 {
		t.Error("should load at least 1 file")
	}
	if len(text) > 1200 { // some overhead for headers
		t.Errorf("text should be around maxChars, got %d", len(text))
	}
}

func TestLoadDirectoryMaxCharsDefault(t *testing.T) {
	dir := t.TempDir()
	os.WriteFile(filepath.Join(dir, "test.md"), []byte("content"), 0644)

	// maxChars <= 0 should use 32000 default
	_, count := LoadDirectory(dir, 0)
	if count != 1 {
		t.Errorf("expected 1 file, got %d", count)
	}
}

func TestLoadDirectorySubdirectories(t *testing.T) {
	dir := t.TempDir()
	subdir := filepath.Join(dir, "sub")
	os.MkdirAll(subdir, 0755)
	os.WriteFile(filepath.Join(subdir, "nested.txt"), []byte("nested content"), 0644)
	os.WriteFile(filepath.Join(dir, "root.txt"), []byte("root content"), 0644)

	text, count := LoadDirectory(dir, 32000)
	if count != 2 {
		t.Errorf("expected 2 files (recursive), got %d", count)
	}
	if !strings.Contains(text, "nested.txt") {
		t.Error("should contain nested.txt via Walk")
	}
}

func TestSupportedExtensions(t *testing.T) {
	exts := []string{".md", ".txt", ".yaml", ".yml", ".csv", ".json", ".conf", ".cfg", ".ini", ".toml"}
	for _, ext := range exts {
		if !supportedExtensions[ext] {
			t.Errorf("extension %s should be supported", ext)
		}
	}

	unsupported := []string{".go", ".py", ".js", ".exe", ".bin"}
	for _, ext := range unsupported {
		if supportedExtensions[ext] {
			t.Errorf("extension %s should NOT be supported", ext)
		}
	}
}
