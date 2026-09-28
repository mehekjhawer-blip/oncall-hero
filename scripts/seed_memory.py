#!/usr/bin/env python
"""Load data/past_incidents.json into Hindsight. Run once before the demo.

    python scripts/seed_memory.py            # live Hindsight
    python scripts/seed_memory.py --offline  # local replay backend, no keys needed
    python scripts/seed_memory.py --fresh    # try to delete the bank first (avoids duplicate memories)
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config, metrics, simulation, ui


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--fresh", action="store_true")
    args = ap.parse_args()
    if args.offline:
        config.set_offline(True)

    incidents = json.loads((config.DATA_DIR / "past_incidents.json").read_text())
    client = config.get_hindsight_client()
    bank = config.HINDSIGHT_BANK_ID
    mode = "OFFLINE replay" if config.is_offline() else "live Hindsight"
    ui.say(f"[bold cyan]Seeding {len(incidents)} incidents into bank '{bank}' ({mode})[/bold cyan]")

    if args.fresh or config.is_offline():
        try:
            client.delete_bank(bank_id=bank)
            ui.say("[dim]Deleted existing bank for a clean start.[/dim]")
        except Exception as e:  # noqa: BLE001
            ui.say(f"[yellow]Could not delete bank automatically ({e}). If it already holds these incidents, "
                   f"delete it in the Hindsight UI or use a new HINDSIGHT_BANK_ID to avoid duplicates.[/yellow]")
        metrics.reset()

    simulation.seed_bank(client, bank, incidents,
                         lambda i, n, iid: ui.say(f"  [green]retained[/green] {iid} ({i}/{n})"))
    ui.say("\n[bold green]Done.[/bold green] Next: python scripts/demo.py")


if __name__ == "__main__":
    main()
