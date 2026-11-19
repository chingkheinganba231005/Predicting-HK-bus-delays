-- Health of the collection: requests, failures, latency and how often the
-- operator had not refreshed its data since the previous poll.
SELECT
    source,
    count(*)                                         AS n_requests,
    avg(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) AS error_rate,
    avg(CASE WHEN attempts > 1 THEN 1 ELSE 0 END)    AS retried_rate,
    quantile_cont(latency_ms, 0.5)                   AS latency_p50_ms,
    quantile_cont(latency_ms, 0.95)                  AS latency_p95_ms,
    avg(CASE WHEN n_rows > 0 AND n_new_rows = 0 THEN 1 ELSE 0 END) AS unchanged_rate,
    to_timestamp(min(requested_at))                  AS first_request,
    to_timestamp(max(requested_at))                  AS last_request
FROM raw_poll
GROUP BY source
ORDER BY source;
