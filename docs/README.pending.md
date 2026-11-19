# How Reliable Are Hong Kong's Bus Arrival Times?

KMB and Citybus publish a predicted arrival time for the next three buses at
every stop, refreshed every minute. This project measures how far off those
predictions are (by how far ahead they look, route, stop, time of day and
weather) and builds a corrected arrival time with a calibrated 90% range.

**Where it stands:** the collector, the ground-truth logic, the models, the
evaluation and the dashboard are finished and tested on a real
{{sample_hours}}-hour sample. The results need 2–4 weeks of collected data.
When the collection is done, `make all` reruns everything and rewrites this
README from `results/*.json`. Until then it reports no model results.

## Why it matters

When the app says "7 min", it doesn't say how often that turns into 10. A
rider deciding when to leave home has no idea how much buffer to allow. A
corrected time with an honest range would let them leave later without
missing the bus more often. The evaluation measures exactly that, in minutes
spent waiting at the stop.

## Data

| Source | What | How often |
|---|---|---|
| KMB real-time arrival API (`data.etabus.gov.hk`) | next 3 arrivals at every stop of routes 1A, 2, 6C, 271, 960 and 68X | every minute |
| Citybus real-time arrival API v2 (`rt.data.gov.hk`) | next 3 arrivals at 10 stops per direction of routes 1, 5B, 10, 970 and A21 | every minute |
| Hong Kong Observatory | hourly rainfall by district, temperature, humidity, warnings in force | every 10 minutes |
| 1823 public holiday calendar | Hong Kong general holidays | saved in `config/` |

All four are listed on DATA.GOV.HK. The routes cover urban Kowloon, Hong Kong
Island, cross-harbour, New Territories express and the airport.

The sample in `sample_data/collector_sample.sqlite` runs from {{sample_start}}
to {{sample_end}} HKT: {{sample_requests}} API requests ({{sample_error_rate}}
failed), {{sample_predictions}} distinct predictions and {{sample_tracks}}
finished bus tracks. {{sample_arrived}} of those tracks give an inferred
arrival time, which makes {{sample_labels}} labelled predictions. The tests
and CI run the whole pipeline on it.

## How it works

### Collecting

`src/hkbus/collector.py` polls each route or stop once a minute, with
timeouts and retries with backoff. Raw responses are stored compressed and
only once per distinct content. Predictions are deduplicated on operator,
route, direction, stop, position in the list and the operator's own
timestamp, so an unchanged feed adds nothing. Any pause longer than three
minutes (laptop asleep, crash, no network) is recorded as a gap. The
collector writes to SQLite in WAL mode, which survives crashes and can be
read while it runs; the analysis copies everything into DuckDB.
[docs/collecting.md](docs/collecting.md) explains how to leave it running
for weeks on macOS or Windows.

### Working out when buses actually arrived

The APIs only give predictions, and there is no trip or vehicle ID. I link
the predictions for each stop from one minute to the next into one track per
bus: when a bus leaves, the list shifts up by one. The shift with the
smallest change in times wins, and only a bus that was due can leave. The two
operators then need different rules, which I found by looking at the raw
data:

- KMB keeps a bus on the list while it waits at the stop, with its predicted
  time frozen at the moment it arrived. That frozen time is the arrival.
- Citybus removes a bus as soon as it reaches the stop. The arrival is
  somewhere between the last snapshot that listed it and the first that
  didn't, so it is known to within the one-minute polling interval. I keep
  both ends of that window.

Tracks that vanish while the bus is still far away, that cross a gap in the
data, or that are buses parked at the terminus get no label. Every earlier
prediction for a bus that did arrive becomes a labelled example with
`error = actual - predicted`. The details and edge cases are in
[docs/ground_truth.md](docs/ground_truth.md). On the sample:

{{track_table}}

### Features

Built in DuckDB SQL (`sql/04_features.sql`), using only what was known when
each prediction was published: how far ahead it looks, its position in the
list, the stop's position on the route, hour, weekday and holidays, how much
this bus's predicted time has already moved, the average error of buses on
the same route over the previous 30 minutes, and the latest weather and
warnings. Weather is joined on the time the collector first saw it, not the
time it refers to. HKO publishes hourly rainfall late, so joining on the
observation time would use data nobody had yet.

### Models

LightGBM quantile regression for the 5th, 50th and 95th percentiles of the
error (`src/hkbus/model.py`). The corrected time is the official time plus
the predicted median error. The 5–95% range is then calibrated on a separate
period with conformal prediction, separately for short and long horizons, so
the range holds for "2 min" and "20 min" alike. The baselines are the official
time itself, and the official time plus the typical error for that operator
and horizon.

### Evaluation

Split by service day: the first 60% for training, the next 20% for
calibration, the last 20% for the test, used once. Predictions whose bus
arrived after the next period had started are dropped, so no label crosses a
boundary. I report mean absolute error with 95% intervals from a bootstrap
over (day, route) blocks, since one late bus produces many correlated late
predictions, plus coverage and width of the 90% range, broken down by route,
time of day and horizon. Citybus arrivals are re-scored at both ends of their
one-minute window to show how much that uncertainty matters.

The rider measure: someone who wants at most a 5% chance of missing the bus
arrives at a calibrated lower bound. With the official time that means the
official time minus a buffer learned from past errors; with the corrected
time it is the model's lower bound. At the same miss rate, the difference in
average waiting time is the time saved per trip.

### Dashboard

`streamlit run dashboard/app.py` shows reliability by route and hour, the
test results, and a live view that calls the APIs and shows the corrected
time, its 90% range and a "be at the stop by" time.

## Limitations

- The arrival times come from the operators' own data. They are checked for
  consistency, but only an independent check
  ([docs/field_check.md](docs/field_check.md)) can rule out a systematic
  offset.
- Only buses whose track ends cleanly are labelled, so cancelled or
  short-worked trips are under-represented.
- 11 routes over a few weeks in one season. Typhoon-season behaviour is only
  covered if the collection happens to include it.
- The live dashboard uses a model without the bus-history features, because
  a single snapshot has no history.

## Reproducing

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt && pip install -e . --no-deps
pytest -q                     # tests, including the sample pipeline
make sample                   # the whole pipeline on the committed sample
python -m hkbus.collector     # start collecting (see docs/collecting.md)
make all                      # after the collection: dataset, models, README
```

## Data sources and terms

Real-time arrival data from The Kowloon Motor Bus Co. (1933) Ltd and Citybus
Limited, weather from the Hong Kong Observatory, and public holidays from
1823 (HKSAR Government), all obtained through DATA.GOV.HK and used under its
terms of use, which require attribution. The Government and the providers
own the intellectual property rights in the data. This project is not
affiliated with the operators.
