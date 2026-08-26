// timescale_query: the same read-only sensors DB the CFOperator agent
// exposes, reached from the LAN (NodePort) rather than from inside the
// cluster. Registered only when host and password are set — a tool that
// can only fail is how mqtt-top-talkers ended up grepping the whole disk
// for a binary that does not exist.

package tools

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/aachtenberg/cfoperator/cfassist-go/internal/client"
	"github.com/aachtenberg/cfoperator/cfassist-go/internal/config"
	"github.com/jackc/pgx/v5"
)

const (
	timescaleConnectTimeout   = 5 * time.Second
	timescaleStatementTimeout = 15 * time.Second
	timescaleDefaultMaxRows   = 200
	timescaleMaxRowsCap       = 1000
)

// SCHEMA_CHEAT_SHEET is copied into the tool description so the model does
// not spend turns rediscovering table names. Keep in lockstep with
// tools/timescale.py.
const timescaleSchemaCheatSheet = "MQTT telemetry tables (one row per message; all have time, topic, and device or device_id/device_name): " +
	"esp_temperature(celsius, humidity, battery_percent), " +
	"esp_weather(temperature, humidity, pressure), " +
	"esp_status(uptime_seconds, wifi_rssi, free_heap, battery_voltage), " +
	"esp_gps(gps_latitude, gps_longitude, gps_satellites), " +
	"surveillance(device, ip, motion_count, capture_count, framerate, wifi_rssi), " +
	"esphome_display(device, value), lora_gateway(gateway_id, status, rssi), " +
	"solar_battery(voltage, current, soc), solar_mppt(mppt1_pv_power, mppt2_pv_power). " +
	"Example - loudest MQTT device last hour: " +
	"SELECT device, count(*) FROM (SELECT device, time FROM esp_temperature UNION ALL " +
	"SELECT device, time FROM esp_weather UNION ALL SELECT device, time FROM esp_status UNION ALL " +
	"SELECT device, time FROM surveillance UNION ALL SELECT device, time FROM esphome_display UNION ALL " +
	"SELECT device, time FROM solar_battery UNION ALL SELECT device, time FROM solar_mppt) t " +
	"WHERE time > now() - interval '1 hour' GROUP BY device ORDER BY 2 DESC; " +
	"Also present: river/flood data (wsc_readings, river_readings, dam_releases, dam_levels, " +
	"reservoir_readings, swe_daily, orrpb_river_levels) and eccc_climate_daily."

// One statement, must read like a query. Comments are stripped before this
// check so "-- comment\nSELECT ..." passes and "DELETE ... -- SELECT" fails.
var timescaleAllowedPrefix = regexp.MustCompile(`(?i)^\s*(SELECT|WITH)\b`)

var (
	timescaleLineComment  = regexp.MustCompile(`--[^\n]*`)
	timescaleBlockComment = regexp.MustCompile(`(?s)/\*.*?\*/`)
	timescaleTrailingSemi = regexp.MustCompile(`[;\s]+$`)
)

func addTimescale(r *Registry, cfg config.TimescaleConfig) {
	if !cfg.Configured() {
		return
	}
	t := newTimescaleTool(cfg)
	r.tools["timescale_query"] = tool{
		schema: client.ToolSchema{
			Type: "function",
			Function: client.ToolSchemaFunction{
				Name: "timescale_query",
				Description: "Run a read-only SQL query against the telemetry TimescaleDB " +
					"(database 'sensors') where telegraf stores every MQTT message and " +
					"the flood-monitoring history. USE THIS for questions about IoT/MQTT " +
					"device activity, message rates, last-seen times, sensor readings, or " +
					"river/dam data - one SQL query replaces many Prometheus/Loki calls. " +
					"This is a tool, not a binary: never grep or find the filesystem for it. " +
					timescaleSchemaCheatSheet,
				Parameters: map[string]any{
					"type": "object",
					"properties": map[string]any{
						"sql": map[string]any{
							"type": "string",
							"description": "A single SELECT (or WITH ... SELECT) statement. " +
								"No writes, no multiple statements. Prefer aggregates " +
								"(count, max(time), avg) over raw row dumps.",
						},
						"max_rows": map[string]any{
							"type":        "integer",
							"description": "Maximum rows to return (default 200, cap 1000)",
						},
					},
					"required": []string{"sql"},
				},
			},
		},
		execute: t.execute,
	}
}

type timescaleTool struct {
	cfg     config.TimescaleConfig
	queryFn func(ctx context.Context, sql string, maxRows int) map[string]any
}

func newTimescaleTool(cfg config.TimescaleConfig) *timescaleTool {
	t := &timescaleTool{cfg: cfg}
	t.queryFn = t.queryPostgres
	return t
}

func (t *timescaleTool) execute(ctx context.Context, args map[string]any) map[string]any {
	sql, _ := args["sql"].(string)
	cleaned, err := validateReadonlySQL(sql)
	if err != nil {
		return map[string]any{"error": err.Error()}
	}
	maxRows := clampIntArg(args, "max_rows", timescaleDefaultMaxRows, 1, timescaleMaxRowsCap)
	return t.queryFn(ctx, cleaned, maxRows)
}

func validateReadonlySQL(sql string) (string, error) {
	if strings.TrimSpace(sql) == "" {
		return "", fmt.Errorf("Empty SQL query")
	}
	cleaned := timescaleLineComment.ReplaceAllString(sql, " ")
	cleaned = timescaleBlockComment.ReplaceAllString(cleaned, " ")
	cleaned = strings.TrimSpace(cleaned)
	cleaned = timescaleTrailingSemi.ReplaceAllString(cleaned, "")
	if strings.Contains(cleaned, ";") {
		return "", fmt.Errorf("Multiple SQL statements are not allowed - send one SELECT per call")
	}
	if !timescaleAllowedPrefix.MatchString(cleaned) {
		return "", fmt.Errorf("Only SELECT (or WITH ... SELECT) queries are allowed - this tool is read-only")
	}
	return cleaned, nil
}

func (t *timescaleTool) queryPostgres(ctx context.Context, sql string, maxRows int) map[string]any {
	connCfg, err := t.connConfig()
	if err != nil {
		return map[string]any{"error": err.Error()}
	}
	connectCtx, cancel := context.WithTimeout(ctx, timescaleConnectTimeout)
	defer cancel()
	conn, err := pgx.ConnectConfig(connectCtx, connCfg)
	if err != nil {
		return map[string]any{"error": fmt.Sprintf("Query failed: %s", firstLine(err.Error()))}
	}
	defer conn.Close(context.Background())

	rows, err := conn.Query(ctx, sql)
	if err != nil {
		return map[string]any{"error": fmt.Sprintf("Query failed: %s", firstLine(err.Error()))}
	}
	defer rows.Close()

	fields := rows.FieldDescriptions()
	result := make([]map[string]any, 0, 16)
	truncated := false
	for rows.Next() {
		vals, err := rows.Values()
		if err != nil {
			return map[string]any{"error": fmt.Sprintf("Query failed: %s", firstLine(err.Error()))}
		}
		if len(result) >= maxRows {
			truncated = true
			break
		}
		row := make(map[string]any, len(fields))
		for i, fd := range fields {
			var v any
			if i < len(vals) {
				v = jsonable(vals[i])
			}
			row[fd.Name] = v
		}
		result = append(result, row)
	}
	if err := rows.Err(); err != nil {
		return map[string]any{"error": fmt.Sprintf("Query failed: %s", firstLine(err.Error()))}
	}

	out := map[string]any{
		"success":   true,
		"row_count": len(result),
		"rows":      result,
	}
	if truncated {
		out["truncated"] = true
		out["hint"] = fmt.Sprintf(
			"Result exceeded max_rows=%d; aggregate (GROUP BY / count) or add LIMIT instead of paging raw rows.",
			maxRows,
		)
	}
	return out
}

func (t *timescaleTool) connConfig() (*pgx.ConnConfig, error) {
	host := strings.TrimSpace(t.cfg.Host)
	port := t.cfg.Port
	if port <= 0 {
		port = 5432
	}
	database := t.cfg.Database
	if database == "" {
		database = "sensors"
	}
	user := t.cfg.User
	if user == "" {
		user = "cfoperator_ro"
	}

	u := &url.URL{
		Scheme: "postgres",
		User:   url.UserPassword(user, t.cfg.Password),
		Host:   net.JoinHostPort(host, strconv.Itoa(port)),
		Path:   "/" + database,
	}
	q := u.Query()
	q.Set("connect_timeout", strconv.Itoa(int(timescaleConnectTimeout.Seconds())))
	u.RawQuery = q.Encode()

	cfg, err := pgx.ParseConfig(u.String())
	if err != nil {
		return nil, fmt.Errorf("Query failed: %s", firstLine(err.Error()))
	}
	cfg.ConnectTimeout = timescaleConnectTimeout
	if cfg.RuntimeParams == nil {
		cfg.RuntimeParams = map[string]string{}
	}
	cfg.RuntimeParams["default_transaction_read_only"] = "on"
	cfg.RuntimeParams["statement_timeout"] = strconv.Itoa(int(timescaleStatementTimeout.Milliseconds()))
	return cfg, nil
}

func jsonable(v any) any {
	if v == nil {
		return nil
	}
	switch t := v.(type) {
	case time.Time:
		return t.UTC().Format(time.RFC3339Nano)
	case []byte:
		return string(t)
	}
	if _, err := json.Marshal(v); err != nil {
		return fmt.Sprint(v)
	}
	return v
}

func clampIntArg(args map[string]any, key string, def, min, max int) int {
	n := def
	if v, ok := args[key]; ok && v != nil {
		switch t := v.(type) {
		case float64:
			n = int(t)
		case int:
			n = t
		case int64:
			n = int(t)
		case json.Number:
			if parsed, err := t.Int64(); err == nil {
				n = int(parsed)
			}
		}
	}
	if n < min {
		return min
	}
	if n > max {
		return max
	}
	return n
}

func firstLine(s string) string {
	s = strings.TrimSpace(s)
	if i := strings.IndexByte(s, '\n'); i >= 0 {
		return s[:i]
	}
	return s
}
