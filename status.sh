#!/bin/bash
# status.sh — is everything alive and collecting?
#
#   ./status.sh

cd "$(dirname "$0")"

echo "======================================================================"
echo "PROCESSES"
echo "======================================================================"
check () {
  local name="$1" pattern="$2"
  local pid
  pid=$(pgrep -f "$pattern" | head -1)
  if [ -n "$pid" ]; then
    printf "  %-14s RUNNING (pid %s)\n" "$name" "$pid"
  else
    printf "  %-14s *** NOT RUNNING ***\n" "$name"
  fi
}
check "recorder2"   "recorder2.py --run"
check "scan_all"    "scan_all.py"
check "arb_scanner" "arb_scanner.py"
check "caffeinate"  "caffeinate"

echo ""
echo "======================================================================"
echo "DATA (row counts)"
echo "======================================================================"
count () {
  local db="$1" tbl="$2" label="$3"
  if [ -f "$db" ]; then
    local n
    n=$(sqlite3 -cmd ".timeout 5000" "$db" "SELECT COUNT(*) FROM $tbl;" 2>/dev/null || echo "LOCKED")
    printf "  %-28s %12s\n" "$label" "$n"
  else
    printf "  %-28s %12s\n" "$label" "(no db)"
  fi
}
count kalshi_15m.db  snapshots        "15m snapshots"
count kalshi_15m.db  orderbooks       "15m order books"
count kalshi_15m.db  spot             "spot prices"
count kalshi_15m.db  settlements      "15m settlements"
count kalshi_wide.db wide_snapshots   "wide snapshots"
count kalshi_wide.db wide_settlements "wide settlements"
count kalshi_arb.db  violations       "arb violations"

echo ""
echo "======================================================================"
echo "LOGS  (age = seconds since last written; empty log after a few"
echo "       minutes means buffering or a silent crash, not idleness)"
echo "======================================================================"
now=$(date +%s)
for f in recorder.log scan.log arb.log; do
  if [ -f "$f" ]; then
    mtime=$(stat -f %m "$f" 2>/dev/null || echo "$now")
    age=$(( now - mtime ))
    size=$(wc -c < "$f" | tr -d ' ')
    if [ "$size" -eq 0 ]; then
      printf "  %-14s EMPTY  *** check for a crash or missing -u flag ***\n" "$f"
    else
      printf "  %-14s %4ss ago  " "$f" "$age"
      tr '\r' '\n' < "$f" | grep -v '^\s*$' | tail -1 | cut -c1-70
    fi
  else
    printf "  %-14s (no file)\n" "$f"
  fi
done

echo ""
echo "======================================================================"
echo "IS DATA STILL ARRIVING?  (seconds since newest row)"
echo "======================================================================"
# NOTE: -cmd ".timeout 5000" makes sqlite WAIT up to 5s for a write lock instead
# of failing instantly. The collectors hold locks constantly, so without this the
# query fails — and because the error was sent to /dev/null, a LOCKED table was
# displayed as "(no rows yet)". An error rendered as an absence of data: exactly
# the bug class this script exists to catch, committed by the script itself.
#
# Now: distinguish three states explicitly — has data, has no data, query failed.
fresh () {
  local db="$1" tbl="$2" col="$3" label="$4" expect="$5"
  if [ ! -f "$db" ]; then
    printf "  %-22s %s\n" "$label" "(no database)"
    return
  fi
  local out rc last age
  out=$(sqlite3 -cmd ".timeout 5000" "$db" \
        "SELECT COALESCE(CAST(MAX($col) AS INT), -1) FROM $tbl;" 2>&1)
  rc=$?
  if [ $rc -ne 0 ]; then
    printf "  %-22s *** QUERY FAILED: %s ***\n" "$label" "$(echo "$out" | head -1 | cut -c1-40)"
    return
  fi
  last=$(echo "$out" | tr -d '[:space:]')
  if [ "$last" = "-1" ] || [ -z "$last" ]; then
    printf "  %-22s %s\n" "$label" "(table is genuinely empty)"
    return
  fi
  age=$(( now - last ))
  if [ "$age" -lt "$expect" ]; then
    printf "  %-22s %6ss ago   OK\n" "$label" "$age"
  else
    printf "  %-22s %6ss ago   *** STALE (expect < %ss) ***\n" "$label" "$age" "$expect"
  fi
}
fresh kalshi_15m.db  snapshots      ts "15m snapshots"  60
fresh kalshi_15m.db  orderbooks     ts "15m order books" 60
fresh kalshi_15m.db  spot           ts "spot prices"    120
fresh kalshi_wide.db wide_snapshots ts "wide snapshots" 1200

echo ""
echo "  full analysis:   python analyze.py"
echo "  data health:     python healthcheck.py"
echo "  stop everything: ./stop.sh"
