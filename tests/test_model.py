import numpy as np
import pandas as pd
import pytest

from hkbus import model as M


def _frame(n, seed, start=1_795_000_000):
    rng = np.random.default_rng(seed)
    h = rng.uniform(30, 1500, n)
    df = pd.DataFrame({
        "co": rng.choice(["KMB", "CTB"], n), "route": rng.choice(["1", "2"], n), "dir": "O",
        "stop": rng.choice(["a", "b", "c"], n), "horizon_s": h,
        "data_ts": np.sort(rng.integers(start, start + 10 * 86400, n)),
    })
    for c in M.NUMERIC:
        if c not in df:
            df[c] = rng.normal(size=n)
    # heteroscedastic error that grows with horizon, biased late for KMB
    df["error_s"] = rng.normal(0.1 * h * (df.co == "KMB"), 20 + 0.15 * h)
    df["arrival_ts"] = df["data_ts"] + h
    df["service_date"] = pd.to_datetime(df["data_ts"] + 8 * 3600, unit="s").dt.date
    return df


def test_conformal_quantile_model_covers_90_percent(monkeypatch):
    monkeypatch.setitem(M.LGB_PARAMS, "n_estimators", 200)
    df = _frame(12000, 0)
    tr, cal, te = df.iloc[:6000], df.iloc[6000:9000], df.iloc[9000:]
    m = M.QuantileModel(seed=0).fit(tr, cal)
    p = m.predict(te)
    cover = ((te.error_s >= p.lo) & (te.error_s <= p.hi)).mean()
    assert 0.86 < cover < 0.94
    # the corrected median beats predicting zero error
    assert (te.error_s - p.pred).abs().mean() < te.error_s.abs().mean()


def test_empirical_baseline_coverage():
    df = _frame(9000, 1)
    tr, cal, te = df.iloc[:5000], df.iloc[5000:7000], df.iloc[7000:]
    for shift in (False, True):
        p = M.EmpiricalBaseline(shift=shift).fit(tr, cal).predict(te)
        cover = ((te.error_s >= p.lo) & (te.error_s <= p.hi)).mean()
        assert 0.86 < cover < 0.94
    assert (M.EmpiricalBaseline(shift=False).fit(tr, cal).predict(te)["pred"] == 0).all()


def test_time_split_orders_periods_and_embargoes():
    df = _frame(5000, 2)
    split, info = M.time_split(df)
    assert info["mode"] == "days"
    t = df.data_ts
    assert t[split == "train"].max() < t[split == "cal"].min() <= t[split == "cal"].max() < t[split == "test"].min()
    # no training label arrives after the calibration period starts
    assert (df.arrival_ts[split == "train"] < info["cal_start"]).all()
    assert (df.arrival_ts[split == "cal"] < info["test_start"]).all()


def test_prepare_keeps_categories_stable():
    a = pd.DataFrame({"co": ["KMB"], "route": ["1A"], "dir": ["O"], "stop": ["s1"], **{c: [1.0] for c in M.NUMERIC}})
    b = a.assign(stop=["unseen"])
    Xa, cats = M.prepare(a)
    Xb, _ = M.prepare(b, cats)
    assert Xb["stop_key"].isna().all()
    assert list(Xa["route_key"].cat.categories) == list(Xb["route_key"].cat.categories)


def test_subsample_keeps_whole_tracks():
    df = pd.DataFrame({"track_id": np.repeat(np.arange(100), 10), "x": 1})
    sub = M.subsample_tracks(df, 300, seed=0)
    assert len(sub) == 300
    assert (sub.groupby("track_id").size() == 10).all()
    assert len(M.subsample_tracks(df, 5000, seed=0)) == 1000


def test_rider_policy():
    err = np.array([-30.0, 0.0, 60.0, 120.0])
    out = M.rider_policy(err, np.full(4, -10.0))
    assert out["miss_rate"] == pytest.approx(0.25)
    assert out["mean_wait_s"] == pytest.approx((10 + 70 + 130) / 3)


def test_horizon_bands():
    b = M.horizon_band([0, 119, 120, 1199, 5000])
    assert list(b) == ["0-2 min", "0-2 min", "2-5 min", "10-20 min", "20+ min"]
