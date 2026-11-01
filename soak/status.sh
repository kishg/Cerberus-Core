#!/bin/bash
# Health snapshot for the 24h soak run.
# Usage: ./soak/status.sh
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="$HERE/cerberus.pid"
LOG_FILE="$HERE/cerberus.log"
DB="$HERE/data/cerberus.db"
PY="$HERE/../.venv/bin/python"

echo "=== Cerberus soak status @ $(date '+%Y-%m-%d %H:%M:%S') ==="

if [[ -f "$PID_FILE" ]]; then
  PID=$(cat "$PID_FILE")
  if ps -p "$PID" >/dev/null 2>&1; then
    ETIME=$(ps -o etime= -p "$PID" | tr -d ' ')
    RSS=$(ps -o rss= -p "$PID" | tr -d ' ')
    echo "process: alive (pid=$PID, uptime=$ETIME, rss=${RSS}KB)"
  else
    echo "process: DEAD (stale pid file: $PID)"
  fi
else
  echo "process: pid file missing"
fi

echo
echo "--- log tail (last 5 lines) ---"
tail -5 "$LOG_FILE" 2>/dev/null || echo "(no log yet)"

if [[ -f "$DB" ]]; then
  echo
  echo "--- db ($DB) ---"
  "$PY" - <<PY
import sqlite3, datetime
c = sqlite3.connect("$DB")
samples = c.execute("SELECT COUNT(*), MIN(ts), MAX(ts) FROM battery_samples").fetchone()
events = c.execute("SELECT COUNT(*) FROM events").fetchone()[0]
print(f"samples: {samples[0]}  span: {datetime.datetime.fromtimestamp(samples[1]).strftime('%H:%M:%S') if samples[1] else '-'} -> {datetime.datetime.fromtimestamp(samples[2]).strftime('%H:%M:%S') if samples[2] else '-'}")
print(f"events:  {events}")
latest = list(c.execute("SELECT ts, type, severity, detail FROM events ORDER BY ts DESC LIMIT 5"))
if latest:
    print("recent events:")
    for ts, t, sev, det in latest:
        print(f"  {datetime.datetime.fromtimestamp(ts).strftime('%H:%M:%S')} {sev:5s} {t:30s} {det[:120]}")
PY
fi

echo
echo "--- counts in log (grepped) ---"
echo "  Cerbo online toggles: $(grep -c 'Cerbo online\|Cerbo offline' "$LOG_FILE" 2>/dev/null || echo 0)"
echo "  reconnects:           $(grep -c 'connected; subscribing\|will auto-reconnect' "$LOG_FILE" 2>/dev/null || echo 0)"
echo "  event-emit lines:     $(grep -c 'event: ' "$LOG_FILE" 2>/dev/null || echo 0)"
echo "  ERROR / Exception:    $(grep -cE 'ERROR|Exception' "$LOG_FILE" 2>/dev/null || echo 0)"
