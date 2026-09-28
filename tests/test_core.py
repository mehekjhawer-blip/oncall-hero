"""Core behaviour tests. Run with:  python -m unittest discover -s tests -v
They use the offline backends, so no network or API keys are needed."""
import json
import random
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import agent, config, guardrails, hindsight_memory as hm, metrics, mock_backends, simulation, workflow

DATA = Path(__file__).resolve().parent.parent / "data"
INCIDENTS = json.loads((DATA / "past_incidents.json").read_text())
ALERTS = {a["alert_id"]: a for a in json.loads((DATA / "incoming_alerts.json").read_text())}
BANK = "test-bank"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.patches = [
            mock.patch.object(config, "STATE_DIR", self.tmp),
            mock.patch.object(metrics, "FILE", self.tmp / "metrics.json"),
            mock.patch.object(metrics, "CURVE_FILE", self.tmp / "curve.json"),
            mock.patch("time.sleep", lambda *_: None),
        ]
        for p in self.patches:
            p.start()
        self.hs = mock_backends.FakeHindsight(self.tmp / "mem.json")
        self.groq = mock_backends.FakeGroq()
        simulation.seed_bank(self.hs, BANK, INCIDENTS)

    def tearDown(self):
        for p in self.patches:
            p.stop()


class TestRecall(Base):
    def test_known_failures_match_the_right_incident(self):
        expect = {"ALERT-001": "INC-104", "ALERT-003": "INC-101", "ALERT-004": "INC-103", "ALERT-006": "INC-106"}
        for aid, inc in expect.items():
            rec = workflow.triage(self.hs, self.groq, BANK, ALERTS[aid], include_stateless=False).rec
            self.assertEqual(rec.matched_incident_id, inc, aid)
            self.assertIn(rec.confidence, {"high", "medium"})

    def test_novel_alert_is_not_guessed(self):
        tri = workflow.triage(self.hs, self.groq, BANK, ALERTS["ALERT-005"], include_stateless=False)
        self.assertIsNone(tri.rec.matched_incident_id)
        self.assertEqual(tri.rec.confidence, "low")
        self.assertEqual(tri.gate.action, "escalate")

    def test_left_company_warning_surfaces(self):
        rec = workflow.triage(self.hs, self.groq, BANK, ALERTS["ALERT-003"], include_stateless=False).rec
        self.assertTrue(any("left the company" in w for w in rec.warnings))


class TestLiveLearningLoop(Base):
    def test_retained_memory_is_recalled_next_time(self):
        t1 = workflow.triage(self.hs, self.groq, BANK, ALERTS["ALERT-001"], include_stateless=False)
        res = workflow.resolve(self.hs, self.groq, BANK, t1, rng=random.Random(1), quiet=True)
        t2 = workflow.triage(self.hs, self.groq, BANK, ALERTS["ALERT-002"], include_stateless=False)
        self.assertTrue(any(res.incident_id in m for m in t2.rec.raw_recall_matches))
        self.assertLess(res.mttr, res.baseline)

    def test_novel_failure_is_learned_then_recognised(self):
        t = workflow.triage(self.hs, self.groq, BANK, ALERTS["ALERT-005"], include_stateless=False)
        res = workflow.resolve(self.hs, self.groq, BANK, t, quiet=True)
        self.assertTrue(res.escalated)
        again = workflow.triage(self.hs, self.groq, BANK, ALERTS["ALERT-009"], include_stateless=False)
        self.assertEqual(again.rec.matched_incident_id, res.incident_id)

    def test_failed_fix_is_remembered_as_a_warning(self):
        t = workflow.triage(self.hs, self.groq, BANK, ALERTS["ALERT-001"], include_stateless=False)
        workflow.resolve(self.hs, self.groq, BANK, t, outcome="failure", quiet=True)
        t2 = workflow.triage(self.hs, self.groq, BANK, ALERTS["ALERT-002"], include_stateless=False)
        self.assertTrue(any("FAILED" in w for w in t2.rec.warnings))

    def test_wait_until_recallable_times_out_gracefully(self):
        self.assertIsNone(hm.wait_until_recallable(self.hs, BANK, "INC-999", ALERTS["ALERT-001"]["message"], timeout=0.05))

    def test_simulation_runs_all_steps_and_learns(self):
        steps = simulation.run_sequence(self.hs, self.groq, "sim", list(ALERTS.values()), INCIDENTS)
        self.assertEqual(len(steps), len(simulation.CURVE_ORDER))
        self.assertFalse([s for s in steps if "error" in s])
        self.assertTrue(steps[2]["escalated"])            # ALERT-005: never seen
        self.assertFalse(steps[-1]["escalated"])          # ALERT-009: recognised
        self.assertGreater(sum(s["saved_minutes"] for s in steps), 100)


class TestAgentRobustness(Base):
    def test_stateless_is_real_advice_not_an_echo(self):
        out = agent.handle_alert_stateless(self.groq, ALERTS["ALERT-001"]["message"])
        self.assertGreaterEqual(len(out.split()), 15)
        self.assertNotIn("PagerDuty", out)

    def test_stateless_falls_back_when_llm_is_down(self):
        broken = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            create=lambda **k: (_ for _ in ()).throw(RuntimeError("down")))))
        self.assertEqual(agent.handle_alert_stateless(broken, "x"), agent.GENERIC_FALLBACK)

    def test_extract_json_tolerates_fences_and_prose(self):
        self.assertEqual(agent.extract_json('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(agent.extract_json('Sure! {"a": 2} hope that helps'), {"a": 2})
        self.assertIsNone(agent.extract_json("no json here"))

    def test_hallucinated_incident_id_is_discarded(self):
        liar = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **k: SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(
                {"matched_incident_id": "INC-999", "confidence": "high", "recommended_fix_command": "rm -rf /"})))]))))
        rec = agent.handle_alert_with_memory(self.hs, liar, BANK, ALERTS["ALERT-001"]["message"])
        self.assertIsNone(rec.matched_incident_id)
        self.assertEqual(rec.confidence, "low")
        self.assertTrue(any("INC-999" in w for w in rec.warnings))

    def test_chat_falls_back_to_next_model(self):
        calls = []

        def create(model, **k):
            calls.append(model)
            if len(calls) == 1:
                raise RuntimeError("rate limited")
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])

        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        self.assertEqual(agent.chat(client, [], temperature=0, max_tokens=5), "ok")
        self.assertGreaterEqual(len(calls), 2)

    def test_retry_recovers_from_transient_errors(self):
        n = {"c": 0}

        def flaky():
            n["c"] += 1
            if n["c"] < 3:
                raise ConnectionError("blip")
            return "done"

        self.assertEqual(hm._retry(flaky), "done")


class TestGuardrails(unittest.TestCase):
    def test_destructive_is_blocked(self):
        self.assertEqual(guardrails.gate("redis-cli flushall", "high", "INC-1").action, "blocked")
        self.assertEqual(guardrails.gate("kubectl delete namespace prod", "high", "INC-1").action, "blocked")

    def test_mutating_needs_approval_and_readonly_is_auto(self):
        self.assertEqual(guardrails.gate("kubectl rollout undo deployment/x", "high", "INC-1").action, "approval")
        self.assertEqual(guardrails.gate("kubectl get pods -n prod", "high", "INC-1").action, "auto")
        self.assertEqual(guardrails.gate("kubectl get pods", "medium", "INC-1").action, "approval")

    def test_pipeline_takes_the_riskiest_segment(self):
        self.assertEqual(guardrails.assess_command("kubectl get pods && kubectl delete pod x")[0], "high")

    def test_unknown_commands_fail_closed(self):
        self.assertEqual(guardrails.assess_command("somecustomtool --do-it")[0], "medium")

    def test_low_confidence_or_no_match_escalates(self):
        self.assertEqual(guardrails.gate("kubectl get pods", "low", "INC-1").action, "escalate")
        self.assertEqual(guardrails.gate("kubectl get pods", "high", None).action, "escalate")


class TestMetricsAndInsights(Base):
    def test_summary_math(self):
        metrics.record({"mode": "memory", "outcome": "success", "mttr_minutes": 10, "baseline_minutes": 40})
        metrics.record({"mode": "escalated", "outcome": "success", "mttr_minutes": 30, "baseline_minutes": 30})
        s = metrics.summary(cost_per_minute=1000)
        self.assertEqual((s["incidents"], s["memory_assisted"], s["escalated"]), (2, 1, 1))
        self.assertEqual((s["minutes_saved"], s["dollars_saved"]), (30.0, 30000))

    def test_next_incident_id_advances(self):
        self.assertEqual(metrics.next_incident_id(), "INC-111")
        metrics.record({"mode": "memory", "outcome": "success", "mttr_minutes": 1, "baseline_minutes": 2})
        self.assertEqual(metrics.next_incident_id(), "INC-112")

    def test_team_insights_flag_knowledge_loss(self):
        text = hm.reflect_team_insights(self.hs, BANK).text
        self.assertIn("Marcus Lee", text)
        self.assertIn("left the company", text)

    def test_memory_text_marks_departed_engineers(self):
        marcus = next(i for i in INCIDENTS if i["resolved_by"] == "Marcus Lee")
        self.assertIn("has since left the company", hm.incident_to_memory_text(marcus))


if __name__ == "__main__":
    unittest.main()
