import subprocess
import sys
from pathlib import Path

from hkbus import store
from hkbus.api import EtaRow

ROOT = Path(__file__).resolve().parents[1]


def _db(path, rows, poll_times):
    con = store.connect(path)
    for t in poll_times:
        pid = store.insert_poll(con, source="kmb_route_eta", key="1A/1", requested_at=t)
    store.insert_etas(con, rows, pid)
    con.commit()
    con.close()


def test_merge_deduplicates_and_recomputes_gaps(tmp_path):
    a = [EtaRow("KMB", "1A", "O", 1, "S", 5, 1, 1000 + i, "", 900 + 60 * i, None) for i in range(3)]
    b = [EtaRow("KMB", "1A", "O", 1, "S", 5, 1, 1000 + i, "", 900 + 60 * i, None) for i in range(2, 5)]
    _db(tmp_path / "mac.sqlite", a, [0, 60, 120])
    _db(tmp_path / "win.sqlite", b, [120, 180, 1000])   # 820 s without any poll: a gap
    out = tmp_path / "merged.sqlite"
    subprocess.run([sys.executable, str(ROOT / "scripts" / "merge_collections.py"), str(out),
                    str(tmp_path / "mac.sqlite"), str(tmp_path / "win.sqlite")], check=True)
    con = store.connect(out)
    assert con.execute("SELECT count(*) FROM eta").fetchone()[0] == 5      # one overlapping row kept once
    assert con.execute("SELECT count(*) FROM poll").fetchone()[0] == 6
    assert con.execute("SELECT gap_start, gap_end FROM collector_gap").fetchall() == [(180.0, 1000.0)]
