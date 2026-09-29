"""
Phase 3 of plans/REVIEW_INBOX_AUTONOMY_PLAN.md — the one piece of this plan
that is genuinely an agent LOOP (search, then read, then decide) rather than
a single structured-output call.

Why this exists separately from the resolver
----------------------------------------------
processing/field_adjudicator.py judges a field from the company's OWN
description. That settles `sub_industry` fine — the description usually
says what a company does — but it structurally cannot settle `city`,
`website`, `funding_stage`, `address`, `country` or `contact_info`: a
description rarely states where a company is headquartered. Measured on the
live queue (24 Sep): 61 pending reviews sit on exactly these fields, and
Phase 2's resolver correctly declines most of them (low confidence or
none_fit) rather than guess — which is the right behaviour, but it means
they just sit there forever without a real chance at resolution. This
module gives them one: `ingestion.web_search.search()`, already used by
processing/web_verifier.py, stands in as the evidence a description can't
provide.

Same asymmetry as the resolver
-------------------------------
A search-informed verdict that CONFIRMS the stored value closes the review
(reversible — a suppression row). A verdict that prefers the proposal is
attached with its evidence and left pending — overwriting a field is not a
decision this makes unattended, however good the citation looks. Reuses
field_adjudicator.group_may_auto_apply unchanged: this module's
research_field_group() returns the exact same {"winner", "none_fit",
"confidence", ...} shape adjudicate_field_group() does, specifically so the
one auto-apply policy function keeps deciding both.

The citation-fabrication guard
-------------------------------
regional/enrich.py found the local model inventing a plausible-looking
citation URL that never appeared in its own search results (a fabricated
Northdata source for a Diehl Controls revenue figure, 11 Aug). The same
model doing the same kind of "cite a source" task here gets the same guard:
a `source_url` that isn't one of the URLs actually shown is not trusted, and
the verdict is downgraded to "none" rather than accepted at face value.

Budget
------
There is no quota accounting for Tavily or any other provider anywhere in
this codebase — settings.resolver_max_searches is a hard per-run cap, the
same blunt-cap pattern as web_verify_chain_limit and
ig_max_calls_per_run. Exhausting it ends the run cleanly; unresearched
groups are simply left for the next run.
"""
import asyncio
import json
import logging
from collections import defaultdict
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# The fields Phase 2 structurally cannot settle from a company's own
# description. website is included deliberately — it is also an identity
# field, so group_may_auto_apply (reused unchanged from field_adjudicator)
# already refuses to auto-close it regardless of verdict; research can still
# attach useful evidence for a human even though it can never close this one.
RESEARCH_FIELDS = {"city", "website", "funding_stage", "address", "country", "contact_info"}

_FIELD_QUERY_HINT = {
    "city": "headquarters location city",
    "website": "official website",
    "funding_stage": "latest funding round stage investment",
    "address": "company address",
    "country": "headquarters country",
    "contact_info": "contact phone email",
}

_CONSECUTIVE_FAILURE_THRESHOLD = 3

_SYSTEM = (
    "You verify one fact about a company using web search results. Judge "
    "only from the evidence shown — never use outside knowledge, and never "
    "invent a fact, a source, or a URL that is not present in the search "
    "results below."
)

_PROMPT = """Which value is correct for this company's {field}?

  company : {name}

  currently stored : {current}

  candidate(s) proposed by our data pipeline:
{options}

  web search results:
{results}

Reply "current" if the search results confirm the stored value, or don't
contradict it. Reply with the exact candidate text (quoted exactly as shown
above) if the search results clearly support that value instead. Reply
"none" if the results don't settle it either way — that is a real answer
here: guessing from thin evidence is worse than admitting it's inconclusive.

source_url must be the exact URL of the ONE search result you actually
relied on. If you did not rely on any single result, leave it empty.

Give one sentence of reasoning citing what the search result actually said."""


def _build_query(record, field: str) -> str:
    parts = [record.name]
    if record.city:
        parts.append(record.city)
    elif record.country:
        parts.append(record.country)
    parts.append(_FIELD_QUERY_HINT.get(field, field))
    return " ".join(parts)


def _format_results(results: list) -> str:
    if not results:
        return "(no results found)"
    return "\n".join(
        f"- {r.get('title', '')}\n  URL: {r.get('url', '')}\n  \"{r.get('snippet', '')}\""
        for r in results)


def _schema(options: list) -> dict:
    return {
        "type": "object",
        "properties": {
            "choice": {"type": "string", "enum": ["current", "none", *options]},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "source_url": {"type": "string"},
            "reasoning": {"type": "string"},
        },
        "required": ["choice", "confidence", "source_url", "reasoning"],
    }


def research_field_group(record, field: str, current, candidates: list, results: list,
                         *, model: Optional[str] = None) -> Optional[dict]:
    """
    Judge one (record, field) using web search results as evidence, on top
    of whatever candidates the pipeline already proposed.

    Returns the SAME shape as field_adjudicator.adjudicate_field_group —
    {"winner", "none_fit", "confidence", "reasoning", "considered", "model"}
    — plus "source_url" and "search_results", specifically so
    field_adjudicator.group_may_auto_apply can judge this result unchanged.
    Returns None if the model was unavailable, gave something unusable, or
    cited a URL that was never actually shown to it (see this module's
    docstring on the fabricated-citation guard) — treated as unusable
    rather than trusted, since a fabricated citation is exactly the failure
    mode this field is already full of.
    """
    from config import settings
    from reasoning.qwen_client import qwen_client

    uniq = []
    for c in candidates:
        text = str(c).strip()
        if text and text not in uniq:
            uniq.append(text)
    if not uniq:
        return None

    model = model or getattr(settings, "adjudicator_model", None) or settings.ollama_reason_model
    prompt = _PROMPT.format(
        field=field, name=getattr(record, "name", "?"),
        current="(empty)" if current in (None, "", [], {}) else str(current)[:200],
        options="\n".join(f"    - {c}" for c in uniq),
        results=_format_results(results),
    )

    try:
        response = qwen_client._client().chat(
            model=model,
            messages=[{"role": "system", "content": _SYSTEM},
                      {"role": "user", "content": prompt}],
            format=_schema(uniq), think=False,
            options={"temperature": 0, "num_predict": 400},
        )
        data = json.loads(response["message"]["content"])
    except Exception as exc:
        logger.warning(f"[Researcher] call failed: {type(exc).__name__}: {exc}")
        return None

    choice = (data.get("choice") or "").strip()
    confidence = (data.get("confidence") or "low").strip()
    reasoning = (data.get("reasoning") or "").strip()[:800]
    source_url = (data.get("source_url") or "").strip()

    # Fabricated-citation guard: a source_url must be one of the URLs the
    # model was ACTUALLY shown. Anything else is untrusted regardless of how
    # plausible it looks — same guard as regional/enrich.py's
    # proposals_from_verdict, for the same reason.
    shown = {r.get("url", "").strip() for r in results if r.get("url")}
    if choice.lower() not in ("current", "none") and source_url and source_url not in shown:
        logger.warning(
            f"[Researcher] '{record.name}' — cited URL not in the results shown, "
            "discarding the finding rather than trusting it")
        choice, confidence = "none", "low"

    if choice.lower() == "none":
        return {
            "winner": None, "none_fit": True, "confidence": confidence,
            "reasoning": reasoning, "considered": uniq, "model": model,
            "source_url": source_url or None, "search_results": results,
        }

    winner = None
    if choice and choice.lower() != "current":
        match = next((c for c in uniq if c.casefold() == choice.casefold()), None)
        if match is None:
            logger.warning(f"[Researcher] choice {choice!r} is not one of the options")
            return None
        winner = match

    return {
        "winner": winner, "none_fit": False, "confidence": confidence,
        "reasoning": reasoning, "considered": uniq, "model": model,
        "source_url": source_url or None, "search_results": results,
    }


def _proposed(review) -> dict:
    prop = review.proposed_changes or {}
    if isinstance(prop, str):
        try:
            prop = json.loads(prop)
        except Exception:
            return {}
    return prop or {}


async def research_pending(limit: int = 40, *, apply: bool = False,
                           master_ids=None) -> dict:
    """
    Research up to `limit` pending field_update GROUPS on RESEARCH_FIELDS
    (city, website, funding_stage, address, country, contact_info) — one
    web search per group, budget-capped separately by
    settings.resolver_max_searches so a large backlog can never turn into
    an unbounded number of outbound search calls in one run.

    apply=False (default): writes evidence and reasoning; closes nothing.
    apply=True: closes the reversible direction only (group_may_auto_apply,
    reused unchanged from field_adjudicator) — a confirmed stored value at
    high confidence. Never overwrites a field, at any confidence.

    master_ids: see review_resolver.resolve_pending's docstring — same
    reason, same default (None = the whole pending queue).

    Never raises. Returns a stats dict; "searches_used" and
    "budget_exhausted" describe how far the search budget got, independent
    of "unavailable" (model failures).
    """
    from config import settings
    from sqlalchemy.orm.attributes import flag_modified

    from database.connection import SessionLocal
    from database.models import DuplicateReview, Startup
    from processing.field_adjudicator import group_may_auto_apply
    from processing.review_actions import record_rejection
    from processing.scout_controller import scout_controller
    from ingestion.web_search import search as web_search

    stats = defaultdict(int)
    db = SessionLocal()
    try:
        query = db.query(DuplicateReview).filter(
            DuplicateReview.status == "pending",
            DuplicateReview.review_type == "field_update")
        if master_ids is not None:
            query = query.filter(DuplicateReview.master_id.in_(master_ids))
        pending = query.order_by(DuplicateReview.created_at.asc()).all()

        groups = defaultdict(list)
        for r in pending:
            for field, change in _proposed(r).items():
                if field in RESEARCH_FIELDS and isinstance(change, dict):
                    groups[(r.master_id, field)].append((r, change))

        loop = asyncio.get_event_loop()
        consecutive_failures = 0
        searches_used = 0
        max_searches = settings.resolver_max_searches
        judged = 0
        total_groups = len(groups)

        for i, ((master_id, field), items) in enumerate(groups.items()):
            if judged >= limit:
                break
            if searches_used >= max_searches:
                stats["budget_exhausted"] += total_groups - i
                logger.info(f"[Researcher] search budget ({max_searches}) exhausted, "
                            f"{stats['budget_exhausted']} group(s) left for next run")
                break

            master = db.query(Startup).filter(Startup.id == master_id).first()
            if master is None:
                continue

            current = getattr(master, field, None)  # live value, never change["old"]
            candidates = [c.get("new") for _r, c in items]

            query_text = _build_query(master, field)
            results = await loop.run_in_executor(None, web_search, query_text, 5)
            searches_used += 1
            stats["searches_used"] = searches_used

            async with scout_controller.gpu_mutex:
                res = await loop.run_in_executor(
                    None, research_field_group, master, field, current, candidates, results)

            if not res:
                consecutive_failures += 1
                stats["unavailable"] += 1
                if consecutive_failures >= _CONSECUTIVE_FAILURE_THRESHOLD:
                    logger.warning(
                        f"[Researcher] {consecutive_failures} consecutive failures — "
                        "stopping for tonight, will retry next run")
                    break
                continue
            consecutive_failures = 0
            judged += 1

            label = ("none_fit" if res.get("none_fit")
                     else "keep_current" if res["winner"] is None
                     else "prefers_proposal")
            stats[label] += 1

            for r, _c in items:
                ev = dict(r.evidence or {})
                field_adj = {k: v for k, v in res.items() if k != "search_results"}
                ev["field_adjudication"] = {
                    **field_adj, "at": datetime.utcnow().isoformat(timespec="seconds")}
                ev["web_verdict"] = {
                    "summary": res["reasoning"], "source_url": res.get("source_url")}
                ev["search_results"] = results
                r.evidence = ev
                flag_modified(r, "evidence")
                r.llm_explanation = (
                    f"[researched — {'keep stored' if res['winner'] is None else 'prefers: ' + str(res['winner'])}"
                    f" / {res['confidence']}] {res['reasoning']}")[:2000]

            if apply and group_may_auto_apply(res, field):
                for r, _c in items:
                    record_rejection(db, r, commit=False)
                    stats["auto_closed"] += 1
            db.commit()
    except Exception as exc:
        db.rollback()
        logger.error(f"[Researcher] run failed: {type(exc).__name__}: {exc}")
        stats["error"] = str(exc)
    finally:
        db.close()

    logger.info(f"[Researcher] run complete: {dict(stats)}")
    return dict(stats)
