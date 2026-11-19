"""Merge collector databases from several machines into one.

Usage: python scripts/merge_collections.py data/merged.sqlite mac.sqlite windows.sqlite

Rows are combined with INSERT OR IGNORE on each table's natural key, so a
prediction collected on both machines is kept once. Poll records are
appended. Collector gaps are not copied: when machines overlap one fills the
other's gaps, so gaps are recomputed from the merged poll times instead.
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from hkbus import store  # noqa: E402

KEYED = ["eta", "raw_response", "weather_obs", "warning_snapshot", "route_stop", "stop"]


def main(out: Path, sources: list[Path]) -> None:
    con = store.connect(out)
    for src in sources:
        con.execute("ATTACH DATABASE ? AS src", (str(src),))
        for t in KEYED:
            cols = [r[1] for r in con.execute(f"PRAGMA src.table_info({t})")]
            if not cols:
                continue
            col_list = ", ".join(cols)
            con.execute(f"INSERT OR IGNORE INTO main.{t} ({col_list}) SELECT {col_list} FROM src.{t}")
        cols = [r[1] for r in con.execute("PRAGMA src.table_info(poll)") if r[1] != "poll_id"]
        col_list = ", ".join(cols)
        con.execute(f"INSERT INTO main.poll ({col_list}) SELECT {col_list} FROM src.poll")
        con.commit()
        con.execute("DETACH DATABASE src")
        print(f"merged {src}")
    # gaps: periods of more than three minutes with no bus poll at all
    con.execute("DELETE FROM collector_gap")
    con.execute("""
        INSERT INTO collector_gap
        SELECT prev, t FROM (
            SELECT requested_at AS t, lag(requested_at) OVER (ORDER BY requested_at) AS prev
            FROM poll WHERE source IN ('kmb_route_eta', 'ctb_eta'))
        WHERE t - prev > 180
    """)
    con.commit()
    n = con.execute("SELECT count(*) FROM collector_gap").fetchone()[0]
    print(f"done: {out}, {n} gaps after merging")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    main(Path(sys.argv[1]), [Path(p) for p in sys.argv[2:]])
