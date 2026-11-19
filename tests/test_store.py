import sqlite3

from hkbus import store
from hkbus.api import EtaRow


def _row(data_ts=100, eta=200, seq=1):
    return EtaRow("KMB", "1A", "O", 1, "S", 5, seq, eta, "", data_ts, data_ts)


def test_identical_snapshot_is_stored_once(tmp_path):
    con = store.connect(tmp_path / "c.sqlite")
    pid = store.insert_poll(con, source="x", key="k", requested_at=1.0)
    assert store.insert_etas(con, [_row(), _row(seq=2)], pid) == 2
    assert store.insert_etas(con, [_row(), _row(seq=2)], pid) == 0      # same data_ts: ignored
    assert store.insert_etas(con, [_row(data_ts=160, eta=205)], pid) == 1  # refreshed: kept
    assert con.execute("SELECT count(*) FROM eta").fetchone()[0] == 3


def test_raw_bodies_deduplicated(tmp_path):
    con = store.connect(tmp_path / "c.sqlite")
    a = store.save_raw(con, b'{"a": 1}', 1.0)
    b = store.save_raw(con, b'{"a": 1}', 2.0)
    assert a == b
    assert con.execute("SELECT count(*) FROM raw_response").fetchone()[0] == 1


def test_migration_adds_fetched_at(tmp_path):
    p = tmp_path / "old.sqlite"
    old = sqlite3.connect(p)
    old.execute("CREATE TABLE weather_obs (obs_ts INTEGER NOT NULL, kind TEXT NOT NULL, place TEXT NOT NULL, "
                "value REAL, PRIMARY KEY (obs_ts, kind, place)) WITHOUT ROWID")
    old.execute("INSERT INTO weather_obs VALUES (1000, 'temp_c', 'X', 20.0)")
    old.commit()
    old.close()
    con = store.connect(p)
    assert con.execute("SELECT fetched_at FROM weather_obs").fetchone()[0] == 1000
    store.insert_weather(con, [{"obs_ts": 2000, "kind": "temp_c", "place": "X", "value": 21.0}], 2100.0)
    assert con.execute("SELECT fetched_at FROM weather_obs WHERE obs_ts = 2000").fetchone()[0] == 2100.0
