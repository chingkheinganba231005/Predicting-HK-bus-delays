"""Cut a small, self-contained sample out of a collector database.

Usage: python scripts/make_sample.py data/collector.sqlite sample_data/collector_sample.sqlite [--hours 3]

Keeps the last ``hours`` of predictions, polls, weather and warnings, the
route-stop lists and the stops that were selected. Raw responses are kept
only for the Hong Kong Observatory (bus responses are large and fully
represented by the parsed rows).
"""

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from hkbus import store  # noqa: E402


def main(src: Path, out: Path, hours: float) -> None:
    if out.exists():
        out.unlink()
    con = store.connect(out)
    con.execute("ATTACH DATABASE ? AS src", (str(src),))
    last = con.execute("SELECT max(requested_at) FROM src.poll").fetchone()[0]
    start = last - hours * 3600
    con.execute("INSERT INTO poll SELECT * FROM src.poll WHERE requested_at >= ?", (start,))
    con.execute("INSERT INTO eta SELECT * FROM src.eta WHERE data_ts >= ?", (start,))
    con.execute("INSERT INTO weather_obs SELECT * FROM src.weather_obs WHERE fetched_at >= ?", (start - 3600,))
    con.execute("INSERT INTO warning_snapshot SELECT * FROM src.warning_snapshot WHERE fetched_at >= ?", (start - 3600,))
    con.execute("INSERT INTO route_stop SELECT * FROM src.route_stop")
    con.execute("""INSERT INTO stop SELECT * FROM src.stop
                   WHERE (company, stop) IN (SELECT company, stop FROM src.route_stop)""")
    con.execute("""INSERT INTO raw_response SELECT r.* FROM src.raw_response r
                   WHERE r.body_sha IN (SELECT body_sha FROM src.poll
                                        WHERE source LIKE 'hko%' AND requested_at >= ?)""", (start,))
    con.execute("INSERT INTO collector_gap SELECT * FROM src.collector_gap WHERE gap_end >= ?", (start,))
    con.commit()
    con.execute("DETACH DATABASE src")
    con.execute("PRAGMA journal_mode = DELETE")
    con.execute("VACUUM")
    con.close()
    n = sqlite3.connect(out).execute("SELECT count(*) FROM eta").fetchone()[0]
    print(f"{out}: {n:,} predictions from the last {hours} h, {out.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("src", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--hours", type=float, default=3.0)
    a = ap.parse_args()
    main(a.src, a.out, a.hours)
