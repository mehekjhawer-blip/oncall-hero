"""
Simulated remediation. On-Call Hero's job here is to prove the memory loop, not to touch a
real cluster from a laptop. Resolution times are SIMULATED (and labelled as such everywhere):
a fix the agent already knows takes a fraction of the time it took the first time a human
found it, with a floor so the numbers stay believable.
"""
from __future__ import annotations

import random
import time

from . import ui


def simulate_resolution_minutes(prior_minutes: float | None, baseline_minutes: float,
                                rng: random.Random | None = None) -> float:
    rng = rng or random
    if prior_minutes:
        return round(max(4.0, prior_minutes * rng.uniform(0.25, 0.4)), 1)
    return round(baseline_minutes * rng.uniform(0.95, 1.05), 1)


def run_fix(command: str, *, prior_minutes: float | None, baseline_minutes: float,
            rng: random.Random | None = None, quiet: bool = False) -> float:
    if not quiet:
        ui.say(f"[bold yellow]$[/bold yellow] {ui.esc(command)}")
        for _ in range(3):
            time.sleep(0.25)
            ui.say("[dim]...[/dim]")
    minutes = simulate_resolution_minutes(prior_minutes, baseline_minutes, rng)
    if not quiet:
        ui.say(f"[bold green]Fix applied (simulated).[/bold green] Resolved in {minutes} min "
               f"(unassisted baseline: {baseline_minutes} min).\n")
    return minutes
