# Field check of the inferred arrival times

The arrival times used as ground truth are inferred from the operators' own
data (see `ground_truth.md`). A small independent check makes the result
much easier to defend.

## What to do

1. Pick one stop on a collected route that you can stand at for an hour,
   ideally one KMB and one Citybus stop (the two operators use different rules).
   The stop must be one of the selected stops: list them with

   ```bash
   python -c "import sqlite3; c=sqlite3.connect('data/collector.sqlite'); print(c.execute(\"SELECT DISTINCT company, route, dir, seq, stop FROM route_stop WHERE selected=1 ORDER BY 1,2,3,4\").fetchall())"
   ```

2. While the collector is running, write down for each bus of that route:
   the time the bus **stops** at the stop (doors open), to the second, from a
   phone clock synchronised to network time. 20 to 30 buses is enough.
3. Save them as `data/field_check.csv` with columns
   `company,route,dir,stop,observed_hkt` (time as `2026-11-25 08:14:32`).
4. Run `python scripts/field_check.py`. It matches each observation to the
   nearest inferred arrival at that stop and reports the median and spread
   of the difference.

A median difference within about 30 s supports the labels; a consistent
offset would be reported as a limitation and could be subtracted.
