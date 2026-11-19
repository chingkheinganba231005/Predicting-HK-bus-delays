"""Reliability of Hong Kong's official bus ETAs, and a corrected ETA with its range.

Run with:  streamlit run dashboard/app.py
Reads results/*.json and models/corrected_snapshot.pkl (produced by
scripts/02_train_evaluate.py). The live tab calls the official APIs.
"""

from __future__ import annotations

import json
import pickle
import sys
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import altair as alt
import numpy as np
import pandas as pd
import requests
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from hkbus import api  # noqa: E402

HKT = timezone(timedelta(hours=8))
RESULTS = ROOT / "results"


def load_json(name: str) -> dict | None:
    p = RESULTS / name
    return json.loads(p.read_text()) if p.exists() else None


@st.cache_resource
def load_model():
    p = ROOT / "models" / "corrected_snapshot.pkl"
    if not p.exists():
        return None
    with open(p, "rb") as f:
        return pickle.load(f)


@st.cache_data(ttl=3600)
def route_stops(company: str, route: str, direction: str, service_type: int) -> pd.DataFrame:
    url = (api.kmb_route_stop_url(route, direction, service_type) if company == "KMB"
           else api.ctb_route_stop_url(route, direction))
    rows = api.parse_route_stops(requests.get(url, timeout=10).json(), company)
    df = pd.DataFrame(rows)
    names = []
    for s in df["stop"]:
        u = f"{api.KMB_BASE}/stop/{s}" if company == "KMB" else api.ctb_stop_url(s)
        try:
            names.append(api.parse_stop(requests.get(u, timeout=10).json())["name_en"])
        except Exception:
            names.append(s)
    df["name_en"] = names
    df["stop_frac"] = df["seq"] / df["seq"].max()
    return df


@st.cache_data(ttl=300)
def live_weather() -> dict:
    try:
        rows = api.parse_hko_rhrread(requests.get(api.hko_url("rhrread"), timeout=10).json())
        warns = api.parse_hko_warnsum(requests.get(api.hko_url("warnsum"), timeout=10).json())
    except Exception:
        return {}
    rain = [r["value"] for r in rows if r["kind"] == "rain_mm_hour"]
    temp = [r["value"] for r in rows if r["kind"] == "temp_c" and r["place"] == "Hong Kong Observatory"]
    return {"rain_max_mm": max(rain) if rain else 0.0, "rain_mean_mm": float(np.mean(rain)) if rain else 0.0,
            "temp_c": temp[0] if temp else np.nan,
            "rainstorm_warning": int(any("WRAIN" in w for w in warns)),
            "typhoon_signal": int(any(w.startswith("TC") or "WTCSGNL" in w for w in warns)),
            "n_warnings": len(warns)}


def reliability_tab(rel: dict) -> None:
    df = pd.DataFrame(rel["by_route_hour"])
    metric = st.radio("Measure", ["mae_s", "median_error_s", "late_2min_share"], horizontal=True,
                      format_func={"mae_s": "Mean absolute error (s)",
                                   "median_error_s": "Median error (s, + = late)",
                                   "late_2min_share": "Share more than 2 min late"}.get)
    min_n = st.slider("Hide cells with fewer predictions than", 0, 500, 50, step=25)
    d = df[df["n"] >= min_n]
    # sequential blues for magnitudes; diverging blue (early) to red (late) around 0 for signed error
    scale = (alt.Scale(scheme="redblue", reverse=True, domainMid=0) if metric == "median_error_s"
             else alt.Scale(scheme="blues"))
    chart = alt.Chart(d).mark_rect().encode(
        x=alt.X("hour:O", title="Hour of day (HKT)"),
        y=alt.Y("route_label:N", title=None),
        color=alt.Color(f"{metric}:Q", scale=scale),
        tooltip=["route_label", "hour", "n", alt.Tooltip("mae_s:Q", format=".0f"),
                 alt.Tooltip("median_error_s:Q", format=".0f"), alt.Tooltip("late_2min_share:Q", format=".1%")],
    ).properties(height=34 * d["route_label"].nunique() + 40)
    st.altair_chart(chart, width="stretch")
    st.caption(f"Official ETA error, all {rel['n_labelled']:,} labelled predictions. "
               "Error = actual arrival minus predicted arrival.")
    q = pd.DataFrame(rel["error_quantiles_by_horizon"])
    st.subheader("Error spread by prediction horizon")
    st.dataframe(q.rename(columns={"0.05": "5%", "0.25": "25%", "0.5": "median", "0.75": "75%", "0.95": "95%"})
                  .style.format({c: "{:.0f}" for c in ["5%", "25%", "median", "75%", "95%"]}),
                 width="stretch", hide_index=True)


def results_tab(ev: dict) -> None:
    h = ev["headline"]
    c1, c2, c3 = st.columns(3)
    c1.metric("Official ETA, mean absolute error", f"{h['official']['mae_s']:.0f} s")
    c2.metric("Corrected ETA, mean absolute error", f"{h['corrected']['mae_s']:.0f} s",
              f"-{h['mae_reduction_pct']:.0%}", delta_color="inverse")
    c3.metric("Corrected 90% range: coverage", f"{h['corrected']['coverage_90']:.1%}",
              f"mean width {h['corrected']['mean_width_s'] / 60:.1f} min", delta_color="off")
    st.caption(f"Test period {ev['test_period'][0]} to {ev['test_period'][1]}, "
               f"{ev['n']['test']:,} predictions never used for fitting or calibration.")
    for key, title in (("by_route", "By route"), ("by_time_band", "By time of day"),
                       ("by_horizon_band", "By horizon")):
        st.subheader(title)
        t = pd.DataFrame(ev["breakdown"][key])[["group", "n", "mae_official", "mae_corrected",
                                               "coverage_official", "coverage_corrected"]]
        st.dataframe(t.style.format({"mae_official": "{:.0f}", "mae_corrected": "{:.0f}",
                                     "coverage_official": "{:.1%}", "coverage_corrected": "{:.1%}"}),
                     width="stretch", hide_index=True)


def live_tab(model) -> None:
    if model is None:
        st.info("Train the models first: python scripts/02_train_evaluate.py")
        return
    cfg = tomllib.loads((ROOT / "config" / "routes.toml").read_text())
    routes = [(r["company"], r["route"], r.get("service_type", 1)) for r in cfg["route"]]
    c1, c2 = st.columns(2)
    company, route, st_type = c1.selectbox("Route", routes, format_func=lambda r: f"{r[0]} {r[1]}")
    direction = c2.radio("Direction", ["O", "I"], horizontal=True,
                         format_func={"O": "Outbound", "I": "Inbound"}.get)
    stops = route_stops(company, route, direction, st_type)
    stops = stops[stops["seq"] > 1]
    row = st.selectbox("Stop", stops.to_dict("records"), format_func=lambda r: f"{r['seq']}. {r['name_en']}")
    if company == "KMB":
        payload = requests.get(f"{api.KMB_BASE}/eta/{row['stop']}/{route}/{st_type}", timeout=10).json()
        rows = [r for r in payload.get("data", []) if r.get("eta") and r["dir"] == direction]
        etas = [(int(r["eta_seq"]), api.to_epoch(r["eta"]), api.to_epoch(r["data_timestamp"]), r.get("rmk_en") or "")
                for r in rows]
    else:
        payload = requests.get(api.ctb_eta_url(row["stop"], route), timeout=10).json()
        etas = [(r.eta_seq, r.eta, r.data_ts, r.rmk_en) for r in api.parse_ctb_eta(payload)
                if r.eta and r.dir == direction]
    if not etas:
        st.warning("No buses predicted at this stop right now.")
        return
    now_local = datetime.now(HKT)
    w = live_weather()
    hol = {h["date"] for h in json.loads((ROOT / "config" / "hk_public_holidays.json").read_text())["holidays"]}
    feats = pd.DataFrame([{
        "co": company, "route": route, "dir": direction, "stop": row["stop"],
        "horizon_s": eta - data_ts, "eta_seq": seq, "seq": row["seq"], "stop_frac": row["stop_frac"],
        "scheduled": int("scheduled" in rmk.lower()), "hour": now_local.hour,
        "minute_of_day": now_local.hour * 60 + now_local.minute, "dow": now_local.isoweekday(),
        "non_working_day": int(now_local.isoweekday() >= 6 or now_local.strftime("%Y-%m-%d") in hol),
        **{k: w.get(k, np.nan) for k in ("rain_max_mm", "rain_mean_mm", "temp_c", "rainstorm_warning",
                                         "typhoon_signal", "n_warnings")},
        "eta": eta, "data_ts": data_ts,
    } for seq, eta, data_ts, rmk in etas])
    for c in ("n_prev_obs", "eta_change_s", "since_prev_obs_s", "eta_drift_s", "first_horizon_s",
              "unchanged_streak", "recent_arrivals_30m", "recent_error_30m_s"):
        feats[c] = np.nan  # not used by the snapshot model
    p = model.predict(feats)
    fmt = lambda ts: datetime.fromtimestamp(ts, HKT).strftime("%H:%M:%S")
    out = pd.DataFrame({
        "bus": feats["eta_seq"],
        "official ETA": [fmt(e) for e in feats["eta"]],
        "corrected ETA": [fmt(e + d) for e, d in zip(feats["eta"], p["pred"])],
        "90% range": [f"{fmt(e + lo)} to {fmt(e + hi)}" for e, lo, hi in zip(feats["eta"], p["lo"], p["hi"])],
        "be at the stop by": [fmt(e + lw) for e, lw in zip(feats["eta"], p["lower95"])],
    })
    st.dataframe(out, width="stretch", hide_index=True)
    st.caption(f"Fetched {now_local.strftime('%H:%M:%S')} HKT. 'Be at the stop by' gives a 5% chance of the "
               "bus arriving before you, according to the calibrated lower bound. This live view uses the "
               "model without track-history features, because one snapshot has no history.")


def main() -> None:
    st.set_page_config(page_title="HK bus ETA reliability", layout="wide")
    st.title("How reliable are Hong Kong's bus arrival estimates?")
    rel, ev = load_json("reliability.json"), load_json("evaluation.json")
    tabs = st.tabs(["Reliability by route and hour", "Corrected ETA: test results", "Live corrected ETA"])
    with tabs[0]:
        reliability_tab(rel) if rel else st.info("Run scripts/02_train_evaluate.py first.")
    with tabs[1]:
        results_tab(ev) if ev else st.info("Run scripts/02_train_evaluate.py first.")
    with tabs[2]:
        live_tab(load_model())
    st.caption("Data: KMB and Citybus real-time arrival data and Hong Kong Observatory weather, "
               "from DATA.GOV.HK. Not affiliated with the operators.")


if __name__ == "__main__":
    main()
