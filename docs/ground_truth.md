# Inferring actual arrival times from predictions

The ETA APIs publish predictions, never actual arrivals, and they carry no
trip or vehicle identifier. This document explains how the actual arrival
time of each bus at each stop is reconstructed from the stream of
predictions, why the rule is defensible, and where it can be wrong. The code
is `src/hkbus/ground_truth.py`; the tests are `tests/test_ground_truth.py`.

## What the APIs show

For every (route, direction, stop) the operator lists the next three buses
(`eta_seq` 1–3) with a predicted arrival time and the time the prediction was
made (`data_timestamp`). Polling every minute gives one snapshot per minute.
A real KMB 1A sequence (19 Nov 2026, HKT):

| snapshot | 1st bus | 2nd bus | 3rd bus |
|---|---|---|---|
| 18:32:16 | 18:35:34 | 18:42:30 | 18:57:30 |
| 18:33:48 | 18:35:18 | 18:42:19 | 18:58:19 |
| 18:34:46 | 18:35:29 | 18:42:01 | 18:57:43 |
| 18:35:46 | 18:35:42 | 18:41:39 | 18:57:43 |
| 18:36:46 | **18:35:47** | 18:41:24 | 18:57:26 |
| 18:37:46 | 18:41:14 | 18:57:42 | 19:09:48 |

At 18:36:46 the first bus is still listed with an ETA of 18:35:47, a minute
in the past: it is standing at the stop and its ETA has frozen at the moment
it arrived. One minute later it has left, and the whole list moves up by one.

Citybus behaves differently: a bus is removed from the list without its ETA
ever reaching the past. In the first hours of collection, KMB's last shown
ETA was a median 48 s *before* the snapshot (the bus was at the stop), while
Citybus's was a median 40 s *after* it.

## Step 1: link predictions into tracks

Between two consecutive snapshots of one stop, either the same buses are
listed (shift 0) or the first `s` buses have left and the rest moved up
(shift `s`). For each candidate shift the matched ETAs are compared, and the
shift with the smallest mean absolute change is chosen, subject to:

- **A bus can only leave if it was due**: its ETA was no more than 2 minutes
  after the new snapshot. This stops the matcher from "explaining" a
  timetable change by deleting a bus that was 10 minutes away.
- **Prefer "nobody left" when unsure**: each departed bus adds a 30 s penalty.
- At most two buses can leave in one minute (bunching happens; three does not
  in one minute at a single stop).
- Snapshots more than 3 minutes apart, or with a collector outage between
  them, end all tracks as censored instead of guessing.
- If even the best shift changes the ETAs by more than 15 minutes on
  average, the match is declared unreliable and all tracks are restarted.

Each chain of matched predictions is a **track**: one bus approaching one stop.

## Step 2: decide when the bus arrived

A track that leaves the list between snapshot `L` (last seen) and `N` (first
snapshot without it) with last ETA `a`:

| case | condition | arrival | bounds |
|---|---|---|---|
| frozen (KMB) | `a <= L` | `a` | `[a, a]` |
| window (Citybus, some KMB) | `L < a <= L + 3 min` and `a <= N + 2 min` | `a` clipped to `[L, N]` | `[L, N]` |
| layover | `a <= L - 10 min` | none: the bus was parked (terminus) | |
| not converged | `a > L + 3 min` | none: it vanished while still far away | |
| left before ETA | `a > N + 2 min` | none | |

Tracks that end because the list shrank (end of service), because of an
outage, or that are still open at the end of the data have no arrival.

**Why the frozen value is the arrival time.** While a KMB bus waits at the
stop its ETA stays constant at a time already past, and it only drops off
the list when it leaves. The constant value is the time the operator's
system registered the bus at the stop, from the vehicle's position.

**Why the window rule.** Citybus removes a bus once it reaches the stop, so
the arrival happened after the last snapshot that listed it and no later
than the first that did not. With one-minute polling this pins it down to
within 60 s. The last ETA is used as the point estimate when it lies inside
the window, otherwise the nearest edge.

## Step 3: labels

Every earlier prediction of an arrived track is a labelled example:

```
horizon = predicted arrival - time of prediction
error   = actual arrival - predicted arrival   (positive: the bus came late)
```

The final prediction of each track is excluded (it defines the label), as is
anything published after the arrival. The first stop of each route is
excluded from modelling: there the "ETA" is a departure from the terminus,
which is a different quantity.

## Limitations and how they are handled

1. **The label is derived from the operator's own data.** The frozen ETA is
   the operator's record of when the bus arrived, not an independent
   measurement. It comes from vehicle positioning, so it should be accurate
   to seconds, but a systematic offset (for example the system registering
   arrival 100 m before the stop) would shift every label. A field check
   resolves this: stand at a stop, note the time 20 or so buses actually stop,
   and compare with the inferred arrivals. `docs/field_check.md` explains how.
2. **Citybus labels are interval-censored.** The evaluation reports the mean
   absolute error with Citybus arrivals set to the start and to the end of
   each window, which bounds how much this uncertainty can move the headline.
3. **Selection.** Only buses whose track ends cleanly get labels. Buses that
   are cancelled, short-worked or lost in an outage are not represented, and
   those may be the least predictable ones. The share of tracks in each end
   state is reported (`sql/03_track_status.sql`).
4. **Bunching and overtaking.** If two buses swap order between snapshots the
   matcher can link the wrong ones. Overtaking is rare on the same route at a
   single stop, and the cost-based matcher is robust to the common case of
   two buses arriving together.
