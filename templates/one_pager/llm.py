"""
Self-contained local-Ollama client for the one-pager generator.

ISOLATION CONTRACT (see FORMAT.md §7): this deliberately does NOT import
reasoning/qwen_client.py, even though that client is more capable. Two reasons:

  1. The existing one-pager tooling imports zero project modules. Reaching into
     reasoning/ would make a pipeline refactor able to break one-pager
     generation, which is exactly what the owner asked to prevent.
  2. It sidesteps a real version trap. qwen_client uses the `ollama` package's
     Client.chat(..., think=False, format=<dict schema>). requirements.txt pins
     ollama==0.6.2 and system python has it — but the repo's own venv/ still
     carries ollama 0.2.1, whose chat() has no `think` parameter at all and
     accepts only format: Literal['', 'json']. Running under that interpreter
     would raise TypeError. A raw HTTP POST has no such coupling.

What IS copied from qwen_client, because it was learned the hard way there:
  * think=false — measured on this machine, 5x faster (65s -> 12.6s) with no
    loss of instruction-following.
  * A JSON-schema `format`, wrapped in an OBJECT (never a top-level array),
    with every key required and no anyOf/nullable types — nullable types
    break llama.cpp constrained decoding on 7B models and yield empty output.
  * Empty-string sentinels normalised back to None after parsing.
  * 2 attempts with a fixed 2s pause.

What is deliberately INVERTED: qwen_client raises so its caller can decide.
Here the caller has exactly one sane policy — carry on and let a human write
the prose — so this returns None instead. A drafting failure must never cost
the deck parsing and image extraction that already succeeded.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Optional

logger = logging.getLogger(__name__)

BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
# The 7B extraction model, not the 14B: this is structured extraction from
# supplied text, which is exactly what qwen_client routes to 7B ("14B for
# judgment, 7B for volume"). Overridable without touching code.
MODEL = os.environ.get("ONEPAGER_MODEL", "qwen2.5:7b-instruct")
TIMEOUT_S = float(os.environ.get("ONEPAGER_TIMEOUT", "75"))  # matches _extract_client's budget

SECTION_KEYS = ("loesung", "mehrwerte", "usp", "zielgruppe", "geschaeftsmodell")
META_KEYS = ("location", "founded", "team_size")

_SCHEMA = {
    "type": "object",
    "properties": {
        "claim": {"type": "string"},
        "location": {"type": "string"},
        "founded": {"type": "string"},
        "team_size": {"type": "string"},
        "loesung": {"type": "string"},
        "mehrwerte": {"type": "string"},
        "usp": {"type": "string"},
        "zielgruppe": {"type": "string"},
        "geschaeftsmodell": {"type": "string"},
    },
    "required": ["claim", "location", "founded", "team_size", *SECTION_KEYS],
}

_SYSTEM = (
    "Du bist Analyst bei GT Hub und schreibst One-Pager über Startups auf Deutsch. "
    "Du schreibst ausschließlich auf Deutsch, sachlich und nüchtern. "
    "Du erfindest niemals Fakten. Wenn der Pitch Deck zu einem Punkt nichts hergibt, "
    "gibst du für dieses Feld einen leeren String zurück — niemals eine Vermutung, "
    "niemals einen Platzhalter."
)

_PROMPT = """Hier ist der Text aus dem Pitch Deck von "{name}".

REGELN — halte dich strikt daran:
- Nutze AUSSCHLIESSLICH Informationen, die im Deck-Text unten stehen.
- Kein Marketing-Sprech. Streiche "führend", "innovativ", "revolutionär".
- Wenn das Deck zu einem Feld nichts sagt: leerer String "". Nicht raten.

claim: Eine Nominalphrase, KEIN Satz — Produktkategorie plus das eine
  Unterscheidungsmerkmal. Kein Verb, kein Punkt am Ende, maximal 70 Zeichen.
  Gut: "Elektrischer Traktor mit Wechselbatterien"
  Schlecht: "Wir revolutionieren die Landwirtschaft."

location:  Ort/Stadt des Unternehmens, nur wenn im Deck genannt.
founded:   Gründungsjahr, nur wenn im Deck genannt.
team_size: Anzahl Personen im Team, nur wenn im Deck genannt (nur die Zahl).

loesung:          Was das Produkt ist und wie es funktioniert. 2-3 Sätze.
mehrwerte:        KPIs und Leistung. MUSS mindestens eine konkrete Zahl aus dem
                  Deck enthalten. 1-2 Sätze.
usp:              Warum nicht der etablierte Anbieter oder der Wettbewerber.
                  Nenne die konkrete Alternative, wenn das Deck sie nennt. 2-3 Sätze.
zielgruppe:       Wer kauft, plus Traktion (Kunden, Pilotprojekte, LOIs) — aber
                  nur, was im Deck belegt ist. 2-3 Sätze.
geschaeftsmodell: Wie Geld verdient wird. 1-2 Sätze.

DECK-TEXT:
{deck_text}
"""


def draft(name: str, deck_text: str, extra_text: str = "") -> Optional[dict]:
    """
    Draft the claim, meta fields and five sections from deck text.

    Returns a dict with every key present (unsupported ones as None), or None
    if Ollama could not be reached or returned unusable output. Never raises.
    """
    if not deck_text.strip():
        logger.warning("[llm] no deck text to draft from")
        return None

    body = deck_text if not extra_text.strip() else f"{deck_text}\n\n[Website]\n{extra_text.strip()}"
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": _PROMPT.format(name=name, deck_text=body)},
        ],
        "stream": False,
        "think": False,
        "format": _SCHEMA,
        "options": {"temperature": 0, "num_ctx": 8192, "num_predict": 1200},
    }

    last: Optional[Exception] = None
    for attempt in (1, 2):
        try:
            import httpx

            with httpx.Client(timeout=TIMEOUT_S) as client:
                resp = client.post(f"{BASE_URL}/api/chat", json=payload)
                resp.raise_for_status()
                content = resp.json()["message"]["content"]
            return _normalise(json.loads(_strip_thinking(content)))
        except Exception as exc:
            last = exc
            if attempt == 1:
                logger.warning(f"[llm] attempt 1 failed ({exc}) — retrying in 2s")
                time.sleep(2)

    logger.error(
        f"[llm] drafting failed twice ({last}). Continuing without prose — the "
        f"YAML is still written with everything extracted from the deck."
    )
    return None


def _strip_thinking(text: str) -> str:
    """Safety net in case a future model reintroduces a <think> block."""
    if "<think>" in text and "</think>" in text:
        return text.split("</think>", 1)[-1].strip()
    return text.strip()


def _normalise(data: dict) -> dict:
    """
    Empty-string sentinels -> None (the schema forbids nullable types, so ""
    is how the model says "the deck doesn't state this"). Also strips a
    trailing period off the claim, which FORMAT.md forbids and the model
    reliably adds anyway — a deterministic fix is better than another prompt
    round-trip, and render.py's validator rejects it outright.
    """
    out = {}
    for key in ("claim", *META_KEYS, *SECTION_KEYS):
        val = data.get(key)
        val = val.strip() if isinstance(val, str) else None
        out[key] = val or None

    if out.get("claim"):
        claim = out["claim"].rstrip()
        while claim.endswith("."):
            claim = claim[:-1].rstrip()
        out["claim"] = claim or None

    # team_size should carry only the number; the model tends to write
    # "6 Personen". The meta line renders it as "Team: {value}".
    if out.get("team_size"):
        m = re.search(r"\d+", out["team_size"])
        out["team_size"] = m.group() if m else out["team_size"]

    return out


def health() -> Optional[str]:
    """Return None if Ollama is reachable, else a short human reason."""
    try:
        import httpx

        with httpx.Client(timeout=5) as client:
            r = client.get(f"{BASE_URL}/api/tags")
            r.raise_for_status()
        return None
    except Exception as exc:
        return f"Ollama nicht erreichbar unter {BASE_URL} ({exc})"
