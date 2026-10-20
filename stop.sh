#!/bin/bash
# stop.sh — stop everything. Databases are safe; collectors append on restart.
cd "$(dirname "$0")"
echo "Stopping..."
pkill -f "recorder2.py --run" 2>/dev/null && echo "  recorder2 stopped"
pkill -f "recorder.py --run"  2>/dev/null && echo "  recorder stopped"
pkill -f "scan_all.py"        2>/dev/null && echo "  scan_all stopped"
pkill -f "arb_scanner.py"     2>/dev/null && echo "  arb_scanner stopped"
pkill -x caffeinate           2>/dev/null && echo "  caffeinate stopped"
sleep 1
echo ""
echo "Still running (should be empty):"
pgrep -fl "recorder2.py|scan_all.py|arb_scanner.py" || echo "  nothing — all clear"
