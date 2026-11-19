# How Reliable Are Hong Kong's Bus Arrival Times?

{{headline}}

![Official ETA error by horizon](figures/official_error_by_horizon.png)

## Why it matters

KMB and Citybus publish a predicted arrival time for the next three buses at
every stop, every minute. When the app says "7 min", it doesn't say how often
that turns into 10. This project measures how far off the official times
are, by how far ahead they look, route, stop, time of day and weather, and
builds a corrected time with a calibrated 90% range.

## Data

| Source | What | How often |
|---|---|---|
| KMB real-time arrival API (`data.etabus.gov.hk`) | next 3 arrivals at every stop of routes 1A, 2, 6C, 271, 960 and 68X | every minute |
| Citybus real-time arrival API v2 (`rt.data.gov.hk`) | next 3 arrivals at 10 stops per direction of routes 1, 5B, 10, 970 and A21 | every minute |
| Hong Kong Observatory | hourly rainfall by district, temperature, humidity, warnings in force | every 10 minutes |
| 1823 public holiday calendar | Hong Kong general holidays | saved in `config/` |

I collected {{n_days}} service days. How each bus track ended, by route:

{{track_table}}

## How it works

1. **Collecting** (`src/hkbus/collector.py`): one request per route or stop
   per minute, retries with backoff, compressed and deduplicated raw
   responses, recorded gaps. SQLite while collecting, DuckDB for analysis.
   See [docs/collecting.md](docs/collecting.md).
2. **Actual arrival times** (`src/hkbus/ground_truth.py`): predictions are
   linked into one track per bus. For KMB the arrival is the predicted time
   that freezes while the bus waits at the stop. For Citybus it lies in the
   minute between the last snapshot showing the bus and the first without
   it. See [docs/ground_truth.md](docs/ground_truth.md).
3. **Features** (`sql/04_features.sql`): horizon, list position, stop
   position, hour, weekday, holidays, the bus's own prediction history, the
   recent error on the same route and the weather, all as known at the time.
   The most important by gain: {{top_features}}.
4. **Model** (`src/hkbus/model.py`): LightGBM quantile regression of the
   error, with the 90% range calibrated by conformal prediction for each
   horizon band.
5. **Evaluation**: split by service day ({{n_train}} training, {{n_cal}}
   calibration and {{n_test}} test predictions), labels crossing a boundary
   dropped, test used once. Intervals from a bootstrap over {{n_clusters}}
   (day, route) blocks.

## Results

{{headline_table}}

The official time with a range built from its own past errors covers
{{coverage_official}} of arrivals with an average width of {{width_official}}.
The corrected range covers {{coverage_corrected}} with a width of
{{width_corrected}}.

![MAE by route](figures/mae_by_route.png)

By route:

{{by_route_table}}

By time of day:

{{by_time_band_table}}

By how far ahead the prediction looks:

{{by_horizon_band_table}}

![Coverage by horizon](figures/coverage_by_horizon.png)

### Time at the stop

A rider who checks the app 5–20 minutes ahead and wants at most a 5% chance
of missing the bus has to allow a buffer. Using the official time with a
buffer learned from its past errors, they miss {{rider_miss_official}} of
buses and wait {{rider_wait_official}} on average. Using the corrected lower
bound, they miss {{rider_miss_corrected}} and wait {{rider_wait_corrected}}.

## Limitations

- The arrival times are inferred from the operators' own data; see
  [docs/field_check.md](docs/field_check.md) for an independent check.
  Citybus arrivals are only known to within a minute. Scoring them at the
  start or the end of that minute gives an official error of
  {{label_lo_official}} to {{label_hi_official}} and a corrected error of
  {{label_lo_corrected}} to {{label_hi_corrected}}.
- Only buses whose track ends cleanly are labelled.
- 11 routes over {{n_days}} days, one season.
- The live dashboard uses the model without bus-history features.

## Reproducing

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt && pip install -e . --no-deps
pytest -q
make all DB=data/collector.sqlite      # dataset, models, evaluation, README
streamlit run dashboard/app.py
```

## Data sources and terms

Real-time arrival data from The Kowloon Motor Bus Co. (1933) Ltd and Citybus
Limited, weather from the Hong Kong Observatory, and public holidays from
1823 (HKSAR Government), all obtained through DATA.GOV.HK and used under its
terms of use, which require attribution. The Government and the providers
own the intellectual property rights in the data. This project is not
affiliated with the operators.
