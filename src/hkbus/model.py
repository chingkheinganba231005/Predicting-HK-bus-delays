"""Correct the official ETA and attach a calibrated prediction interval.

Target: error = actual arrival - official ETA (seconds). The corrected ETA is
official ETA + predicted median error; the interval comes from quantile
gradient boosting, then conformalised on a separate calibration period
(conformalized quantile regression, Romano, Patterson and Candes, 2019),
separately for each prediction-horizon band so that coverage holds for
short and long horizons alike (a "Mondrian" split).

Baselines, both calibrated on the same periods:
  official:     error prediction 0; interval = official ETA plus the
                calibration-period quantiles of the error in each horizon band
  bias_horizon: median error by operator and horizon band, learned on the
                training period, with the same kind of empirical interval
"""

from __future__ import annotations

from dataclasses import dataclass, field

import lightgbm as lgb
import numpy as np
import pandas as pd

ALPHA = 0.10                       # 90% intervals
QUANTILES = (ALPHA / 2, 0.5, 1 - ALPHA / 2)
HORIZON_BANDS = [0, 120, 300, 600, 1200, np.inf]
HORIZON_LABELS = ["0-2 min", "2-5 min", "5-10 min", "10-20 min", "20+ min"]

CATEGORICAL = ["co", "route_key", "stop_key"]
NUMERIC = [
    "horizon_s", "eta_seq", "seq", "stop_frac", "scheduled",
    "hour", "minute_of_day", "dow", "non_working_day",
    "n_prev_obs", "eta_change_s", "since_prev_obs_s", "eta_drift_s", "first_horizon_s",
    "unchanged_streak", "recent_arrivals_30m", "recent_error_30m_s",
    "rain_max_mm", "rain_mean_mm", "temp_c", "rainstorm_warning", "typhoon_signal", "n_warnings",
]
# Features that need the history of a track or of the route. The dashboard's
# live view only sees one snapshot, so it uses a model without them.
HISTORY = ["n_prev_obs", "eta_change_s", "since_prev_obs_s", "eta_drift_s", "first_horizon_s",
           "unchanged_streak", "recent_arrivals_30m", "recent_error_30m_s"]

LGB_PARAMS = dict(n_estimators=2000, learning_rate=0.05, num_leaves=31, min_child_samples=200,
                  subsample=0.8, subsample_freq=1, colsample_bytree=0.8, reg_lambda=1.0,
                  verbose=-1, n_jobs=4)
EARLY_STOPPING = 50
# Predictions of the same bus a minute apart are highly correlated, so the
# quantile models are trained on a random subset of whole tracks. Calibration
# and testing always use every row.
MAX_TRAIN_ROWS = 2_000_000


def subsample_tracks(df: pd.DataFrame, max_rows: int, seed: int) -> pd.DataFrame:
    if len(df) <= max_rows:
        return df
    tracks = df["track_id"].unique()
    rng = np.random.default_rng(seed)
    frac = max_rows / len(df)
    keep = rng.choice(tracks, size=max(1, int(frac * len(tracks))), replace=False)
    return df[df["track_id"].isin(keep)]


def horizon_band(h: pd.Series | np.ndarray) -> pd.Categorical:
    return pd.cut(np.asarray(h, dtype=float), HORIZON_BANDS, labels=HORIZON_LABELS, right=False)


def prepare(df: pd.DataFrame, categories: dict[str, list] | None = None) -> tuple[pd.DataFrame, dict]:
    """Model matrix with stable categorical codes (unseen levels become missing)."""
    X = df.copy()
    X["route_key"] = X["co"] + ":" + X["route"] + ":" + X["dir"]
    X["stop_key"] = X["co"] + ":" + X["stop"]
    cats = categories or {c: sorted(X[c].dropna().unique().tolist()) for c in CATEGORICAL}
    for c in CATEGORICAL:
        X[c] = pd.Categorical(X[c].where(X[c].isin(cats[c])), categories=cats[c])
    for c in NUMERIC:
        X[c] = pd.to_numeric(X[c], errors="coerce").astype(float)
    return X[CATEGORICAL + NUMERIC], cats


def time_split(df: pd.DataFrame, train_frac: float = 0.6, cal_frac: float = 0.2) -> tuple[pd.Series, dict]:
    """Assign 'train' / 'cal' / 'test' by the time a prediction was published.

    With at least five service days the boundaries fall on midnights (Hong
    Kong time) so every period contains whole days; with less data they fall
    on time quantiles. Predictions whose bus arrived after the next period had
    started are dropped (embargo), so no label straddles a boundary.
    """
    dates = np.sort(df["service_date"].unique())
    if len(dates) >= 5:
        n_tr = max(1, int(round(train_frac * len(dates))))
        n_cal = max(1, int(round(cal_frac * len(dates))))
        start = lambda d: int(pd.Timestamp(d).timestamp()) - 8 * 3600  # local midnight in UTC
        b1, b2 = start(dates[n_tr]), start(dates[min(n_tr + n_cal, len(dates) - 1)])
        mode = "days"
    else:
        b1, b2 = (int(x) for x in np.quantile(df["data_ts"], [train_frac, train_frac + cal_frac]))
        mode = "time_quantiles"
    split = pd.Series(np.where(df["data_ts"] < b1, "train", np.where(df["data_ts"] < b2, "cal", "test")),
                      index=df.index)
    crosses = ((split == "train") & (df["arrival_ts"] >= b1)) | ((split == "cal") & (df["arrival_ts"] >= b2))
    split[crosses] = "embargo"
    return split, {"mode": mode, "cal_start": b1, "test_start": b2, "n_dates": int(len(dates))}


def _conformal_quantile(scores: np.ndarray, alpha: float) -> float:
    n = len(scores)
    if n == 0:
        return 0.0
    level = min(1.0, np.ceil((n + 1) * (1 - alpha)) / n)
    return float(np.quantile(scores, level, method="higher"))


@dataclass
class BandConformal:
    """Per-horizon-band additive corrections for a lower and an upper bound."""

    alpha: float = ALPHA
    q: dict = field(default_factory=dict)

    def fit(self, lo: np.ndarray, hi: np.ndarray, y: np.ndarray, band: pd.Categorical):
        overall = _conformal_quantile(np.maximum(lo - y, y - hi), self.alpha)
        for b in HORIZON_LABELS:
            m = np.asarray(band == b)
            self.q[b] = _conformal_quantile(np.maximum(lo[m] - y[m], y[m] - hi[m]), self.alpha) if m.sum() >= 30 else overall
        self.q["_overall"] = overall
        return self

    def apply(self, lo, hi, band):
        adj = np.array([self.q.get(b, self.q["_overall"]) for b in np.asarray(band)], dtype=float)
        return lo - adj, hi + adj


@dataclass
class QuantileModel:
    """LightGBM median and interval quantiles, conformalised per horizon band."""

    features: list[str] = field(default_factory=lambda: CATEGORICAL + NUMERIC)
    seed: int = 0
    models: dict = field(default_factory=dict)
    categories: dict = field(default_factory=dict)
    conformal: BandConformal = field(default_factory=BandConformal)
    lower_one_sided: dict = field(default_factory=dict)

    def fit(self, train: pd.DataFrame, cal: pd.DataFrame, early_stop_frac: float = 0.15):
        train = subsample_tracks(train, MAX_TRAIN_ROWS, self.seed)
        Xtr, self.categories = prepare(train)
        Xtr = Xtr[self.features]
        y = train["error_s"].to_numpy(float)
        # last part of the training period for early stopping (never the calibration set)
        order = np.argsort(train["data_ts"].to_numpy())
        cut = int(len(order) * (1 - early_stop_frac))
        fit_idx, es_idx = order[:cut], order[cut:]
        for q in QUANTILES:
            m = lgb.LGBMRegressor(objective="quantile", alpha=q, random_state=self.seed, **LGB_PARAMS)
            m.fit(Xtr.iloc[fit_idx], y[fit_idx], eval_X=(Xtr.iloc[es_idx],), eval_y=(y[es_idx],),
                  callbacks=[lgb.early_stopping(EARLY_STOPPING, verbose=False)])
            self.models[q] = m
        pc = self.predict_raw(cal)
        yc = cal["error_s"].to_numpy(float)
        band = horizon_band(cal["horizon_s"])
        self.conformal.fit(pc[QUANTILES[0]], pc[QUANTILES[2]], yc, band)
        # one-sided lower bound for the rider policy (5% chance of the bus coming earlier)
        for b in HORIZON_LABELS:
            m = np.asarray(band == b)
            s = pc[QUANTILES[0]][m] - yc[m]
            self.lower_one_sided[b] = _conformal_quantile(s, ALPHA / 2) if m.sum() >= 30 else 0.0
        return self

    def predict_raw(self, df: pd.DataFrame) -> dict:
        X, _ = prepare(df, self.categories)
        X = X[self.features]
        out = {q: m.predict(X) for q, m in self.models.items()}
        # quantile crossing is rare but possible; sort to keep lo <= median <= hi
        stacked = np.sort(np.vstack([out[q] for q in QUANTILES]), axis=0)
        return dict(zip(QUANTILES, stacked))

    def predict(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.predict_raw(df)
        band = horizon_band(df["horizon_s"])
        lo, hi = self.conformal.apply(p[QUANTILES[0]], p[QUANTILES[2]], band)
        lower = p[QUANTILES[0]] - np.array([self.lower_one_sided.get(b, 0.0) for b in np.asarray(band)])
        return pd.DataFrame({"pred": p[0.5], "lo": lo, "hi": hi, "lower95": lower}, index=df.index)


@dataclass
class EmpiricalBaseline:
    """Error quantiles by group (operator x horizon band), optionally with a median shift."""

    shift: bool = True
    table: pd.DataFrame | None = None
    conformal: BandConformal = field(default_factory=BandConformal)
    lower_one_sided: dict = field(default_factory=dict)

    @staticmethod
    def _keys(df):
        return pd.DataFrame({"co": df["co"].to_numpy(), "band": np.asarray(horizon_band(df["horizon_s"]))})

    def fit(self, train: pd.DataFrame, cal: pd.DataFrame):
        k = self._keys(train)
        k["e"] = train["error_s"].to_numpy(float)
        g = k.groupby(["co", "band"], observed=True)["e"]
        self.table = pd.DataFrame({
            "med": g.median() if self.shift else 0.0,
            "lo": g.quantile(QUANTILES[0]), "hi": g.quantile(QUANTILES[2]),
        })
        p = self._raw(cal)
        yc = cal["error_s"].to_numpy(float)
        band = horizon_band(cal["horizon_s"])
        self.conformal.fit(p["lo"], p["hi"], yc, band)
        for b in HORIZON_LABELS:
            m = np.asarray(band == b)
            self.lower_one_sided[b] = _conformal_quantile(p["lo"][m] - yc[m], ALPHA / 2) if m.sum() >= 30 else 0.0
        return self

    def _raw(self, df):
        k = self._keys(df)
        j = k.join(self.table, on=["co", "band"])
        fill = {"med": 0.0, "lo": float(self.table["lo"].min()), "hi": float(self.table["hi"].max())}
        return {c: j[c].fillna(v).to_numpy(float) for c, v in fill.items()}

    def predict(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self._raw(df)
        band = horizon_band(df["horizon_s"])
        lo, hi = self.conformal.apply(p["lo"], p["hi"], band)
        lower = p["lo"] - np.array([self.lower_one_sided.get(b, 0.0) for b in np.asarray(band)])
        return pd.DataFrame({"pred": p["med"], "lo": lo, "hi": hi, "lower95": lower}, index=df.index)


def rider_policy(err: np.ndarray, lower: np.ndarray) -> dict:
    """A rider aims to be at the stop at official ETA + lower (seconds).

    The bus is missed if it arrives before that time; otherwise the rider
    waits (actual - planned). ``err`` is actual - official ETA.
    """
    missed = err < lower
    wait = np.where(missed, np.nan, err - lower)
    return {"miss_rate": float(missed.mean()), "mean_wait_s": float(np.nanmean(wait)) if (~missed).any() else None}
