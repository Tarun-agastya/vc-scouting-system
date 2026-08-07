"""
Field enrichment for the regional register (Phase RC-4).

Fills Mitarbeiter / Branche / Kurzbeschreibung / Website from live web search,
with a source URL recorded per field.

ISOLATION — this is the point of the module (owner, 7 Aug: "don't mix this
with our main pipeline"). It imports exactly three things from the rest of the
project, all READ-ONLY, and edits nothing:

    ingestion.web_search.search()             generic query -> results
    reasoning.qwen_client.web_verify_record() takes a PROMPT STRING, returns
                                              {identity_match, summary, findings}
    processing.scout_controller.gpu_mutex     machine safety, see below

It deliberately does NOT reuse processing/web_verifier.py, even though the
shape is similar, because that module is startup-specific in ways that would
have forced edits to it: `_build_search_query` appends the literal words
"startup founded" to every query (actively wrong for a 120-year-old
Maschinenbau firm), and `_CHECK_FIELDS` is funding_stage / founded_year /
employee_count rather than this register's fields. Changing either would risk
the startup pipeline. A separate ~150-line module is cheaper than that risk.

Nothing here writes to `startups`, to DuplicateReview (the startup Review
Inbox), or to Qdrant.

WHY THE GPU MUTEX IS NOT OPTIONAL: this calls the local 14B model, and the
Mac mini has one GPU. Running enrichment while an ingestion sweep is in flight
is exactly the oversubscription that caused the cascading timeouts this whole
project was built to fix (§A.3). Acquiring `scout_controller.gpu_mutex`
directly — rather than registering a run through `_execute` — respects that
rule while keeping this module out of the controller's lifecycle.

WRITE POLICY: fill empties, never silently overwrite. A field that already
has a value is left alone and the proposal is recorded in `field_sources`
under a "proposed_<field>" key for a human to look at. Consistent with the
project's stewardship stance without needing a whole second review queue.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from typing import List, Optional

logger = logging.getLogger(__name__)

# The register's own fields, and how a model finding's field name maps onto
# them. Deliberately a small surface — this is not trying to verify everything.
FIELD_ALIASES = {
    "employees": "employees", "employee_count": "employees",
    "mitarbeiter": "employees", "headcount": "employees", "staff": "employees",
    "number_of_employees": "employees",
    "branche": "branche", "industry": "branche", "sector": "branche",
    "kurzbeschreibung": "kurzbeschreibung", "description": "kurzbeschreibung",
    "short_description": "kurzbeschreibung", "summary": "kurzbeschreibung",
    "website": "website", "url": "website", "homepage": "website",
    "city": "city", "standort": "city", "location": "city",
}

ENRICHABLE = ("employees", "branche", "kurzbeschreibung", "website", "city")

# Aggregator/social/directory hosts that are never a company's own site.
# Same guard as the startup path applies, restated here rather than imported
# so this module has no dependency on web_verifier's internals.
_NON_OFFICIAL_HOSTS = (
    "linkedin.", "xing.", "facebook.", "instagram.", "twitter.", "x.com",
    "youtube.", "wikipedia.", "wikidata.", "crunchbase.", "northdata.",
    "kununu.", "indeed.", "stepstone.", "glassdoor.", "dnb.com",
    "companyhouse.", "firmenwissen.", "wer-zu-wem.", "openstreetmap.",
    "google.", "bing.", "yelp.", "gelbeseiten.", "dasoertliche.",
)

_PROMPT = """You are checking public facts about an ESTABLISHED German company \
(a Mittelstand employer, NOT a startup). Use only the search results below.

Company: {name}
Location: {city}

Known values (may be empty — an empty value is what we are trying to fill):
{fields}

Search results:
{results}

For each field you can establish from the results, return a finding with:
  field         one of: employees, branche, kurzbeschreibung, website
  verdict       "confirmed" if the known value is right, "contradicted" if the
                results give a different or a previously-missing value
  correct_value the value the results support
  source_url    the exact result URL that supports it

Rules:
  - employees: a number only (e.g. "450"). If the results give a range, use a \
representative figure. If no headcount is stated anywhere, do NOT invent one — \
omit the finding entirely.
  - branche: a short German industry label (e.g. "Maschinenbau", \
"Verpackungsindustrie").
  - kurzbeschreibung: one or two sentences in German describing what the \
company actually makes or does.
  - website: the company's OWN domain. Never LinkedIn, Wikipedia, Northdata, \
Kununu, a job board or any directory.
  - Set identity_match=false if the results are clearly about a DIFFERENT \
company that merely shares the name. Do not return findings in that case.
  - Never guess. A missing finding is always better than a fabricated value.
"""


def _norm_field(name: str) -> Optional[str]:
    key = (name or "").strip().lower().replace(" ", "_").replace("-", "_")
    return FIELD_ALIASES.get(key)


def _normalize_url(url: str) -> Optional[str]:
    """Accept a bare domain and give it a scheme.

    The model frequently returns 'stoba.one' rather than 'https://stoba.one',
    and an earlier scheme check rejected that as 'non-official' — throwing away
    a perfectly good company domain for a cosmetic reason. Only reject things
    that genuinely aren't a domain.
    """
    u = (url or "").strip()
    if not u:
        return None
    if not u.lower().startswith(("http://", "https://")):
        u = "https://" + u.lstrip("/")
    host = u.split("//", 1)[-1].split("/", 1)[0]
    if "." not in host or " " in host:
        return None
    return u


def _is_official_site(url: str) -> bool:
    """True when the URL is a company's own domain rather than an aggregator,
    social network, job board or directory."""
    u = (url or "").lower()
    return bool(u) and not any(h in u for h in _NON_OFFICIAL_HOSTS)


def _parse_employees(raw) -> Optional[int]:
    """'ca. 450' | '450 Mitarbeiter' | '1.200' -> int, else None."""
    if raw is None:
        return None
    digits = re.sub(r"[^\d]", "", str(raw))
    if not digits:
        return None
    try:
        val = int(digits)
    except ValueError:
        return None
    # A plausibility gate: the model occasionally returns a year or a revenue
    # figure in the employees slot. Anything outside this range is not a
    # headcount for a company in this register.
    return val if 1 <= val <= 500_000 else None


def build_query(company) -> str:
    """SME-shaped, NOT startup-shaped. Deliberately different from
    web_verifier._build_search_query, which appends 'startup founded'."""
    parts = [company.name]
    if company.city:
        parts.append(company.city)
    parts.append("Unternehmen Mitarbeiter")
    return " ".join(parts)


def build_prompt(company, results: List[dict]) -> str:
    known = "\n".join(
        f"  - {f}: {getattr(company, f, None)!r}" for f in ENRICHABLE)
    if results:
        rendered = "\n".join(
            f"- {r.get('title','')}\n  URL: {r.get('url','')}\n  \"{r.get('snippet','')}\""
            for r in results)
    else:
        rendered = "(no results found)"
    return _PROMPT.format(name=company.name, city=company.city or "unknown",
                          fields=known, results=rendered)


def proposals_from_verdict(company, verdict: dict) -> dict:
    """
    verdict -> {field: {"value": ..., "source_url": ...}}, applying the
    plausibility and official-domain guards. Returns {} when the model says
    the results describe a different company.
    """
    if not verdict or verdict.get("identity_match") is False:
        return {}

    out: dict = {}
    for f in verdict.get("findings") or []:
        attr = _norm_field(f.get("field"))
        if attr not in ENRICHABLE:
            continue
        raw = f.get("correct_value")
        if raw is None or str(raw).strip() == "":
            continue
        src = (f.get("source_url") or "").strip()

        if attr == "employees":
            val = _parse_employees(raw)
            if val is None:
                continue
        elif attr == "website":
            val = _normalize_url(str(raw))
            if not val:
                continue
            if not _is_official_site(val):
                logger.info(f"[RegionalEnrich] rejected non-official website for "
                            f"{company.name!r}: {val!r}")
                continue
        else:
            val = " ".join(str(raw).split())
            if not val:
                continue

        out[attr] = {"value": val, "source_url": src}
    return out


def apply_proposals(company, proposals: dict) -> dict:
    """
    Fill empty fields; never silently overwrite a value that is already there.
    Returns {"filled": [...], "proposed_only": [...]}.
    """
    filled, proposed_only = [], []
    sources = dict(company.field_sources or {})

    for attr, p in proposals.items():
        current = getattr(company, attr, None)
        if current in (None, "", 0):
            setattr(company, attr, p["value"])
            sources[attr] = p["source_url"]
            filled.append(attr)
        elif str(current).strip() != str(p["value"]).strip():
            # Disagreement with an existing value is recorded, not applied —
            # a human decides. Mirrors the project's never-auto-overwrite rule.
            sources[f"proposed_{attr}"] = {
                "value": p["value"], "source_url": p["source_url"],
                "current": str(current), "seen_at": datetime.utcnow().isoformat(),
            }
            proposed_only.append(attr)

    company.field_sources = sources
    company.last_verified_at = datetime.utcnow()
    return {"filled": filled, "proposed_only": proposed_only}


# Verdict shape the model must return. Declared here rather than reused from
# qwen_client so this module keeps its own contract and needs no edit to the
# main pipeline.
_VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "identity_match": {"type": "boolean"},
        "summary": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field":         {"type": "string"},
                    "verdict":       {"type": "string"},
                    "correct_value": {"type": "string"},
                    "source_url":    {"type": "string"},
                },
                "required": ["field", "verdict", "correct_value", "source_url"],
            },
        },
    },
    "required": ["identity_match", "summary", "findings"],
}


def _verify_sync(prompt: str) -> dict:
    """
    One structured-output call on the SMALL model.

    Deliberately not qwen_client.web_verify_record, which targets the 14B
    reasoning model. Measured 7 Aug on this Mac mini: the 14B wedged Ollama
    outright — a trivial "Say OK" prompt timed out after 60s while the machine
    sat at 20% free memory with 50k pageouts, and the enrichment run stalled on
    a single company for over an hour. The 14B needs 10.4 GB resident, which on
    24 GB alongside Postgres, Qdrant and the API is simply too tight to hold
    for hours. The same prompt on qwen2.5:7b-instruct answered in 3.3 seconds.

    This is the project's own §A.2 two-tier rule applied where it had been
    overlooked: high-volume work belongs on the small model, and enrichment
    across 1,200 companies is high-volume work. It is also exactly what the
    deferred Phase T-5 recommended for the recheck path.
    """
    import ollama
    from config import settings

    client = ollama.Client(host=settings.ollama_base_url, timeout=120)
    resp = client.chat(
        model=settings.ollama_extract_model,
        messages=[
            {"role": "system",
             "content": "You verify public facts about established German companies. "
                        "Answer only from the supplied search results. Never invent a value."},
            {"role": "user", "content": prompt},
        ],
        format=_VERDICT_SCHEMA,
        options={"temperature": 0, "num_predict": 800},
    )
    import json as _json
    return _json.loads(resp["message"]["content"])


async def enrich_one(company) -> dict:
    """
    One company: search -> local LLM verdict -> proposals. Does not write.
    Both blocking calls go through run_in_executor, and the LLM half holds the
    GPU mutex, matching the project's convention for every Ollama call.
    """
    from ingestion.web_search import search
    from processing.scout_controller import scout_controller

    loop = asyncio.get_event_loop()
    try:
        results = await loop.run_in_executor(None, lambda: search(build_query(company), 5))
    except Exception as exc:
        logger.warning(f"[RegionalEnrich] search failed for {company.name!r}: {exc}")
        return {}

    if not results:
        logger.info(f"[RegionalEnrich] no search results for {company.name!r}")
        return {}

    prompt = build_prompt(company, results)
    async with scout_controller.gpu_mutex:
        try:
            verdict = await loop.run_in_executor(None, lambda: _verify_sync(prompt))
        except Exception as exc:
            # One company failing must never stop a batch of hundreds.
            logger.warning(f"[RegionalEnrich] verify failed for {company.name!r}: {exc}")
            return {}

    return proposals_from_verdict(company, verdict)
