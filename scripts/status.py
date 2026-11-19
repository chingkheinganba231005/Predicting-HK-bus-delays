"""Quick health check of a running collection.

Usage: python scripts/status.py --db data/collector.sqlite
"""

import argparse
import sqlite3
import time
from pathlib import Path


def main(db: Path) -> None:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    now = time.time()
    first, last, n = con.execute("SELECT min(requested_at), max(requested_at), count(*) FROM poll").fetchone()
    if not n:
        print("no polls yet")
        return
    print(f"database:        {db} ({db.stat().st_size / 1e6:,.0f} MB)")
    print(f"collecting for:  {(last - first) / 86400:.2f} days, last poll {now - last:.0f} s ago")
    for window, label in ((3600, "last hour"), (86400, "last 24 h")):
        req, err, rows = con.execute(
            "SELECT count(*), sum(error IS NOT NULL), coalesce(sum(n_new_rows), 0) FROM poll WHERE requested_at > ?",
            (now - window,)).fetchone()
        print(f"{label:16s} {req:,} requests, {err or 0:,} failed, {rows:,} new prediction rows")
    gaps = con.execute("SELECT count(*), coalesce(sum(gap_end - gap_start), 0) FROM collector_gap").fetchone()
    print(f"gaps:            {gaps[0]} totalling {gaps[1] / 3600:.1f} h (sleep, crashes or no network)")
    print(f"ETA rows:        {con.execute('SELECT count(*) FROM eta').fetchone()[0]:,}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=Path("data/collector.sqlite"))
    main(ap.parse_args().db)
