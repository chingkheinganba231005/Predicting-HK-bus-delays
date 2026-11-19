"""Linking and arrival inference on hand-built sequences.

Times are seconds; the first case mirrors a real KMB 1A sequence observed on
19 Nov 2026 (bus due 18:35:4x, frozen at 18:35:47 while at the stop, gone by
the 18:37:46 snapshot).
"""

import numpy as np
import pandas as pd
import pytest

from hkbus import ground_truth as gt

T0 = 1_795_000_000


def m(minutes, seconds=0):
    return T0 + 60 * minutes + seconds


def test_real_pattern_single_departure():
    # KMB 1A outbound, 19 Nov 2026, minutes after 18:32 HKT
    ts = np.array([m(0, 16), m(1, 48), m(2, 46), m(3, 46), m(4, 46), m(5, 46)])
    etas = [
        [m(3, 34), m(10, 30), m(25, 30)],
        [m(3, 18), m(10, 19), m(26, 19)],
        [m(3, 29), m(10, 1), m(25, 43)],
        [m(3, 42), m(9, 39), m(25, 43)],
        [m(3, 47), m(9, 24), m(25, 26)],   # bus at the stop: ETA frozen, already in the past
        [m(9, 14), m(25, 42), m(37, 48)],  # gone; the list shifts by one
    ]
    ids, ended = gt.link_stream(ts, etas)
    assert ids[4] == [0, 1, 2] and ids[5] == [1, 2, 3]
    first = next(r for r in ended if r["track"] == 0)
    assert first["end_reason"] == "departed"
    res = gt.arrival_from_track(first)
    assert res["status"] == "arrived" and res["method"] == "frozen"
    assert res["arrival_ts"] == m(3, 47) == res["arrival_lo"] == res["arrival_hi"]


def test_no_departure_when_eta_moves_a_little():
    s, cost = gt.choose_shift([m(5), m(15)], [m(5, 40), m(15, 20)], now=m(1), p=gt.Params())
    assert s == 0 and cost == pytest.approx(30)


def test_a_bus_that_is_not_due_cannot_leave():
    # the first bus is 10 minutes away, so even though the values line up
    # better with a shift, the list cannot have lost it
    s, _ = gt.choose_shift([m(10), m(20), m(30)], [m(20), m(30), m(40)], now=m(0), p=gt.Params())
    assert s == 0


def test_two_buses_leave_together_bunching():
    ts = np.array([m(0), m(1)])
    etas = [[m(0, 50), m(1, 5), m(12)], [m(12, 10), m(24)]]
    ids, ended = gt.link_stream(ts, etas)
    reasons = {r["track"]: r["end_reason"] for r in ended}
    assert reasons[0] == "departed" and reasons[1] == "departed"
    assert ids[1] == [2, 3]


def test_track_that_disappears_while_far_away_is_not_an_arrival():
    rec = {"end_reason": "departed", "last_eta": m(8), "last_seen": m(0), "next_ts": m(1)}
    assert gt.arrival_from_track(rec)["status"] == "not_converged"


def test_bus_shown_as_arriving_long_after_it_vanished_is_rejected():
    rec = {"end_reason": "departed", "last_eta": m(2, 50), "last_seen": m(0, 0), "next_ts": m(0, 30)}
    assert gt.arrival_from_track(rec)["status"] == "left_before_eta"


def test_citybus_window_arrival_is_clipped_into_the_window():
    # real Citybus pattern: last seen 18:39:03 predicting 18:40:45, gone at 18:40:03
    rec = {"end_reason": "departed", "last_eta": m(1, 42), "last_seen": m(0, 0), "next_ts": m(1, 0)}
    res = gt.arrival_from_track(rec)
    assert res["method"] == "window" and res["arrival_ts"] == m(1, 0)
    assert (res["arrival_lo"], res["arrival_hi"]) == (m(0), m(1))
    rec["last_eta"] = m(0, 40)
    assert gt.arrival_from_track(rec)["arrival_ts"] == m(0, 40)


def test_long_frozen_eta_is_a_layover():
    rec = {"end_reason": "departed", "last_eta": m(0), "last_seen": m(15), "next_ts": m(16)}
    assert gt.arrival_from_track(rec)["status"] == "layover"


def test_outage_censors_tracks():
    ts = np.array([m(0), m(10)])  # ten-minute hole in the data
    etas = [[m(1), m(9)], [m(19), m(29)]]
    ids, ended = gt.link_stream(ts, etas)
    assert {r["end_reason"] for r in ended if r["track"] in (0, 1)} == {"outage"}
    assert ids[1] == [2, 3]


def test_collector_gap_censors_tracks():
    ts = np.array([m(0), m(1)])
    etas = [[m(0, 40), m(9)], [m(9), m(20)]]
    ids, ended = gt.link_stream(ts, etas, gaps=[(m(0, 10), m(0, 50))])
    assert ended[0]["end_reason"] == "outage"


def test_list_shrinking_at_end_of_service_is_dropped_not_arrived():
    ts = np.array([m(0), m(1)])
    etas = [[m(5), m(20), m(35)], [m(5, 10), m(20, 5)]]
    ids, ended = gt.link_stream(ts, etas)
    assert next(r for r in ended if r["track"] == 2)["end_reason"] == "dropped"


def test_new_bus_appears_at_the_end():
    ts = np.array([m(0), m(1)])
    etas = [[m(5)], [m(5, 5), m(18)]]
    ids, _ = gt.link_stream(ts, etas)
    assert ids[1] == [0, 1]


def _frame(rows):
    return pd.DataFrame(rows, columns=["co", "route", "dir", "service_type", "stop",
                                       "data_ts", "eta_seq", "eta", "rmk_en"])


def test_infer_and_labels_end_to_end():
    key = ("KMB", "1A", "O", 1, "S1")
    seq = [
        (m(0), [m(3, 30), m(10, 30)]),
        (m(1), [m(3, 18), m(10, 19)]),
        (m(2), [m(3, 42), m(9, 39)]),
        (m(3), [m(3, 47), m(9, 24)]),
        (m(4), [m(3, 47), m(9, 20)]),   # frozen while the bus waits at the stop
        (m(5), [m(9, 14), m(21)]),
    ]
    rows = [(*key, ts, i + 1, e, "") for ts, es in seq for i, e in enumerate(es)]
    rows.append((*key, m(5), 3, None, ""))  # empty slot: ignored
    obs, tracks = gt.infer(_frame(rows))
    lab = gt.labels(obs, tracks)
    first = tracks.loc[tracks.status == "arrived"].iloc[0]
    assert first.arrival_ts == m(3, 47) and first.method == "frozen"
    # labelled predictions for the first bus: published before it arrived,
    # excluding the final (label-defining) snapshot
    mine = lab[lab.track_id == first.track_id].sort_values("data_ts")
    assert list(mine.data_ts) == [m(0), m(1), m(2), m(3)]
    assert list(mine.error_s) == [17, 29, 5, 0]
    assert list(mine.horizon_s) == [210, 138, 102, 47]
    assert (mine.error_lo_s == mine.error_s).all()  # frozen: no window


def test_streams_are_independent():
    rows = []
    for stop in ("A", "B"):
        rows += [("CTB", "1", "O", 1, stop, m(0), 1, m(0, 30), ""),
                 ("CTB", "1", "O", 1, stop, m(1), 1, m(12), "")]
    obs, tracks = gt.infer(_frame(rows))
    assert obs.track_id.nunique() == 4
    assert (tracks.status == "arrived").sum() == 2
