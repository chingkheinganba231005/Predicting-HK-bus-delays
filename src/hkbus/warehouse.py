"""Build the DuckDB analysis warehouse from the collector's SQLite file.

Steps (each a function, all run by scripts/01_build_dataset.py):
  1. copy the collector tables into DuckDB, in chunks
  2. infer actual arrivals in Python (hkbus.ground_truth); store tracks and labels
  3. build the feature table with sql/04_features.sql
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

import duckdb
import pandas as pd

from . import ground_truth as gt

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
SQL_DIR = ROOT / "sql"
HOLIDAYS = ROOT / "config" / "hk_public_holidays.json"

TABLES = ["eta", "poll", "weather_obs", "warning_snapshot", "route_stop", "stop", "collector_gap"]


def read_sql(name: str, **params) -> str:
    text = (SQL_DIR / name).read_text()
    return text.format(**params) if params else text


def connect(path: Path) -> duckdb.DuckDBPyConnection:
    path.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(path))


def run(con: duckdb.DuckDBPyConnection, sql: str) -> None:
    con.execute(sql).fetchall()


def query(con: duckdb.DuckDBPyConnection, name: str, **params) -> pd.DataFrame:
    return con.sql(read_sql(name, **params)).df()


def copy_from_sqlite(con: duckdb.DuckDBPyConnection, sqlite_path: Path, chunk: int = 1_000_000) -> dict:
    """Copy every collector table into DuckDB (replacing previous copies)."""
    src = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    counts = {}
    for table in TABLES:
        run(con, f"DROP TABLE IF EXISTS raw_{table}")
        n = 0
        cols = None
        for part in pd.read_sql_query(f"SELECT * FROM {table}", src, chunksize=chunk):
            cols = part.columns
            con.register("part", part)
            if n == 0:
                run(con, f"CREATE TABLE raw_{table} AS SELECT * FROM part")
            else:
                run(con, f"INSERT INTO raw_{table} SELECT * FROM part")
            con.unregister("part")
            n += len(part)
        if cols is None:  # empty table: create it with the right columns
            empty = pd.read_sql_query(f"SELECT * FROM {table} LIMIT 0", src)
            con.register("part", empty)
            run(con, f"CREATE TABLE raw_{table} AS SELECT * FROM part")
            con.unregister("part")
        counts[table] = n
        log.info("copied %s: %d rows", table, n)
    src.close()
    hol = pd.DataFrame(json.loads(HOLIDAYS.read_text())["holidays"])
    hol["date"] = pd.to_datetime(hol["date"]).dt.date
    con.register("hol", hol)
    run(con, "CREATE OR REPLACE TABLE holiday AS SELECT * FROM hol")
    con.unregister("hol")
    return counts


def build_ground_truth(con: duckdb.DuckDBPyConnection, params: gt.Params = gt.Params()) -> dict:
    eta = query(con, "02_prediction_streams.sql")
    gaps = con.sql("SELECT gap_start, gap_end FROM raw_collector_gap").df()
    obs, tracks = gt.infer(eta, gaps, params)
    lab = gt.labels(obs, tracks)
    for name, frame in (("tracks", tracks), ("labels", lab)):
        con.register("frame", frame)
        run(con, f"CREATE OR REPLACE TABLE {name} AS SELECT * FROM frame")
        con.unregister("frame")
    summary = tracks.groupby(["co", "status"]).size().unstack(fill_value=0) if not tracks.empty else pd.DataFrame()
    log.info("tracks by status:\n%s", summary)
    return {"n_predictions": len(eta), "n_tracks": len(tracks), "n_labels": len(lab)}


def build_features(con: duckdb.DuckDBPyConnection) -> int:
    run(con, read_sql("04_features.sql"))
    return con.execute("SELECT count(*) FROM features").fetchall()[0][0]
