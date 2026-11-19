-- Feature table: one row per labelled prediction.
-- Every feature uses only information available when the prediction was
-- published (data_ts): track history up to that snapshot, weather and
-- warnings observed before it, and buses on the same route that had
-- already arrived.
CREATE OR REPLACE TABLE features AS
WITH
stops AS (
    -- latest route-stop list: position of each stop along its route
    SELECT company AS co, route, dir, service_type, seq, stop,
           seq / max(seq) OVER (PARTITION BY company, route, dir, service_type) AS stop_frac
    FROM raw_route_stop
    QUALIFY fetched_at = max(fetched_at) OVER (PARTITION BY company, route, dir, service_type)
),
track_hist AS (
    SELECT
        l.*,
        row_number() OVER w - 1                          AS n_prev_obs,
        l.eta - lag(l.eta) OVER w                        AS eta_change_s,
        l.data_ts - lag(l.data_ts) OVER w                AS since_prev_obs_s,
        l.eta - first_value(l.eta) OVER w                AS eta_drift_s,
        first_value(l.horizon_s) OVER w                  AS first_horizon_s,
        CASE WHEN l.eta = lag(l.eta) OVER w THEN 0 ELSE 1 END AS eta_moved
    FROM labels l
    -- the first stop is the terminus: its "ETA" is a departure time, so it is
    -- left out of the modelling data
    WHERE l.seq > 1
    WINDOW w AS (PARTITION BY l.track_id ORDER BY l.data_ts)
),
track_streak AS (
    SELECT *,
        sum(eta_moved) OVER (PARTITION BY track_id ORDER BY data_ts) AS move_group
    FROM track_hist
),
with_streak AS (
    SELECT *,
        row_number() OVER (PARTITION BY track_id, move_group ORDER BY data_ts) - 1 AS unchanged_streak
    FROM track_streak
),
-- running totals of completed arrivals per route direction, for the
-- "recent delay on this route" feature (two ASOF lookups give a 30-minute window)
arrivals AS (
    SELECT co, route, dir, track_id, arrival_ts, avg(error_s) AS track_error_s
    FROM labels
    GROUP BY co, route, dir, track_id, arrival_ts
),
arrival_cum AS (
    SELECT co, route, dir, arrival_ts,
           sum(track_error_s) OVER w AS cum_err,
           count(*) OVER w           AS cum_n
    FROM arrivals
    WINDOW w AS (PARTITION BY co, route, dir ORDER BY arrival_ts, track_id
                 ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
),
-- one row per arrival time, so the ASOF lookups below never land on a
-- partial total when two buses arrive in the same second
arrival_totals AS (
    SELECT co, route, dir, arrival_ts, max(cum_n) AS cum_n, arg_max(cum_err, cum_n) AS cum_err
    FROM arrival_cum
    GROUP BY co, route, dir, arrival_ts
),
-- weather is joined on the time the collector first saw each observation,
-- not the time it refers to: HKO publishes the past hour's rainfall with a
-- delay, and joining on obs_ts would use data that was not yet public
rain AS (
    SELECT min(fetched_at) AS available_at, max(value) AS rain_max_mm, avg(value) AS rain_mean_mm
    FROM raw_weather_obs WHERE kind = 'rain_mm_hour' GROUP BY obs_ts
),
temp AS (
    SELECT fetched_at AS available_at, value AS temp_c
    FROM raw_weather_obs WHERE kind = 'temp_c' AND place = 'Hong Kong Observatory'
),
warn AS (
    SELECT fetched_at, codes,
           CASE WHEN codes LIKE '%WRAIN%' THEN 1 ELSE 0 END   AS rainstorm_warning,
           CASE WHEN codes LIKE '%WTCSGNL%' THEN 1 ELSE 0 END AS typhoon_signal,
           CASE WHEN codes = '' THEN 0 ELSE length(codes) - length(replace(codes, ',', '')) + 1 END AS n_warnings
    FROM raw_warning_snapshot
),
base AS (
    SELECT
        h.*,
        epoch_ms((h.data_ts + 8 * 3600) * 1000)          AS local_ts,
        s.stop_frac
    FROM with_streak h
    LEFT JOIN stops s USING (co, route, dir, service_type, stop)
),
recent_now AS (
    SELECT b.track_id, b.data_ts, c.cum_err, c.cum_n
    FROM base b
    ASOF LEFT JOIN arrival_totals c
      ON b.co = c.co AND b.route = c.route AND b.dir = c.dir AND b.data_ts > c.arrival_ts
),
recent_before AS (
    SELECT b.track_id, b.data_ts, c.cum_err, c.cum_n
    FROM base b
    ASOF LEFT JOIN arrival_totals c
      ON b.co = c.co AND b.route = c.route AND b.dir = c.dir AND b.data_ts - 1800 > c.arrival_ts
)
SELECT
    b.co, b.route, b.dir, b.service_type, b.stop, b.seq, b.stop_frac,
    b.track_id, b.data_ts, b.eta, b.arrival_ts, b.eta_seq, b.horizon_s, b.error_s,
    b.error_lo_s, b.error_hi_s, b.method AS arrival_method,
    CASE WHEN b.rmk_en ILIKE '%scheduled%' THEN 1 ELSE 0 END AS scheduled,
    b.local_ts,
    CAST(strftime(b.local_ts, '%Y-%m-%d') AS DATE)   AS service_date,
    hour(b.local_ts)                                  AS hour,
    hour(b.local_ts) * 60 + minute(b.local_ts)        AS minute_of_day,
    isodow(b.local_ts)                                AS dow,
    CASE WHEN isodow(b.local_ts) >= 6 OR hol.date IS NOT NULL THEN 1 ELSE 0 END AS non_working_day,
    b.n_prev_obs, b.eta_change_s, b.since_prev_obs_s, b.eta_drift_s, b.first_horizon_s,
    b.unchanged_streak,
    coalesce(rn.cum_n, 0) - coalesce(rb.cum_n, 0)     AS recent_arrivals_30m,
    (coalesce(rn.cum_err, 0) - coalesce(rb.cum_err, 0))
        / nullif(coalesce(rn.cum_n, 0) - coalesce(rb.cum_n, 0), 0) AS recent_error_30m_s,
    r.rain_max_mm, r.rain_mean_mm, t.temp_c,
    coalesce(w.rainstorm_warning, 0) AS rainstorm_warning,
    coalesce(w.typhoon_signal, 0)    AS typhoon_signal,
    coalesce(w.n_warnings, 0)        AS n_warnings
FROM base b
LEFT JOIN holiday hol ON hol.date = CAST(strftime(b.local_ts, '%Y-%m-%d') AS DATE)
LEFT JOIN recent_now rn USING (track_id, data_ts)
LEFT JOIN recent_before rb USING (track_id, data_ts)
ASOF LEFT JOIN rain r ON b.data_ts >= r.available_at
ASOF LEFT JOIN temp t ON b.data_ts >= t.available_at
ASOF LEFT JOIN warn w ON b.data_ts >= w.fetched_at
ORDER BY b.data_ts, b.track_id;
