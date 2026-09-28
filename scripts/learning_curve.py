#!/usr/bin/env python
"""
Replay 9 realistic alerts against a FRESH memory bank and chart how resolution time changes as
the agent accumulates experience -- including a never-seen failure (escalated to a human, retained,
then recognised the next time).

    python scripts/learning_curve.py                       # live Hindsight + Groq
    python scripts/learning_curve.py --offline             # local replay backend
    python scripts/learning_curve.py --save-chart docs/learning_curve.png

recall / reflect / retain are real Hindsight calls; resolution TIMES are simulated (see runbooks.py).
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config, metrics, simulation, ui


def save_chart(steps: list[dict], path: str, offline: bool) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    ok = [s for s in steps if "error" not in s]
    x = [s["step"] for s in ok]
    fig, ax = plt.subplots(figsize=(9, 4.6), dpi=140)
    ax.plot(x, [s["baseline_minutes"] for s in ok], "o--", color="#9ca3af", label="Without memory (unassisted baseline)")
    ax.plot(x, [s["with_memory_minutes"] for s in ok], "o-", color="#16a34a", lw=2.5, label="With Hindsight memory")
    for s in ok:
        if s["escalated"]:
            ax.annotate("never seen:\nescalated to human,\nthen retained", (s["step"], s["with_memory_minutes"]),
                        xytext=(s["step"] - 0.2, s["with_memory_minutes"] + 9), fontsize=8, ha="center",
                        arrowprops=dict(arrowstyle="->", color="#b45309"), color="#b45309")
        if s["alert_id"] == "ALERT-009":
            ax.annotate("recognised: learned\nat step 3", (s["step"], s["with_memory_minutes"]),
                        xytext=(s["step"] - 0.6, s["with_memory_minutes"] + 14), fontsize=8, ha="center",
                        arrowprops=dict(arrowstyle="->", color="#166534"), color="#166534")
    ax.set_xticks(x)
    ax.set_xlabel("Incident number (in order of arrival)")
    ax.set_ylabel("Time to resolve (minutes)")
    ax.set_title("On-Call Hero: time to resolve as memory accumulates")
    ax.set_ylim(0, 55)
    ax.grid(alpha=0.25)
    ax.legend(loc="upper right")
    note = "Resolution times are simulated; recall/reflect/retain are " + ("from the OFFLINE replay backend." if offline else "live Hindsight calls.")
    fig.text(0.01, 0.01, note, fontsize=7, color="#6b7280")
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--save-chart", metavar="PNG")
    args = ap.parse_args()
    if args.offline:
        config.set_offline(True)

    alerts = json.loads((config.DATA_DIR / "incoming_alerts.json").read_text())
    incidents = json.loads((config.DATA_DIR / "past_incidents.json").read_text())
    hs, groq = config.get_hindsight_client(), config.get_groq_client()

    ui.say("[bold cyan]Learning-curve simulation[/bold cyan] (fresh bank; seeding 10 incidents first)")
    steps = simulation.run_sequence(
        hs, groq, config.HINDSIGHT_BANK_ID, alerts, incidents,
        on_seed=lambda i, n, iid: ui.say(f"  seeded {iid} ({i}/{n})"),
        on_step=lambda s: ui.say(f"  step {s['step']}: {s['title']} -> "
                                 + (s.get("error") or f"{s['with_memory_minutes']} min vs {s['baseline_minutes']} baseline")),
    )
    metrics.save_curve(steps)
    ok = [s for s in steps if "error" not in s]
    ui.table("Results", ["#", "Alert", "Matched", "Escalated", "With memory (min)", "Baseline (min)"],
             [[s["step"], s["title"], s["matched"] or "-", "yes" if s["escalated"] else "no",
               s["with_memory_minutes"], s["baseline_minutes"]] for s in ok])
    saved = sum(s["saved_minutes"] for s in ok)
    ui.say(f"\n[bold green]Total time saved: {round(saved, 1)} min[/bold green] across {len(ok)} incidents (simulated times).")
    if args.save_chart:
        save_chart(steps, args.save_chart, config.is_offline())
        ui.say(f"Chart saved to {args.save_chart}")


if __name__ == "__main__":
    main()
