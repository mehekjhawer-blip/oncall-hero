"""
Environment configuration and client factories.

Every script (demo, seed, simulation, dashboard) builds its Groq / Hindsight clients
here, so live vs. offline behaviour is decided in exactly one place.

OFFLINE mode swaps in deterministic local stand-ins (src/mock_backends.py). It exists
for two reasons: (1) tests can run without network or API keys, and (2) it is a safety
net if venue Wi-Fi dies mid-demo. It is always labelled OFFLINE on screen.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

try:  # python-dotenv is a convenience, not a hard dependency
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
STATE_DIR = ROOT / ".oncall_state"  # runtime files (metrics, offline memory); gitignored


def _flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


OFFLINE = _flag("ONCALL_OFFLINE")

# --- Groq (reasoning) ---------------------------------------------------------
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
# Tried in order if the primary model errors or returns nothing (rate limit, deprecation).
GROQ_FALLBACK_MODELS = [
    m.strip()
    for m in os.getenv("GROQ_FALLBACK_MODELS", "openai/gpt-oss-120b,qwen/qwen3-32b").split(",")
    if m.strip()
]

# --- Hindsight (memory) ---------------------------------------------------------
HINDSIGHT_API_KEY = os.getenv("HINDSIGHT_API_KEY", "")
HINDSIGHT_BASE_URL = os.getenv("HINDSIGHT_BASE_URL", "https://api.hindsight.vectorize.io")
HINDSIGHT_BANK_ID = os.getenv("HINDSIGHT_BANK_ID", "prod-cluster-3")

# --- Business assumption (clearly labelled wherever it is displayed) -----------
OUTAGE_COST_PER_MINUTE = float(os.getenv("OUTAGE_COST_PER_MINUTE", "5000"))


def set_offline(value: bool = True) -> None:
    global OFFLINE
    OFFLINE = value


def is_offline() -> bool:
    return OFFLINE


def require_env(*names: str) -> None:
    missing = [n for n in names if not os.getenv(n)]
    if missing:
        print(
            f"\n[on-call-hero] Missing environment variable(s): {', '.join(missing)}\n"
            "Copy .env.example to .env and fill them in, or run with --offline to use the\n"
            "local replay backend (no keys or network needed).\n",
            file=sys.stderr,
        )
        sys.exit(1)


def get_groq_client():
    if OFFLINE:
        from .mock_backends import FakeGroq

        return FakeGroq()
    from groq import Groq

    require_env("GROQ_API_KEY")
    return Groq(api_key=GROQ_API_KEY)


def get_hindsight_client():
    if OFFLINE:
        from .mock_backends import FakeHindsight

        return FakeHindsight(STATE_DIR / "offline_memory.json")
    from hindsight_client import Hindsight

    require_env("HINDSIGHT_API_KEY")
    return Hindsight(base_url=HINDSIGHT_BASE_URL, api_key=HINDSIGHT_API_KEY)
