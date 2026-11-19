-- Every distinct prediction for the selected stops, ready for track linking.
-- One "stream" is (company, route, direction, service type, stop); within a
-- stream, a snapshot is the set of rows sharing a data_ts.
SELECT
    e.co, e.route, e.dir, e.service_type, e.stop,
    e.seq, e.data_ts, e.eta_seq, e.eta, e.rmk_en
FROM raw_eta e
WHERE e.eta IS NOT NULL
ORDER BY e.co, e.route, e.dir, e.service_type, e.stop, e.data_ts, e.eta_seq;
