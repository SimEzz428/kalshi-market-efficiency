import argparse, os, sqlite3, sys

def run(db, table, conds, dry):
    if not os.path.exists(db):
        print(f"  {db}: not found, skipping")
        return 0
    c = sqlite3.connect(db)
    try:
        total = c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except sqlite3.Error as e:
        print(f"  {db}.{table}: {e}")
        return 0
    removed = 0
    for label, where in conds:
        n = c.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()[0]
        if n:
            print(f"    {label:<38} {n:>9,}")
            if not dry:
                c.execute(f"DELETE FROM {table} WHERE {where}")
            removed += n
    if not dry and removed:
        c.commit()
        print("    running VACUUM to reclaim disk...")
        c.execute("VACUUM")
    after = c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    print(f"    {'-'*48}")
    print(f"    {table}: {total:,} -> {after if not dry else total-removed:,} "
          f"({removed:,} {'would be ' if dry else ''}removed)")
    c.close()
    return removed

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    print("DRY RUN — nothing will be deleted\n" if a.dry_run else "DELETING\n")

    print("  kalshi_wide.db / wide_snapshots")
    run("kalshi_wide.db", "wide_snapshots", [
        ("quote at 0 or 100 (not a real quote)",
         "NOT (yes_bid > 0 AND yes_bid < 100 AND yes_ask > 0 AND yes_ask < 100)"),
        ("duplicate ticker+ts",
         "id NOT IN (SELECT MIN(id) FROM wide_snapshots GROUP BY ticker, ts)"),
    ], a.dry_run)

    print("\n  kalshi_15m.db / snapshots")
    run("kalshi_15m.db", "snapshots", [
        ("duplicate ticker+ts",
         "id NOT IN (SELECT MIN(id) FROM snapshots GROUP BY ticker, ts)"),
    ], a.dry_run)

    print("\n  kalshi_15m.db / orderbooks")
    run("kalshi_15m.db", "orderbooks", [
        ("empty book on both sides",
         "(yes_book IN ('[]','') OR yes_book IS NULL) AND "
         "(no_book IN ('[]','') OR no_book IS NULL)"),
    ], a.dry_run)

    print("\n  NOTE: 15-min snapshots with a 0 bid are KEPT deliberately — near")
    print("  expiry a contract legitimately trades to 0 or 100, and Q2/Q7 need")
    print("  those rows to measure when quoting stops. Different market, different")
    print("  rule. Context decides what counts as bad data.")
