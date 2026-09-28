"""
The agent, deliberately split in two so the before/after is impossible to miss:

  handle_alert_stateless()    what a plain LLM does with zero memory
  handle_alert_with_memory()  the same LLM once Hindsight recall + reflect are in the loop

We call Hindsight directly from code instead of exposing it as an LLM tool, so there is no
function-calling step that can fail mid-incident. LLM calls fall back across models and the
JSON reply is parsed defensively.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field

from . import config, hindsight_memory

STATELESS_SYSTEM = (
    "You are an on-call SRE assistant with no access to past incident history or postmortems. "
    "Given an alert, reply with a numbered list of 4-5 generic troubleshooting steps. "
    "Do not repeat or restate the alert. Be concise."
)

MEMORY_SYSTEM = (
    "You are an on-call SRE assistant with access to this team's incident memory (recalled from Hindsight, "
    "below). Give a specific recommendation grounded ONLY in that memory. Rules: cite the matching past incident "
    "ID only if it appears in the memory; if nothing genuinely matches, set matched_incident_id to null and "
    "confidence to \"low\" instead of guessing; never recommend a remediation the memory records as FAILED; "
    "add a warning if the engineer who resolved it has left the company. Respond with ONLY a JSON object with keys: "
    '"matched_incident_id" (string|null), "root_cause" (string), "recommended_fix_command" (string), '
    '"resolved_by_last_time" (string|null), "prior_resolution_time_minutes" (number|null), '
    '"confidence" ("high"|"medium"|"low"), "warnings" (array of strings), '
    '"summary_for_engineer" (1-2 confident sentences). No markdown, no code fences.'
)

GENERIC_FALLBACK = (
    "1. Check application and system logs around the alert time.\n2. Inspect CPU, memory, and disk usage.\n"
    "3. Review recent deployments and config changes.\n4. Restart the affected service or pod.\n"
    "5. Escalate if the issue persists."
)


@dataclass
class MemoryRecommendation:
    matched_incident_id: str | None
    root_cause: str
    recommended_fix_command: str
    resolved_by_last_time: str | None
    prior_resolution_time_minutes: float | None
    confidence: str
    summary_for_engineer: str
    warnings: list[str] = field(default_factory=list)
    raw_recall_matches: list[str] = field(default_factory=list)
    raw_scores: list = field(default_factory=list)
    raw_reflection: str = ""
    latency_seconds: float = 0.0

    @property
    def is_novel(self) -> bool:
        return not self.matched_incident_id or self.confidence == "low"


def _clean(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()


def chat(groq_client, messages, *, temperature: float, max_tokens: int) -> str:
    """Try the primary model, then fallbacks; treat empty output as a failure."""
    models = list(dict.fromkeys([config.GROQ_MODEL, *config.GROQ_FALLBACK_MODELS]))
    last = None
    for model in models:
        for attempt in range(2):
            try:
                out = groq_client.chat.completions.create(
                    model=model, messages=messages, temperature=temperature, max_tokens=max_tokens
                )
                text = _clean(out.choices[0].message.content)
                if not text:
                    raise ValueError("empty completion")
                return text
            except Exception as e:  # noqa: BLE001
                last = e
                time.sleep(0.7 * (attempt + 1))
    raise RuntimeError(f"All Groq models failed: {last}")


def extract_json(raw: str) -> dict | None:
    """Parse the model's JSON reply, tolerating prose or code fences around it."""
    candidates = [raw.strip()]
    m = re.search(r"\{.*\}", raw, re.S)
    if m:
        candidates.append(m.group(0))
    for c in candidates:
        try:
            obj = json.loads(c)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    return None


def format_alert(message: str, logs: list[str] | None = None) -> str:
    return message + ("\nLog excerpt:\n" + "\n".join(logs) if logs else "")


def handle_alert_stateless(groq_client, message: str, logs: list[str] | None = None) -> str:
    try:
        text = chat(groq_client, [
            {"role": "system", "content": STATELESS_SYSTEM},
            {"role": "user", "content": format_alert(message, logs)},
        ], temperature=0.4, max_tokens=500)
    except RuntimeError:
        return GENERIC_FALLBACK
    return text if len(text.split()) >= 15 else GENERIC_FALLBACK  # guard against echoes/truncation


def _to_float(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def handle_alert_with_memory(hindsight_client, groq_client, bank_id: str, message: str,
                             logs: list[str] | None = None) -> MemoryRecommendation:
    t0 = time.time()
    alert_text = format_alert(message, logs)
    recall = hindsight_memory.recall_similar_incidents(hindsight_client, bank_id, alert_text)
    reflect = hindsight_memory.reflect_on_alert(hindsight_client, bank_id, alert_text)
    s = hindsight_memory.summarize_recall(recall, reflect)
    for r in hindsight_memory.recall_failed_attempts(hindsight_client, bank_id, alert_text):
        if r.text not in s.raw_matches:  # surface recorded failures even if the main recall missed them
            s.raw_matches.append(r.text)
            s.scores.append(getattr(r, "score", None))

    lines = [
        f"- [relevance {sc}] {m}" if sc is not None else f"- {m}"
        for m, sc in zip(s.raw_matches, s.scores)
    ]
    context = "Recalled memory:\n" + ("\n".join(lines) or "(nothing recalled)") + \
              "\n\nHindsight reflection:\n" + s.reflection_text

    raw = chat(groq_client, [
        {"role": "system", "content": MEMORY_SYSTEM},
        {"role": "user", "content": f"Alert: {alert_text}\n\n{context}"},
    ], temperature=0.2, max_tokens=700)
    parsed = extract_json(raw)
    warnings: list[str] = []

    if parsed is None:
        parsed = {"root_cause": "Could not parse a structured answer.", "confidence": "low",
                  "summary_for_engineer": raw[:300]}
        warnings.append("Model reply was not valid JSON; treating as low confidence.")

    known_ids = set(re.findall(r"INC-\d+", " ".join(s.raw_matches) + " " + s.reflection_text))
    mid = parsed.get("matched_incident_id")
    if mid and mid not in known_ids:  # anti-hallucination: only cite IDs that were actually recalled
        warnings.append(f"Model cited {mid}, which is not in recalled memory; discarded.")
        mid = None
    conf = str(parsed.get("confidence", "low")).lower()
    if conf not in {"high", "medium", "low"} or not mid:
        conf = "low"
    fix = parsed.get("recommended_fix_command") or ""
    if isinstance(fix, list):
        fix = " && ".join(map(str, fix))
    extra = parsed.get("warnings") or []
    warnings += [str(w) for w in (extra if isinstance(extra, list) else [extra])]

    return MemoryRecommendation(
        matched_incident_id=mid,
        root_cause=str(parsed.get("root_cause", "")),
        recommended_fix_command=str(fix),
        resolved_by_last_time=parsed.get("resolved_by_last_time"),
        prior_resolution_time_minutes=_to_float(parsed.get("prior_resolution_time_minutes")),
        confidence=conf,
        summary_for_engineer=str(parsed.get("summary_for_engineer", "")),
        warnings=warnings,
        raw_recall_matches=s.raw_matches,
        raw_scores=s.scores,
        raw_reflection=s.reflection_text,
        latency_seconds=round(time.time() - t0, 1),
    )
