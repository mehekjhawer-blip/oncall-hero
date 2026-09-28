"""Local run metrics powering the business-impact view. Times are simulated; the dollar
figure uses a configurable, clearly-labelled assumption (OUTAGE_COST_PER_MINUTE)."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from . import config

FILE = config.STATE_DIR / "metrics.json"
CURVE_FILE = config.STATE_DIR / "curve.json"
FIRST_LIVE_ID = 111


def _read(path):
    return json.loads(path.read_text()) if path.exists() else []


def _write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, indent=2))


def load() -> list[dict]:
    return _read(FILE)


def record(entry: dict) -> None:
    rows = load()
    rows.append({"ts": datetime.now(timezone.utc).isoformat(), **entry})
    _write(FILE, rows)


def reset() -> None:
    for p in (FILE, CURVE_FILE):
        if p.exists():
            p.unlink()


def next_incident_id() -> str:
    return f"INC-{FIRST_LIVE_ID + len(load())}"


def summary(cost_per_minute: float | None = None) -> dict:
    cost = config.OUTAGE_COST_PER_MINUTE if cost_per_minute is None else cost_per_minute
    rows = load()
    saved = sum(max(0.0, r["baseline_minutes"] - r["mttr_minutes"]) for r in rows if r.get("outcome") == "success")
    n = len(rows)
    return {
        "incidents": n,
        "memory_assisted": sum(1 for r in rows if r.get("mode") == "memory" and r.get("outcome") == "success"),
        "escalated": sum(1 for r in rows if r.get("mode") == "escalated"),
        "avg_mttr": round(sum(r["mttr_minutes"] for r in rows) / n, 1) if n else 0.0,
        "avg_baseline": round(sum(r["baseline_minutes"] for r in rows) / n, 1) if n else 0.0,
        "minutes_saved": round(saved, 1),
        "dollars_saved": round(saved * cost),
        "cost_per_minute": cost,
    }


def save_curve(steps: list[dict]) -> None:
    _write(CURVE_FILE, steps)


def load_curve() -> list[dict]:
    return _read(CURVE_FILE)
