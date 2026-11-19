"""Helpers shared by the stage scripts."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

RESULTS = ROOT / "results"
FIGURES = ROOT / "figures"
DATA = ROOT / "data"
MODELS = ROOT / "models"
WAREHOUSE = DATA / "warehouse.duckdb"
SEED = 20261119

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def _clean(o):
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, np.ndarray):
        return [_clean(v) for v in o.tolist()]
    if isinstance(o, (np.floating, float)):
        return None if not np.isfinite(o) else float(o)
    if isinstance(o, np.integer):
        return int(o)
    if hasattr(o, "isoformat"):
        return o.isoformat()
    return o


def set_output(base: Path) -> None:
    """Redirect results, figures and models (used for runs on the sample data)."""
    global RESULTS, FIGURES, MODELS
    RESULTS, FIGURES, MODELS = base / "results", base / "figures", base / "models"


def write_json(name: str, payload: dict) -> Path:
    RESULTS.mkdir(parents=True, exist_ok=True)
    path = RESULTS / name
    path.write_text(json.dumps(_clean(payload), indent=2) + "\n")
    logging.info("wrote %s", path)
    return path


def read_json(name: str) -> dict:
    return json.loads((RESULTS / name).read_text())
