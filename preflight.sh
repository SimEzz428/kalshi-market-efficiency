#!/bin/bash
# preflight.sh — final check before leaving everything unattended for a week.
#
#   ./preflight.sh
#
# verify.py checks that the data is CORRECT.
# This checks that the run will SURVIVE a week without you.
#
# Different question, different failure modes: disk exhaustion, a dead process
# nobody restarts, a sleeping Mac, credentials that expire, logs that grow
# without bound.

cd "$(dirname "$0")"
fails=0
warns=0

ok ()   { printf "  [PASS] %s\n" "$1"; }
bad ()  { printf "  [FAIL] %s\n         %s\n" "$1" "$2"; fails=$((fails+1)); }
warn () { printf "  [WARN] %s\n         %s\n" "$1" "$2"; warns=$((warns+1)); }

echo "======================================================================"
echo "PREFLIGHT — will this survive a week unattended?"
echo "======================================================================"
echo ""
echo "1. PROCESSES"

for p in "recorder2.py --run:recorder2" "scan_all.py:scan_all" \
         "arb_scanner.py:arb_scanner" "caffeinate:caffeinate"; do
  pat="${p%%:*}"; name="${p##*:}"
  if pgrep -f "$pat" > /dev/null; then ok "$name running"
  else bad "$name NOT running" "run ./start.sh"; fi
done

if pgrep -f "watchdog.sh" > /dev/null; then
  ok "watchdog running (will restart anything that dies)"
else
  warn "watchdog NOT running" \
       "a collector that crashes will stay dead. Start it:
         nohup ./watchdog.sh > watchdog.log 2>&1 &"
fi

echo ""
echo "2. DISK"

free_gb=$(df -g . | awk 'NR==2 {print $4}')
db_mb=$(du -cm *.db 2>/dev/null | tail -1 | awk '{print $1}')
db_mb=${db_mb:-0}

# growth rate from the 15m db, which dominates
snap=$(sqlite3 -cmd ".timeout 5000" kalshi_15m.db \
       "SELECT COUNT(*) FROM snapshots;" 2>/dev/null || echo 0)
span=$(sqlite3 -cmd ".timeout 5000" kalshi_15m.db \
       "SELECT CAST((MAX(ts)-MIN(ts))/3600 AS INT) FROM snapshots;" 2>/dev/null || echo 1)
[ "${span:-0}" -lt 1 ] && span=1
per_day=$(( db_mb * 24 / span ))
week=$(( per_day * 7 ))

printf "         current databases   %6s MB\n" "$db_mb"
printf "         growth              %6s MB/day\n" "$per_day"
printf "         projected +7 days   %6s MB\n" "$week"
printf "         free space          %6s GB\n" "$free_gb"

if [ "$free_gb" -gt $(( week / 1024 + 5 )) ]; then
  ok "enough disk for a week (with 5GB margin)"
else
  bad "disk may run out" "need ~$(( week / 1024 + 5 ))GB, have ${free_gb}GB"
fi

echo ""
echo "3. LOG GROWTH"
for f in recorder.log scan.log arb.log; do
  if [ -f "$f" ]; then
    mb=$(( $(wc -c < "$f") / 1048576 ))
    proj=$(( mb * 24 * 7 / span ))
    printf "         %-14s %4sMB now, ~%sMB after a week\n" "$f" "$mb" "$proj"
  fi
done
if pgrep -f "watchdog.sh" > /dev/null; then
  ok "watchdog will rotate logs past 50MB"
else
  warn "no log rotation" "without the watchdog, logs grow unbounded"
fi

echo ""
echo "4. POWER AND SLEEP"
if pmset -g | grep -q "sleep.*0"; then
  ok "system sleep disabled"
else
  warn "system sleep may be enabled" \
       "closing the lid stops collection. Run:
         sudo pmset -c disablesleep 1
       or leave the lid open and plugged in."
fi
if pmset -g batt | grep -q "AC Power"; then
  ok "on AC power"
else
  warn "on battery" "plug in — collection stops when the battery dies"
fi

echo ""
echo "5. CREDENTIALS"
if [ -f kalshi_key.pem ]; then
  ok "private key present"
  perm=$(stat -f "%OLp" kalshi_key.pem)
  if [ "$perm" = "600" ]; then ok "key permissions 600"
  else warn "key permissions $perm" "run: chmod 600 kalshi_key.pem"; fi
else
  bad "kalshi_key.pem missing" "collectors cannot authenticate"
fi

echo ""
echo "6. DATA FRESHNESS"
now=$(date +%s)
for spec in "kalshi_15m.db:snapshots:15m snapshots:120" \
            "kalshi_15m.db:orderbooks:order books:120" \
            "kalshi_15m.db:spot:spot prices:180" \
            "kalshi_wide.db:wide_snapshots:wide sweep:1500"; do
  IFS=: read -r db tbl label limit <<< "$spec"
  last=$(sqlite3 -cmd ".timeout 5000" "$db" \
         "SELECT COALESCE(CAST(MAX(ts) AS INT),-1) FROM $tbl;" 2>/dev/null || echo -1)
  if [ "$last" = "-1" ]; then
    bad "$label has no data" "collector may not be writing"
  else
    age=$(( now - last ))
    if [ "$age" -lt "$limit" ]; then ok "$label fresh (${age}s)"
    else bad "$label stale (${age}s)" "expected under ${limit}s"; fi
  fi
done

echo ""
echo "======================================================================"
if [ "$fails" -gt 0 ]; then
  echo "  $fails FAILURE(S), $warns warning(s) — fix before leaving it"
elif [ "$warns" -gt 0 ]; then
  echo "  READY, with $warns warning(s). Review them above."
else
  echo "  ALL CLEAR — safe to leave running for a week."
fi
echo "======================================================================"
echo ""
echo "  While away:  ./status.sh    (pulse)"
echo "               python verify.py  (correctness)"
echo "               tail watchdog.log (did anything crash?)"
exit $(( fails > 0 ? 1 : 0 ))
