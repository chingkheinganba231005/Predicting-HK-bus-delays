# How reliable are Hong Kong's real-time bus arrival estimates?

**Status: data collection in progress.** The collector, the ground-truth
inference, the models, the evaluation and the dashboard are built and tested
on a real 1.7-hour sample. The headline results (official vs
corrected ETA error, interval coverage, minutes saved) will be generated from
2–4 weeks of collected data by `make all`, which rewrites this README from
`results/*.json`. No result is reported before then.

Every minute, KMB and Citybus publish the next three predicted arrivals at
every stop. This project measures how wrong those predictions are,
as a function of how far ahead they look, the route, the stop, the time of
day and the weather, and builds a corrected ETA with a calibrated 90% range.

## Problem

A passenger deciding when to leave for the bus stop needs two things the
official apps do not give: a sense of how far off "7 min" usually is, and a
range they can rely on. The official feed is a single number with no
uncertainty. The question is whether the operators' own feed, plus weather
and recent conditions on the route, can be turned into a more accurate ETA
and an honest interval, and what that is worth in minutes at the stop.

## Data

| Source | What | Frequency |
|---|---|---|
| KMB real-time arrival API (`data.etabus.gov.hk`) | Next 3 ETAs at every stop of 6 routes (1A, 2, 6C, 271, 960, 68X) | every minute |
| Citybus real-time arrival API v2 (`rt.data.gov.hk`) | Next 3 ETAs at 10 stops per direction of 5 routes (1, 5B, 10, 970, A21) | every minute |
| Hong Kong Observatory open data | Hourly rainfall by district, temperature, humidity, warnings in force | every 10 minutes |
| 1823 public holiday calendar | Hong Kong general holidays | static (`config/`) |

All four sources are listed on DATA.GOV.HK. Routes cover urban Kowloon,
Hong Kong Island, cross-harbour, New Territories express and an airport
route.

**Real sample in this repository** (`sample_data/collector_sample.sqlite`):
19 Nov 2026 18:31 to 19 Nov 2026 20:13 HKT, 11,130 bus API requests
with a failure rate of 0.00%, 64,877
distinct predictions, 1,656 finished bus tracks of which
99% resolve to an inferred arrival, giving 43,528
labelled predictions. Tests and CI run the whole pipeline on it.

## Method

### 1. Collection (`src/hkbus/collector.py`)

Polls every route or stop once a minute, aligned to the minute, with
connection pooling, timeouts and exponential-backoff retries. Raw responses
are stored compressed and deduplicated by content hash; parsed predictions
are deduplicated on their natural key (operator, route, direction, stop,
position, operator timestamp), so an operator that has not refreshed its data
adds nothing. Pauses longer than three minutes (sleep, crashes, no network)
are recorded as gaps. SQLite in WAL mode is the landing store because it is
crash-safe and readable while the collector writes; DuckDB is the analysis
warehouse. Instructions for running it unattended on macOS or Windows:
[docs/collecting.md](docs/collecting.md).

### 2. Ground truth: actual arrival times (`src/hkbus/ground_truth.py`)

The APIs give predictions, never actual arrivals, and no trip identifier.
Predictions for the same stop are linked across snapshots into one track
per bus (the list shifts up by one when a bus leaves; the shift is chosen by
matching cost, and only buses that were due may leave). The two operators
then need different rules, found by inspecting the real data:

- **KMB keeps a bus on the list while it is at the stop, with its ETA frozen at
  the moment it arrived.** The frozen value is the arrival time.
- **Citybus removes a bus when it reaches the stop.** The arrival lies between
  the last snapshot that listed it and the first that did not, so it is known
  to within the one-minute polling interval; both bounds are kept.

Tracks that vanish while the bus is still far away, that span an outage, or
that are layovers at a terminus get no label. Each earlier prediction of an
arrived bus gives `error = actual - predicted`. Full reasoning, edge cases and
limitations: [docs/ground_truth.md](docs/ground_truth.md). On the sample:

| Route | Finished tracks | Arrived | List shrank | Censored | Median time from arrival to leaving the list |
|---|---|---|---|---|---|
| CTB 1 | 118 | 100% | 0% | 0% | 0 s |
| CTB 10 | 137 | 100% | 0% | 0% | 2 s |
| CTB 5B | 75 | 100% | 0% | 0% | 0 s |
| CTB 970 | 158 | 99% | 1% | 0% | 6 s |
| CTB A21 | 220 | 99% | 0% | 1% | 12 s |
| KMB 1A | 199 | 100% | 0% | 0% | 105 s |
| KMB 2 | 106 | 100% | 0% | 0% | 104 s |
| KMB 271 | 147 | 100% | 0% | 0% | 104 s |
| KMB 68X | 134 | 96% | 3% | 1% | 105 s |
| KMB 6C | 135 | 100% | 0% | 0% | 103 s |
| KMB 960 | 227 | 100% | 0% | 0% | 107 s |

### 3. Features (`sql/04_features.sql`)

Built in DuckDB SQL from information available when each prediction was
published: horizon, position in the list, stop position along the route,
hour, day of week, holiday; the track's own history (how much the ETA has
moved, how long it has been frozen); the mean error of buses on the same
route that arrived in the previous 30 minutes; and the latest weather and
warnings, joined on the time the collector first saw them (HKO publishes
rainfall with a delay, so joining on the observation time would leak).

### 4. Models (`src/hkbus/model.py`)

LightGBM quantile regression for the 5th, 50th and 95th percentiles of the
error. The corrected ETA is the official ETA plus the predicted median
error. The interval is conformalised on a separate calibration period
(conformalized quantile regression), separately for each horizon band, so
coverage holds for 2-minute and 20-minute predictions alike. Baselines: the
official ETA, and the official ETA plus the median error for its operator and
horizon band, both with empirical calibrated intervals.

### 5. Evaluation (`scripts/02_train_evaluate.py`)

Time-based split by service day: first 60% for training, next 20% for
calibration, last 20% for the test, which is used once. Predictions whose
bus arrived after the next period began are dropped, so no label crosses a
boundary. Mean absolute error, with 95% confidence intervals from a cluster
bootstrap over (day, route) blocks, because one late bus produces many
correlated late predictions. Coverage and width of the 90% interval.
Breakdowns by route, time of day and horizon. Citybus labels are re-scored at
both ends of their arrival window to bound the effect of label uncertainty.

**Business measure.** A rider who wants at most a 5% chance of missing the
bus arrives at the stop at a calibrated lower bound. For the official ETA
that bound is the official time minus a buffer learned from past errors; for
the corrected ETA it is the model's lower bound. At the same miss rate, the
difference in average waiting time is the time saved per trip.

### 6. Dashboard (`dashboard/app.py`)

`streamlit run dashboard/app.py`: reliability heatmap by route and hour,
test results, and a live view that calls the APIs and shows the corrected
ETA, its 90% range and a "be at the stop by" time.

## Limitations

- The ground truth comes from the operators' own data. It is checked for
  internal consistency, but an independent field check
  ([docs/field_check.md](docs/field_check.md)) is the way to rule out a
  systematic offset.
- Only buses whose track ends cleanly are labelled; cancelled or
  short-worked trips are under-represented.
- 11 routes for a few weeks: results describe these routes and this season
  (no typhoon season unless the collection overlaps one).
- The live dashboard uses a model without track-history features, because a
  single snapshot has no history.

## Reproduce

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt && pip install -e . --no-deps
pytest -q                                   # tests, including the sample pipeline
make sample                                 # whole pipeline on the committed sample
python -m hkbus.collector                   # start collecting (docs/collecting.md)
make all                                    # after collection: dataset, models, README
```

## Data sources and terms

Real-time arrival data: The Kowloon Motor Bus Co. (1933) Ltd and Citybus
Limited. Weather: Hong Kong Observatory. Public holidays: 1823, HKSAR
Government. All obtained from DATA.GOV.HK and used under its terms of use,
which allow use with attribution; the Government and the providers own the
intellectual property rights in the data. This project is not affiliated
with the operators.
