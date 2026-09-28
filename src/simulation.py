"""
Learning-curve simulation: replay a realistic sequence of alerts against a FRESH memory bank
and record how resolution time changes as the agent accumulates experience.

recall / reflect / retain are real Hindsight calls; resolution *times* are simulated (see runbooks.py).
Includes a never-before-seen failure that is escalated, resolved by a human, retained, and then
recognised the next time it occurs.
"""
from __future__ import annotations

import random
import time
from typing import Callable

from . import config, hindsight_memory, workflow

CURVE_ORDER = ["ALERT-001", "ALERT-003", "ALERT-005", "ALERT-002", "ALERT-008",
               "ALERT-004", "ALERT-006", "ALERT-007", "ALERT-009"]


def seed_bank(hindsight, bank_id: str, incidents: list[dict], on_progress: Callable | None = None) -> None:
    try:
        hindsight.create_bank(bank_id=bank_id, name=f"On-Call Hero - {bank_id}")
    except Exception:  # noqa: BLE001 - bank may already exist
        pass
    for i, inc in enumerate(incidents, 1):
        hindsight_memory.retain_past_incident(hindsight, bank_id, inc)
        if on_progress:
            on_progress(i, len(incidents), inc["incident_id"])


def run_sequence(hindsight, groq, base_bank: str, alerts: list[dict], incidents: list[dict], *, seed: int = 7,
                 on_step: Callable | None = None, on_seed: Callable | None = None) -> list[dict]:
    rng = random.Random(seed)
    bank = f"{base_bank}-curve-{int(time.time())}"
    seed_bank(hindsight, bank, incidents, on_seed)
    by_id = {a["alert_id"]: a for a in alerts}
    steps, next_num = [], 111
    for n, aid in enumerate(CURVE_ORDER, 1):
        alert = by_id[aid]
        try:
            tri = workflow.triage(hindsight, groq, bank, alert, include_stateless=False)
            res = workflow.resolve(hindsight, groq, bank, tri, incident_id=f"INC-{next_num}", rng=rng,
                                   quiet=True, wait=True, record_metrics=False)
            next_num += 1
            step = {
                "step": n, "alert_id": aid, "title": alert["title"], "matched": tri.rec.matched_incident_id,
                "confidence": tri.rec.confidence, "escalated": res.escalated,
                "with_memory_minutes": res.mttr, "baseline_minutes": res.baseline,
                "saved_minutes": round(res.baseline - res.mttr, 1),
            }
        except Exception as e:  # noqa: BLE001 - never let one bad step kill the whole run
            step = {"step": n, "alert_id": aid, "title": alert["title"], "error": str(e)}
        steps.append(step)
        if on_step:
            on_step(step)
    return steps
