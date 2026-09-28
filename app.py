"""
On-Call Hero dashboard (optional; scripts/demo.py is the terminal version).

    streamlit run app.py

All logic lives in src/workflow.py (unit-tested). This file is presentation only.
State is kept in st.session_state so buttons survive Streamlit's rerun-on-click model.
"""
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd
import streamlit as st

from src import config, hindsight_memory as hm, metrics, simulation, workflow

st.set_page_config(page_title="On-Call Hero", page_icon="🦸", layout="wide")

incidents = json.loads((config.DATA_DIR / "past_incidents.json").read_text())
alerts = json.loads((config.DATA_DIR / "incoming_alerts.json").read_text())
BANK = config.HINDSIGHT_BANK_ID


# ------------------------------------------------------------------ sidebar
with st.sidebar:
    st.title("🦸 On-Call Hero")
    offline = st.toggle("Offline replay mode", value=config.is_offline(),
                        help="Local stand-in for Hindsight + Groq. No keys or network needed.")
    config.set_offline(offline)
    if offline:
        st.warning("OFFLINE replay backend (not live Hindsight)")


@st.cache_resource
def get_clients(is_offline: bool):
    config.set_offline(is_offline)
    return config.get_hindsight_client(), config.get_groq_client()


hs, groq = get_clients(offline)

with st.sidebar:
    if offline and "seeded" not in st.session_state:
        try:
            hs.delete_bank(bank_id=BANK)
        except Exception:
            pass
        simulation.seed_bank(hs, BANK, incidents)
        metrics.reset()
        st.session_state["seeded"] = True
    if not offline and st.button("Seed Hindsight bank (run once)"):
        with st.spinner("Retaining past incidents..."):
            simulation.seed_bank(hs, BANK, incidents)
        st.success("Seeded.")
    live_count = len(metrics.load())
    st.metric("Incidents in memory", len(incidents) + live_count, delta=f"+{live_count} learned live" if live_count else None)
    st.subheader("📚 Incident history")
    for inc in incidents:
        badge = " 👋 resolver left" if inc.get("resolver_left_company") else ""
        with st.expander(f"{inc['incident_id']} · {inc['severity']}{badge}"):
            st.write(f"**Symptom:** {inc['symptom']}")
            st.write(f"**Root cause:** {inc['root_cause']}")
            st.write(f"**Fix:** {inc['resolution']}")
            st.caption(f"Resolved by {inc['resolved_by']} in {inc['resolution_time_minutes']} min")

st.title("🦸 On-Call Hero")
st.caption("An incident copilot whose memory (Hindsight) grows every time it is used. "
           "Resolution times are simulated; recall / reflect / retain are real Hindsight calls (unless offline).")

tab_live, tab_curve, tab_impact, tab_team = st.tabs(
    ["🚨 Live incident", "📈 Learning curve", "💰 Business impact", "🧠 Team memory"])

# ------------------------------------------------------------------ live incident
with tab_live:
    idx = st.selectbox("Simulated PagerDuty alert", range(len(alerts)),
                       format_func=lambda i: f"{alerts[i]['alert_id']}: {alerts[i]['title']}")
    alert = alerts[idx]
    st.code(alert["message"] + "\n\n" + "\n".join(alert.get("logs", [])), language="text")

    if st.button("🚨 Trigger alert", type="primary"):
        st.session_state.pop("res", None)
        with st.spinner("Recalling from Hindsight..."):
            st.session_state["tri"] = workflow.triage(hs, groq, BANK, alert)

    tri = st.session_state.get("tri")
    if tri and tri.alert["alert_id"] == alert["alert_id"]:
        rec, gate = tri.rec, tri.gate
        left, right = st.columns(2)
        with left:
            st.markdown("### 🤖 Without memory")
            st.info(tri.stateless)
        with right:
            st.markdown("### 🧠 With Hindsight memory")
            if rec.is_novel:
                st.warning(rec.summary_for_engineer)
            else:
                st.success(rec.summary_for_engineer)
                st.write(f"**Matched incident:** {rec.matched_incident_id} · **Confidence:** {rec.confidence}")
                st.write(f"**Root cause:** {rec.root_cause}")
                st.write(f"**Fix:** `{rec.recommended_fix_command}`")
            for w in rec.warnings:
                st.warning(w)
            with st.expander("Raw recall() matches"):
                for m, sc in zip(rec.raw_recall_matches, rec.raw_scores):
                    st.write(f"- {f'[relevance {sc}] ' if sc is not None else ''}{m}")
            with st.expander("Raw reflect() output"):
                st.write(rec.raw_reflection)
            st.caption(f"Memory round-trip: {rec.latency_seconds}s")

        st.markdown(f"#### 🛡️ Safety gate: **{gate.action.upper()}** (risk: {gate.risk})")
        for r in gate.reasons:
            st.write(f"- {r}")

        st.session_state.setdefault("run_seed", 11)
        if "res" not in st.session_state:
            if gate.action in {"auto", "approval"}:
                c1, c2 = st.columns(2)
                if c1.button("✅ Approve & run fix (simulated): it worked"):
                    st.session_state["res"] = workflow.resolve(hs, groq, BANK, tri, outcome="success", quiet=True,
                                                                rng=random.Random(st.session_state["run_seed"]))
                    st.rerun()
                if c2.button("❌ Fix did not work"):
                    st.session_state["res"] = workflow.resolve(hs, groq, BANK, tri, outcome="failure", quiet=True)
                    st.rerun()
            elif gate.action == "escalate":
                if st.button("👩‍💻 A human resolved it: retain what they did"):
                    st.session_state["res"] = workflow.resolve(hs, groq, BANK, tri, quiet=True)
                    st.rerun()

        res = st.session_state.get("res")
        if res:
            if res.outcome == "success":
                st.success(f"Resolved in {res.mttr} min (unassisted baseline {res.baseline} min, simulated). "
                           f"Retained as **{res.incident_id}**"
                           + (f"; searchable after {res.searchable_after}s." if res.searchable_after is not None else "."))
                with st.expander("📝 Auto-drafted postmortem", expanded=True):
                    st.markdown(res.postmortem_md)
                    st.download_button("Download postmortem", res.postmortem_md,
                                       file_name=f"{res.incident_id}.md", mime="text/markdown")
                st.info("Trigger a similar alert again to watch the agent recall what it just learned.")
            else:
                st.error(f"Failure retained as {res.incident_id}: the agent will warn against this fix next time.")

# ------------------------------------------------------------------ learning curve
with tab_curve:
    st.write("Replays 9 alerts against a **fresh** memory bank, including a failure the agent has never seen: "
             "escalated, fixed by a human, retained, then recognised when it recurs.")
    if st.button("▶ Run learning-curve simulation"):
        bar = st.progress(0.0, text="Seeding a fresh memory bank...")
        steps = simulation.run_sequence(
            hs, groq, BANK, alerts, incidents,
            on_seed=lambda i, n, iid: bar.progress(0.3 * i / n, text=f"Seeding {iid}"),
            on_step=lambda s: bar.progress(0.3 + 0.7 * s["step"] / len(simulation.CURVE_ORDER), text=f"Alert {s['step']}: {s['title']}"))
        metrics.save_curve(steps)
        bar.empty()
    curve = [s for s in metrics.load_curve() if "error" not in s]
    if curve:
        df = pd.DataFrame(curve).set_index("step")[["baseline_minutes", "with_memory_minutes"]]
        df.columns = ["Without memory (baseline)", "With Hindsight memory"]
        st.line_chart(df)
        st.dataframe(pd.DataFrame(curve)[["step", "title", "matched", "escalated", "with_memory_minutes", "baseline_minutes"]],
                     hide_index=True, use_container_width=True)
        st.caption(f"Total time saved: {round(sum(s['saved_minutes'] for s in curve), 1)} min (simulated times).")
    elif (config.ROOT / "docs" / "learning_curve.png").exists():
        st.image(str(config.ROOT / "docs" / "learning_curve.png"), caption="Example output (offline replay backend)")

# ------------------------------------------------------------------ business impact
with tab_impact:
    cost = st.number_input("Assumed cost of downtime ($/minute)", value=float(config.OUTAGE_COST_PER_MINUTE), step=500.0,
                           help="An assumption, not a measurement. Set to your own number.")
    s = metrics.summary(cost)
    a, b, c, d = st.columns(4)
    a.metric("Incidents handled", s["incidents"])
    b.metric("Avg time to resolve", f"{s['avg_mttr']} min", delta=f"{round(s['avg_mttr'] - s['avg_baseline'], 1)} vs unassisted" if s["incidents"] else None, delta_color="inverse")
    c.metric("Minutes saved", s["minutes_saved"])
    d.metric("Downtime cost avoided", f"${s['dollars_saved']:,}")
    st.caption("Times are simulated; the dollar figure multiplies simulated minutes saved by your assumption above.")
    if metrics.load():
        st.dataframe(pd.DataFrame(metrics.load()).drop(columns=["ts"]), hide_index=True, use_container_width=True)

# ------------------------------------------------------------------ team memory
with tab_team:
    st.write("Institutional memory: what does the team's incident history say when you reason over *all* of it? "
             "Engineers leave; their fixes should not.")
    if st.button("🧠 Reflect on the whole memory bank"):
        with st.spinner("reflect() over all incidents..."):
            st.session_state["insights"] = hm.reflect_team_insights(hs, BANK).text
    if "insights" in st.session_state:
        st.info(st.session_state["insights"])
