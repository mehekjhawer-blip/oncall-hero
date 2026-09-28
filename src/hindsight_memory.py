"""
Every Hindsight (Vectorize) call in On-Call Hero lives in this one file, so the whole
"how does the memory work" story is readable top to bottom:

  retain()   store an incident, a fix outcome, or a failed attempt
  recall()   find the past incidents closest to what is happening right now
  reflect()  reason over memory: a recommendation, or team-level insights

The SDK calls are wrapped defensively (retry + argument fallback) because a live demo must
survive a transient network error or a minor SDK signature difference.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _retry(fn, *args, attempts: int = 3, **kwargs):
    last = None
    for i in range(attempts):
        try:
            return fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001 - surface the final error after retries
            last = e
            time.sleep(0.6 * (i + 1))
    raise last


# --------------------------------------------------------------------------- retain
def incident_to_memory_text(inc: dict[str, Any]) -> str:
    """Write a postmortem the way a human would; Hindsight extracts entities and facts from it."""
    left = " (has since left the company)" if inc.get("resolver_left_company") else ""
    logs = " | ".join(inc.get("logs", []))
    return (
        f"Incident {inc['incident_id']} ({inc.get('severity', 'SEV-?')}) on {inc['service']} "
        f"[category: {inc.get('category', 'unknown')}]. "
        f"Symptom: {inc['symptom']} "
        f"Root cause: {inc['root_cause']} "
        f"Resolution: {inc['resolution']} "
        f"Resolved by {inc['resolved_by']}{left} in {inc['resolution_time_minutes']} minutes."
        + (f" Log evidence: {logs}" if logs else "")
    )


def _retain(client, bank_id: str, content: str, context: str, timestamp: str, metadata: dict):
    try:
        return _retry(client.retain, bank_id=bank_id, content=content, context=context,
                      timestamp=timestamp, metadata=metadata)
    except TypeError:  # older/newer SDK without some optional kwargs
        return _retry(client.retain, bank_id=bank_id, content=content, context=context)


def retain_past_incident(client, bank_id: str, inc: dict[str, Any]) -> Any:
    return _retain(
        client, bank_id, incident_to_memory_text(inc), "incident postmortem", inc["timestamp"],
        {"incident_id": inc["incident_id"], "category": inc.get("category"), "source": "seed"},
    )


def retain_live_outcome(
    client, bank_id: str, *, incident_id: str, service: str, symptom: str, root_cause: str,
    resolution: str, resolved_by: str, resolution_time_minutes: float,
    matched_prior_incident: str | None = None, outcome: str = "success", category: str = "unknown",
) -> Any:
    """Store what just happened. Success teaches the agent what works; failure teaches it what
    NOT to recommend again -- both are retained, because a memory of mistakes is worth having."""
    if outcome == "success":
        note = f" Matched the pattern from {matched_prior_incident} and reused its fix." if matched_prior_incident else ""
        content = (
            f"Incident {incident_id} ({service}) [category: {category}]. Symptom: {symptom} "
            f"Root cause: {root_cause} Resolution: {resolution} "
            f"Resolved by {resolved_by} in {resolution_time_minutes} minutes. OUTCOME: SUCCESS.{note}"
        )
    else:
        content = (
            f"Incident {incident_id} ({service}) [category: {category}]. Symptom: {symptom} "
            f"Attempted remediation: {resolution} Attempted by {resolved_by}. OUTCOME: FAILED - this did not "
            f"resolve the incident; do not recommend it again for this symptom."
        )
    return _retain(
        client, bank_id, content, "live incident outcome", datetime.now(timezone.utc).isoformat(),
        {"incident_id": incident_id, "outcome": outcome, "source": "live"},
    )


# --------------------------------------------------------------------------- recall
def recall_similar_incidents(client, bank_id: str, alert_text: str, *, budget: str = "high"):
    query = f"Past incident matching this alert: {alert_text}"
    try:
        return _retry(client.recall, bank_id=bank_id, query=query, budget=budget)
    except Exception:  # noqa: BLE001 - e.g. budget kwarg unsupported: retry with defaults
        return _retry(client.recall, bank_id=bank_id, query=query)


def recall_failed_attempts(client, bank_id: str, alert_text: str) -> list:
    """Explicitly look for remediations recorded as FAILED for a similar symptom, so the agent never
    re-recommends something the team already learned does not work. Best-effort: never blocks triage."""
    query = f"Remediation that FAILED and did not resolve this kind of incident: {alert_text}"
    try:
        res = _retry(client.recall, bank_id=bank_id, query=query, attempts=2)
    except Exception:  # noqa: BLE001
        return []
    return [r for r in getattr(res, "results", []) if "OUTCOME: FAILED" in r.text]


def reflect_on_alert(client, bank_id: str, alert_text: str):
    query = (
        f'A new alert just fired: "{alert_text}". Based on past incidents in memory, what is the most likely '
        "root cause, which past incident ID does it match, who resolved it, and what exact command fixed it? "
        "Also note any remediation recorded as FAILED for a similar symptom."
    )
    return _retry(client.reflect, bank_id=bank_id, query=query)


def reflect_team_insights(client, bank_id: str):
    """Team-level reasoning over the whole bank: recurring themes, knowledge-loss risk,
    failed fixes. This is the 'memory as institutional knowledge' view."""
    query = (
        "Across all incidents in memory, what are the recurring failure themes, which resolutions depend on a "
        "single engineer or an engineer who has left the company (knowledge-loss risk), and which remediation "
        "attempts failed?"
    )
    return _retry(client.reflect, bank_id=bank_id, query=query)


def wait_until_recallable(client, bank_id: str, incident_id: str, probe_query: str,
                          timeout: float = 15.0, interval: float = 1.0) -> float | None:
    """Retention may be processed asynchronously. Poll until the new memory is actually
    searchable, so the 'recall what we just learned' beat never races the write.
    Returns seconds waited, or None on timeout."""
    start = time.time()
    while time.time() - start < timeout:
        res = recall_similar_incidents(client, bank_id, probe_query)
        if any(incident_id in r.text for r in getattr(res, "results", [])):
            return round(time.time() - start, 1)
        time.sleep(interval)
    return None


@dataclass
class RecallSummary:
    raw_matches: list[str] = field(default_factory=list)
    scores: list[float | None] = field(default_factory=list)
    reflection_text: str = ""


def summarize_recall(recall_result, reflect_result) -> RecallSummary:
    results = getattr(recall_result, "results", []) or []
    return RecallSummary(
        raw_matches=[r.text for r in results],
        scores=[getattr(r, "score", None) for r in results],
        reflection_text=getattr(reflect_result, "text", str(reflect_result)),
    )
