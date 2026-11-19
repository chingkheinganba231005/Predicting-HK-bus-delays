"""Poll the KMB and Citybus ETA APIs every minute and store the responses.

Run:  python -m hkbus.collector --db data/collector.sqlite
See docs/collecting.md for running it unattended for several weeks.
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import signal
import sqlite3
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import api, store

log = logging.getLogger("collector")
USER_AGENT = "hk-bus-eta-reliability-study/1.0 (research; polite polling, 1 request per route per minute)"


# ---------------------------------------------------------------- configuration

@dataclass
class Route:
    company: str
    route: str
    service_type: int = 1
    note: str = ""


@dataclass
class Config:
    routes: list[Route]
    poll_interval_seconds: int = 60
    weather_interval_seconds: int = 600
    stops_per_direction: int = 10
    http_timeout_seconds: float = 10
    max_workers: int = 8

    @classmethod
    def load(cls, path: Path) -> "Config":
        raw = tomllib.loads(Path(path).read_text())
        routes = [Route(**r) for r in raw.pop("route")]
        return cls(routes=routes, **raw)


def select_stops(seqs: list[int], k: int) -> list[int]:
    """k sequence numbers evenly spaced along the route, terminus excluded.

    The final stop is dropped because buses end their trip there and the
    operators often stop predicting it. The first stop is kept: its ETAs are
    departures from the terminus and are often timetable-based.
    """
    seqs = sorted(seqs)
    usable = seqs[:-1] if len(seqs) > 1 else seqs
    if len(usable) <= k:
        return usable
    idx = np.unique(np.round(np.linspace(0, len(usable) - 1, k)).astype(int))
    return [usable[i] for i in idx]


# ---------------------------------------------------------------- HTTP

def make_session(timeout: float) -> requests.Session:
    s = requests.Session()
    retry = Retry(total=3, connect=3, read=3, backoff_factor=0.5,
                  status_forcelist=(429, 500, 502, 503, 504), allowed_methods=("GET",),
                  respect_retry_after_header=True)
    s.mount("https://", HTTPAdapter(max_retries=retry, pool_maxsize=16))
    s.headers["User-Agent"] = USER_AGENT
    s.request_timeout = timeout
    return s


@dataclass
class Fetch:
    source: str
    key: str
    url: str
    requested_at: float = 0.0
    status: int | None = None
    body: bytes | None = None
    error: str | None = None
    latency_ms: float | None = None
    attempts: int = 1


def fetch(session: requests.Session, job: Fetch) -> Fetch:
    job.requested_at = time.time()
    t0 = time.perf_counter()
    try:
        r = session.get(job.url, timeout=session.request_timeout)
        job.status = r.status_code
        retries = getattr(r.raw, "retries", None)
        job.attempts = 1 + len(retries.history) if retries else 1
        if r.ok:
            job.body = r.content
        else:
            job.error = f"HTTP {r.status_code}"
    except requests.RequestException as e:
        job.error = f"{type(e).__name__}: {e}"[:500]
    job.latency_ms = (time.perf_counter() - t0) * 1000
    return job


# ---------------------------------------------------------------- collector

@dataclass
class Collector:
    cfg: Config
    db_path: Path
    keep_raw: bool = True
    con: sqlite3.Connection = field(init=False)
    session: requests.Session = field(init=False)
    kmb_stops: dict = field(default_factory=dict)   # route -> {(dir, seq): stop}
    ctb_jobs: list = field(default_factory=list)    # (route, stop)
    last_cycle: float | None = None
    last_weather: float = 0.0
    last_meta_day: str = ""
    stopping: bool = False

    def __post_init__(self):
        self.con = store.connect(self.db_path)
        self.session = make_session(self.cfg.http_timeout_seconds)
        row = self.con.execute("SELECT max(requested_at) FROM poll").fetchone()
        self.last_cycle = row[0]

    # ---- metadata: route-stop lists and stop locations, refreshed daily after 05:00 HKT

    def _get_json(self, url: str) -> dict:
        r = self.session.get(url, timeout=self.cfg.http_timeout_seconds)
        r.raise_for_status()
        return r.json()

    def refresh_metadata(self) -> None:
        now = int(time.time())
        kmb_stops, ctb_jobs, rows = {}, [], []
        for rt in self.cfg.routes:
            for direction in ("O", "I"):
                if rt.company == "KMB":
                    url = api.kmb_route_stop_url(rt.route, direction, rt.service_type)
                else:
                    url = api.ctb_route_stop_url(rt.route, direction)
                try:
                    stops = api.parse_route_stops(self._get_json(url), rt.company)
                except requests.RequestException as e:
                    log.warning("route-stop failed for %s %s %s: %s", rt.company, rt.route, direction, e)
                    continue
                if not stops:
                    continue
                chosen = set(select_stops([s["seq"] for s in stops], self.cfg.stops_per_direction))
                for s in stops:
                    s["selected"] = s["seq"] in chosen
                    rows.append(s)
                    if not s["selected"]:
                        continue
                    if rt.company == "KMB":
                        kmb_stops.setdefault(rt.route, {})[(direction, s["seq"])] = s["stop"]
                    else:
                        ctb_jobs.append((rt.route, s["stop"]))
        if not rows:
            raise RuntimeError("could not download any route-stop list; check the network")
        store.insert_route_stops(self.con, rows, now)

        # stop names and coordinates (for the dashboard map)
        try:
            store.upsert_stops(self.con, "KMB", api.parse_kmb_stop_list(self._get_json(api.kmb_stop_list_url())))
        except requests.RequestException as e:
            log.warning("KMB stop list failed: %s", e)
        known = {r[0] for r in self.con.execute("SELECT stop FROM stop WHERE company='CTB'")}
        for _, stop_id in ctb_jobs:
            if stop_id in known:
                continue
            try:
                s = api.parse_stop(self._get_json(api.ctb_stop_url(stop_id)))
                if s:
                    store.upsert_stops(self.con, "CTB", [s])
                    known.add(stop_id)
            except requests.RequestException as e:
                log.warning("CTB stop %s failed: %s", stop_id, e)
        self.con.commit()
        self.kmb_stops, self.ctb_jobs = kmb_stops, sorted(set(ctb_jobs))
        log.info("metadata: %d KMB routes, %d Citybus stop/route pairs",
                 len(self.kmb_stops), len(self.ctb_jobs))

    def _maybe_refresh_metadata(self) -> None:
        hk = time.gmtime(time.time() + 8 * 3600)
        day = time.strftime("%Y-%m-%d", hk)
        first_run = not self.kmb_stops and not self.ctb_jobs
        after_daily_update = (hk.tm_hour, hk.tm_min) >= (5, 30)  # operators refresh at 05:00 HKT
        if first_run or (day != self.last_meta_day and after_daily_update):
            self.refresh_metadata()
            self.last_meta_day = day

    # ---- one polling cycle

    def jobs(self, now: float) -> list[Fetch]:
        out = [Fetch("kmb_route_eta", f"{rt.route}/{rt.service_type}",
                     api.kmb_route_eta_url(rt.route, rt.service_type))
               for rt in self.cfg.routes if rt.company == "KMB" and rt.route in self.kmb_stops]
        out += [Fetch("ctb_eta", f"{stop}/{route}", api.ctb_eta_url(stop, route))
                for route, stop in self.ctb_jobs]
        if now - self.last_weather >= self.cfg.weather_interval_seconds:
            out += [Fetch("hko_rhrread", "rhrread", api.hko_url("rhrread")),
                    Fetch("hko_warnsum", "warnsum", api.hko_url("warnsum"))]
            self.last_weather = now
        return out

    def store_result(self, f: Fetch) -> tuple[int, int]:
        sha = gen = None
        n_rows = n_new = 0
        if f.body is not None:
            try:
                payload = json.loads(f.body)
                keep = self.keep_raw or f.source.startswith("hko")
                sha = store.save_raw(self.con, f.body, f.requested_at, keep=keep)
                if f.source == "kmb_route_eta":
                    route = f.key.split("/")[0]
                    rows = api.parse_kmb_route_eta(payload, self.kmb_stops.get(route, {}))
                elif f.source == "ctb_eta":
                    rows = api.parse_ctb_eta(payload)
                else:
                    rows = []
                gen = api.generated_ts(payload) if isinstance(payload, dict) else None
                if f.source == "hko_rhrread":
                    w = api.parse_hko_rhrread(payload)
                    n_rows, n_new = len(w), store.insert_weather(self.con, w, f.requested_at)
                elif f.source == "hko_warnsum":
                    store.insert_warnings(self.con, int(f.requested_at), api.parse_hko_warnsum(payload))
                    n_rows = n_new = 1
            except (ValueError, KeyError, TypeError) as e:
                f.error = f"parse error: {type(e).__name__}: {e}"[:500]
                rows = []
        else:
            rows = []
        poll_id = store.insert_poll(
            self.con, source=f.source, key=f.key, requested_at=f.requested_at, http_status=f.status,
            latency_ms=f.latency_ms, attempts=f.attempts, error=f.error, body_sha=sha,
            generated_ts=gen, n_rows=len(rows) or n_rows, n_new_rows=n_new)
        if rows:
            n_new = store.insert_etas(self.con, rows, poll_id)
            self.con.execute("UPDATE poll SET n_new_rows = ? WHERE poll_id = ?", (n_new, poll_id))
        return len(rows) or n_rows, n_new

    def cycle(self) -> dict:
        now = time.time()
        if self.last_cycle is not None and now - self.last_cycle > 3 * self.cfg.poll_interval_seconds:
            store.record_gap(self.con, self.last_cycle, now)
            log.warning("gap of %.0f s since the last cycle recorded", now - self.last_cycle)
        self._maybe_refresh_metadata()
        jobs = self.jobs(now)
        with ThreadPoolExecutor(self.cfg.max_workers) as pool:
            results = list(pool.map(lambda j: fetch(self.session, j), jobs))
        stats = {"requests": len(results), "errors": 0, "rows": 0, "new_rows": 0}
        with self.con:
            for f in results:
                if f.error:
                    stats["errors"] += 1
                    log.warning("%s %s: %s", f.source, f.key, f.error)
                n, new = self.store_result(f)
                stats["rows"] += n
                stats["new_rows"] += new
        self.last_cycle = now
        return stats

    def run(self, duration_minutes: float | None = None) -> None:
        end = None if duration_minutes is None else time.time() + 60 * duration_minutes
        interval = self.cfg.poll_interval_seconds
        totals = {"cycles": 0, "requests": 0, "errors": 0, "new_rows": 0}
        while not self.stopping and (end is None or time.time() < end):
            try:
                s = self.cycle()
                totals["cycles"] += 1
                for k in ("requests", "errors", "new_rows"):
                    totals[k] += s[k]
                if totals["cycles"] % 10 == 1:
                    log.info("cycle %d: %d requests, %d errors, %d new rows (totals %s)",
                             totals["cycles"], s["requests"], s["errors"], s["new_rows"], totals)
            except Exception:  # keep running through unexpected failures; they are logged
                log.exception("cycle failed")
            # sleep to the next multiple of the interval, so polls stay aligned;
            # after an overrun, skip to the next slot rather than catching up
            next_start = (int(time.time() // interval) + 1) * interval
            while not self.stopping and time.time() < next_start:
                time.sleep(min(1.0, next_start - time.time()))
        self.con.close()
        log.info("stopped after %d cycles: %s", totals["cycles"], totals)


def setup_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh = logging.handlers.RotatingFileHandler(log_dir / "collector.log", maxBytes=5_000_000, backupCount=5)
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers[:] = [fh, sh]


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", type=Path, default=Path("config/routes.toml"))
    ap.add_argument("--db", type=Path, default=Path("data/collector.sqlite"))
    ap.add_argument("--log-dir", type=Path, default=Path("data/logs"))
    ap.add_argument("--minutes", type=float, default=None, help="stop after this many minutes")
    ap.add_argument("--no-raw", action="store_true", help="do not keep raw bus responses (saves disk)")
    args = ap.parse_args(argv)
    setup_logging(args.log_dir)
    c = Collector(Config.load(args.config), args.db, keep_raw=not args.no_raw)

    def stop(*_):
        log.info("stop requested")
        c.stopping = True

    signal.signal(signal.SIGINT, stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop)
    log.info("collector starting: %d routes, db %s", len(c.cfg.routes), args.db)
    c.run(args.minutes)


if __name__ == "__main__":
    main()
