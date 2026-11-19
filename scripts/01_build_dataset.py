"""Stage 1: collector SQLite -> DuckDB warehouse -> arrivals -> features.

Usage: python scripts/01_build_dataset.py --sqlite data/collector.sqlite
Writes results/dataset.json.
"""

import argparse
import logging
from pathlib import Path

import _common
from _common import WAREHOUSE, write_json
from hkbus import warehouse

log = logging.getLogger("build")


def main(sqlite_path: Path, db_path: Path) -> None:
    con = warehouse.connect(db_path)
    counts = warehouse.copy_from_sqlite(con, sqlite_path)
    gt = warehouse.build_ground_truth(con)
    n_features = warehouse.build_features(con)
    health = warehouse.query(con, "01_collection_health.sql")
    status = warehouse.query(con, "03_track_status.sql")
    span = con.execute("SELECT min(data_ts), max(data_ts), count(DISTINCT service_date) FROM features").fetchall()[0]
    overall = con.execute("""
        SELECT status, count(*) AS n FROM tracks WHERE status <> 'open' GROUP BY status ORDER BY n DESC
    """).df()
    gaps = con.execute("SELECT count(*), coalesce(sum(gap_end - gap_start), 0) FROM raw_collector_gap").fetchall()[0]
    con.close()
    write_json("dataset.json", {
        "source_rows": counts,
        "ground_truth": gt,
        "n_features_rows": n_features,
        "first_prediction_ts": span[0], "last_prediction_ts": span[1], "n_service_days": span[2],
        "collection_health": health.to_dict(orient="records"),
        "track_status_by_route": status.to_dict(orient="records"),
        "track_status_overall": overall.to_dict(orient="records"),
        "collector_gaps": {"n": gaps[0], "total_seconds": gaps[1]},
    })


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--sqlite", type=Path, default=Path("data/collector.sqlite"))
    ap.add_argument("--db", type=Path, default=WAREHOUSE)
    ap.add_argument("--out", type=Path, default=None, help="write results here instead of results/")
    a = ap.parse_args()
    if a.out:
        _common.set_output(a.out)
    main(a.sqlite, a.db)
