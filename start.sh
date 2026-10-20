#!/bin/bash
# start.sh — bring everything up cleanly. Safe to re-run.
#
#   ./start.sh
#
# Starts three collectors plus caffeinate, each detached so they survive
# closing the terminal. Re-running kills the old ones first, so it is
# idempotent — you can't accidentally end up with two recorders fighting
# over the same database.

set -u
cd "$(dirname "$0")"

# ---- credentials -----------------------------------------------------------
export KALSHI_KEY_ID="141ff0bb-968b-4d39-9a01-8bedfdd986b7"
export KALSHI_PRIVATE_KEY_PATH="$(pwd)/kalshi_key.pem"
export KALSHI_ENV="prod"

PY="$(pwd)/venv/bin/python"

if [ ! -x "$PY" ]; then
  echo "ERROR: $PY not found. Run:  python3 -m venv venv && ./venv/bin/pip install -r requirements.txt"
  exit 1
fi
if [ ! -f "$KALSHI_PRIVATE_KEY_PATH" ]; then
  echo "ERROR: kalshi_key.pem not found in $(pwd)"
  exit 1
fi

echo "Stopping anything already running..."
pkill -f "recorder2.py --run"   2>/dev/null
pkill -f "recorder.py --run"    2>/dev/null
pkill -f "scan_all.py"          2>/dev/null
pkill -f "arb_scanner.py"       2>/dev/null
pkill -x caffeinate             2>/dev/null
sleep 2

echo "Verifying credentials..."
if ! "$PY" -c "
from kalshi_client import KalshiClient
c = KalshiClient()
c.get_balance()
print('  auth OK')
" 2>&1 | grep -q "auth OK"; then
  echo "ERROR: authentication failed. Check KALSHI_KEY_ID and kalshi_key.pem."
  exit 1
fi
echo "  auth OK"

echo "Starting collectors..."

# -u = UNBUFFERED output.
#
# WHY THIS MATTERS: Python line-buffers when stdout is a terminal, but switches
# to ~8KB block buffering when redirected to a file. recorder2 prints every few
# seconds so it fills the buffer and appears fine. scan_all prints once every 10
# minutes and arb_scanner once every 2 — their output sat invisible in the buffer
# for hours, making working processes look dead.
#
# Without -u you cannot tell "quiet because nothing is happening" from "quiet
# because it crashed". Always run background Python unbuffered.
export PYTHONUNBUFFERED=1

# 1. Crypto 15-min: snapshots + full order books + spot prices
nohup "$PY" -u recorder2.py --run                     > recorder.log 2>&1 &
echo "  [$!] recorder2  -> kalshi_15m.db"

# 2. Wide sweep: everything settling within 24h, every 10 min
nohup "$PY" -u scan_all.py --run --max-hours 24 --interval 600 > scan.log 2>&1 &
echo "  [$!] scan_all   -> kalshi_wide.db"

# 3. Arbitrage watch: logical violations, every 2 min
nohup "$PY" -u arb_scanner.py --watch --interval 120  > arb.log  2>&1 &
echo "  [$!] arb_scanner-> kalshi_arb.db"

sleep 3

# 4. Keep the Mac awake for as long as the recorder lives
REC_PID=$(pgrep -f "recorder2.py --run" | head -1)
if [ -n "$REC_PID" ]; then
  nohup caffeinate -i -w "$REC_PID" > /dev/null 2>&1 &
  echo "  [$!] caffeinate (watching recorder2, pid $REC_PID)"
fi

echo ""
echo "Waiting 30s for first cycles..."
sleep 30
echo ""
./status.sh
