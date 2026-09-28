"""
Safety gate between "the agent knows a fix" and "the fix runs".

A memory-powered agent that blindly re-runs old commands is a liability. This gate:
  * never auto-runs anything when confidence is low (escalates to a human instead),
  * hard-blocks destructive commands,
  * requires human approval for anything that mutates production,
  * only auto-allows read-only commands.
Unknown commands are treated as mutating (fail closed).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

BLOCKED = [
    r"\brm\s+-rf\s+/", r"\bdrop\s+(table|database)\b", r"\bflush(all|db)\b", r"\bmkfs\b",
    r"kubectl\s+delete\s+(ns|namespace)\b", r":\(\)\s*\{", r"\bdd\s+if=.*\bof=/dev/",
]
HIGH = [r"\bkubectl\s+delete\b", r"--force\b", r"\btruncate\b", r"\b(shutdown|reboot)\b"]
LOW = [r"^kubectl\s+(get|describe|logs|top)\b", r"^(cat|tail|head|grep|df|free|uptime)\b"]
_ORDER = {"low": 0, "medium": 1, "high": 2, "blocked": 3}


@dataclass
class GateDecision:
    action: str            # "auto" | "approval" | "escalate" | "blocked"
    risk: str
    reasons: list[str] = field(default_factory=list)

    @property
    def can_run(self) -> bool:
        return self.action in {"auto", "approval"}


def _segments(command: str) -> list[str]:
    return [s.strip() for s in re.split(r"&&|\|\||;|\|", command) if s.strip()]


def assess_command(command: str) -> tuple[str, list[str]]:
    worst, reasons = "low", []
    for seg in _segments(command):
        if any(re.search(p, seg, re.I) for p in BLOCKED):
            risk = "blocked"
        elif any(re.search(p, seg, re.I) for p in HIGH):
            risk = "high"
        elif any(re.search(p, seg, re.I) for p in LOW):
            risk = "low"
        else:
            risk = "medium"  # unknown or mutating -> fail closed
        if risk != "low":
            reasons.append(f"`{seg[:60]}` classified {risk}")
        if _ORDER[risk] > _ORDER[worst]:
            worst = risk
    return worst, reasons


def gate(command: str, confidence: str, matched_incident_id: str | None) -> GateDecision:
    if not command or not matched_incident_id or confidence == "low":
        return GateDecision("escalate", "n/a", ["No confident memory match: paging a human, running nothing."])
    risk, reasons = assess_command(command)
    if risk == "blocked":
        return GateDecision("blocked", risk, reasons + ["Destructive command blocked outright."])
    if risk == "low" and confidence == "high":
        return GateDecision("auto", risk, ["Read-only command with high confidence."])
    return GateDecision("approval", risk, reasons + ["Mutates production: needs on-call engineer approval."])
