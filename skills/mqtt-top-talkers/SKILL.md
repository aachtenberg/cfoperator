---
name: mqtt-top-talkers
description: "Rank IoT/MQTT devices by telemetry volume using one timescale_query call. Use when asked which device is emitting the most (or least) MQTT telemetry, message rates per device, chattiest sensor, or when a device last reported. Keywords: mqtt, telemetry, top talkers, loudest, chattiest, device, message rate, mosquitto, iot, esp, sensor, last seen."
---

# MQTT Top Talkers

Answer "which device is emitting the most telemetry to MQTT?" with a single
`timescale_query` **tool** call. That is a registered function in this
session (same name and contract as the CFOperator agent's), not a binary,
not a kubectl trick, and not a file on disk.

**Do not search the filesystem for it.** Never `grep -R`, `find /`, or walk
`$PATH` looking for `timescale_query`, the SQL below, or this playbook. If
`list_tools` does not show `timescale_query`, say that Timescale is not
configured on this host and stop.

Telegraf subscribes to the MQTT broker and writes one row per message into
TimescaleDB (`sensors` db), tagged by device — so message volume per device
is a `count(*)` there. Do NOT try to answer this from Prometheus (broker
`$SYS` stats are aggregates with no per-client breakdown) or Loki
(mosquitto only logs connects/disconnects).

## When to Use

- "Which device is emitting the most telemetry?"
- "What's the chattiest MQTT sensor?"
- Per-device message rates, or comparing device volumes over a window
- "When did device X last report?" (use `max(time)` instead of `count(*)`)

## What It Does

One UNION ALL across the per-measurement tables telegraf fills from MQTT,
grouped by device:

```sql
SELECT device, count(*) AS msgs,
       round(count(*) / 60.0, 1) AS msgs_per_min,
       max(time) AS last_seen
FROM (
  SELECT device, time FROM esp_temperature
  UNION ALL SELECT device, time FROM esp_weather
  UNION ALL SELECT device, time FROM esp_status
  UNION ALL SELECT device, time FROM esp_gps
  UNION ALL SELECT device, time FROM surveillance
  UNION ALL SELECT device, time FROM esphome_display
  UNION ALL SELECT device, time FROM solar_battery
  UNION ALL SELECT device, time FROM solar_mppt
  UNION ALL SELECT gateway_id AS device, time FROM lora_gateway
) t
WHERE time > now() - interval '1 hour'
GROUP BY device
ORDER BY msgs DESC;
```

Call the `timescale_query` tool with `sql` set to the statement above.
Adjust the interval to the window asked about (divide by the window's
minutes for msgs_per_min). If a column is missing (`device` vs
`device_id`), inspect `information_schema.columns` with another
`timescale_query` call — still the tool, never grep.

## Interpreting Results

- Report the top device with its share of total messages, plus the runners-up.
- A device at 2-5x its peers is usually just a faster publish interval —
  check `esp_status.sensor_interval_seconds` before calling it a fault.
- A device suddenly absent (stale `last_seen`) is often the more important
  finding than the loudest one — mention both.
- Caveat: this counts messages telegraf consumes (esp-sensor-hub, surveillance,
  esphome, solar, lora topics). A publisher on a topic telegraf does not
  subscribe to won't appear; if totals look implausibly low versus the broker's
  `$SYS` message rate in Prometheus (`mosquitto_*` metrics), say so.

## Example Output

```
Loudest MQTT device (last hour): esp32-cam-driveway — 1,842 msgs (31/min, 46% of all telemetry)
Runners-up: weather-station-01 (612), esp32-dock (420)
All devices reporting; nothing stale.
```
