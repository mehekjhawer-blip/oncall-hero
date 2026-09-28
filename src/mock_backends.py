"""
Offline stand-ins for Hindsight and Groq.

NOT the product. They exist so that (a) the test-suite runs with no network or keys, and
(b) there is a safety net if venue Wi-Fi dies during a live demo. Everything using them is
labelled OFFLINE on screen.

FakeHindsight is a real (if simple) memory: retained text is persisted to disk and recall
ranks by IDF-weighted keyword overlap with a recency tie-break, so the retain -> recall loop
genuinely works -- a memory written seconds ago really is found by the next query.
"""
from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

_STOP = set(
    "a an the of on in at to for and or is are was were be been with by from this that it its as into "
    "after before during then than but not no past incident incidents matching alert pagerduty sev "
    "logs again new".split()
)
MATCH_THRESHOLD = 0.22
HIGH_CONFIDENCE = 0.45


def tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9][a-z0-9\-\._]*", text.lower()) if t not in _STOP and len(t) > 2}


class FakeHindsight:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db: dict[str, list[dict]] = json.loads(self.path.read_text()) if self.path.exists() else {}

    def _save(self) -> None:
        self.path.write_text(json.dumps(self._db))

    # -- bank management ------------------------------------------------------
    def create_bank(self, bank_id, name=None, **kw):
        self._db.setdefault(bank_id, [])
        self._save()

    def delete_bank(self, bank_id, **kw):
        self._db.pop(bank_id, None)
        self._save()

    # -- the three Hindsight operations ---------------------------------------
    def retain(self, bank_id, content, context=None, timestamp=None, metadata=None, **kw):
        docs = self._db.setdefault(bank_id, [])
        docs.append(
            {
                "text": content,
                "context": context,
                "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
                "metadata": metadata or {},
                "seq": len(docs),
            }
        )
        self._save()
        return SimpleNamespace(success=True)

    def recall(self, bank_id, query, budget=None, **kw):
        docs = self._db.get(bank_id, [])
        q = tokens(query)
        if not q or not docs:
            return SimpleNamespace(results=[])
        df: dict[str, int] = {}
        doc_tokens = [tokens(d["text"]) for d in docs]
        for dt in doc_tokens:
            for t in dt:
                df[t] = df.get(t, 0) + 1
        n = len(docs)
        idf = lambda t: math.log(1 + n / df.get(t, 0.5))
        denom = sum(idf(t) for t in q) or 1.0
        scored = []
        for d, dt in zip(docs, doc_tokens):
            score = sum(idf(t) for t in q & dt) / denom
            if "OUTCOME:" in d["text"]:
                score += 0.08  # a recorded outcome (worked OR failed) outranks an old write-up
            if score >= (0.10 if "failed" in query.lower() else MATCH_THRESHOLD):
                scored.append((score, d["seq"], d))
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)  # score, then recency
        return SimpleNamespace(
            results=[
                SimpleNamespace(text=d["text"], score=round(s, 3), metadata=d["metadata"])
                for s, _, d in scored[:4]
            ]
        )

    def reflect(self, bank_id, query, **kw):
        docs = self._db.get(bank_id, [])
        if re.search(r"recurring|left the company|single (person|engineer)|knowledge", query, re.I):
            return SimpleNamespace(text=_aggregate_insights(docs))
        res = self.recall(bank_id, query)
        if not res.results:
            return SimpleNamespace(text="No memory closely matches this situation.")
        top = res.results[0]
        return SimpleNamespace(text=f"Closest memory (relevance {top.score}): {top.text}")


def _aggregate_insights(docs: list[dict]) -> str:
    if not docs:
        return "Memory bank is empty."
    resolvers: dict[str, int] = {}
    left: set[str] = set()
    cats: dict[str, int] = {}
    failed = 0
    for d in docs:
        m = re.search(r"Resolved by (.+?) in [\d.]+ minutes", d["text"])
        if m:
            name = re.sub(r"\s*\(has since left the company\)", "", m.group(1))
            resolvers[name] = resolvers.get(name, 0) + 1
            if "has since left the company" in m.group(1):
                left.add(name)
        c = re.search(r"\[category: ([\w\-]+)\]", d["text"])
        if c:
            cats[c.group(1)] = cats.get(c.group(1), 0) + 1
        if "OUTCOME: FAILED" in d["text"]:
            failed += 1
    lines = [f"Memory holds {len(docs)} incidents."]
    if cats:
        top = sorted(cats.items(), key=lambda x: -x[1])[:3]
        lines.append("Most common failure categories: " + ", ".join(f"{k} ({v})" for k, v in top) + ".")
    if resolvers:
        lines.append("Resolutions by engineer: " + ", ".join(f"{k} ({v})" for k, v in sorted(resolvers.items(), key=lambda x: -x[1])) + ".")
    if left:
        lines.append(
            "KNOWLEDGE-LOSS RISK: " + ", ".join(sorted(left)) + " resolved incidents but has since left the company; "
            "these memories are now the only documented runbooks for those failure modes."
        )
    if failed:
        lines.append(f"{failed} remediation attempt(s) are recorded as FAILED and should not be repeated.")
    return " ".join(lines)


GENERIC_STEPS = (
    "1. Check application and system logs for errors around the time the alert fired.\n"
    "2. Inspect resource usage (CPU, memory, disk, network) on the affected hosts or pods.\n"
    "3. Review recent deployments and configuration changes.\n"
    "4. Restart the affected service or pod if it appears hung.\n"
    "5. Escalate to a senior engineer if the issue persists."
)


class FakeGroq:
    """Rule-based stand-in for the Groq chat API. Parses the memory context the agent
    passes in and answers in the same JSON schema the real model is asked for."""

    def __init__(self):
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, model=None, messages=None, **kw):
        system, user = messages[0]["content"], messages[-1]["content"]
        if "no access to past incident" in system:
            content = GENERIC_STEPS
        elif "postmortem action items" in system.lower():
            content = (
                "- Add an alert on the leading indicator so this is caught before customer impact.\n"
                "- Add this failure mode and its fix to the on-call runbook.\n"
                "- Add a regression test or config validation to prevent recurrence."
            )
        else:
            content = json.dumps(self._structured(user))
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])

    @staticmethod
    def _structured(user: str) -> dict:
        matches = re.findall(r"^- \[relevance ([\d.]+)\] (.+)$", user, re.M)
        empty = {
            "matched_incident_id": None,
            "root_cause": "No sufficiently similar past incident in memory.",
            "recommended_fix_command": "",
            "resolved_by_last_time": None,
            "prior_resolution_time_minutes": None,
            "confidence": "low",
            "warnings": ["No confident memory match; escalating to a human."],
            "summary_for_engineer": "I have not seen this failure before, so I will not guess. Escalating to a human; "
            "I will remember whatever fixes it.",
        }
        warnings: list[str] = []
        for _, text in matches:  # surface every recorded failure, wherever it ranks
            if "OUTCOME: FAILED" in text:
                m = re.search(r"Attempted remediation: (.*?) Attempted by", text)
                warnings.append(f"A previous attempt ({(m.group(1) if m else 'a fix')[:70]}) FAILED for a similar alert; avoiding it.")
        for score, text in matches:
            if "OUTCOME: FAILED" in text:
                continue
            inc = re.search(r"Incident (INC-\d+)", text)
            root = re.search(r"Root cause: (.*?) Resolution:", text)
            res = re.search(r"Resolution: (.*?) Resolved by", text)
            who = re.search(r"Resolved by (.+?) in ([\d.]+) minutes", text)
            if not (inc and res and who):
                continue
            cmds = re.findall(r"`([^`]+)`", res.group(1))
            name = re.sub(r"\s*\(has since left the company\)", "", who.group(1))
            if "has since left the company" in who.group(1):
                warnings.append(f"{name} has left the company; this memory is the only documented runbook for this failure.")
            s = float(score)
            conf = "high" if s >= HIGH_CONFIDENCE else "medium"
            return {
                "matched_incident_id": inc.group(1),
                "root_cause": root.group(1) if root else "See matched incident.",
                "recommended_fix_command": " && ".join(cmds),
                "resolved_by_last_time": name,
                "prior_resolution_time_minutes": float(who.group(2)),
                "confidence": conf,
                "warnings": warnings,
                "summary_for_engineer": f"This matches {inc.group(1)}, fixed by {name} in {who.group(2)} min. "
                f"Recommended: {' && '.join(cmds) or 'see incident'}.",
            }
        if warnings:
            empty["warnings"] = warnings + empty["warnings"]
        return empty
