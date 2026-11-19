"""Infer actual arrival times from the sequence of official predictions.

The APIs publish, for each (route, direction, stop), the next three predicted
arrivals ("eta_seq" 1-3). They carry no trip or vehicle identifier, so the
first step is to link predictions across successive snapshots into tracks,
one per bus. The rules are documented in docs/ground_truth.md; in short:

1. Linking. Between two snapshots the list either keeps the same buses
   (shift 0) or loses its first s buses because they have left the stop
   (shift s). The shift is chosen by minimising the mean absolute change
   of the matched ETAs, among shifts where every departing bus was actually
   due (its ETA no later than the new snapshot plus a tolerance). A small
   penalty per departed bus prefers "nobody left" when the evidence is
   ambiguous.
2. Arrival. The two operators behave differently when a bus reaches the
   stop, and both cases are handled:
   - "frozen" (KMB): the bus stays on the list while it is at the stop and
     its ETA stops moving at the time it arrived, so the ETA is already in
     the past when the bus is last seen. The arrival is that frozen value.
   - "window" (Citybus): the bus is removed from the list without its ETA
     ever reaching the past. The arrival lies between the last snapshot that
     showed it (L) and the first that did not (N); the point estimate is the
     last ETA clipped into [L, N], and L and N are kept as bounds.
   A track is only accepted as an arrival if its last prediction had
   converged (no more than ``converged_horizon_s`` ahead of L), the bus did
   not vanish long before its predicted time, the two snapshots around the
   departure are close together and no collector outage falls between them.
   Tracks that end any other way are kept with the reason but get no arrival.
3. Labels. Every earlier prediction of an arrived track gets
   error = actual - predicted (positive: the bus came later than shown).
   The final prediction of each track defines the label and is excluded,
   as is anything published after the bus had arrived.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

STREAM_KEYS = ["co", "route", "dir", "service_type", "stop"]


@dataclass(frozen=True)
class Params:
    max_shift: int = 2
    shift_penalty_s: float = 30.0
    depart_tolerance_s: float = 120.0
    max_match_cost_s: float = 900.0
    converged_horizon_s: float = 180.0
    late_tolerance_s: float = 120.0
    max_snapshot_gap_s: float = 180.0
    max_frozen_s: float = 600.0       # a frozen ETA older than this is a layover, not an arrival


def choose_shift(prev: list[int], cur: list[int], now: int, p: Params) -> tuple[int, float]:
    """Number of buses that left between two snapshots, and the matching cost."""
    best, best_cost = None, np.inf
    for s in range(0, min(p.max_shift, len(prev)) + 1):
        if any(prev[j] > now + p.depart_tolerance_s for j in range(s)):
            break  # a bus that was not yet due cannot have left (and neither can later ones)
        n = min(len(cur), len(prev) - s)
        if n == 0:
            cost = 0.0 if len(cur) == 0 or len(prev) == s else np.inf
        else:
            cost = float(np.mean(np.abs(np.asarray(cur[:n]) - np.asarray(prev[s:s + n]))))
        cost += p.shift_penalty_s * s
        if cost < best_cost:
            best, best_cost = s, cost
    if best is None:
        return 0, np.inf
    return best, best_cost


def link_stream(ts: np.ndarray, etas: list[list[int]], gaps: list[tuple[float, float]] | None = None,
                p: Params = Params()) -> tuple[list[list[int]], list[dict]]:
    """Link one stream's snapshots into tracks.

    ``ts[k]`` is the snapshot time and ``etas[k]`` the predicted arrivals in
    eta_seq order (None values already removed). Returns the track id of
    every entry of every snapshot, and one record per finished track.
    """
    gaps = gaps or []
    next_id = 0
    ids: list[list[int]] = []
    last_eta: dict[int, int] = {}
    ended: list[dict] = []

    def end(track: int, reason: str, last_seen: int, next_ts: int | None):
        ended.append({"track": track, "end_reason": reason, "last_seen": last_seen,
                      "next_ts": next_ts, "last_eta": last_eta[track]})

    for k, (t_now, cur) in enumerate(zip(ts, etas)):
        if k == 0:
            row = list(range(next_id, next_id + len(cur)))
            next_id += len(cur)
        else:
            prev, prev_ids, t_prev = etas[k - 1], ids[k - 1], ts[k - 1]
            outage = (t_now - t_prev > p.max_snapshot_gap_s
                      or any(g0 < t_now and g1 > t_prev for g0, g1 in gaps))
            s, cost = choose_shift(prev, cur, t_now, p)
            if outage or cost > p.max_match_cost_s:
                for tr in prev_ids:
                    end(tr, "outage" if outage else "unmatched", t_prev, t_now)
                row = list(range(next_id, next_id + len(cur)))
                next_id += len(cur)
            else:
                for tr in prev_ids[:s]:
                    end(tr, "departed", t_prev, t_now)
                n = min(len(cur), len(prev) - s)
                row = prev_ids[s:s + n]
                for tr in prev_ids[s + n:]:
                    end(tr, "dropped", t_prev, t_now)
                extra = len(cur) - n
                row = row + list(range(next_id, next_id + extra))
                next_id += extra
        for tr, e in zip(row, cur):
            last_eta[tr] = e
        ids.append(row)
    if ids:
        for tr in ids[-1]:
            end(tr, "open", ts[-1], None)
    return ids, ended


def arrival_from_track(rec: dict, p: Params = Params()) -> dict:
    """Arrival time and bounds of a finished track, or the reason it has none.

    Returns a dict with keys status, method, arrival_ts, arrival_lo, arrival_hi.
    """
    out = {"status": rec["end_reason"], "method": None,
           "arrival_ts": None, "arrival_lo": None, "arrival_hi": None}
    if rec["end_reason"] != "departed":
        return out
    a, last_seen, nxt = rec["last_eta"], rec["last_seen"], rec["next_ts"]
    if a <= last_seen:
        if last_seen - a > p.max_frozen_s:
            out["status"] = "layover"
            return out
        out.update(status="arrived", method="frozen", arrival_ts=float(a),
                   arrival_lo=float(a), arrival_hi=float(a))
        return out
    if a - last_seen > p.converged_horizon_s:
        out["status"] = "not_converged"
        return out
    if a > nxt + p.late_tolerance_s:
        out["status"] = "left_before_eta"
        return out
    out.update(status="arrived", method="window", arrival_ts=float(min(max(a, last_seen), nxt)),
               arrival_lo=float(last_seen), arrival_hi=float(nxt))
    return out


def infer(eta: pd.DataFrame, gaps: pd.DataFrame | None = None, p: Params = Params()) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Track ids for every prediction and one row per track with its arrival.

    ``eta`` has columns STREAM_KEYS + [data_ts, eta_seq, eta, rmk_en].
    Returns (observations, tracks).
    """
    gap_list = [] if gaps is None or gaps.empty else list(zip(gaps.gap_start, gaps.gap_end))
    df = eta.dropna(subset=["eta"]).sort_values(STREAM_KEYS + ["data_ts", "eta_seq"])
    obs_parts, track_parts = [], []
    track_offset = 0
    for key, g in df.groupby(STREAM_KEYS, sort=False):
        snap = g.groupby("data_ts", sort=True)
        ts = np.array(list(snap.groups.keys()), dtype=np.int64)
        etas = [x["eta"].astype(np.int64).tolist() for _, x in snap]
        ids, ended = link_stream(ts, etas, gap_list, p)
        flat = np.concatenate([np.asarray(r, dtype=np.int64) for r in ids]) if ids else np.array([], np.int64)
        part = g.sort_values(["data_ts", "eta_seq"]).copy()
        part["track_id"] = flat + track_offset
        obs_parts.append(part)
        tr = pd.DataFrame(ended)
        if not tr.empty:
            res = pd.DataFrame([arrival_from_track(r, p) for r in ended])
            tr = pd.concat([tr.reset_index(drop=True), res], axis=1)
            tr["track_id"] = tr.pop("track") + track_offset
            for k_name, k_val in zip(STREAM_KEYS, key):
                tr[k_name] = k_val
            track_parts.append(tr)
        track_offset += int(flat.max()) + 1 if len(flat) else 0
    obs = pd.concat(obs_parts, ignore_index=True) if obs_parts else df.assign(track_id=pd.Series(dtype=np.int64))
    tracks = pd.concat(track_parts, ignore_index=True) if track_parts else pd.DataFrame()
    return obs, tracks


def labels(obs: pd.DataFrame, tracks: pd.DataFrame) -> pd.DataFrame:
    """Predictions with a known outcome: error = actual - predicted, in seconds.

    Excludes each track's final prediction (it defines the label) and any
    prediction published at or after the actual arrival.
    """
    arrived = tracks.loc[tracks.status == "arrived",
                         ["track_id", "arrival_ts", "arrival_lo", "arrival_hi", "method", "last_seen"]]
    out = obs.merge(arrived, on="track_id", how="inner")
    out = out[(out.data_ts < out.last_seen) & (out.data_ts < out.arrival_ts)].copy()
    out["horizon_s"] = out["eta"] - out["data_ts"]
    out["error_s"] = out["arrival_ts"] - out["eta"]
    out["error_lo_s"] = out["arrival_lo"] - out["eta"]
    out["error_hi_s"] = out["arrival_hi"] - out["eta"]
    return out.drop(columns=["last_seen"])
