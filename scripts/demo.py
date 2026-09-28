#!/usr/bin/env python
"""
THE command to run live in front of judges.

    python scripts/demo.py            # full story, ~2 min: recall, live learning, honest "I don't know"
    python scripts/demo.py --quick    # 60-second cut: alert -> fix -> learn -> recall it
    python scripts/demo.py --offline  # local replay backend (no keys / no Wi-Fi needed), clearly labelled

Run `python scripts/seed_memory.py` first (offline mode seeds itself).
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config, metrics, simulation, ui, workflow

STORY = (
    "It is 2:07 AM. Sarah is on call. PagerDuty fires: Redis is dying, checkout is degrading, and every\n"
    "minute of downtime costs the company real money. Six weeks ago a teammate fixed this exact failure,\n"
    "but the postmortem is buried in a wiki nobody can search at 2 AM.\n\n"
    "Watch what happens when the on-call agent has a memory."
)


def show_recall(rec, new_id: str | None = None) -> None:
    lines = []
    for i, (m, sc) in enumerate(zip(rec.raw_recall_matches, rec.raw_scores), 1):
        tag = f"relevance {sc}" if sc is not None else "match"
        mark = "  <-- the memory we wrote seconds ago" if new_id and new_id in m else ""
        lines.append(ui.esc(f"{i}. [{tag}] {m[:170]}...{mark}"))
    ui.panel("\n".join(lines) or "(nothing recalled)", "recall(): what memory returned", "magenta")
    ui.panel(ui.esc((rec.raw_reflection[:600].rsplit(" ", 1)[0] + " ...") if len(rec.raw_reflection) > 600 else (rec.raw_reflection or "(empty)")), "reflect(): Hindsight's reasoning", "magenta")


def show_recommendation(rec, gate) -> None:
    if rec.is_novel:
        ui.panel(ui.esc(f"{rec.summary_for_engineer}\n\nGate: {gate.action.upper()}: {gate.reasons[0]}"),
                 "Agent knows what it does NOT know", "yellow")
        return
    warn = ("\n[bold yellow]Warnings:[/bold yellow] " + ui.esc(" | ".join(rec.warnings))) if rec.warnings else ""
    ui.panel(
        f"[bold]Matched incident:[/bold] {rec.matched_incident_id}   [bold]Confidence:[/bold] {rec.confidence}\n"
        f"[bold]Root cause:[/bold] {ui.esc(rec.root_cause)}\n"
        f"[bold]Last fixed by:[/bold] {ui.esc(rec.resolved_by_last_time)} in {rec.prior_resolution_time_minutes} min\n"
        f"[bold]Fix:[/bold] {ui.esc(rec.recommended_fix_command)}{warn}",
        "On-Call Hero recommendation", "green")
    ui.panel(f"Action: {gate.action.upper()}   Risk: {gate.risk}\n" + "\n".join(ui.esc(f"- {r}") for r in gate.reasons),
             "Safety gate", "yellow" if gate.action != "auto" else "green")


def approve(gate, interactive: bool) -> bool:
    if not gate.can_run:
        return False
    if gate.action == "approval" and interactive:
        return input("Approve running this fix? [y/N] ").strip().lower() == "y"
    if gate.action == "approval":
        ui.say("[green]APPROVED[/green] by on-call engineer [dim](demo auto-approve)[/dim]")
    return True


def handle(hs, groq, bank, alert, *, pause, interactive, show_stateless=False, new_id_hint=None):
    ui.panel(ui.esc(alert["message"] + "\n\n" + "\n".join(alert.get("logs", []))), f"PagerDuty: {alert['alert_id']}", "red")
    pause()
    tri = workflow.triage(hs, groq, bank, alert, include_stateless=show_stateless)
    if show_stateless:
        ui.panel(ui.esc(tri.stateless), "WITHOUT memory: generic LLM", "yellow")
        pause()
    show_recall(tri.rec, new_id_hint)
    show_recommendation(tri.rec, tri.gate)
    pause()
    return tri


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--quick", action="store_true", help="60-second cut")
    ap.add_argument("--fast", action="store_true", help="no dramatic pauses")
    ap.add_argument("--interactive", action="store_true", help="ask before running the fix")
    args = ap.parse_args()
    if args.offline:
        config.set_offline(True)
    pause = (lambda s=1.2: None) if args.fast else (lambda s=1.2: time.sleep(s))

    alerts = {a["alert_id"]: a for a in json.loads((config.DATA_DIR / "incoming_alerts.json").read_text())}
    incidents = json.loads((config.DATA_DIR / "past_incidents.json").read_text())
    hs, groq, bank = config.get_hindsight_client(), config.get_groq_client(), config.HINDSIGHT_BANK_ID

    if config.is_offline():
        _reset(hs, bank)
        simulation.seed_bank(hs, bank, incidents)
        metrics.reset()
    label = "OFFLINE replay backend" if config.is_offline() else "live Hindsight + Groq"
    ui.rule(f"ON-CALL HERO  ({label})")
    ui.panel(STORY, "The problem", "cyan")
    ui.say(f"Memory bank '{bank}' holds [bold]{len(incidents)}[/bold] past incidents.")
    pause(2)

    # ---- Alert 1: recall a known incident ---------------------------------------------------
    ui.rule("1. A known failure: the agent remembers")
    tri1 = handle(hs, groq, bank, alerts["ALERT-001"], pause=pause, interactive=args.interactive, show_stateless=True)
    if not approve(tri1.gate, args.interactive):
        ui.say("[red]Fix not approved; stopping.[/red]")
        return

    ui.rule("2. Fix it, then LEARN: retain() a brand-new memory, live")
    res1 = workflow.resolve(hs, groq, bank, tri1)
    ui.say(f"[green]retain() -> {res1.incident_id}[/green] written to bank '{bank}'"
           + (f"; searchable after {res1.searchable_after}s" if res1.searchable_after is not None
              else "; [yellow]not yet searchable (async indexing)[/yellow]"))
    ui.panel(ui.esc("\n".join(res1.postmortem_md.splitlines()[:9]) + "\n..."), f"Postmortem auto-drafted for {res1.incident_id}", "blue")
    pause()

    ui.rule("3. Same failure again: it recalls what it learned seconds ago")
    tri2 = handle(hs, groq, bank, alerts["ALERT-002"], pause=pause, interactive=args.interactive,
                  new_id_hint=res1.incident_id)
    found = next((i + 1 for i, m in enumerate(tri2.rec.raw_recall_matches) if res1.incident_id in m), None)
    ui.say(f"[bold green]Proof the loop is live:[/bold green] {res1.incident_id} (written this run) "
           + (f"came back in recall at rank {found}." if found else "[yellow]was not in the top results.[/yellow]"))
    if approve(tri2.gate, args.interactive):
        workflow.resolve(hs, groq, bank, tri2, quiet=True)

    if not args.quick:
        ui.rule("4. A failure it has never seen: it says so, then learns")
        tri3 = handle(hs, groq, bank, alerts["ALERT-005"], pause=pause, interactive=args.interactive)
        res3 = workflow.resolve(hs, groq, bank, tri3, quiet=True)
        ui.say(f"A human fixed it ({res3.resolved_by}, {res3.mttr} min). [green]retain() -> {res3.incident_id}[/green]")
        pause()
        tri4 = handle(hs, groq, bank, alerts["ALERT-009"], pause=pause, interactive=args.interactive,
                      new_id_hint=res3.incident_id)
        if approve(tri4.gate, args.interactive):
            workflow.resolve(hs, groq, bank, tri4, quiet=True)

    s = metrics.summary()
    ui.rule("Impact of this run")
    ui.panel(
        f"Incidents handled: {s['incidents']}  (memory-assisted: {s['memory_assisted']}, escalated: {s['escalated']})\n"
        f"Avg time to resolve: {s['avg_mttr']} min vs {s['avg_baseline']} min unassisted (simulated times)\n"
        f"Minutes saved: {s['minutes_saved']}   Est. downtime cost avoided: ${s['dollars_saved']:,} "
        f"(assumes ${s['cost_per_minute']:,.0f}/min; set OUTAGE_COST_PER_MINUTE)",
        "Business impact", "green")


def _reset(hs, bank) -> None:
    """Offline only: start from an empty bank before seeding."""
    try:
        hs.delete_bank(bank_id=bank)
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    main()
