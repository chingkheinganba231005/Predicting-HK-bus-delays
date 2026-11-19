"""Compare hand-recorded arrivals with the inferred ones (see docs/field_check.md)."""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

from _common import WAREHOUSE, write_json
from hkbus import warehouse


def main(csv: Path = Path("data/field_check.csv")) -> None:
    obs = pd.read_csv(csv, dtype={"route": str, "stop": str})
    obs["observed_ts"] = (pd.to_datetime(obs["observed_hkt"]) - pd.Timedelta(hours=8)).astype("int64") // 10**9
    con = warehouse.connect(WAREHOUSE)
    arr = con.sql("SELECT co AS company, route, dir, stop, arrival_ts, method FROM tracks WHERE status = 'arrived'").df()
    con.close()
    rows = []
    for _, o in obs.iterrows():
        cand = arr[(arr.company == o.company) & (arr.route == o.route) & (arr.dir == o.dir) & (arr.stop == o.stop)]
        if cand.empty:
            continue
        j = (cand.arrival_ts - o.observed_ts).abs().idxmin()
        rows.append({"company": o.company, "diff_s": float(cand.loc[j, "arrival_ts"] - o.observed_ts),
                     "method": cand.loc[j, "method"]})
    d = pd.DataFrame(rows)
    if d.empty:
        sys.exit("no observation could be matched to an inferred arrival")
    out = {"n": len(d), "median_diff_s": float(d.diff_s.median()),
           "mean_abs_diff_s": float(d.diff_s.abs().mean()),
           "p90_abs_diff_s": float(np.quantile(d.diff_s.abs(), 0.9)),
           "by_company": d.groupby("company").diff_s.median().to_dict()}
    print(out)
    write_json("field_check.json", out)


if __name__ == "__main__":
    main()
