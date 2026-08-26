package tools

import (
	"context"
	"os"
	"strings"
	"testing"

	"github.com/aachtenberg/cfoperator/cfassist-go/internal/config"
)

func TestTimescaleQueryNotRegisteredByDefault(t *testing.T) {
	r := newTestRegistry()
	result := r.Execute(context.Background(), "timescale_query", map[string]any{"sql": "SELECT 1"})
	if result["error"] != "unknown tool: timescale_query" {
		t.Fatalf("default config must not offer timescale_query: %v", result)
	}
}

func TestTimescaleQueryNotRegisteredWithoutPassword(t *testing.T) {
	cfg := config.Defaults()
	cfg.Memory.Directory = os.TempDir()
	cfg.Tools.Timescale.Host = "raspberrypi2"
	r := New(cfg)
	result := r.Execute(context.Background(), "timescale_query", map[string]any{"sql": "SELECT 1"})
	if result["error"] != "unknown tool: timescale_query" {
		t.Fatalf("host without password must not register the tool: %v", result)
	}
}

func TestTimescaleQueryRegisteredWhenConfigured(t *testing.T) {
	cfg := config.Defaults()
	cfg.Memory.Directory = os.TempDir()
	cfg.Tools.Timescale.Host = "example.invalid"
	cfg.Tools.Timescale.Password = "x"
	r := New(cfg)

	var desc string
	for _, s := range r.GetSchemas() {
		if s.Function.Name == "timescale_query" {
			desc = s.Function.Description
		}
	}
	if desc == "" {
		t.Fatal("timescale_query missing from schemas")
	}
	if !strings.Contains(desc, "not a binary") {
		t.Error("description must tell the model not to grep for a binary")
	}
	if !strings.Contains(desc, "esp_temperature") {
		t.Error("description must carry the schema cheat sheet")
	}
}

func TestValidateReadonlySQL(t *testing.T) {
	ok := func(sql, want string) {
		t.Helper()
		got, err := validateReadonlySQL(sql)
		if err != nil {
			t.Fatalf("validateReadonlySQL(%q) unexpected error: %v", sql, err)
		}
		if got != want {
			t.Fatalf("validateReadonlySQL(%q) = %q, want %q", sql, got, want)
		}
	}
	reject := func(sql, substr string) {
		t.Helper()
		_, err := validateReadonlySQL(sql)
		if err == nil {
			t.Fatalf("validateReadonlySQL(%q) should have failed", sql)
		}
		if !strings.Contains(err.Error(), substr) {
			t.Fatalf("validateReadonlySQL(%q) error %q, want substring %q", sql, err, substr)
		}
	}

	ok("SELECT 1", "SELECT 1")
	ok("WITH t AS (SELECT device, time FROM esp_status) SELECT device, count(*) FROM t GROUP BY device",
		"WITH t AS (SELECT device, time FROM esp_status) SELECT device, count(*) FROM t GROUP BY device")
	ok("  \n select * from esp_temperature limit 5 ", "select * from esp_temperature limit 5")
	ok("SELECT 1;", "SELECT 1")
	ok("SELECT 1;; ;", "SELECT 1")
	ok("-- top talkers\nSELECT 1", "SELECT 1")

	for _, sql := range []string{
		"DELETE FROM esp_status",
		"INSERT INTO esp_status (device) VALUES ('x')",
		"UPDATE esp_status SET device = 'x'",
		"DROP TABLE esp_status",
		"TRUNCATE esp_status",
		"CREATE TABLE pwned (id int)",
		"GRANT ALL ON esp_status TO public",
		"EXPLAIN ANALYZE DELETE FROM esp_status",
		"VACUUM esp_status",
		"SET default_transaction_read_only = off",
		"CALL some_proc()",
	} {
		reject(sql, "read-only")
	}

	reject("SELECT 1; DELETE FROM esp_status", "one SELECT")
	reject("SELECT 1; SELECT 2", "one SELECT")
	reject("/* SELECT */ DELETE FROM esp_status", "read-only")
	reject("-- SELECT 1\nDROP TABLE esp_status", "read-only")
	reject("SELECT 1 /* x */; DELETE FROM esp_status", "one SELECT")
	reject("", "Empty")
	reject("   ", "Empty")
}

func TestTimescaleQueryRejectsWritesWithoutConnecting(t *testing.T) {
	cfg := config.Defaults()
	cfg.Memory.Directory = os.TempDir()
	cfg.Tools.Timescale.Host = "example.invalid"
	cfg.Tools.Timescale.Password = "x"
	r := New(cfg)

	result := r.Execute(context.Background(), "timescale_query", map[string]any{
		"sql": "DELETE FROM esp_status",
	})
	errMsg, _ := result["error"].(string)
	if !strings.Contains(errMsg, "read-only") {
		t.Fatalf("write must fail at the SQL gate, not at connect: %v", result)
	}
}

func TestTimescaleQueryUsesInjectedRunner(t *testing.T) {
	ttool := newTimescaleTool(config.TimescaleConfig{Host: "h", Password: "p"})
	called := false
	ttool.queryFn = func(ctx context.Context, sql string, maxRows int) map[string]any {
		called = true
		if sql != "SELECT 1" {
			t.Errorf("sql = %q", sql)
		}
		if maxRows != 10 {
			t.Errorf("maxRows = %d", maxRows)
		}
		return map[string]any{"success": true, "row_count": 1, "rows": []map[string]any{{"?column?": 1}}}
	}
	result := ttool.execute(context.Background(), map[string]any{
		"sql":      "SELECT 1;",
		"max_rows": float64(10),
	})
	if !called {
		t.Fatal("queryFn was not called")
	}
	if result["success"] != true {
		t.Fatalf("result = %v", result)
	}
}

func TestClampIntArg(t *testing.T) {
	if n := clampIntArg(nil, "max_rows", 200, 1, 1000); n != 200 {
		t.Errorf("default = %d", n)
	}
	if n := clampIntArg(map[string]any{"max_rows": float64(0)}, "max_rows", 200, 1, 1000); n != 1 {
		t.Errorf("zero clamps to min, got %d", n)
	}
	if n := clampIntArg(map[string]any{"max_rows": float64(9999)}, "max_rows", 200, 1, 1000); n != 1000 {
		t.Errorf("over cap = %d", n)
	}
}
