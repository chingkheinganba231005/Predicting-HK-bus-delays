"""Stage 2: fit the corrections on the training period, calibrate, test once.

Writes results/evaluation.json, results/reliability.json, models/*.pkl and
figures/*.png. The test period (the last 20% of service days) is used only
here, after every modelling choice has been fixed.
"""

import argparse
import logging
import pickle
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import _common  # noqa: E402
from _common import ROOT, SEED, WAREHOUSE, write_json  # noqa: E402
from hkbus import model as M  # noqa: E402
from hkbus import plots, warehouse  # noqa: E402

log = logging.getLogger("train")
N_BOOT = 500
TIME_BANDS = [(0, 7, "00-07 night/early"), (7, 10, "07-10 AM peak"), (10, 16, "10-16 midday"),
              (16, 20, "16-20 PM peak"), (20, 24, "20-24 evening")]


def time_band(hour: pd.Series) -> pd.Series:
    out = pd.Series("", index=hour.index)
    for a, b, name in TIME_BANDS:
        out[(hour >= a) & (hour < b)] = name
    return out


def cluster_bootstrap(frame: pd.DataFrame, cols: list[str], clusters: np.ndarray, n_boot: int, seed: int):
    """Mean absolute value of each column, resampling whole clusters (service day x route).

    Errors of predictions on the same route and day are strongly correlated
    (one late bus produces many late predictions), so rows are not resampled
    independently.
    """
    rng = np.random.default_rng(seed)
    codes, uniq = pd.factorize(clusters)
    k = len(uniq)
    sums = np.vstack([np.bincount(codes, weights=frame[c].abs().to_numpy(), minlength=k) for c in cols])
    counts = np.bincount(codes, minlength=k).astype(float)
    out = np.empty((n_boot, len(cols)))
    for b in range(n_boot):
        w = rng.multinomial(k, np.full(k, 1 / k)).astype(float)
        out[b] = (sums @ w) / (counts @ w)
    return out


def metrics_table(df: pd.DataFrame, preds: dict, group: pd.Series | None = None) -> list[dict]:
    rows = []
    groups = [("all", df.index)] if group is None else list(df.groupby(group).groups.items())
    for g, idx in groups:
        sub = df.loc[idx]
        row = {"group": g, "n": len(sub)}
        for name, p in preds.items():
            pp = p.loc[idx]
            res = sub["error_s"] - pp["pred"]
            row[f"mae_{name}"] = float(res.abs().mean())
            row[f"bias_{name}"] = float(res.mean())
            if "lo" in pp:
                inside = (sub["error_s"] >= pp["lo"]) & (sub["error_s"] <= pp["hi"])
                row[f"coverage_{name}"] = float(inside.mean())
                row[f"width_{name}"] = float((pp["hi"] - pp["lo"]).mean())
        rows.append(row)
    return rows


def main(db: Path) -> None:
    con = warehouse.connect(db)
    df = con.sql("SELECT * FROM features").df()
    con.close()
    df["service_date"] = pd.to_datetime(df["service_date"]).dt.date
    split, info = M.time_split(df)
    df["split"] = split
    tr, cal, te = (df[df.split == s].copy() for s in ("train", "cal", "test"))
    log.info("split %s: train %d, cal %d, test %d, embargo %d", info["mode"], len(tr), len(cal), len(te),
             int((df.split == "embargo").sum()))

    official = M.EmpiricalBaseline(shift=False).fit(tr, cal)
    bias = M.EmpiricalBaseline(shift=True).fit(tr, cal)
    full = M.QuantileModel(seed=SEED).fit(tr, cal)
    snap = M.QuantileModel(features=[f for f in M.CATEGORICAL + M.NUMERIC if f not in M.HISTORY],
                           seed=SEED).fit(tr, cal)
    models = {"official": official, "bias_horizon": bias, "corrected": full, "corrected_snapshot": snap}
    preds = {k: m.predict(te) for k, m in models.items()}

    # headline metrics with cluster-bootstrap CIs
    clusters = (te["service_date"].astype(str) + "|" + te["co"] + "|" + te["route"]).to_numpy()
    resid = pd.DataFrame({k: te["error_s"] - p["pred"] for k, p in preds.items()})
    boot = cluster_bootstrap(resid, list(preds), clusters, N_BOOT, SEED)
    headline = {}
    for j, k in enumerate(preds):
        p = preds[k]
        inside = (te["error_s"] >= p["lo"]) & (te["error_s"] <= p["hi"])
        headline[k] = {
            "mae_s": float(resid[k].abs().mean()),
            "mae_ci": [float(x) for x in np.quantile(boot[:, j], [0.025, 0.975])],
            "median_ae_s": float(resid[k].abs().median()),
            "bias_s": float(resid[k].mean()),
            "coverage_90": float(inside.mean()),
            "mean_width_s": float((p["hi"] - p["lo"]).mean()),
        }
    j_off, j_cor = list(preds).index("official"), list(preds).index("corrected")
    diff = boot[:, j_off] - boot[:, j_cor]
    headline["mae_reduction_s"] = headline["official"]["mae_s"] - headline["corrected"]["mae_s"]
    headline["mae_reduction_ci"] = [float(x) for x in np.quantile(diff, [0.025, 0.975])]
    headline["mae_reduction_pct"] = headline["mae_reduction_s"] / headline["official"]["mae_s"]

    # label uncertainty: Citybus arrivals are only known to within a window
    sens = {}
    for edge in ("error_lo_s", "error_hi_s"):
        sens[edge] = {k: float((te[edge] - p["pred"]).abs().mean()) for k, p in preds.items()}

    # breakdowns
    te["time_band"] = time_band(te["hour"])
    te["horizon_band"] = np.asarray(M.horizon_band(te["horizon_s"]))
    te["route_label"] = te["co"] + " " + te["route"]
    breakdown = {
        "by_route": metrics_table(te, preds, te["route_label"]),
        "by_time_band": metrics_table(te, preds, te["time_band"]),
        "by_horizon_band": metrics_table(te, preds, te["horizon_band"]),
        "by_operator": metrics_table(te, preds, te["co"]),
    }

    # rider policy: be at the stop at official ETA + lower bound (5% planned miss rate);
    # compare waiting time, for predictions 5-20 minutes ahead (when people decide to leave)
    rider = {}
    for band_name, mask in (("all", np.ones(len(te), bool)),
                            ("5-20 min ahead", ((te["horizon_s"] >= 300) & (te["horizon_s"] < 1200)).to_numpy())):
        rider[band_name] = {k: M.rider_policy(te["error_s"].to_numpy()[mask], preds[k]["lower95"].to_numpy()[mask])
                            for k in ("official", "bias_horizon", "corrected")}
        rider[band_name]["n"] = int(mask.sum())
        off, cor = rider[band_name]["official"], rider[band_name]["corrected"]
        if off["mean_wait_s"] is not None and cor["mean_wait_s"] is not None:
            rider[band_name]["wait_saved_s_per_trip"] = off["mean_wait_s"] - cor["mean_wait_s"]

    # feature importance (gain) of the median model
    med = full.models[0.5]
    imp = pd.Series(med.booster_.feature_importance("gain"), index=full.features)
    importance = (imp / max(imp.sum(), 1e-12)).sort_values(ascending=False).round(4).to_dict()

    models_dir = _common.MODELS
    models_dir.mkdir(exist_ok=True)
    for k in ("corrected", "corrected_snapshot"):
        with open(models_dir / f"{k}.pkl", "wb") as f:
            pickle.dump(models[k], f)

    write_json("evaluation.json", {
        "split": info,
        "n": {"train": len(tr), "cal": len(cal), "test": len(te), "embargo": int((df.split == "embargo").sum())},
        "test_period": [str(te["service_date"].min()), str(te["service_date"].max())],
        "headline": headline,
        "label_window_sensitivity": sens,
        "breakdown": breakdown,
        "rider_policy": rider,
        "feature_importance_gain": importance,
        "n_bootstrap": N_BOOT,
        "n_clusters": int(len(np.unique(clusters))),
    })

    # descriptive reliability of the official ETA, all labelled data, for the dashboard
    df["route_label"] = df["co"] + " " + df["route"]
    df["abs_error"] = df["error_s"].abs()
    df["horizon_band"] = np.asarray(M.horizon_band(df["horizon_s"]))
    rel = (df.groupby(["route_label", "hour"])
             .agg(n=("error_s", "size"), mae_s=("abs_error", "mean"), median_error_s=("error_s", "median"),
                  p90_abs_error_s=("abs_error", lambda x: float(np.quantile(x, 0.9))),
                  late_2min_share=("error_s", lambda x: float((x > 120).mean())))
             .reset_index())
    by_h = (df.groupby(["co", "horizon_band"], observed=True)["error_s"]
              .quantile([0.05, 0.25, 0.5, 0.75, 0.95]).unstack().reset_index())
    by_h.columns = [str(c) for c in by_h.columns]
    write_json("reliability.json", {"by_route_hour": rel.to_dict(orient="records"),
                                    "error_quantiles_by_horizon": by_h.to_dict(orient="records"),
                                    "n_labelled": len(df)})
    figures(te, preds, headline, breakdown, df)


def figures(te, preds, headline, breakdown, df) -> None:
    plots.style()
    # official error against horizon
    fig, ax = plt.subplots(figsize=(7, 3.8))
    for i, co in enumerate(sorted(df["co"].unique())):
        d = df[df["co"] == co]
        mids = np.arange(30, 1800, 60)
        q = [d.loc[(d.horizon_s >= m - 30) & (d.horizon_s < m + 30), "error_s"].quantile([0.1, 0.5, 0.9])
             if ((d.horizon_s >= m - 30) & (d.horizon_s < m + 30)).sum() >= 30 else pd.Series([np.nan] * 3, index=[0.1, 0.5, 0.9])
             for m in mids]
        q = pd.DataFrame(q)
        c = plots.SERIES[i]
        ax.fill_between(mids / 60, q[0.1] / 60, q[0.9] / 60, color=c, alpha=0.15, lw=0)
        ax.plot(mids / 60, q[0.5] / 60, color=c, label=f"{co} median (band: 10-90%)")
    ax.axhline(0, color=plots.AXIS, lw=1)
    ax.set_xlabel("Prediction horizon (minutes ahead)")
    ax.set_ylabel("Actual minus predicted (minutes)")
    ax.set_title("How wrong the official ETA is, by how far ahead it predicts")
    ax.legend(loc="upper left")
    plots.save(fig, _common.FIGURES / "official_error_by_horizon.png")

    # MAE by route, official vs corrected
    br = pd.DataFrame(breakdown["by_route"]).sort_values("mae_official")
    fig, ax = plt.subplots(figsize=(7, 0.35 * len(br) + 1.2))
    y = np.arange(len(br))
    ax.barh(y + 0.2, br["mae_official"], height=0.38, color=plots.SERIES[1], label="Official ETA")
    ax.barh(y - 0.2, br["mae_corrected"], height=0.38, color=plots.SERIES[0], label="Corrected ETA")
    ax.set_yticks(y, br["group"])
    ax.set_xlabel("Mean absolute error on the test period (seconds)")
    ax.set_title("Error by route")
    ax.legend(loc="lower right")
    plots.save(fig, _common.FIGURES / "mae_by_route.png")

    # coverage by horizon band
    bh = pd.DataFrame(breakdown["by_horizon_band"])
    fig, ax = plt.subplots(figsize=(7, 3.2))
    x = np.arange(len(bh))
    for i, (k, lab) in enumerate((("official", "Official ETA + empirical band"), ("corrected", "Corrected ETA, conformal"))):
        ax.bar(x + (i - 0.5) * 0.38, bh[f"coverage_{k}"], width=0.36, color=plots.SERIES[1 - i], label=lab)
    ax.axhline(0.9, color=plots.INK_2, ls="--", lw=1)
    ax.set_xticks(x, bh["group"])
    ax.set_ylim(0.5, 1.0)
    ax.set_ylabel("Share inside the 90% interval")
    ax.set_title("Interval coverage on the test period, by horizon")
    ax.legend(loc="lower left")
    plots.save(fig, _common.FIGURES / "coverage_by_horizon.png")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=WAREHOUSE)
    ap.add_argument("--out", type=Path, default=None,
                    help="write results, figures and models here instead of the repository folders")
    a = ap.parse_args()
    if a.out:
        _common.set_output(a.out)
    main(a.db)
