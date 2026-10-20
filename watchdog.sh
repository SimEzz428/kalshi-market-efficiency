#!/bin/bash
# watchdog.sh — keep the collectors alive for an unattended week.
#
#   nohup ./watchdog.sh > watchdog.log 2>&1 &
#
# WHY THIS EXISTS
#   Nothing currently restarts a collector that dies. If recorder2 crashes at
#   3am on Tuesday you lose until you next look — potentially days of the very
#   data the experiment depends on. Over a one-week unattended run that is the
#   single largest risk, larger than any bug in the analysis.
#
# WHAT IT DOES, every 60 seconds:
#   1. Restarts any collector that is not running
#   2. Restarts caffeinate if it died (Mac would then sleep)
#   3. Truncates logs that grow past 50 MB (a week of errors could fill the disk)
#   4. Halts everything and shouts if free disk drops below 2 GB
#
# It writes only when it ACTS, so a quiet watchdog.log means a quiet week.

set -u
cd "$(dirname "$0")"

export KALSHI_KEY_ID="141ff0bb-968b-4d39-9a01-8bedfdd986b7"
export KALSHI_PRIVATE_KEY_PATH="$(pwd)/kalshi_key.pem"
export KALSHI_ENV="prod"
export PYTHONUNBUFFERED=1

PY="$(pwd)/venv/bin/python"
CHECK_EVERY=60
MAX_LOG_MB=50
MIN_FREE_GB=2

log () { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

log "watchdog started (checking every ${CHECK_EVERY}s)"

restarts=0
while true; do

  # --- disk guard: stop everything rather than fill the disk ----------------
  free_gb=$(df -g . | awk 'NR==2 {print $4}')
  if [ "${free_gb:-99}" -lt "$MIN_FREE_GB" ]; then
    log "!! ONLY ${free_gb}GB FREE — stopping all collectors to protect the disk"
    pkill -f "recorder2.py --run"; pkill -f "scan_all.py"; pkill -f "arb_scanner.py"
    log "!! stopped. Free space, then run ./start.sh"
    exit 1
  fi

  # --- log rotation: a week of errors must not fill the disk ---------------
  for f in recorder.log scan.log arb.log watchdog.log; do
    if [ -f "$f" ]; then
      mb=$(( $(wc -c < "$f") / 1048576 ))
      if [ "$mb" -gt "$MAX_LOG_MB" ]; then
        tail -c 5000000 "$f" > "$f.tmp" && mv "$f.tmp" "$f"
        log "rotated $f (was ${mb}MB, kept last 5MB)"
      fi
    fi
  done

  # --- restart anything that died ------------------------------------------
  if ! pgrep -f "recorder2.py --run" > /dev/null; then
    nohup "$PY" -u recorder2.py --run > recorder.log 2>&1 &
    restarts=$((restarts+1))
    log "RESTARTED recorder2 (restart #$restarts)"
    sleep 3
  fi

  if ! pgrep -f "scan_all.py" > /dev/null; then
    nohup "$PY" -u scan_all.py --run --max-hours 24 --interval 600 >> scan.log 2>&1 &
    restarts=$((restarts+1))
    log "RESTARTED scan_all (restart #$restarts)"
    sleep 3
  fi

  if ! pgrep -f "arb_scanner.py" > /dev/null; then
    nohup "$PY" -u arb_scanner.py --watch --interval 120 >> arb.log 2>&1 &
    restarts=$((restarts+1))
    log "RESTARTED arb_scanner (restart #$restarts)"
    sleep 3
  fi

  # --- keep the Mac awake ---------------------------------------------------
  if ! pgrep -x caffeinate > /dev/null; then
    rec=$(pgrep -f "recorder2.py --run" | head -1)
    if [ -n "$rec" ]; then
      nohup caffeinate -i -w "$rec" > /dev/null 2>&1 &
      log "RESTARTED caffeinate (watching pid $rec)"
    fi
  fi

  sleep "$CHECK_EVERY"
done
