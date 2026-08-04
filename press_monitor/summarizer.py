"""
Short local-LLM summary for one press-monitor match (Phase PM, 4 Aug 2026).

Reuses reasoning/qwen_client.py's existing generate() — the 14B reasoning
model, same tier as every other low-volume judgment task in this project
(a handful of matches a day at most, not per-chunk extraction volume).
Local only, same privacy invariant as the rest of the pipeline.
"""
import logging

logger = logging.getLogger(__name__)

_SYSTEM = (
    "Du bist ein Presse-Monitoring-Assistent für den GreenTech Hub. Du fasst "
    "Zeitungsartikel so konkret zusammen, dass jemand den eigentlichen Inhalt "
    "versteht, ohne den Artikel selbst zu lesen. Erfinde keine Informationen "
    "über den Text hinaus."
)

_PROMPT = """Im folgenden Zeitungsausschnitt wurde der Begriff "{term}" gefunden.

Textausschnitt:
{excerpt}

Fasse den INHALT dieses Artikels konkret zusammen — nicht nur das Thema,
sondern die wichtigsten Fakten, Zahlen, Zitate oder Aussagen, die tatsächlich
im Text stehen. Was genau ist passiert oder wurde gesagt? Wer ist beteiligt?
Schreibe 3-4 Sätze auf Deutsch, so konkret wie möglich — jemand, der nur diese
Zusammenfassung liest, soll wissen, was im Artikel steht.

Falls "{term}" hier klar erkennbar die relevante Firma/Organisation ist,
erwähne das kurz. Falls es sich erkennbar um eine andere Bedeutung handelt
(z. B. ein Personenname, ein Zufallstreffer), weise das stattdessen
ausdrücklich aus — dann reicht ein Satz, ein Inhalts-Detail ist dann nicht nötig.

Antworte nur mit der Zusammenfassung, keine Einleitung."""


def summarize_match(term: str, excerpt: str) -> str:
    """
    Never raises — a summary failure degrades to a short deterministic
    fallback (the excerpt itself, truncated) rather than blocking the whole
    daily digest over one bad LLM call.
    """
    try:
        from reasoning.qwen_client import qwen_client
        prompt = _PROMPT.format(term=term, excerpt=excerpt[:3000])
        return qwen_client.generate(prompt, system=_SYSTEM, temperature=0.2, max_tokens=450).strip()
    except Exception as exc:
        logger.warning(f"[PressMonitor] summary generation failed for {term!r}: {exc}")
        snippet = excerpt.strip().replace("\n", " ")[:280]
        return f"(Automatische Zusammenfassung nicht verfügbar) Textausschnitt: {snippet}…"
