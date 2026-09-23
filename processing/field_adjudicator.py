"""
LLM adjudication for the field changes no rule can settle.

What is left after the deterministic passes
-------------------------------------------
The list-field union (23 Sep) closed 426 reviews without a model, because all
517 tags/founders proposals were lossless set additions — not decisions. What
survives is 144 reviews where the two values genuinely disagree and one of
them is wrong:

    sub_industry  105   "Payments" -> "HealthTech" for a company the
                        description calls "ein deutsches Fintech-Startup"
    city/website/stage/...  ~39   needs looking the company up

The sampled sub_industry proposals are mostly DOWNGRADES — a later crawl
producing a vaguer label than the one already stored ("Supplements - Health &
Wellness" -> "Supplements", "B2B SaaS - Sales Tech" -> "B2B Sales"). The
record's own description is usually enough to tell, which is why this is
worth a model rather than a research agent.

The asymmetry that makes auto-apply safe
----------------------------------------
Only one direction changes nothing:

  keep_old   REJECT the review. No field is written. It records a
             rejected_value suppression so the same proposal stops coming
             back, and deleting that one row undoes it completely.
  take_new   APPROVE — overwrites a stored value. The change log records
             old and new now, so it is recoverable, but there is no one-click
             revert. NEVER auto-applied, at any confidence.
  unsure     left pending.

So the model is allowed to say "the value we already have is correct" and act
on it, and is never allowed to overwrite. A wrong keep_old costs one stale
label that the next crawl proposes again; a wrong take_new silently replaces
a correct value with a worse one, which is the failure this database has
actually suffered.

`website` is excluded from auto-apply entirely regardless of verdict: it feeds
the identity fingerprint, so changing it changes what the record IS, and that
is never a decision to take without a person.
"""
import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

VERDICTS = ("keep_old", "take_new", "unsure")

# Never auto-resolved either way — the value participates in record identity.
IDENTITY_FIELDS = {"website", "name"}

_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": list(VERDICTS)},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "reasoning": {"type": "string"},
    },
    "required": ["verdict", "confidence", "reasoning"],
}

_SYSTEM = (
    "You check whether a proposed change to a company database record is an "
    "improvement. You are conservative: replacing a correct value with a "
    "vaguer or wrong one is far worse than leaving a slightly stale value."
)

_PROMPT = """A crawl proposes changing one field on this company.

  company     : {name}
  description : {description}
  industry    : {industry}

  field       : {field}
  stored now  : {old}
  proposed    : {new}

Which value is right?

- "keep_old"  — the stored value is as good or better. Choose this when the
  proposal is vaguer, less specific, or contradicted by the description.
- "take_new"  — the proposal is clearly more accurate or more specific.
- "unsure"    — the description does not settle it.

Judge only from the description and industry shown. Do not use outside
knowledge about this company. A more specific label beats a broader one when
both are true. Give one sentence of reasoning citing the description."""


def adjudicate_field_change(record, field: str, old, new,
                            *, model: Optional[str] = None) -> Optional[dict]:
    """
    Ask which value is right. Returns a verdict dict, or None if the model was
    unavailable or gave something unusable. Never raises, never writes.
    """
    from config import settings
    from reasoning.qwen_client import qwen_client

    model = model or getattr(settings, "adjudicator_model", None) or settings.ollama_reason_model
    desc = (getattr(record, "short_description", None)
            or getattr(record, "description", None) or "—")
    prompt = _PROMPT.format(
        name=getattr(record, "name", "?"),
        description=str(desc)[:600],
        industry=getattr(record, "industry", None) or "—",
        field=field,
        old="(empty)" if old in (None, "", [], {}) else str(old)[:300],
        new="(empty)" if new in (None, "", [], {}) else str(new)[:300],
    )

    try:
        response = qwen_client._client().chat(
            model=model,
            messages=[{"role": "system", "content": _SYSTEM},
                      {"role": "user", "content": prompt}],
            format=_SCHEMA, think=False,
            options={"temperature": 0, "num_predict": 400},
        )
        data = json.loads(response["message"]["content"])
    except Exception as exc:
        logger.warning(f"[FieldAdj] call failed: {type(exc).__name__}: {exc}")
        return None

    verdict = (data.get("verdict") or "").strip()
    if verdict not in VERDICTS:
        logger.warning(f"[FieldAdj] unusable verdict {verdict!r}")
        return None
    return {
        "verdict": verdict,
        "confidence": (data.get("confidence") or "low").strip(),
        "reasoning": (data.get("reasoning") or "").strip()[:800],
        "model": model,
    }


def may_auto_apply(result: dict, field: str) -> bool:
    """
    The policy, in one place: only a confident `keep_old`, and never on a
    field that participates in identity.

    Before relaxing this, weigh the two failure modes. A wrong keep_old leaves
    one stale label, and the next crawl proposes the change again. A wrong
    take_new silently replaces a correct value with a worse one and nothing
    proposes it back.
    """
    if not result or field in IDENTITY_FIELDS:
        return False
    return result["verdict"] == "keep_old" and result["confidence"] == "high"
