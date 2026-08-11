"""
Candidate filtering for the regional register (Phase RC-2).

Reuses the Phase J institutional-junk vocabulary (config/tuning.yaml ->
institutional_junk) rather than inventing a second list — the Wikidata sweep
returned exactly the class it was written for: Sparkassen, Raiffeisenbanken,
Volksbanken, BKK Verbundplus, Stadtwerke.

ONE DELIBERATE INVERSION, and getting it wrong would gut the register:
Phase J also drops `known_incumbents` (Liebherr, PERI, Goldbeck, Züblin...)
because a large established industrial group is NOT a startup. Here the
opposite is true — an established regional employer with 100-4000 staff is
precisely the target. Liebherr-Verzahntechnik (Kempten, ~900 staff) and
Liebherr-Hydraulikbagger (Kirchdorf, 10 km away) are two of the best
partnership prospects the discovery sweep found. So this module takes
`institution_keywords` and the structural junk tests, and explicitly does
NOT apply `known_incumbents`.

Also filtered here, and NOT part of Phase J because startups are never these:
public utilities and transit operators (Stadtwerke, Verkehrsbetriebe) and
health insurers, which the Wikidata sweep surfaced in quantity.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

# Institution types that are not membership prospects. Complements — does not
# replace — the shared institution_keywords from config/tuning.yaml.
_EXTRA_INSTITUTION_TERMS = [
    # Municipal utilities and public transit. Confirmed live 7 Aug: the
    # Wikidata sweep returned SWU Verkehr (337), Technische Werke Schussental
    # (141) and Erdgas Südwest (116) as prospects. They are public-service
    # operators, not companies a private hub recruits as members.
    "stadtwerke", "gemeindewerke", "technische werke", "verkehrsbetriebe",
    "verkehrsgesellschaft", "regionalverkehr", "verkehr", "erdgas",
    "energieversorgung", "netzgesellschaft", "wasserversorgung",
    "abfallwirtschaft", "zweckverband", "berufsgenossenschaft",
    # Health insurers.
    "bkk", "aok", "barmer", "krankenkasse",
    # Education and healthcare providers.
    "universität", "universitaet", "hochschule", "fachhochschule",
    "klinikum", "kreisklinik", "bezirkskliniken", "krankenhaus",
    # Public economic-development bodies.
    "wirtschaftsförderung", "wirtschaftsfoerderung",
    "genossenschaftsbewegung", "sparda",
]

# EU SME-ish working band for this register, as specified by the owner.
# Enforced as a BAND, not a floor: the 7 Aug sweep surfaced Schlecker (36,000)
# and Müller (33,635) — both far outside what a regional hub recruits, and
# both technically "in radius with a headcount". An upper bound is as much a
# part of the definition as the lower one.
DEFAULT_MIN_EMPLOYEES = 100
DEFAULT_MAX_EMPLOYEES = 4000


def in_employee_band(employees, *, lo: int = DEFAULT_MIN_EMPLOYEES,
                     hi: int = DEFAULT_MAX_EMPLOYEES) -> bool:
    """True when the headcount is known AND inside the band. An unknown
    headcount returns False here — callers decide separately whether to keep
    unsized candidates for enrichment (RC-4 does; a final report should not)."""
    return employees is not None and lo <= employees <= hi


# Revenue as a SECOND, independent qualifying signal (11 Aug 2026, owner):
# many established Mittelstand firms simply don't publish a headcount, but a
# recent, sourced revenue figure is equally strong evidence of scale. Owner's
# own words: "the threshold is 25 million at least if its lower we will not
# consider and skip" — a floor, not a band (unlike employees, there is no
# upper revenue cutoff; a large, well-documented revenue figure is never
# grounds to exclude a company).
REVENUE_THRESHOLD_EUR_MILLIONS = 25.0

# "needs to be the latest which is of the latest year or the previous year" —
# a revenue figure is only trusted for an automated decision if it is dated
# to the current fiscal year or the one immediately before it. Older or
# undated figures are still stored (a human can see them via their citation)
# but are never used to auto-qualify or auto-disqualify a company.
REVENUE_MAX_AGE_YEARS = 1


def meets_revenue_threshold(revenue_eur_millions, revenue_year, *,
                            min_revenue: float = REVENUE_THRESHOLD_EUR_MILLIONS,
                            max_age_years: int = REVENUE_MAX_AGE_YEARS,
                            as_of_year: Optional[int] = None) -> Optional[bool]:
    """
    Three-way answer, never a guess:

      True  — revenue is known, dated to the current year or the one prior,
              and meets the EUR 25m floor.
      False — revenue is known and fresh enough to trust, but BELOW the
              floor. Per the owner's own instruction, this is treated exactly
              like a headcount outside the employee band: skip.
      None  — revenue is unknown, OR known but undated / too old to trust for
              an automated decision. The value may still be worth showing a
              human (it stays in the database with its citation) — it simply
              cannot decide the tier on its own. "Unknown" is never silently
              read as "too small", the same discipline `in_employee_band`
              and the footprint proxy already apply.

    `as_of_year` is only ever overridden by tests (so the freshness window is
    testable without depending on wall-clock time); production code always
    uses the real current year.
    """
    if revenue_eur_millions is None:
        return None
    if as_of_year is None:
        as_of_year = datetime.utcnow().year
    if revenue_year is None or revenue_year < as_of_year - max_age_years:
        return None
    return revenue_eur_millions >= min_revenue

# Legal-form and generic suffixes to ignore when comparing names.
_LEGAL_SUFFIX_RE = re.compile(
    r"\b(gmbh|mbh|ag|se|kg|ohg|e\.?\s?k\.?|e\.?\s?v\.?|ug|co\.?|kgaa|"
    r"holding|gruppe|group|deutschland)\b", re.IGNORECASE)


def _word_present(term: str, text: str) -> bool:
    """Word-boundary match for alphabetic terms, mirroring Phase J's
    _signal_present: a loose substring would reject 'Bankmann GmbH' for
    containing 'bank', or 'Kammerer' for containing 'kammer'."""
    t = term.strip().lower()
    if not t:
        return False
    if t.replace(" ", "").isalpha():
        return re.search(rf"\b{re.escape(t)}\b", text, re.IGNORECASE) is not None
    return t in text.lower()


def is_institution(name: str, cfg: Optional[dict] = None) -> bool:
    """
    True if `name` is a bank, chamber, law firm, insurer, public body,
    utility, university or clinic — none of which are membership prospects.

    Never rejects for `known_incumbents`: see this module's docstring. A large
    established manufacturer is the target here, not junk.
    """
    if not name or not name.strip():
        return True

    if cfg is None:
        try:
            from config.tuning_loader import get_institutional_junk_config
            cfg = get_institutional_junk_config()
        except Exception as exc:      # config problems must not break discovery
            logger.debug(f"[Regional] junk config unavailable ({exc}) — using defaults")
            cfg = {}

    if cfg.get("enabled") is False:
        return False

    terms = list(cfg.get("institution_keywords") or []) + _EXTRA_INSTITUTION_TERMS
    return any(_word_present(t, name) for t in terms)


# OSM labels buildings, gates and depots by name, so a `man_made=works` area
# can be called "Halle 10" or "Tor 3". Confirmed live 7 Aug: the register had
# Halle 2/3/5/9/10/11 as separate "companies", all from one industrial site.
# These are places within a company, never a company.
_BUILDING_LABEL_RE = re.compile(
    r"^(halle|geb[äa]ude|haus|tor|pforte|werk|werkstor|lager|parkplatz|"
    r"eingang|zufahrt|warenannahme|warenanlieferung|kantine|verwaltung|"
    r"b[üu]ro|bau)\s*[-–]?\s*\d+[a-z]?$",
    re.IGNORECASE)


def is_building_label(name: str) -> bool:
    """True for 'Halle 10', 'Tor 3', 'Gebäude 2' — a place inside a site."""
    return bool(_BUILDING_LABEL_RE.match((name or "").strip()))


def is_structural_junk(name: str) -> bool:
    """
    Headline / multi-entity / fragment shapes, the same structural tells
    Phase J uses — a Wikipedia category or an OSM `name` tag occasionally
    carries a disambiguation string or a list rather than one company name.
    """
    if not name:
        return True
    n = name.strip()

    if len(n) < 2 or len(n) > 120:
        return True
    if n.count(",") > 1:                       # "A, B, C" = several companies
        return True
    if ":" in n or "?" in n:                   # headline punctuation
        return True
    if re.match(r"^(ein|eine|a|an)\s+\w", n, re.IGNORECASE):
        return True                            # "eine Digitalagentur"
    if re.match(r"^Q\d+$", n):                 # unlabelled Wikidata item
        return True
    # Wikipedia disambiguation suffix, e.g. "Wanzl (Unternehmen)" — the name
    # itself is fine, the parenthetical is not part of it. Handled by
    # clean_name() rather than rejected here.
    return False


def clean_name(name: str) -> str:
    """Strip a Wikipedia disambiguation parenthetical and collapse whitespace.
    'Wanzl (Unternehmen)' -> 'Wanzl'."""
    if not name:
        return ""
    n = re.sub(r"\s*\((?:Unternehmen|Firma|Konzern|Begriffskl[äa]rung)[^)]*\)\s*$",
               "", name.strip(), flags=re.IGNORECASE)
    return " ".join(n.split())


def core_name(name: str) -> str:
    """Name with legal-form suffixes removed, for loose comparison.
    'Josef Hebel GmbH & Co. KG' -> 'josef hebel'."""
    n = _LEGAL_SUFFIX_RE.sub(" ", (name or "").lower())
    n = re.sub(r"[^\w\s]", " ", n)
    return " ".join(n.split())


def accept(name: str, cfg: Optional[dict] = None) -> bool:
    """One call for the whole gate. Returns True if the candidate should be
    kept as a possible membership prospect."""
    cleaned = clean_name(name)
    return (bool(cleaned)
            and not is_structural_junk(cleaned)
            and not is_building_label(cleaned)
            and not is_institution(cleaned, cfg))
