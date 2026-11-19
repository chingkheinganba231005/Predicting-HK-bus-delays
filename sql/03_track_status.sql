-- How tracks ended, by operator and route. "arrived" tracks give labels.
SELECT
    co,
    route,
    count(*)                                              AS n_tracks,
    avg(CASE WHEN status = 'arrived' THEN 1 ELSE 0 END)   AS share_arrived,
    avg(CASE WHEN status = 'not_converged' THEN 1 ELSE 0 END) AS share_not_converged,
    avg(CASE WHEN status = 'left_before_eta' THEN 1 ELSE 0 END) AS share_left_before_eta,
    avg(CASE WHEN status = 'dropped' THEN 1 ELSE 0 END)   AS share_dropped,
    avg(CASE WHEN status IN ('outage', 'unmatched') THEN 1 ELSE 0 END) AS share_censored,
    -- for arrived buses: time between the inferred arrival and the first
    -- snapshot without the bus (dwell time plus polling delay)
    quantile_cont(next_ts - arrival_ts, 0.5) FILTER (WHERE status = 'arrived') AS gone_after_p50_s,
    quantile_cont(next_ts - arrival_ts, 0.9) FILTER (WHERE status = 'arrived') AS gone_after_p90_s
FROM tracks
WHERE status <> 'open'
GROUP BY co, route
ORDER BY co, route;
