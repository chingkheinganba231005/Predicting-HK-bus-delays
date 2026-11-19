"""Parsers, on real responses saved from the APIs on 19 Nov 2026."""

import json

import pytest

from conftest import DATA
from hkbus import api


def test_to_epoch_handles_offset():
    assert api.to_epoch("2026-11-19T18:00:00+08:00") == api.to_epoch("2026-11-19T10:00:00+00:00")
    assert api.to_epoch("") is None and api.to_epoch(None) is None


def test_parse_kmb_route_eta_maps_stops_and_applies_selection():
    payload = json.loads((DATA / "kmb_route_eta_1A.json").read_text())
    rs = json.loads((DATA / "kmb_route_stop_1A.json").read_text())
    all_stops = {(r["dir"], r["seq"]): r["stop"] for r in rs}
    selected = {(r["dir"], r["seq"]): r["stop"] for r in rs if r["selected"]}
    rows_all = api.parse_kmb_route_eta(payload, all_stops)
    rows_sel = api.parse_kmb_route_eta(payload, selected)
    assert 0 < len(rows_sel) < len(rows_all)
    r = rows_sel[0]
    assert r.co == "KMB" and r.route == "1A" and r.eta_seq in (1, 2, 3)
    assert r.stop == selected[(r.dir, r.seq)]
    assert all(x.data_ts > 1.7e9 for x in rows_all)


def test_parse_ctb_eta():
    payload = json.loads((DATA / "ctb_eta.json").read_text())
    rows = api.parse_ctb_eta(payload)
    assert rows and all(r.co == "CTB" and r.route == "A21" for r in rows)
    assert [r.eta_seq for r in rows if r.dir == rows[0].dir][:2] == [1, 2]


def test_parse_hko_rhrread():
    rows = api.parse_hko_rhrread(json.loads((DATA / "hko_rhrread.json").read_text()))
    kinds = {r["kind"] for r in rows}
    assert {"rain_mm_hour", "temp_c"} <= kinds
    hko = [r for r in rows if r["kind"] == "temp_c" and r["place"] == "Hong Kong Observatory"]
    assert len(hko) == 1 and -5 < hko[0]["value"] < 45


def test_parse_hko_warnsum_format():
    # structure as documented in the HKO Open Data API specification
    payload = {
        "WRAIN": {"name": "Rainstorm Warning Signal", "code": "WRAINA", "actionCode": "ISSUE"},
        "WTCSGNL": {"name": "Tropical Cyclone Warning Signal", "code": "TC8NE", "actionCode": "ISSUE"},
        "WHOT": {"name": "Very Hot Weather Warning", "code": "WHOT", "actionCode": "CANCEL"},
    }
    assert api.parse_hko_warnsum(payload) == ["TC8NE", "WRAINA"]
    assert api.parse_hko_warnsum({}) == []


@pytest.mark.parametrize("route,direction,st,expected", [
    ("1A", "O", 1, "https://data.etabus.gov.hk/v1/transport/kmb/route-stop/1A/outbound/1"),
])
def test_urls(route, direction, st, expected):
    assert api.kmb_route_stop_url(route, direction, st) == expected
    assert api.ctb_eta_url("001027", "1").endswith("/eta/CTB/001027/1")
