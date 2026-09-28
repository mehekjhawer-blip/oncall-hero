"""Auto-drafted postmortems. Real teams skip postmortems because they are tedious, which is
exactly why the same incident bites twice. The agent drafts one the moment an incident closes."""
from __future__ import annotations

import re
from pathlib import Path

from . import agent, config

DEFAULT_ACTIONS = [
    "Add an alert on the leading indicator so this is caught before customer impact.",
    "Add this failure mode and its fix to the on-call runbook.",
    "Add a regression test or config validation to prevent recurrence.",
]


def _action_items(groq_client, root_cause: str) -> list[str]:
    try:
        text = agent.chat(groq_client, [
            {"role": "system", "content": "You write postmortem action items. Reply with exactly 3 short lines, each "
                                           "starting with '- ', each a concrete prevention step."},
            {"role": "user", "content": f"Root cause: {root_cause}"},
        ], temperature=0.3, max_tokens=250)
        items = [re.sub(r"^-\s*", "", l).strip() for l in text.splitlines() if l.strip().startswith("-")]
        return items[:3] or DEFAULT_ACTIONS
    except RuntimeError:
        return DEFAULT_ACTIONS


def draft_postmortem(groq_client, *, incident_id: str, alert_message: str, logs: list[str], root_cause: str,
                     resolution: str, resolved_by: str, minutes: float, baseline_minutes: float,
                     matched_incident_id: str | None, escalated: bool) -> str:
    how = ("Escalated to a human (no confident memory match); the fix is now retained for next time."
           if escalated else f"Agent recalled {matched_incident_id} from memory and proposed its known fix.")
    actions = "\n".join(f"- [ ] {a}" for a in _action_items(groq_client, root_cause))
    log_block = "\n".join(logs) if logs else "(none captured)"
    return (
        f"# Postmortem: {incident_id}\n\n"
        f"**Alert:** {alert_message}\n\n"
        f"**Time to resolve:** {minutes} min (unassisted baseline {baseline_minutes} min; simulated)\n"
        f"**Resolved by:** {resolved_by}\n\n"
        f"## What happened\n{how}\n\n"
        f"## Root cause\n{root_cause}\n\n"
        f"## Resolution\n{resolution}\n\n"
        f"## Evidence\n```\n{log_block}\n```\n\n"
        f"## Action items\n{actions}\n"
    )


def save_postmortem(md: str, incident_id: str) -> Path:
    d = config.STATE_DIR / "postmortems"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{incident_id}.md"
    path.write_text(md)
    return path
