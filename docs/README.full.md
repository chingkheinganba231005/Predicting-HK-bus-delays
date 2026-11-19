# How reliable are Hong Kong's real-time bus arrival estimates?

{{headline}}

![Official ETA error by horizon](figures/official_error_by_horizon.png)

## Problem

Every minute, KMB and Citybus publish the next three predicted arrivals at
every stop. A passenger deciding when to leave needs to know how far off
"7 min" usually is and a range they can rely on; the feed gives a single
number. This project measures the error of the official ETA by horizon,
route, stop, time of day and weather, and builds a corrected ETA with a
calibrated 90% range.

## Data

| Source | What | Frequency |
|---|---|---|
| KMB real-time arrival API (`data.etabus.gov.hk`) | Next 3 ETAs at every stop of 6 routes (1A, 2, 6C, 271, 960, 68X) | every minute |
| Citybus real-time arrival API v2 (`rt.data.gov.hk`) | Next 3 ETAs at 10 stops per direction of 5 routes (1, 5B, 10, 970, A21) | every minute |
| Hong Kong Observatory open data | Hourly rainfall by district, temperature, humidity, warnings in force | every 10 minutes |
| 1823 public holiday calendar | Hong Kong general holidays | static (`config/`) |

{{n_days}} service days were collected. Tracks by route and how they ended:

{{track_table}}

## Method

1. **Collection** (`src/hkbus/collector.py`): one request per route or stop
   per minute, retries with backoff, raw responses stored compressed and
   deduplicated, gaps recorded. SQLite (WAL) landing store, DuckDB warehouse.
   [docs/collecting.md](docs/collecting.md)
2. **Ground truth** (`src/hkbus/ground_truth.py`): predictions linked into one
   track per bus; KMB arrivals are the ETA frozen while the bus stands at the
   stop, Citybus arrivals lie in the minute between the last snapshot listing
   the bus and the first without it (bounds kept). Every earlier prediction of
   an arrived bus is labelled `error = actual - predicted`.
   [docs/ground_truth.md](docs/ground_truth.md)
3. **Features** (`sql/04_features.sql`): horizon, list position, stop position,
   hour, weekday, holiday, the track's ETA history, the mean error of buses on
   the same route in the last 30 minutes, and weather joined on the time it
   became public. Most important (gain share): {{top_features}}.
4. **Model** (`src/hkbus/model.py`): LightGBM quantile regression (5%, 50%,
   95%) of the error, conformalised per horizon band on a separate
   calibration period.
5. **Evaluation**: time split by service day ({{n_train}} training,
   {{n_cal}} calibration, {{n_test}} test predictions), labels crossing a
   boundary dropped, test used once. 95% CIs from a cluster bootstrap over
   {{n_clusters}} (day, route) blocks.

## Results

{{headline_table}}

The official ETA with an interval built from its own past errors covers
{{coverage_official}} with a mean width of {{width_official}}; the corrected
interval covers {{coverage_corrected}} with a width of {{width_corrected}}.

![MAE by route](figures/mae_by_route.png)

**By route**

{{by_route_table}}

**By time of day**

{{by_time_band_table}}

**By horizon**

{{by_horizon_band_table}}

![Coverage by horizon](figures/coverage_by_horizon.png)

### Minutes at the stop

A rider who checks the app 5–20 minutes ahead and wants at most a 5% chance of
missing the bus arrives at a calibrated lower bound. Using the official ETA
with a buffer learned from its past errors, they miss {{rider_miss_official}}
of buses and wait {{rider_wait_official}} on average; using the corrected
lower bound, they miss {{rider_miss_corrected}} and wait
{{rider_wait_corrected}}.

## Limitations

- Ground truth is inferred from the operators' own data; see
  [docs/field_check.md](docs/field_check.md) for an independent check.
  Citybus arrivals are known to within a minute: scoring them at the start or
  end of their window gives an official MAE of {{label_lo_official}} to
  {{label_hi_official}} and a corrected MAE of {{label_lo_corrected}} to
  {{label_hi_corrected}}.
- Only buses whose track ends cleanly are labelled.
- 11 routes over {{n_days}} days; one season.
- The live dashboard uses the snapshot-only model (no track history).

## Reproduce

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt && pip install -e . --no-deps
pytest -q
make all DB=data/collector.sqlite      # dataset, models, evaluation, README
streamlit run dashboard/app.py
```

## Data sources and terms

Real-time arrival data: The Kowloon Motor Bus Co. (1933) Ltd and Citybus
Limited. Weather: Hong Kong Observatory. Public holidays: 1823, HKSAR
Government. All obtained from DATA.GOV.HK and used under its terms of use,
which allow use with attribution; the Government and the providers own the
intellectual property rights in the data. This project is not affiliated
with the operators.
