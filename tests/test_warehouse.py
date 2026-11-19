"""End-to-end build on the committed real sample, with leakage checks."""

from pathlib import Path

import numpy as np
import pytest

from hkbus import warehouse

SAMPLE = Path(__file__).resolve().parents[1] / "sample_data" / "collector_sample.sqlite"
pytestmark = pytest.mark.skipif(not SAMPLE.exists(), reason="sample database not present")


@pytest.fixture(scope="module")
def con(tmp_path_factory):
    c = warehouse.connect(tmp_path_factory.mktemp("wh") / "w.duckdb")
    warehouse.copy_from_sqlite(c, SAMPLE)
    warehouse.build_ground_truth(c)
    warehouse.build_features(c)
    yield c
    c.close()


def test_features_exist_and_labels_are_consistent(con):
    f = con.sql("SELECT * FROM features").df()
    assert len(f) > 1000
    assert (f.arrival_ts > f.data_ts).all()                      # predictions made before arrival
    np.testing.assert_allclose(f.error_s, f.arrival_ts - f.eta)
    np.testing.assert_allclose(f.horizon_s, f.eta - f.data_ts)
    assert (f.seq > 1).all()                                      # terminus excluded
    assert (f.error_lo_s <= f.error_s).all() and (f.error_s <= f.error_hi_s).all()


def test_weather_is_only_joined_once_public(con):
    bad = con.execute("""
        SELECT count(*) FROM features f
        WHERE f.temp_c IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM raw_weather_obs w
            WHERE w.kind = 'temp_c' AND w.place = 'Hong Kong Observatory'
              AND w.fetched_at <= f.data_ts AND w.value = f.temp_c)
    """).fetchall()[0][0]
    assert bad == 0


def test_recent_route_error_uses_only_past_arrivals(con):
    f = con.sql("SELECT * FROM features WHERE recent_arrivals_30m > 0 USING SAMPLE 40 ROWS").df()
    lab = con.sql("SELECT co, route, dir, track_id, arrival_ts, avg(error_s) AS e FROM labels "
                  "GROUP BY ALL").df()
    for _, r in f.iterrows():
        m = lab[(lab.co == r.co) & (lab.route == r.route) & (lab.dir == r.dir)
                & (lab.arrival_ts < r.data_ts) & (lab.arrival_ts >= r.data_ts - 1800)]
        assert len(m) == r.recent_arrivals_30m
        assert m.e.mean() == pytest.approx(r.recent_error_30m_s)
        assert r.track_id not in set(m.track_id)


def test_track_history_features_look_backwards(con):
    f = con.sql("SELECT track_id, data_ts, n_prev_obs, eta, eta_change_s FROM features ORDER BY track_id, data_ts").df()
    first = f.groupby("track_id").head(1)
    assert first.eta_change_s.isna().all()
