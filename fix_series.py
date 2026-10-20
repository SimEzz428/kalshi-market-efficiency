import argparse, sqlite3, sys
from collections import Counter

def series_of(ev):
    return ev.split("-")[0] if ev else "?"

ap = argparse.ArgumentParser()
ap.add_argument("--dry-run", action="store_true")
ap.add_argument("--db", default="kalshi_wide.db")
a = ap.parse_args()

c = sqlite3.connect(a.db)
for tbl in ("wide_snapshots", "wide_settlements"):
    rows = c.execute(f"SELECT DISTINCT series FROM {tbl}").fetchall()
    before = len(rows)
    after = len({series_of(r[0]) for r in rows})
    print(f"  {tbl}: {before:,} distinct -> {after:,} after repair")
    if not a.dry_run:
        c.execute(f"""UPDATE {tbl} SET series =
                      CASE WHEN instr(series,'-') > 0
                           THEN substr(series, 1, instr(series,'-') - 1)
                           ELSE series END""")
c.commit()

print("\n  Top series after repair:")
for s, n in c.execute("""SELECT series, COUNT(*) FROM wide_snapshots
                         GROUP BY series ORDER BY COUNT(*) DESC LIMIT 12"""):
    print(f"    {s:<24} {n:>8,} snapshots")
c.close()
print("\n  (dry run — nothing written)" if a.dry_run else "\n  repaired.")
