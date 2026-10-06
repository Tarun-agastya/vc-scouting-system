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
# Gemma 4 12B, chosen by side-by-side test on the real Eidola deck (Oct 2026):
#   qwen2.5:7b  47s  fluent-looking but wrong — called a customer a competitor
#   qwen3:14b  111s  good German, but copied a figure from the prompt's example
#   gemma4:12b  77s  clearest prose, found the city and founding year, named the
#                    real references, and left out a figure the deck doesn't have
# The one-pager is written once per startup and read by partners, so quality
# beats speed here. Overridable without touching code.
MODEL = os.environ.get("ONEPAGER_MODEL", "gemma4:12b")
# A whole deck (~12-14k characters once cleaned, see deck.content_text) plus
# website text, the example page and the answer need ~7-8k tokens; 16k leaves
# headroom. The old 8k context is why decks used to be cut to a quarter.
NUM_CTX = int(os.environ.get("ONEPAGER_NUM_CTX", "16384"))
TIMEOUT_S = float(os.environ.get("ONEPAGER_TIMEOUT", "240"))

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

# ── Drafting prompts ─────────────────────────────────────────────────────────
# History: the first prompt asked for "2-3 sentences" and got ONE short fragment
# per section on real decks ("Identifizieren, veredeln und liefern mineralischer
# Nebenprodukte.") — nothing a reader who doesn't know the company can follow.
# What fixed it, measured on the real Eidola deck: the WHOLE deck reaching the
# model (deck.content_text), a word range per section, and an explicit reader
# ("someone who has never heard of the company").
#
# Deliberately NO example page. One was tried (LIGARO's finished page): qwen3
# copied its "bis zu 90 %" into Eidola's draft, and the number check could not
# catch it because a "90" happened to sit on one of Eidola's chart axes. A
# figure from another company on an outward-facing page is the worst failure
# this tool can have, so the length guidance lives in the word ranges instead.

_SYSTEM_EN = (
    "You are an analyst at GT Hub writing one-pagers about startups in English for "
    "corporate partners who have never heard of the company. You write clear, complete, "
    "factual sentences that explain what the company does — never fragments, never "
    "marketing language. You never invent facts: if the material says nothing about a "
    "point, you return an empty string for that field."
)

_PROMPT_EN = """Write the one-pager for "{name}" from the pitch deck text (and website text, if
given) below. The reader is a decision-maker at a corporate partner who has never heard
of this company and needs to understand it from this page alone.

RULES:
- Use ONLY facts stated in the material below. Do not guess, do not calculate new numbers.
- The PITCH DECK is the main source. The company website and the [Web search] results
  are extra: use a web result only when it is clearly about this company, and when it
  disagrees with the deck, follow the deck.
- Write complete sentences that explain, not keyword lists. Name concrete things:
  the material, the technology, the customers, pilot partners, figures, years.
- No marketing speak ("leading", "innovative", "revolutionary", "unique" without proof).
- Each section has a word range. Stay inside it — shorter is NOT better here.
- Field empty "" only if the material really says nothing about it.

claim: a noun phrase, NOT a sentence — product category plus the one differentiator.
  No verb, no full stop, at most 70 characters.

location:  city of the company, only if stated (e.g. in an address).
founded:   the year the COMPANY was founded (look for "founded", "incorporated",
           a milestone like "June 2025: company founded"). Not the date of the deck,
           not when research started. Only if stated.
team_size: number of people on the team, only if stated (number only).

loesung (35-55 words, 3-4 sentences): what the company offers and how it works —
  input, process, output. A reader must be able to picture the product or service.
mehrwerte (30-50 words, 2-3 sentences): the measurable benefit for the customer and
  the environment/economy. MUST contain at least one concrete figure from the material —
  look in results, case studies and milestones too (percentages, tonnes, costs, volumes).
usp (35-55 words, 2-3 sentences): why this instead of the usual way or the competitors.
  Name the concrete alternative if the material does.
zielgruppe (30-50 words, 2-3 sentences): who buys or uses it, plus evidence of traction —
  named pilot partners, customers, references, LOIs, milestones.
geschaeftsmodell (20-35 words, 1-2 sentences): how money is made — what is sold to whom.

MATERIAL ABOUT "{name}":
{deck_text}
"""

_SYSTEM_DE = (
    "Du bist Analyst bei GT Hub und schreibst One-Pager über Startups auf Deutsch – für "
    "Entscheider bei Konzernpartnern, die das Unternehmen noch nie gehört haben. Du "
    "schreibst klare, vollständige, sachliche Sätze, die erklären, was das Unternehmen "
    "tut – niemals Stichworte, niemals Marketing-Sprech. Du erfindest niemals Fakten: Wenn "
    "das Material zu einem Punkt nichts hergibt, gibst du für dieses Feld einen leeren "
    "String zurück."
)

_PROMPT_DE = """Schreibe den One-Pager für "{name}" aus dem Pitch-Deck-Text (und ggf. dem
Website-Text) unten. Der Leser ist Entscheider bei einem Konzernpartner, kennt das
Unternehmen nicht und muss es allein aus dieser Seite verstehen.

REGELN:
- Nutze AUSSCHLIESSLICH Fakten aus dem Material unten. Nicht raten, keine neuen Zahlen errechnen.
- Das PITCH DECK ist die Hauptquelle. Website und [Websuche]-Ergebnisse ergänzen es: Nutze
  ein Websuch-Ergebnis nur, wenn es eindeutig dieses Unternehmen betrifft, und folge bei
  Widersprüchen dem Deck.
- Schreibe vollständige, erklärende Sätze, keine Stichwortlisten. Nenne Konkretes:
  Material, Technologie, Kunden, Pilotpartner, Zahlen, Jahreszahlen.
- Kein Marketing-Sprech ("führend", "innovativ", "revolutionär", "einzigartig" ohne Beleg).
- Jeder Abschnitt hat eine Wortspanne. Halte sie ein – kürzer ist hier NICHT besser.
- Leerer String "" nur, wenn das Material zu dem Feld wirklich nichts sagt.

claim: Eine Nominalphrase, KEIN Satz – Produktkategorie plus das eine
  Unterscheidungsmerkmal. Kein Verb, kein Punkt am Ende, maximal 70 Zeichen.

location:  Ort/Stadt des Unternehmens, nur wenn genannt (z. B. in einer Adresse).
founded:   Das Jahr, in dem das UNTERNEHMEN gegründet wurde (suche nach "gegründet",
           "Gründung", einem Meilenstein wie "Juni 2025: Unternehmensgründung"). Nicht das
           Datum des Decks, nicht der Forschungsstart. Nur wenn genannt.
team_size: Anzahl Personen im Team, nur wenn genannt (nur die Zahl).

loesung (35-55 Wörter, 3-4 Sätze): Was das Unternehmen anbietet und wie es
  funktioniert – Ausgangsstoff, Verfahren, Ergebnis. Der Leser muss sich das Produkt
  oder die Dienstleistung vorstellen können.
mehrwerte (30-50 Wörter, 2-3 Sätze): Der messbare Nutzen für Kunden und Umwelt/Wirtschaft.
  MUSS mindestens eine konkrete Zahl aus dem Material enthalten – suche auch in
  Ergebnissen, Fallstudien und Meilensteinen (Prozent, Tonnen, Kosten, Mengen).
usp (35-55 Wörter, 2-3 Sätze): Warum dies statt des üblichen Wegs oder der Wettbewerber.
  Nenne die konkrete Alternative, wenn das Material sie nennt.
zielgruppe (30-50 Wörter, 2-3 Sätze): Wer kauft oder nutzt es, plus Belege für Traktion –
  namentliche Pilotpartner, Kunden, Referenzen, LOIs, Meilensteine.
geschaeftsmodell (20-35 Wörter, 1-2 Sätze): Wie Geld verdient wird – was an wen verkauft wird.

MATERIAL ZU "{name}":
{deck_text}
"""

_PROMPTS = {"en": (_SYSTEM_EN, _PROMPT_EN), "de": (_SYSTEM_DE, _PROMPT_DE)}

# ── Translation ──────────────────────────────────────────────────────────────
# The second language is TRANSLATED from the first draft, not drafted again
# from the deck. Two independent drafts would state different facts (the model
# picks different KPIs each time), and an English and a German page about the
# same startup must say the same thing. generate.py re-checks every number in
# the translation against the source draft.

TRANSLATE_KEYS = ("claim", "location", "founded", "team_size", *SECTION_KEYS)

_LANG_NAMES = {"en": "English", "de": "German"}

_TRANSLATE_SYSTEM = (
    "You are a professional translator for GT Hub, a German innovation hub. "
    "You translate startup one-pagers between English and German. You translate "
    "faithfully: you never add, drop or change a fact, a number, a unit or a name."
)

_TRANSLATE_PROMPT = """Translate every field below from {src} to {dst}.

RULES — follow them strictly:
- Keep every number, percentage, currency amount and unit exactly as written.
  Only adapt the decimal/thousand separators to {dst} conventions.
- Keep company, product and person names unchanged.
- Translate city names to their usual {dst} form (e.g. München <-> Munich).
- claim stays a noun phrase without a full stop, at most 70 characters.
- Keep the same sober, factual tone. Add nothing, explain nothing.
- A field that is empty "" stays empty "".
- "k. A." (German) and "n/a" (English) both mean "not stated": write {unknown}.

FIELDS (JSON):
{fields}
"""


def translate(fields: dict, src: str, dst: str) -> Optional[dict]:
    """
    Translate claim, meta fields and the five sections from `src` to `dst`.
    Returns a dict with every TRANSLATE_KEYS key, or None on failure. Never raises.
    """
    source = {k: (str(fields.get(k) or "")) for k in TRANSLATE_KEYS}
    if not any(v.strip() for v in source.values()):
        return {k: None for k in TRANSLATE_KEYS}
    unknown = '"n/a"' if dst == "en" else '"k. A."'
    prompt = _TRANSLATE_PROMPT.format(
        src=_LANG_NAMES[src], dst=_LANG_NAMES[dst], unknown=unknown,
        fields=json.dumps(source, ensure_ascii=False, indent=1))
    out = _chat(_TRANSLATE_SYSTEM, prompt, num_predict=1600)
    if out is None:
        return None
    norm = _normalise(out)
    # Empty in -> empty out, whatever the model did with it.
    for k in TRANSLATE_KEYS:
        if not source[k].strip():
            norm[k] = None
    return norm


def draft(name: str, deck_text: str, extra_text: str = "", lang: str = "en",
          web_text: str = "") -> Optional[dict]:
    """
    Draft the claim, meta fields and five sections from deck text, in `lang`.

    Returns a dict with every key present (unsupported ones as None), or None
    if Ollama could not be reached or returned unusable output. Never raises.
    """
    if not deck_text.strip():
        logger.warning("[llm] no deck text to draft from")
        return None

    body = f"[Pitch Deck]\n{deck_text}"
    if extra_text.strip():
        body += f"\n\n[Website des Unternehmens / company website]\n{extra_text.strip()}"
    if web_text.strip():
        body += ("\n\n[Websuche / Web search — Drittquellen, third-party sources]\n"
                 f"{web_text.strip()}")
    system, prompt = _PROMPTS.get(lang, _PROMPTS["en"])
    out = _chat(system, prompt.format(name=name, deck_text=body), num_predict=1600)
    return None if out is None else _normalise(out)


def _chat(system: str, user: str, *, num_predict: int) -> Optional[dict]:
    """One constrained-JSON call, 2 attempts. Parsed dict, or None. Never raises."""
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "think": False,
        "format": _SCHEMA,
        "options": {"temperature": 0, "num_ctx": NUM_CTX, "num_predict": num_predict},
    }

    last: Optional[Exception] = None
    for attempt in (1, 2):
        try:
            import httpx

            with httpx.Client(timeout=TIMEOUT_S) as client:
                resp = client.post(f"{BASE_URL}/api/chat", json=payload)
                resp.raise_for_status()
                content = resp.json()["message"]["content"]
            return json.loads(_strip_thinking(content))
        except Exception as exc:
            last = exc
            if attempt == 1:
                logger.warning(f"[llm] attempt 1 failed ({exc}) — retrying in 2s")
                time.sleep(2)

    logger.error(
        f"[llm] model call failed twice ({last}). Continuing without prose — the "
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

    # "not stated" markers are the generator's job to write, per language.
    for key in META_KEYS:
        if out.get(key) and out[key].strip().lower() in {"k. a.", "k.a.", "n/a", "n. a."}:
            out[key] = None

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
        return f"Ollama not reachable at {BASE_URL} ({exc})"
