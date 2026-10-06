"""
The two languages a one-pager exists in, and every piece of page text that
depends on the language.

Every startup gets BOTH versions, as two files side by side:

    data/<slug>.en.yaml      data/<slug>.de.yaml

Each file says which language it is in (`lang: en` / `lang: de`), and render.py
and export_pptx.py take the headings, labels and the "unknown" marker from here
by that field. The YAML *keys* (loesung, mehrwerte, ...) are fixed identifiers
and stay the same in both languages — only the text inside them changes.

German is the final, exported version (FINAL_LANG) and is drafted from the
deck; the English twin is translated from it, for reading and sharing
internally. A YAML without `lang:` is German — every file written before the
two-language change was.
"""
from __future__ import annotations

LANGS = ("en", "de")
DEFAULT_LANG = "en"         # fallback for labels() only
# The version that is exported and sent out is ALWAYS German. It is therefore
# the one drafted straight from the deck; English is translated from it.
FINAL_LANG = "de"
LEGACY_LANG = "de"          # files from before `lang:` existed were all German

SECTION_KEYS = ("loesung", "mehrwerte", "usp", "zielgruppe", "geschaeftsmodell")
VISUAL_KEYS = ("visual_solution", "visual_how_it_works")

LABELS = {
    "en": {
        "name": "English",
        "sections": {
            "loesung": "Solution & Functionality",
            "mehrwerte": "Benefits & Performance",
            "usp": "USP & Competitive Differentiation",
            "zielgruppe": "Target Group & Customers",
            "geschaeftsmodell": "Business Model",
        },
        "visuals": {
            "visual_solution": "Visualisation of the Solution",
            "visual_how_it_works": "How the Solution Works",
        },
        "page_label": "Matchmaking Startups",
        "unknown": "n/a",
        "location": "Location",
        "founded": "Founded",
        "team": "Team",
        "draft": "DRAFT — not approved",
    },
    "de": {
        "name": "Deutsch",
        "sections": {
            "loesung": "Lösung & Funktionalität",
            "mehrwerte": "Mehrwerte & Leistungen",
            "usp": "USP & Abgrenzung vom Wettbewerb",
            "zielgruppe": "Zielgruppe & Kunden",
            "geschaeftsmodell": "Geschäftsmodell",
        },
        "visuals": {
            "visual_solution": "Visualisierung der Lösung",
            "visual_how_it_works": "So funktioniert die Lösung",
        },
        "page_label": "Matchmaking-Startups",
        "unknown": "k. A.",
        "location": "Ort",
        "founded": "Gründung",
        "team": "Team",
        "draft": "ENTWURF — nicht freigegeben",
    },
}


def lang_of(data: dict) -> str:
    lang = str((data or {}).get("lang") or LEGACY_LANG).lower()
    return lang if lang in LANGS else LEGACY_LANG


def labels(lang: str) -> dict:
    return LABELS[lang if lang in LANGS else DEFAULT_LANG]


def sections(lang: str) -> list:
    """[(key, heading)] in the fixed reading order."""
    heads = labels(lang)["sections"]
    return [(k, heads[k]) for k in SECTION_KEYS]


def visuals(lang: str) -> list:
    heads = labels(lang)["visuals"]
    return [(k, heads[k]) for k in VISUAL_KEYS]


def is_unknown(value) -> bool:
    """True for either language's 'not stated' marker."""
    return str(value or "").strip() in {L["unknown"] for L in LABELS.values()}


def other(lang: str) -> str:
    return "de" if lang == "en" else "en"
