"""
The shared incident lifecycle used by the demo, the dashboard and the simulation:

    triage()   stateless answer + memory answer + safety gate decision
    resolve()  run/record the fix, RETAIN the outcome, verify it is searchable, draft postmortem

Keeping this in one place means the demo, the UI and the tests all exercise the same code.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

from . import agent, guardrails, hindsight_memory, metrics, postmortem, runbooks

HUMAN_FALLBACK = {
    "root_cause": "Root cause determined by the responding engineer.",
    "resolution": "`manual investigation and remediation`",
    "resolved_by": "On-call engineer",
}


@dataclass
class Triage:
    alert: dict
    stateless: str
    rec: agent.MemoryRecommendation
    gate: guardrails.GateDecision


@dataclass
class Resolution:
    incident_id: str
    mttr: float
    baseline: float
    outcome: str
    escalated: bool
    resolved_by: str
    root_cause: str
    resolution_text: str
    searchable_after: float | None
    postmortem_md: str


def triage(hindsight, groq, bank_id: str, alert: dict, *, include_stateless: bool = True) -> Triage:
    logs = alert.get("logs")
    stateless = agent.handle_alert_stateless(groq, alert["message"], logs) if include_stateless else ""
    rec = agent.handle_alert_with_memory(hindsight, groq, bank_id, alert["message"], logs)
    gate = guardrails.gate(rec.recommended_fix_command, rec.confidence, rec.matched_incident_id)
    return Triage(alert, stateless, rec, gate)


def resolve(hindsight, groq, bank_id: str, tri: Triage, *, outcome: str = "success",
            incident_id: str | None = None, rng: random.Random | None = None, quiet: bool = False,
            wait: bool = True, record_metrics: bool = True) -> Resolution:
    alert, rec = tri.alert, tri.rec
    baseline = float(alert.get("baseline_minutes", 30))
    incident_id = incident_id or metrics.next_incident_id()
    escalated = not tri.gate.can_run

    if escalated:  # agent honestly did not know: a human resolves it, and the agent learns from that
        hr = alert.get("human_resolution") or HUMAN_FALLBACK
        root_cause, resolution_text, resolved_by = hr["root_cause"], hr["resolution"], hr["resolved_by"]
        mttr, outcome, matched = baseline, "success", None
    else:
        root_cause, resolution_text, matched = rec.root_cause, rec.recommended_fix_command, rec.matched_incident_id
        resolved_by = "On-call engineer (agent-assisted)"
        if outcome == "success":
            mttr = runbooks.run_fix(rec.recommended_fix_command, prior_minutes=rec.prior_resolution_time_minutes,
                                    baseline_minutes=baseline, rng=rng, quiet=quiet)
        else:
            mttr = baseline  # the suggested fix did not work; the humans took over

    hindsight_memory.retain_live_outcome(
        hindsight, bank_id, incident_id=incident_id, service=alert["service"], symptom=alert["message"],
        root_cause=root_cause, resolution=resolution_text.strip("`") if outcome != "success" else resolution_text,
        resolved_by=resolved_by, resolution_time_minutes=mttr, matched_prior_incident=matched,
        outcome=outcome, category=alert.get("category", "unknown"),
    )
    waited = None
    if wait and outcome == "success":
        waited = hindsight_memory.wait_until_recallable(hindsight, bank_id, incident_id, alert["message"])

    md = ""
    if outcome == "success":
        md = postmortem.draft_postmortem(
            groq, incident_id=incident_id, alert_message=alert["message"], logs=alert.get("logs", []),
            root_cause=root_cause, resolution=resolution_text, resolved_by=resolved_by, minutes=mttr,
            baseline_minutes=baseline, matched_incident_id=matched, escalated=escalated)
        if record_metrics:
            postmortem.save_postmortem(md, incident_id)

    if record_metrics:
        metrics.record({
            "incident_id": incident_id, "alert_id": alert["alert_id"], "mode": "escalated" if escalated else "memory",
            "matched_incident_id": matched, "confidence": rec.confidence, "mttr_minutes": mttr,
            "baseline_minutes": baseline, "outcome": outcome,
        })
    return Resolution(incident_id, mttr, baseline, outcome, escalated, resolved_by, root_cause,
                      resolution_text, waited, md)
