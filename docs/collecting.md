# Running the collector for 2–4 weeks

The collector polls the official ETA APIs once a minute for 11 routes
(6 KMB, 5 Citybus) and the Hong Kong Observatory every 10 minutes. It makes
about 108 requests a minute: 6 KMB route-level requests (all stops of a route
in one response) and 100 Citybus stop-level requests (10 stops in each
direction of 5 routes).

Run it on **one machine at a time**. If you switch machines part-way, merge
the two files at the end (see [Merging](#merging-files-from-two-machines)).

## 1. Install (both systems)

Python 3.11 or newer.

macOS (Terminal):

```bash
git clone https://github.com/chingkheinganba231005/Predicting-HK-bus-delays.git
cd Predicting-HK-bus-delays
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e . --no-deps
```

Windows (PowerShell):

```powershell
git clone https://github.com/chingkheinganba231005/Predicting-HK-bus-delays.git
cd Predicting-HK-bus-delays
py -3 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -e . --no-deps
```

Check that it works with a two-minute run:

```bash
python -m hkbus.collector --minutes 2 --db data/test.sqlite
python scripts/status.py --db data/test.sqlite
```

You should see `108 requests, 0 errors` per cycle after the first one (the
first cycle also downloads route and stop lists, which takes about a minute).

## 2. Run it unattended

### macOS: launchd

```bash
REPO="$(pwd)"
mkdir -p data/logs
sed "s#REPO#$REPO#g" deploy/macos/hk.bus.collector.plist > ~/Library/LaunchAgents/hk.bus.collector.plist
launchctl load ~/Library/LaunchAgents/hk.bus.collector.plist
```

launchd starts the collector at login and restarts it if it exits. The agent
runs it under `caffeinate -i`, so the Mac does not idle-sleep. Keep it on
mains power; closing the lid of a laptop on battery still puts it to sleep.
To stop:

```bash
launchctl unload ~/Library/LaunchAgents/hk.bus.collector.plist
```

### Windows: Task Scheduler

```powershell
powershell -ExecutionPolicy Bypass -File deploy\windows\install_task.ps1
```

The task starts at logon and restarts every minute if it stops. Stop Windows
from sleeping while plugged in:

```powershell
powercfg /change standby-timeout-ac 0
```

To remove the task: `Unregister-ScheduledTask -TaskName "HK bus ETA collector" -Confirm:$false`.

## 3. Check on it

```bash
python scripts/status.py
```

It prints requests and failures in the last hour and day, rows collected and
total gap time. The log is `data/logs/collector.log` (rotated at 5 MB).

Gaps are expected (sleep, Wi-Fi drops, updates). The collector records any
pause longer than three minutes in the `collector_gap` table, and the
ground-truth step censors buses whose arrival falls in a gap, so gaps cost
data but do not bias the labels.

## 4. Disk space

Measured on the first 1.7 hours of collection (19 November 2026, evening
peak, so a high estimate): about 6.4 MB per hour with raw responses kept,
roughly **4.3 GB for four weeks**. Raw responses are about half of that;
they are kept so the data can be re-parsed later. If space is short, start
with `--no-raw` (about 2 GB for four weeks; Hong Kong Observatory responses
are always kept).

## 5. When the collection is finished

```bash
python scripts/01_build_dataset.py      # SQLite -> DuckDB, arrivals, features
python scripts/02_train_evaluate.py     # models, test-period evaluation
python scripts/render_readme.py         # README numbers from results/*.json
streamlit run dashboard/app.py          # the dashboard
```

or simply `make all`.

## Merging files from two machines

```bash
python scripts/merge_collections.py data/collector.sqlite mac.sqlite windows.sqlite
```

Predictions are deduplicated on their natural key (operator, route,
direction, stop, position in the list, operator timestamp), so periods when
both machines were running are counted once.

## Terms of use

The data come from DATA.GOV.HK and are provided by the Kowloon Motor Bus Co.
(1933) Ltd, Citybus Ltd and the Hong Kong Observatory. The DATA.GOV.HK terms
allow free commercial and non-commercial use provided the source is
identified and attributed (see the README). The collector identifies itself
in its User-Agent and polls each route or stop at most once a minute.
