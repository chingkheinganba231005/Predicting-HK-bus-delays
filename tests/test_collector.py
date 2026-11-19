import json
from pathlib import Path

from conftest import DATA
from hkbus import collector

ROOT = Path(__file__).resolve().parents[1]


def test_select_stops_spacing_and_terminus():
    seqs = list(range(1, 36))
    sel = collector.select_stops(seqs, 10)
    assert len(sel) == 10 and sel[0] == 1 and 35 not in sel
    assert sel == sorted(sel)
    assert collector.select_stops([1, 2, 3], 10) == [1, 2]


def test_config_loads():
    cfg = collector.Config.load(ROOT / "config" / "routes.toml")
    assert len(cfg.routes) == 11
    assert {r.company for r in cfg.routes} == {"KMB", "CTB"}


def test_store_result_from_saved_response(tmp_path):
    cfg = collector.Config.load(ROOT / "config" / "routes.toml")
    c = collector.Collector(cfg, tmp_path / "c.sqlite")
    rs = json.loads((DATA / "kmb_route_stop_1A.json").read_text())
    c.kmb_stops = {"1A": {(r["dir"], r["seq"]): r["stop"] for r in rs if r["selected"]}}
    body = (DATA / "kmb_route_eta_1A.json").read_bytes()
    f = collector.Fetch("kmb_route_eta", "1A/1", "url", requested_at=1.0, status=200, body=body)
    n, new = c.store_result(f)
    assert n > 0 and new == n
    n2, new2 = c.store_result(f)          # the same response again
    assert new2 == 0
    polls = c.con.execute("SELECT count(*), count(DISTINCT body_sha) FROM poll").fetchone()
    assert polls == (2, 1)


def test_failed_request_is_logged_not_raised(tmp_path):
    cfg = collector.Config.load(ROOT / "config" / "routes.toml")
    c = collector.Collector(cfg, tmp_path / "c.sqlite")
    f = collector.Fetch("ctb_eta", "x/1", "url", requested_at=1.0, status=None, error="ConnectTimeout")
    assert c.store_result(f) == (0, 0)
    assert c.con.execute("SELECT error FROM poll").fetchone()[0] == "ConnectTimeout"
