"""SQLite landing store written by the collector.

SQLite in WAL mode is used for collection because it is in the Python
standard library, survives crashes and power loss without corruption, and
lets the analysis read the file while the collector is still writing to it.
Analysis then happens in DuckDB (see hkbus.warehouse and sql/).

Deduplication: a prediction is identified by (company, route, direction,
service type, stop, eta_seq, data_ts). When the operator has not refreshed
its data between two polls, the second copy is ignored by the primary key.
Raw bodies are stored once per distinct content (SHA-256), compressed.
"""

from __future__ import annotations

import hashlib
import sqlite3
import zlib
from pathlib import Path
from typing import Iterable

from .api import EtaRow

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

CREATE TABLE IF NOT EXISTS poll (
    poll_id      INTEGER PRIMARY KEY,
    source       TEXT NOT NULL,      -- kmb_route_eta, ctb_eta, hko_rhrread, ...
    key          TEXT NOT NULL,      -- route or stop/route the request was for
    requested_at REAL NOT NULL,      -- local clock, Unix seconds (UTC)
    http_status  INTEGER,
    latency_ms   REAL,
    attempts     INTEGER,
    error        TEXT,
    body_sha     TEXT,
    generated_ts INTEGER,
    n_rows       INTEGER,
    n_new_rows   INTEGER
);
CREATE INDEX IF NOT EXISTS poll_time ON poll (requested_at);

CREATE TABLE IF NOT EXISTS raw_response (
    body_sha   TEXT PRIMARY KEY,
    first_seen REAL NOT NULL,
    body_zlib  BLOB NOT NULL
);

CREATE TABLE IF NOT EXISTS eta (
    co           TEXT NOT NULL,
    route        TEXT NOT NULL,
    dir          TEXT NOT NULL,
    service_type INTEGER NOT NULL,
    stop         TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    eta_seq      INTEGER NOT NULL,
    eta          INTEGER,            -- predicted arrival, Unix seconds
    rmk_en       TEXT,
    data_ts      INTEGER NOT NULL,   -- when the operator produced the prediction
    generated_ts INTEGER,
    poll_id      INTEGER NOT NULL,
    PRIMARY KEY (co, route, dir, service_type, stop, eta_seq, data_ts)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS weather_obs (
    obs_ts     INTEGER NOT NULL,   -- time the observation refers to (end of the rainfall hour)
    kind       TEXT NOT NULL,
    place      TEXT NOT NULL,
    value      REAL,
    fetched_at REAL,               -- first time the collector saw it: when it became public
    PRIMARY KEY (obs_ts, kind, place)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS warning_snapshot (
    fetched_at INTEGER PRIMARY KEY,
    codes      TEXT NOT NULL          -- comma-separated codes in force, '' if none
);

CREATE TABLE IF NOT EXISTS route_stop (
    company      TEXT NOT NULL,
    route        TEXT NOT NULL,
    dir          TEXT NOT NULL,
    service_type INTEGER NOT NULL,
    seq          INTEGER NOT NULL,
    stop         TEXT NOT NULL,
    selected     INTEGER NOT NULL,
    fetched_at   INTEGER NOT NULL,
    PRIMARY KEY (company, route, dir, service_type, seq, fetched_at)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS stop (
    company TEXT NOT NULL,
    stop    TEXT NOT NULL,
    name_en TEXT,
    lat     REAL,
    lon     REAL,
    PRIMARY KEY (company, stop)
) WITHOUT ROWID;

-- Periods when the collector was not running (sleep, crash, network loss).
-- Trips that span a gap are censored when inferring arrivals.
CREATE TABLE IF NOT EXISTS collector_gap (
    gap_start REAL NOT NULL,
    gap_end   REAL NOT NULL
);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=30)
    con.executescript(SCHEMA)
    _migrate(con)
    return con


def _migrate(con: sqlite3.Connection) -> None:
    """Bring files created by earlier versions of the collector up to date."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(weather_obs)")}
    if "fetched_at" not in cols:
        con.execute("ALTER TABLE weather_obs ADD COLUMN fetched_at REAL")
        # older rows: assume they became public at the end of their period
        con.execute("UPDATE weather_obs SET fetched_at = obs_ts WHERE fetched_at IS NULL")
        con.commit()


def body_sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def save_raw(con: sqlite3.Connection, body: bytes, now: float, keep: bool = True) -> str:
    sha = body_sha(body)
    if keep:
        con.execute("INSERT OR IGNORE INTO raw_response VALUES (?, ?, ?)",
                    (sha, now, zlib.compress(body, 6)))
    return sha


def insert_poll(con: sqlite3.Connection, **kw) -> int:
    cols = ", ".join(kw)
    marks = ", ".join("?" for _ in kw)
    cur = con.execute(f"INSERT INTO poll ({cols}) VALUES ({marks})", tuple(kw.values()))
    return int(cur.lastrowid)


def insert_etas(con: sqlite3.Connection, rows: Iterable[EtaRow], poll_id: int) -> int:
    before = con.total_changes
    con.executemany(
        "INSERT OR IGNORE INTO eta VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [(r.co, r.route, r.dir, r.service_type, r.stop, r.seq, r.eta_seq, r.eta, r.rmk_en,
          r.data_ts, r.generated_ts, poll_id) for r in rows],
    )
    return con.total_changes - before


def insert_weather(con: sqlite3.Connection, rows: list[dict], fetched_at: float) -> int:
    before = con.total_changes
    con.executemany("INSERT OR IGNORE INTO weather_obs (obs_ts, kind, place, value, fetched_at) VALUES (?,?,?,?,?)",
                    [(r["obs_ts"], r["kind"], r["place"], r["value"], fetched_at) for r in rows])
    return con.total_changes - before


def insert_warnings(con: sqlite3.Connection, fetched_at: int, codes: list[str]) -> None:
    con.execute("INSERT OR REPLACE INTO warning_snapshot VALUES (?, ?)", (fetched_at, ",".join(codes)))


def insert_route_stops(con: sqlite3.Connection, rows: list[dict], fetched_at: int) -> None:
    con.executemany(
        "INSERT OR IGNORE INTO route_stop VALUES (?,?,?,?,?,?,?,?)",
        [(r["company"], r["route"], r["dir"], r["service_type"], r["seq"], r["stop"],
          int(r["selected"]), fetched_at) for r in rows],
    )


def upsert_stops(con: sqlite3.Connection, company: str, rows: list[dict]) -> None:
    con.executemany(
        "INSERT OR REPLACE INTO stop VALUES (?,?,?,?,?)",
        [(company, r["stop"], r["name_en"], r["lat"], r["lon"]) for r in rows],
    )


def record_gap(con: sqlite3.Connection, start: float, end: float) -> None:
    con.execute("INSERT INTO collector_gap VALUES (?, ?)", (start, end))
