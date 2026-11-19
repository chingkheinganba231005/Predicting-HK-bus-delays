"""Endpoints of the official open-data APIs and parsers for their responses.

Sources (all listed on DATA.GOV.HK):
  KMB:     https://data.etabus.gov.hk/  (API specification v1.05, Oct 2024)
  Citybus: https://rt.data.gov.hk/v2/transport/citybus/  (API specification v2.01, Jul 2023)
  HKO:     https://data.weather.gov.hk/weatherAPI/opendata/weather.php

The parsers are pure functions of the decoded JSON so they can be tested on
saved responses.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

KMB_BASE = "https://data.etabus.gov.hk/v1/transport/kmb"
CTB_BASE = "https://rt.data.gov.hk/v2/transport/citybus"
HKO_URL = "https://data.weather.gov.hk/weatherAPI/opendata/weather.php"

BOUND = {"O": "outbound", "I": "inbound"}


def kmb_route_eta_url(route: str, service_type: int) -> str:
    return f"{KMB_BASE}/route-eta/{route}/{service_type}"


def kmb_route_stop_url(route: str, direction: str, service_type: int) -> str:
    return f"{KMB_BASE}/route-stop/{route}/{BOUND[direction]}/{service_type}"


def kmb_stop_list_url() -> str:
    return f"{KMB_BASE}/stop"


def ctb_eta_url(stop: str, route: str) -> str:
    return f"{CTB_BASE}/eta/CTB/{stop}/{route}"


def ctb_route_stop_url(route: str, direction: str) -> str:
    return f"{CTB_BASE}/route-stop/CTB/{route}/{BOUND[direction]}"


def ctb_stop_url(stop: str) -> str:
    return f"{CTB_BASE}/stop/{stop}"


def hko_url(data_type: str) -> str:
    return f"{HKO_URL}?dataType={data_type}&lang=en"


def to_epoch(ts: str | None) -> int | None:
    """ISO 8601 timestamp with offset (as served by all three APIs) to Unix seconds."""
    if not ts:
        return None
    return int(datetime.fromisoformat(ts).timestamp())


def generated_ts(payload: dict) -> int | None:
    # Citybus v1.0 used a key with a trailing space; v2 does not. Accept both.
    return to_epoch(payload.get("generated_timestamp") or payload.get("generated_timestamp "))


@dataclass(frozen=True)
class EtaRow:
    co: str
    route: str
    dir: str
    service_type: int
    stop: str
    seq: int
    eta_seq: int
    eta: int | None      # predicted arrival, Unix seconds; None when no bus is predicted
    rmk_en: str
    data_ts: int         # when the operator produced this prediction
    generated_ts: int | None


def parse_kmb_route_eta(payload: dict, stops_by_seq: dict[tuple[str, int], str]) -> list[EtaRow]:
    """Rows of a KMB route-eta response.

    The route-eta payload identifies stops by sequence number only, so the
    route-stop table (``stops_by_seq``: (direction, seq) -> stop id) is needed
    to attach stop ids. Rows whose (direction, seq) is not in the mapping are
    dropped, which is also how the stop selection is applied.
    """
    gen = generated_ts(payload)
    out = []
    for r in payload.get("data", []):
        key = (r["dir"], int(r["seq"]))
        if key not in stops_by_seq:
            continue
        out.append(EtaRow(
            co="KMB", route=r["route"], dir=r["dir"], service_type=int(r["service_type"]),
            stop=stops_by_seq[key], seq=int(r["seq"]), eta_seq=int(r["eta_seq"]),
            eta=to_epoch(r.get("eta")), rmk_en=r.get("rmk_en") or "",
            data_ts=to_epoch(r["data_timestamp"]), generated_ts=gen,
        ))
    return out


def parse_ctb_eta(payload: dict) -> list[EtaRow]:
    gen = generated_ts(payload)
    out = []
    for r in payload.get("data", []):
        if not r.get("data_timestamp"):
            continue
        out.append(EtaRow(
            co="CTB", route=r["route"], dir=r["dir"], service_type=1, stop=r["stop"],
            seq=int(r["seq"]), eta_seq=int(r["eta_seq"]), eta=to_epoch(r.get("eta")),
            rmk_en=r.get("rmk_en") or "", data_ts=to_epoch(r["data_timestamp"]), generated_ts=gen,
        ))
    return out


def parse_route_stops(payload: dict, company: str) -> list[dict]:
    """Route-stop list -> [{direction, seq, stop}] for either operator."""
    out = []
    for r in payload.get("data", []):
        direction = r.get("bound") or r.get("dir")
        out.append({"company": company, "route": r["route"], "dir": direction,
                    "service_type": int(r.get("service_type", 1)),
                    "seq": int(r["seq"]), "stop": r["stop"]})
    return out


def parse_stop(payload: dict) -> dict | None:
    d = payload.get("data")
    if not d:
        return None
    return {"stop": d["stop"], "name_en": d.get("name_en"), "lat": float(d["lat"]),
            "lon": float(d["long"])}


def parse_kmb_stop_list(payload: dict) -> list[dict]:
    return [{"stop": d["stop"], "name_en": d.get("name_en"), "lat": float(d["lat"]),
             "lon": float(d["long"])} for d in payload.get("data", [])]


def parse_hko_rhrread(payload: dict) -> list[dict]:
    """Current weather report -> observation rows (kind, place, value, obs_ts)."""
    rows = []
    rain = payload.get("rainfall") or {}
    r_ts = to_epoch(rain.get("endTime"))
    for d in rain.get("data", []):
        if "max" in d and r_ts is not None:
            rows.append({"obs_ts": r_ts, "kind": "rain_mm_hour", "place": d["place"],
                         "value": float(d["max"])})
    temp = payload.get("temperature") or {}
    t_ts = to_epoch(temp.get("recordTime"))
    for d in temp.get("data", []):
        if t_ts is not None and d.get("value") is not None:
            rows.append({"obs_ts": t_ts, "kind": "temp_c", "place": d["place"],
                         "value": float(d["value"])})
    hum = payload.get("humidity") or {}
    h_ts = to_epoch(hum.get("recordTime"))
    for d in hum.get("data", []):
        if h_ts is not None and d.get("value") is not None:
            rows.append({"obs_ts": h_ts, "kind": "humidity_pct", "place": d["place"],
                         "value": float(d["value"])})
    return rows


def parse_hko_warnsum(payload: dict) -> list[str]:
    """Codes of the weather warnings in force (e.g. WRAINA, WTCSGNL, WHOT)."""
    codes = []
    for key, d in (payload or {}).items():
        if isinstance(d, dict) and d.get("actionCode") != "CANCEL":
            codes.append(d.get("code") or key)
    return sorted(codes)
