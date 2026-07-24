import json
import re
import threading
import time
import logging
from typing import Optional, List, Dict
import httpx
from config import settings

# JSON Schema for structured startup extraction.
# Wrapped in an object (not top-level array) for maximum grammar compatibility.
# Optional string fields use anyOf-null so the model can express "not found".
# All string fields use plain "string" (no anyOf/null) — nullable types confuse
# llama.cpp constrained decoding on 7B models and produce empty extractions.
# All fields are required so the model fills every key; _normalize_startup()
# converts empty-string sentinels back to None before returning.
_STARTUP_EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "startups": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name":           {"type": "string"},
                    "one_liner":      {"type": "string"},
                    "description":    {"type": "string"},
                    "website":        {"type": "string"},
                    "industry":       {"type": "string"},
                    "sub_industry":   {"type": "string"},
                    "tech_cluster":   {"type": "string"},
                    "country":        {"type": "string"},
                    "city":           {"type": "string"},
                    "address":        {"type": "string"},
                    "funding_stage":  {"type": "string"},
                    "funding_amount": {"type": "string"},
                    "founded_year":   {"type": "integer"},
                    "employee_count": {"type": "string"},
                    "contact_info":   {"type": "string"},
                    "published_date": {"type": "string"},
                    "founders":       {"type": "array", "items": {"type": "string"}},
                    "tags":           {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "name", "one_liner", "description", "website", "industry",
                    "sub_industry", "tech_cluster", "country", "city", "address",
                    "funding_stage", "funding_amount", "founded_year",
                    "employee_count", "contact_info", "published_date",
                    "founders", "tags",
                ],
            },
        }
    },
    "required": ["startups"],
}

# JSON Schema for Phase H-3's verification recheck (Layer 2). Deliberately a
# verdict-classification schema, not an extraction schema: never asks the
# model to propose a value, only whether the source text supports the one
# already stored.
_VERIFICATION_SCHEMA = {
    "type": "object",
    "properties": {
        "identity_match":       {"type": "boolean"},
        "summary":              {"type": "string"},
        "unsupported_fields":   {"type": "array", "items": {"type": "string"}},
        "contradicted_fields":  {"type": "array", "items": {"type": "string"}},
    },
    "required": ["identity_match", "summary", "unsupported_fields", "contradicted_fields"],
}

# JSON Schema for Phase W's web-search verification. Unlike _VERIFICATION_SCHEMA
# above (classify-only, never proposes a value), this one CAN propose a
# correct value per finding — it has independent ground truth (search
# results) to draw it from, and always carries the source_url it came from.
_WEB_VERIFICATION_SCHEMA = {
    "type": "object",
    "properties": {
        "identity_match": {"type": "boolean"},
        "summary":        {"type": "string"},
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

# String fields that should be None (not "") when the model returns empty/zero.
_NULLABLE_STR_FIELDS = (
    "one_liner", "description", "website", "industry", "sub_industry",
    "tech_cluster", "country", "city", "address", "funding_stage",
    "funding_amount", "employee_count", "contact_info", "published_date",
)


def _normalize_startup(s: dict) -> dict:
    """Convert empty-string sentinels to None so upsert_startup sees null values."""
    for field in _NULLABLE_STR_FIELDS:
        if s.get(field) == "":
            s[field] = None
    if s.get("founded_year") == 0:
        s["founded_year"] = None
    return s


def _text_contains(value: str, text_lower: str) -> bool:
    """
    Case-insensitive substring check with hyphen/space normalization, so
    "Series A" matches source text like "Series-A-Finanzierung" and "Seed"
    matches "Seed-Runde" (German compounds glue the stage word onto the noun).
    """
    v = (value or "").strip().lower()
    if not v:
        return False
    variants = {v, v.replace(" ", "-"), v.replace("-", " "), v.replace(" ", "")}
    return any(variant and variant in text_lower for variant in variants)


def _signal_present(signals, text_lower: str) -> bool:
    """
    True if any configured signal word/phrase genuinely appears in the text.

    Single alphabetic words are matched on real word boundaries — plain
    substring matching let short signals false-positive inside unrelated
    words: "fte" (meant as "full-time equivalent") matched inside "often",
    and separately "people" matched inside the podcast show name "Pitch &
    People" — both confirmed live (Phase H-4, and the Circular Materials
    case, 23 Jul), both let a fabricated employee_count survive grounding.
    Multi-word phrases and non-alphabetic signals (e.g. "$") keep plain
    substring matching, since \\b doesn't behave usefully around spaces or
    punctuation.
    """
    for sig in signals:
        s = (sig or "").lower().strip()
        if not s:
            continue
        if s.isalpha():
            if re.search(rf"\b{re.escape(s)}\b", text_lower):
                return True
        elif s in text_lower:
            return True
    return False


def _ground_startup(s: dict, source_text: str, cfg: dict) -> dict:
    """
    Phase H-1: null any high-fabrication-risk field that has no literal
    support in the chunk it was extracted from.

    This is what catches the Polysense class of bug — a chunk with no
    founding year or headcount at all was still producing a confident
    "founded_year": 2003 / "employee_count": "51-200" out of nowhere (or
    worse, borrowed from a neighboring company's paragraph in the same
    chunk). Only numeric/enum/name fields are gated; paraphrased fields
    (description, industry, tech_cluster, city, country) are left alone —
    nulling a correct paraphrase would be worse than leaving a wrong one for
    the Phase H-3 LLM deep-recheck to catch later.

    Never drops the record — only clears individual fields — and always
    records what it nulled in `s["_grounding"]` so Phase H-2/H-3 can surface
    it as verification evidence.
    """
    text_lower = source_text.lower()
    nulled: list = []
    dropped_founders: list = []

    if cfg.get("check_founded_year", True) and s.get("founded_year"):
        year = str(s["founded_year"])
        if not re.search(rf"\b{re.escape(year)}\b", source_text):
            s["founded_year"] = None
            nulled.append("founded_year")

    if cfg.get("check_funding_stage", True) and s.get("funding_stage"):
        if not _text_contains(s["funding_stage"], text_lower):
            s["funding_stage"] = None
            nulled.append("funding_stage")

    if cfg.get("check_funding_amount", True) and s.get("funding_amount"):
        signals = cfg.get("funding_amount_signals") or []
        has_digit = bool(re.search(r"\d", source_text))
        has_signal = _signal_present(signals, text_lower)
        if not (has_digit and has_signal):
            s["funding_amount"] = None
            nulled.append("funding_amount")

    if cfg.get("check_employee_count", True) and s.get("employee_count"):
        signals = cfg.get("employee_count_signals") or []
        if not _signal_present(signals, text_lower):
            s["employee_count"] = None
            nulled.append("employee_count")

    if cfg.get("check_founders", True) and s.get("founders"):
        kept = []
        for name in s["founders"]:
            if not isinstance(name, str) or not name.strip():
                continue
            surname = name.strip().split()[-1]
            if re.search(rf"\b{re.escape(surname)}\b", source_text, re.IGNORECASE):
                kept.append(name)
            else:
                dropped_founders.append(name)
        s["founders"] = kept

    if nulled or dropped_founders:
        s["_grounding"] = {"nulled": nulled, "dropped_founders": dropped_founders}
        logger.info(
            f"[Grounding] '{s.get('name', '?')}': nulled={nulled} "
            f"dropped_founders={dropped_founders}"
        )
    return s


logger = logging.getLogger(__name__)


class QwenClient:
    """
    Thin wrapper around Ollama for Qwen3:14b inference.

    Key design principles:
    - Small, focused prompts (no history replay)
    - Hard token cap on output  
    - <think> tag stripping for Qwen3
    - Synchronous — called from FastAPI background tasks or scripts
    """

    def __init__(self):
        self.model = settings.ollama_reason_model
        self.base_url = settings.ollama_base_url
        self._ollama_client = None         # lazy reason client (14B, 120s timeout)
        self._extract_ollama_client = None # lazy extract client (7B, 45s timeout)
        self._verify_ollama_client = None  # lazy verify client (14B, 180s timeout)
        # Derived from max_qwen_workers so config and semaphore stay in sync.
        self._semaphore = threading.Semaphore(settings.max_qwen_workers)

    def _client(self):
        if self._ollama_client is None:
            import ollama
            self._ollama_client = ollama.Client(
                host=self.base_url,
                timeout=120,
            )
        return self._ollama_client

    def _extract_client(self):
        """Separate client for the small extraction model with a tighter timeout."""
        if self._extract_ollama_client is None:
            import ollama
            # 7B model: ~8–15s for typical chunks, up to ~60s for dense portfolio pages
            # with 10+ companies and all fields required. 75s gives adequate headroom.
            self._extract_ollama_client = ollama.Client(
                host=self.base_url,
                timeout=75,
            )
        return self._extract_ollama_client

    def _verify_client(self):
        """
        Separate client for Phase H-3 verification recheck. Reuses the 14B
        reasoning model but with a longer timeout than _client()'s 120s —
        observed in testing: giving the model enough num_predict budget to
        actually finish its <think> reasoning plus a structured-output
        verdict occasionally takes longer than 120s for a dense record.
        """
        if self._verify_ollama_client is None:
            import ollama
            self._verify_ollama_client = ollama.Client(
                host=self.base_url,
                timeout=180,
            )
        return self._verify_ollama_client

    def extract_startups(self, text: str) -> list:
        """
        Extract startup entities from text using the small fast extraction model.

        Uses Ollama structured output (format= JSON schema) so the response is
        guaranteed-valid JSON — no <think> stripping or parse-repair needed.
        Retries once after a 2-second pause on transient failure, then re-raises
        so the caller (worker_queue) can count the failure and move on.

        Phase H-1: after parsing, each startup passes through a deterministic
        source-grounding gate (_ground_startup) that nulls fabrication-prone
        fields unsupported by this chunk's text, and carries a bounded excerpt
        of the source chunk (`_source_excerpt`) for the Phase H-3 recheck.
        """
        from reasoning.prompts import EXTRACTION_PROMPT, SYSTEM_EXTRACTOR
        from config.tuning_loader import get_extraction_rules, get_grounding_config

        rules = get_extraction_rules()
        exclude_rules = "\n".join(f"- {line}" for line in (rules.get("exclude") or []))
        prompt = EXTRACTION_PROMPT.format(
            text=text,
            include_rules=rules.get("include", ""),
            exclude_rules=exclude_rules,
        )
        messages = [
            {"role": "system", "content": SYSTEM_EXTRACTOR},
            {"role": "user",   "content": prompt},
        ]

        last_exc: Exception = RuntimeError("no attempts made")
        for attempt in range(2):
            try:
                with self._semaphore:
                    response = self._extract_client().chat(
                        model=settings.ollama_extract_model,
                        messages=messages,
                        format=_STARTUP_EXTRACTION_SCHEMA,
                        options={"temperature": 0, "num_predict": 3000},
                    )
                data = json.loads(response["message"]["content"])
                startups = [_normalize_startup(s) for s in data.get("startups", [])]

                grounding_cfg = get_grounding_config()
                excerpt = text[:2000]
                for s in startups:
                    if grounding_cfg.get("enabled", True):
                        s = _ground_startup(s, text, grounding_cfg)
                    s["_source_excerpt"] = excerpt

                return startups
            except Exception as exc:
                last_exc = exc
                if attempt == 0:
                    logger.warning(
                        f"[Extract] Attempt 1 failed ({exc}), retrying in 2s…"
                    )
                    time.sleep(2)

        raise last_exc

    def recheck_record(self, prompt: str) -> dict:
        """
        Phase H-3 Layer 2: ask the 14B reasoning model whether a stored
        record's fields are actually supported by its own source_excerpt.

        Uses structured output (format=schema) on the reasoning model, the
        same way extract_startups() does on the extraction model, rather
        than parsing free-text prose — reliable JSON beats hoping a
        "thinking" model formats its answer correctly, the same lesson that
        shaped extract_startups() and led to deleting the old
        parse_json_array() repair-hack. _strip_thinking still runs first
        since Qwen3 emits a <think> block before its answer.

        num_predict is deliberately generous (matches extract_startups'
        budget): Qwen3's <think> reasoning alone can run several hundred
        tokens, and a tight cap here was observed in testing to exhaust the
        whole budget on thinking before the model ever reached the actual
        JSON, returning empty content and failing to parse.

        Returns {"identity_match": bool, "summary": str,
        "unsupported_fields": [...], "contradicted_fields": [...]}.
        Raises on failure — the caller (processing/verifier.py) decides how
        to handle an unreachable/failing Ollama.
        """
        from reasoning.prompts import SYSTEM_VERIFIER

        messages = [
            {"role": "system", "content": SYSTEM_VERIFIER},
            {"role": "user", "content": prompt},
        ]
        with self._semaphore:
            response = self._verify_client().chat(
                model=self.model,
                messages=messages,
                format=_VERIFICATION_SCHEMA,
                options={"temperature": 0, "num_predict": 3000, "num_ctx": 8192},
            )
        content = self._strip_thinking(response["message"]["content"])
        return json.loads(content)

    def web_verify_record(self, prompt: str) -> dict:
        """
        Phase W: like recheck_record, but fed live web-search snippets
        instead of a stored source_excerpt, and the model IS allowed to
        propose a corrected value per finding (it has independent ground
        truth to draw from — recheck_record's source text does not).

        Reuses the same 14B reasoning model + verify client (180s timeout —
        search-result prompts run comparably long to source_excerpt ones)
        and the same structured-output + <think>-stripping pattern.

        Returns {"identity_match": bool, "summary": str, "findings": [...]}.
        Raises on failure — the caller (processing/web_verifier.py) decides
        how to handle an unreachable/failing Ollama.
        """
        from reasoning.prompts import SYSTEM_WEB_VERIFIER

        messages = [
            {"role": "system", "content": SYSTEM_WEB_VERIFIER},
            {"role": "user", "content": prompt},
        ]
        with self._semaphore:
            response = self._verify_client().chat(
                model=self.model,
                messages=messages,
                format=_WEB_VERIFICATION_SCHEMA,
                options={"temperature": 0, "num_predict": 3000, "num_ctx": 8192},
            )
        content = self._strip_thinking(response["message"]["content"])
        return json.loads(content)

    def generate(
        self,
        prompt: str,
        system: Optional[str] = None,
        temperature: float = 0.1,
        max_tokens: int = 1500,
        num_ctx: int = 8192,
    ) -> str:
        """
        Single-turn generation. No conversation history.
        Returns clean text with <think> blocks stripped.
        """
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        with self._semaphore:
            try:
                response = self._client().chat(
                    model=self.model,
                    messages=messages,
                    options={
                        "temperature": temperature,
                        "num_ctx": num_ctx,
                        "num_predict": max_tokens,
                    },
                )
                content: str = response["message"]["content"]
                return self._strip_thinking(content)

            except httpx.TimeoutException:
                logger.error("[Qwen] Timeout after 120 seconds")
                raise

            except Exception as exc:
                logger.error(f"[Qwen] Generation failed: {exc}")
                raise

    def analyze_startup(self, startup: Dict) -> str:
        """Generate investment analysis for a single startup."""
        from reasoning.prompts import STARTUP_ANALYSIS_PROMPT, SYSTEM_VC_ANALYST

        prompt = STARTUP_ANALYSIS_PROMPT.format(
            name=startup.get("name", ""),
            industry=startup.get("industry", ""),
            description=startup.get("description", "")[:500],
            city=startup.get("city", ""),
            country=startup.get("country", ""),
            funding_stage=startup.get("funding_stage", ""),
            website=startup.get("website", ""),
        )
        return self.generate(prompt, system=SYSTEM_VC_ANALYST, temperature=0.1)

    def synthesize_scout_results(self, query: str, startups: List[Dict]) -> str:
        """
        Create an investor-grade report from a list of matched startups.
        Sends ONLY the top-15 to stay well within context.
        """
        from reasoning.prompts import SCOUT_SYNTHESIS_PROMPT, SYSTEM_VC_ANALYST

        top = startups[:15]
        startup_list = "\n\n".join(
            f"**{s.get('name', 'Unknown')}** "
            f"({s.get('city', '')}, {s.get('country', '')} | {s.get('funding_stage', 'Stage unknown')})\n"
            f"Industry: {s.get('industry', '')}\n"
            f"Description: {str(s.get('description', ''))[:250]}"
            for s in top
        )

        prompt = SCOUT_SYNTHESIS_PROMPT.format(
            query=query,
            count=len(startups),
            startup_list=startup_list,
        )
        return self.generate(prompt, system=SYSTEM_VC_ANALYST, temperature=0.2, max_tokens=1200)

    def generate_sector_report(self, sector: str, startups: List[Dict]) -> str:
        """Generate a full sector intelligence report."""
        from reasoning.prompts import SECTOR_REPORT_PROMPT, SYSTEM_VC_ANALYST

        startup_list = "\n".join(
            f"- {s.get('name', 'Unknown')} ({s.get('country', '')}): "
            f"{str(s.get('description', ''))[:100]}"
            for s in startups[:25]
        )
        prompt = SECTOR_REPORT_PROMPT.format(
            sector=sector,
            count=len(startups),
            startup_list=startup_list,
        )
        return self.generate(prompt, system=SYSTEM_VC_ANALYST, temperature=0.2, max_tokens=1500)

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _strip_thinking(text: str) -> str:
        """Remove Qwen3 <think>…</think> blocks from output."""
        if "<think>" in text and "</think>" in text:
            return text.split("</think>", 1)[-1].strip()
        return text.strip()


qwen_client = QwenClient()
