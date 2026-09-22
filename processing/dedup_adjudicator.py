"""
LLM adjudication for the duplicate pairs no deterministic rule can settle.

Where this sits, and what it is NOT
-----------------------------------
The deterministic layers do the volume and must keep doing it — they are
cheaper, faster and auditable. On 22 Sep they took 3,591 records down to
2,899 with no model involved at all: a fingerprint repair, a source-site
website guard, and two merge tiers.

What they cannot settle is a genuine identity question:

    reverion.com    vs  reverion.de          -> one company, two TLDs
    strollme.com    vs  strollme.de          -> one company
    praxis-eins.de  vs  praxiseins.de        -> one company, hyphen variant
    echobot.de      vs  api.echobot.de       -> one company, subdomain
    gravisrobotics.com vs gravistech.com     -> rebrand? or two companies?
    rudy-capital.com   vs rudyproject.com    -> almost certainly two companies

No rule separates the last two from the first four. A model reading both
records can. That — and only that — is this module's job.

The asymmetry that makes it safe
--------------------------------
The model is allowed to take the REVERSIBLE action and not the irreversible
one:

  * `different_company`  -> may auto-apply. It records a known-different pair
    so the two stop being re-flagged. Undone by deleting one row.
  * `same_company`       -> NEVER auto-merged, at any confidence. Merging
    deletes a record and cannot be undone without a backup. It writes the
    verdict and reasoning onto the review and leaves it pending for a human.
  * `insufficient_evidence` -> left pending, with the reasoning attached.

That is deliberate and should survive future editing. The expensive mistake
in this database has always been over-merging, not under-merging.

Follows this repo's hard-won LLM conventions: object-wrapped JSON schema
(never a top-level array), every key required, no nullable types, and
think=False — without which a Qwen3-generation model spends its whole token
budget reasoning and returns nothing (see validation/qwen_model_ab_2026-09.md).

Provider
--------
Uses the local reasoning model by default, so it works today with no account
or key. `settings.adjudicator_model` can point it elsewhere once a cloud key
exists; the prompt and schema are provider-independent. Volume is low (tens of
pairs), so this is a good first candidate to move, not a cost risk either way.
"""
import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

VERDICTS = ("same_company", "different_company", "insufficient_evidence")

_ADJUDICATION_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": list(VERDICTS)},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "key_signal": {"type": "string"},
        "reasoning": {"type": "string"},
    },
    "required": ["verdict", "confidence", "key_signal", "reasoning"],
}

_SYSTEM = (
    "You decide whether two database records describe the SAME company. "
    "You are cautious: merging two different companies is far worse than "
    "leaving two records for a human to look at."
)

_PROMPT = """Two records in a startup database may be the same company.

RECORD A (the existing master)
{a}

RECORD B (the incoming record)
{b}

Decide:
- "same_company" — the same real-world company. Different TLDs (.de/.com),
  a subdomain, a hyphen, or a legal suffix (GmbH/AG) are NOT differences.
- "different_company" — two distinct companies that happen to share a name,
  or a similar name in a different industry or city.
- "insufficient_evidence" — you genuinely cannot tell from what is here.

Rules:
- Judge ONLY from the fields above. Never use outside knowledge about these
  companies, and never invent a fact that is not shown.
- A shared name alone is NOT enough. Names repeat across industries.
- Conflicting industry, city or country is strong evidence AGAINST.
- If only one record has data and the other is nearly empty, that is usually
  "insufficient_evidence", not "same_company".
- key_signal: the single field that decided it, e.g. "website domain" or
  "conflicting city".
- reasoning: one or two sentences, citing the fields you used.
"""


def _card(row) -> str:
    """Compact evidence card. Only fields a human would actually weigh."""
    def g(attr, default="—"):
        v = getattr(row, attr, None) if not isinstance(row, dict) else row.get(attr)
        if isinstance(v, (list, tuple)):
            v = ", ".join(str(x) for x in v[:5]) or default
        return str(v).strip() if v not in (None, "", []) else default

    return "\n".join([
        f"  name        : {g('name')}",
        f"  website     : {g('website')}",
        f"  description : {g('short_description') or g('description')}"[:400],
        f"  industry    : {g('industry')} / {g('sub_industry')}",
        f"  city        : {g('city')}, {g('country')}",
        f"  founded     : {g('founded_year')}",
        f"  stage       : {g('funding_stage')}",
    ])


def adjudicate_pair(record_a, record_b, *, model: Optional[str] = None) -> Optional[dict]:
    """
    Ask the model whether A and B are the same company.

    Returns a dict with verdict/confidence/key_signal/reasoning, or None if the
    model was unavailable or returned something unusable. Never raises, and
    never writes anything — deciding what to DO with a verdict is the caller's
    job, which is what keeps the auto-apply policy in one reviewable place.
    """
    from config import settings
    from reasoning.qwen_client import qwen_client

    model = model or getattr(settings, "adjudicator_model", None) or settings.ollama_reason_model
    prompt = _PROMPT.format(a=_card(record_a), b=_card(record_b))

    try:
        response = qwen_client._client().chat(
            model=model,
            messages=[{"role": "system", "content": _SYSTEM},
                      {"role": "user", "content": prompt}],
            format=_ADJUDICATION_SCHEMA,
            think=False,
            options={"temperature": 0, "num_predict": 600},
        )
        data = json.loads(response["message"]["content"])
    except Exception as exc:
        logger.warning(f"[Adjudicator] call failed: {type(exc).__name__}: {exc}")
        return None

    verdict = (data.get("verdict") or "").strip()
    if verdict not in VERDICTS:
        logger.warning(f"[Adjudicator] unusable verdict {verdict!r}")
        return None

    return {
        "verdict": verdict,
        "confidence": (data.get("confidence") or "low").strip(),
        "key_signal": (data.get("key_signal") or "").strip()[:200],
        "reasoning": (data.get("reasoning") or "").strip()[:1000],
        "model": model,
    }


def may_auto_apply(result: dict) -> bool:
    """
    The policy, in one place: only a confident `different_company` is applied
    automatically, because suppression is the only reversible outcome.

    `same_company` is never auto-applied regardless of confidence — see this
    module's docstring. If you are about to relax this, the thing to weigh is
    that a wrong merge silently destroys a record and is invisible afterwards,
    while a wrong suppression just means a human sees a pair once more.
    """
    return bool(result) and result["verdict"] == "different_company" \
        and result["confidence"] == "high"
