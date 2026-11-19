"""Fill the README from results/*.json so that every number in it is reproducible.

  python scripts/render_readme.py          # write README.md
  python scripts/render_readme.py --check  # fail if README.md is out of date

If results/evaluation.json exists and was produced from at least five service
days, docs/README.full.md is used. Otherwise docs/README.pending.md is used,
which only quotes the ground-truth diagnostics of the committed real sample
(sample_data/output/results/dataset.json).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HKT = timezone(timedelta(hours=8))


def load(path: Path) -> dict | None:
    return json.loads(path.read_text()) if path.exists() else None


def hkt(ts) -> str:
    return datetime.fromtimestamp(ts, HKT).strftime("%d %b %Y %H:%M")


def pct(x, d=0) -> str:
    return "n/a" if x is None else f"{100 * x:.{d}f}%"


def s(x) -> str:
    return "n/a" if x is None else f"{x:.0f} s"


def md_table(header: list[str], rows: list[list]) -> str:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def dataset_context(d: dict) -> dict:
    status = {r["status"]: r["n"] for r in d["track_status_overall"]}
    finished = sum(status.values())
    health = {r["source"]: r for r in d["collection_health"]}
    bus = [health[k] for k in ("kmb_route_eta", "ctb_eta") if k in health]
    n_req = sum(r["n_requests"] for r in bus)
    err = sum(r["error_rate"] * r["n_requests"] for r in bus) / n_req if n_req else 0
    rows = []
    for r in d["track_status_by_route"]:
        rows.append([f"{r['co']} {r['route']}", r["n_tracks"], pct(r["share_arrived"]),
                     pct(r["share_dropped"]), pct(r["share_censored"]),
                     "" if r["gone_after_p50_s"] is None else s(r["gone_after_p50_s"])])
    return {
        "sample_start": hkt(d["first_prediction_ts"]),
        "sample_end": hkt(d["last_prediction_ts"]),
        "sample_hours": f"{(d['last_prediction_ts'] - d['first_prediction_ts']) / 3600:.1f}",
        "sample_requests": f"{n_req:,}",
        "sample_error_rate": pct(err, 2),
        "sample_predictions": f"{d['ground_truth']['n_predictions']:,}",
        "sample_tracks": f"{finished:,}",
        "sample_arrived": pct(status.get("arrived", 0) / finished if finished else 0),
        "sample_labels": f"{d['n_features_rows']:,}",
        "track_table": md_table(["Route", "Finished tracks", "Arrived", "List shrank", "Censored",
                                 "Median time from arrival to leaving the list"], rows),
    }


def full_context(ev: dict, d: dict) -> dict:
    h = ev["headline"]
    rider = ev["rider_policy"].get("5-20 min ahead", ev["rider_policy"]["all"])
    ctx = {
        "test_start": ev["test_period"][0], "test_end": ev["test_period"][1],
        "n_test": f"{ev['n']['test']:,}", "n_train": f"{ev['n']['train']:,}", "n_cal": f"{ev['n']['cal']:,}",
        "n_days": str(ev["split"]["n_dates"]),
        "mae_official": s(h["official"]["mae_s"]), "mae_corrected": s(h["corrected"]["mae_s"]),
        "mae_official_ci": f"{h['official']['mae_ci'][0]:.0f}–{h['official']['mae_ci'][1]:.0f} s",
        "mae_corrected_ci": f"{h['corrected']['mae_ci'][0]:.0f}–{h['corrected']['mae_ci'][1]:.0f} s",
        "mae_reduction": s(h["mae_reduction_s"]), "mae_reduction_pct": pct(h["mae_reduction_pct"]),
        "mae_reduction_ci": f"{h['mae_reduction_ci'][0]:.0f}–{h['mae_reduction_ci'][1]:.0f} s",
        "coverage_corrected": pct(h["corrected"]["coverage_90"], 1),
        "width_corrected": f"{h['corrected']['mean_width_s'] / 60:.1f} min",
        "coverage_official": pct(h["official"]["coverage_90"], 1),
        "width_official": f"{h['official']['mean_width_s'] / 60:.1f} min",
        "rider_wait_official": f"{rider['official']['mean_wait_s'] / 60:.1f} min",
        "rider_wait_corrected": f"{rider['corrected']['mean_wait_s'] / 60:.1f} min",
        "rider_miss_official": pct(rider["official"]["miss_rate"], 1),
        "rider_miss_corrected": pct(rider["corrected"]["miss_rate"], 1),
        "rider_saved": f"{rider.get('wait_saved_s_per_trip', 0):.0f} s",
        "rider_saved_per_1000": f"{rider.get('wait_saved_s_per_trip', 0) * 1000 / 3600:.0f} hours",
        "n_clusters": str(ev["n_clusters"]),
        "label_lo_official": s(ev["label_window_sensitivity"]["error_lo_s"]["official"]),
        "label_hi_official": s(ev["label_window_sensitivity"]["error_hi_s"]["official"]),
        "label_lo_corrected": s(ev["label_window_sensitivity"]["error_lo_s"]["corrected"]),
        "label_hi_corrected": s(ev["label_window_sensitivity"]["error_hi_s"]["corrected"]),
    }
    lo, hi = h["mae_reduction_ci"]
    period = (f"On {ctx['n_test']} predictions from a held-out test period "
              f"({ctx['test_start']} to {ctx['test_end']})")
    if lo > 0:
        verdict = (f"the corrected ETA cuts the mean absolute error of the official KMB and Citybus "
                   f"estimates from {ctx['mae_official']} to {ctx['mae_corrected']} "
                   f"({ctx['mae_reduction_pct']} lower; 95% CI of the reduction {ctx['mae_reduction_ci']})")
    elif hi < 0:
        verdict = (f"the corrected ETA is less accurate than the official estimate "
                   f"({ctx['mae_corrected']} against {ctx['mae_official']} mean absolute error)")
    else:
        verdict = (f"the corrected ETA is not measurably more accurate than the official estimate "
                   f"({ctx['mae_corrected']} against {ctx['mae_official']}; 95% CI of the reduction "
                   f"{ctx['mae_reduction_ci']})")
    saved = rider.get("wait_saved_s_per_trip")
    rider_txt = ""
    if saved is not None and saved > 0:
        rider_txt = (f" For a rider checking 5–20 minutes ahead who aims for a 5% chance of missing the bus, "
                     f"planning with the corrected range means {ctx['rider_saved']} less waiting at the stop "
                     f"per trip (about {ctx['rider_saved_per_1000']} per 1,000 trips), with a miss rate of "
                     f"{ctx['rider_miss_corrected']} against {ctx['rider_miss_official']}.")
    ctx["headline"] = (f"**{period}, {verdict}. Its 90% range covers {ctx['coverage_corrected']} "
                       f"of actual arrivals.{rider_txt}**")
    names = [("official", "Official ETA"), ("bias_horizon", "Official + median bias by operator and horizon"),
             ("corrected_snapshot", "Corrected, snapshot features only"), ("corrected", "Corrected, all features")]
    ctx["headline_table"] = md_table(
        ["Model", "MAE (95% CI)", "Median abs. error", "90% interval coverage", "Mean interval width"],
        [[lab, f"{h[k]['mae_s']:.0f} s ({h[k]['mae_ci'][0]:.0f}–{h[k]['mae_ci'][1]:.0f})",
          s(h[k]["median_ae_s"]), pct(h[k]["coverage_90"], 1), f"{h[k]['mean_width_s'] / 60:.1f} min"]
         for k, lab in names])
    for key, title in (("by_route", "Route"), ("by_time_band", "Time of day"), ("by_horizon_band", "Horizon")):
        ctx[f"{key}_table"] = md_table(
            [title, "Predictions", "MAE official", "MAE corrected", "Coverage corrected"],
            [[r["group"], f"{r['n']:,}", s(r["mae_official"]), s(r["mae_corrected"]),
              pct(r["coverage_corrected"], 1)] for r in ev["breakdown"][key]])
    top = list(ev["feature_importance_gain"].items())[:6]
    ctx["top_features"] = ", ".join(f"`{k}` ({pct(v)})" for k, v in top)
    ctx.update(dataset_context(d))
    return ctx


def render(template: str, ctx: dict) -> str:
    missing = set(re.findall(r"\{\{(\w+)\}\}", template)) - set(ctx)
    if missing:
        raise KeyError(f"no value for: {sorted(missing)}")
    return re.sub(r"\{\{(\w+)\}\}", lambda m: str(ctx[m.group(1)]), template)


def build() -> str:
    ev = load(ROOT / "results" / "evaluation.json")
    if ev and ev["split"]["mode"] == "days":
        d = load(ROOT / "results" / "dataset.json")
        return render((ROOT / "docs" / "README.full.md").read_text(), full_context(ev, d))
    d = load(ROOT / "sample_data" / "output" / "results" / "dataset.json")
    return render((ROOT / "docs" / "README.pending.md").read_text(), dataset_context(d))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    text = build()
    readme = ROOT / "README.md"
    if ap.parse_args().check:
        if not readme.exists() or readme.read_text() != text:
            sys.exit("README.md is out of date: run python scripts/render_readme.py")
        print("README.md matches results")
        return
    readme.write_text(text)
    print("wrote README.md")


if __name__ == "__main__":
    main()
