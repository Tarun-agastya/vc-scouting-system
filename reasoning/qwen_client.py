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
        matches = list(re.finditer(rf"\b{re.escape(year)}\b", source_text))
        if not matches:
            s["founded_year"] = None
            nulled.append("founded_year")
        else:
            # The year must appear near an actual founding-context signal —
            # not merely exist somewhere in the chunk (a coincidental date
            # elsewhere, e.g. an article's own publish date, would
            # otherwise trivially "prove" a fabricated founding year; see
            # config/tuning.yaml's founded_year_signals comment).
            signals = cfg.get("founded_year_signals") or []
            window = cfg.get("founded_year_context_chars", 60)
            has_context = any(
                _signal_present(signals, source_text[max(0, m.start() - window):m.end() + window].lower())
                for m in matches
            )
            if not has_context:
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


def _is_implausible_startup_name(name: str, cfg: dict) -> bool:
    """
    Phase J (4 Aug, extended same day): True if `name` is not a plausible
    single company name — an institution/established incumbent, a
    fabricated generic noun phrase, an article headline/subheading, or
    multiple companies concatenated into one name field. Backs up (never
    replaces) the prompt-level EXCLUDE rules, which proved unreliable on
    their own — confirmed three times live now: a schwaben.digital press
    archive (31 Jul/3 Aug) and a hochschule-biberach.de partner-logo page
    (4 Aug) both extracted banks/chambers/law firms/large industrial
    incumbents (Liebherr, PERI, Goldbeck, Ed. Züblin) as "startups"; then a
    munich-startup.de crawl (4 Aug) surfaced ~272 evidence-free records
    whose "names" were actually article headlines ("Industrie 4.0: Wie
    Münchner Startups die Industrie digitalisieren"), person names bled
    into the name field from bylines, and comma-concatenated multi-company
    lists ("menstruflow, Nouxx, nghty berlin, Olena Scent" — 4 companies in
    one field). Same "prompt alone isn't enough" lesson as the geo_scope
    hard filter (Phase D).

    Unlike _ground_startup, this drops the WHOLE record rather than nulling
    a field — none of these are a real single startup, there is no partial
    record worth keeping.

    Keyword/incumbent matching goes through _signal_present, which requires
    a real word-boundary match for alphabetic terms (not a loose substring)
    so a real company whose name merely contains a similar-looking
    substring is never false-positive dropped.

    The colon/question-mark/multi-comma checks were verified against every
    name already in the database before shipping (4 Aug): 6/6 colon-bearing
    names and 5/5 names with 2+ commas were confirmed headline/list junk,
    zero false positives — real company names essentially never contain a
    raw ':' or '?', and two or more commas in a name field is a reliable
    "multiple entities got concatenated" tell.
    """
    if not cfg.get("enabled", True):
        return False

    stripped = (name or "").strip()
    if not stripped:
        return False
    name_lower = stripped.lower()

    if _signal_present(cfg.get("institution_keywords") or [], name_lower):
        return True
    if _signal_present(cfg.get("known_incumbents") or [], name_lower):
        return True

    pattern = cfg.get("generic_phrase_regex")
    if pattern and re.match(pattern, stripped, re.IGNORECASE):
        return True

    if cfg.get("reject_colon", True) and ":" in stripped:
        return True
    if cfg.get("reject_question_mark", True) and "?" in stripped:
        return True
    max_commas = cfg.get("max_commas", 1)
    if max_commas is not None and stripped.count(",") > max_commas:
        return True

    headline_pattern = cfg.get("headline_number_pattern")
    if headline_pattern and re.match(headline_pattern, stripped, re.IGNORECASE):
        return True

    regional_pattern = cfg.get("regional_collective_pattern")
    if regional_pattern and re.search(regional_pattern, stripped, re.IGNORECASE):
        return True

    person_title_pattern = cfg.get("person_title_pattern")
    if person_title_pattern and re.search(person_title_pattern, stripped, re.IGNORECASE):
        return True

    return False


logger = logging.getLogger(__name__)


def _site_strategy_context(url: str, signals: dict, deterministic: dict, own_pattern: str) -> dict:
    """
    Turn StructuralSignals.to_dict() / PageStrategy.to_dict() into the exact
    kwargs SITE_STRATEGY_PROMPT expects — a compact, human-readable summary,
    never raw HTML, never the full nested dicts (a 7B model does better with
    a short prose-like card than with dumped JSON, the same lesson every
    other prompt in this module already follows).
    """
    def pct(v) -> str:
        return f"{round((v or 0) * 100)}%"

    group = signals.get("primary_group")
    if group:
        group_summary = (
            f"{group['signature']}  n={group['n']}  score={group['score']}\n"
            f"  linked: {pct(group['frac_with_link'])}  distinct links: {pct(group['frac_unique_href'])}  "
            f"distinct names: {pct(group['frac_unique_name'])}\n"
            f"  with image: {pct(group['frac_with_img'])}  with heading: {pct(group['frac_with_heading'])}  "
            f"headline-shaped names: {pct(group['frac_headline_names'])}\n"
            f"  median item text: {group['median_text_len']} chars  name-only: {group['name_only']}\n"
            f"  sample names: {group['sample_names']}"
        )
    else:
        group_summary = "none — no repeating structural group cleared the detection threshold"

    others = signals.get("other_groups") or []
    other_groups_summary = (
        "; ".join(f"{g['signature']} n={g['n']} score={g['score']}" for g in others[:4])
        if others else "none"
    )

    jsonld_types = signals.get("jsonld_types") or {}
    jsonld_summary = (
        f"{signals.get('jsonld_item_count', 0)} item(s), types: {jsonld_types}"
        if jsonld_types else "none found"
    )

    render_gain = signals.get("render_gain")
    render_gain_str = f"{render_gain}x" if render_gain is not None else "not tested (rendered directly)"

    return {
        "url": url,
        "own_pattern": own_pattern or "(domain default)",
        "text_len": signals.get("text_len", 0),
        "render_gain": render_gain_str,
        "prose_density": signals.get("prose_density", 0),
        "link_density": signals.get("link_density", 0),
        "jsonld_summary": jsonld_summary,
        "pagination_kind": signals.get("pagination_kind") or "none detected",
        "group_summary": group_summary,
        "other_groups_summary": other_groups_summary,
        "detail_link_pattern": signals.get("detail_link_pattern") or "none detected",
        "detail_link_coverage": pct(signals.get("detail_link_coverage")),
        "det_page_shape": deterministic.get("page_shape", "unknown"),
        "det_text_extraction": deterministic.get("text_extraction", "full_text"),
        "det_chunking": deterministic.get("chunking", "sliding_window"),
        "det_needs_render": deterministic.get("needs_render", False),
        "det_reason": deterministic.get("reason", ""),
    }


class QwenClient:
    """
    Thin wrapper around Ollama for Qwen3:14b inference.

    Key design principles:
    - Small, focused prompts (no history replay)
    - Hard token cap on output
    - <think> tag stripping for Qwen3
    - Synchronous — called from FastAPI background tasks or scripts

    think=False on every self.model call (11 Aug 2026): live A/B tested on
    this machine against a real COMPARISON_PROMPT case — think=False took
    the same call from ~65s to ~12.6s (5x) with no loss of quality or
    instruction-following (verified: it still correctly flagged low-
    verification-status startups, still declined to invent facts). Thinking
    mode was never adding value here, only latency and GPU-mutex hold time —
    the latter plausibly a contributor to the historical 14B-wedges-Ollama
    incidents this class's docstring/callers reference elsewhere. Output is
    still passed through _strip_thinking() as a no-op safety net in case a
    future prompt or model revision reintroduces a <think> block anyway.
    """

    def __init__(self):
        self.model = settings.ollama_reason_model
        self.base_url = settings.ollama_base_url
        self._ollama_client = None         # lazy reason client (14B, 120s timeout)
        self._extract_ollama_client = None # lazy extract client (7B, 45s timeout)
        self._verify_ollama_client = None  # lazy verify client (14B, 180s timeout)
        self._classify_ollama_client = None # lazy classify client (7B, 120s timeout)
        self._web_verify_ollama_client = None  # lazy web-verify client (14B, 300s timeout)
        self._site_strategy_ollama_client = None  # lazy site-strategist client (14B, 240s timeout)
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

        Bumped 180s -> 240s (27 Jul): confirmed live that individual calls
        can legitimately run past 180s with nothing else competing for the
        GPU (recheck_record now also retries once — see its docstring —
        but the retry should rarely be needed once the ceiling itself has
        real headroom).
        """
        if self._verify_ollama_client is None:
            import ollama
            self._verify_ollama_client = ollama.Client(
                host=self.base_url,
                timeout=240,
            )
        return self._verify_ollama_client

    def _web_verify_client(self):
        """
        Separate client for Phase W web-verification. Reuses the 14B model
        but with a longer timeout than _verify_client()'s 180s — confirmed
        live 27 Jul: web_verify_record's prompt embeds up to 5 full
        search-result snippets (title+URL+excerpt each) on top of the
        record's own field list, measurably heavier than recheck_record's
        single 2000-char source_excerpt, and two consecutive real calls
        (same record, same 5 results) both exceeded 180s with nothing else
        competing for the GPU — this isn't a fluke, the prompt is just
        bigger than the original 180s budget assumed.
        """
        if self._web_verify_ollama_client is None:
            import ollama
            self._web_verify_ollama_client = ollama.Client(
                host=self.base_url,
                timeout=300,
            )
        return self._web_verify_ollama_client

    def _classify_client(self):
        """
        Separate client for Phase V-2 classification. Still the small 7B
        model, but _extract_client()'s 75s timeout isn't always enough here —
        confirmed live 27 Jul: the classification grammar constrains
        tech_cluster to a 114-value enum (config/taxonomy.yaml's full flat
        cluster list) plus the grouped taxonomy spelled out in the prompt
        text, and constrained decoding over that much larger grammar than a
        normal extraction call occasionally exceeded 75s (one real call timed
        out at 75s; a similar call finished cleanly in 56s) even though the
        model and call shape are otherwise identical to extract_startups.
        """
        if self._classify_ollama_client is None:
            import ollama
            self._classify_ollama_client = ollama.Client(
                host=self.base_url,
                timeout=120,
            )
        return self._classify_ollama_client

    def _site_strategy_client(self):
        """
        Separate client for Phase R-3's site strategist. Runs on the 14B
        REASONING model (self.model), not the 7B extraction model the rest
        of this file's docstring/plan initially specified — deviation
        confirmed necessary live, 3 Aug: the 7B model agreed with every
        deterministic verdict handed to it verbatim, INCLUDING two real,
        already-known-wrong ones (uni-augsburg.de and cdtm.de's homepage
        hero/CTA sections, whose sample names are literally "Newsletter",
        "Social Media", "Meet them" — not companies), even after a
        strengthened prompt with an explicit worked counter-example. The 14B
        model, given the EXACT SAME prompt, correctly caught both
        (page_shape: non_content, "sample names include UI labels, page
        copy, and asset labels"). This is a genuine judgment task — telling
        a marketing block from a real company directory from short text
        samples — not the simple enum-pick classify_startup() does, and it
        matches this project's own established two-tier split (14B for
        judgment, dozens of calls; 7B for volume, thousands of calls): this
        call happens once per (domain, url_pattern), cached for
        settings.site_profile_ttl_days, so 14B's extra latency (~78s
        observed) is negligible at the actual call volume.

        240s timeout, matching _verify_client() — same model, same
        <think>-then-structured-output shape, same empirically-needed
        headroom.
        """
        if self._site_strategy_ollama_client is None:
            import ollama
            self._site_strategy_ollama_client = ollama.Client(
                host=self.base_url,
                timeout=240,
            )
        return self._site_strategy_ollama_client

    def extract_startups(self, text: str, chunk_kind: Optional[str] = None) -> list:
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

        chunk_kind (Phase R-4): None (every legacy caller) or "name_batch"
        always gets the full EXTRACTION_PROMPT — that's today's exact,
        unconditional behaviour, since a legacy chunk's own text already
        carries the LOGO_GRID_CHUNK_HEADER self-description when it's a name
        batch. Only an explicit "prose"/"card" (the adaptive pipeline telling
        this call directly that the chunk isn't a bare name list) switches to
        EXTRACTION_PROMPT_PROSE — ~30 lines shorter per call, a real token
        saving, since the model no longer needs instructions for a shape this
        chunk provably isn't.
        """
        from reasoning.prompts import EXTRACTION_PROMPT, EXTRACTION_PROMPT_PROSE, SYSTEM_EXTRACTOR
        from config.tuning_loader import get_extraction_rules, get_grounding_config, get_institutional_junk_config

        rules = get_extraction_rules()
        exclude_rules = "\n".join(f"- {line}" for line in (rules.get("exclude") or []))
        template = EXTRACTION_PROMPT if chunk_kind in (None, "name_batch") else EXTRACTION_PROMPT_PROSE
        prompt = template.format(
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

                junk_cfg = get_institutional_junk_config()
                startups = [s for s in startups if not _is_implausible_startup_name(s.get("name"), junk_cfg)]

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
        Raises on repeated failure — the caller (processing/verifier.py)
        decides how to handle an unreachable/failing Ollama.

        One retry with backoff (added 27 Jul after a live find: this call
        previously had none, unlike extract_startups/classify_startup/
        web_verify_record — a single call landing on the slow side of the
        latency range (observed 30-265s live this session) would raise
        immediately, and the caller's "this looks like Ollama is down"
        heuristic then abandoned the ENTIRE rest of a 400+ record batch on
        one outlier, every night, for at least 5 days straight before this
        was caught).
        """
        from reasoning.prompts import SYSTEM_VERIFIER

        messages = [
            {"role": "system", "content": SYSTEM_VERIFIER},
            {"role": "user", "content": prompt},
        ]
        last_exc: Exception = RuntimeError("no attempts made")
        for attempt in range(2):
            try:
                with self._semaphore:
                    response = self._verify_client().chat(
                        model=self.model,
                        messages=messages,
                        format=_VERIFICATION_SCHEMA,
                        think=False,
                        options={"temperature": 0, "num_predict": 3000, "num_ctx": 8192},
                    )
                content = self._strip_thinking(response["message"]["content"])
                return json.loads(content)
            except Exception as exc:
                last_exc = exc
                if attempt == 0:
                    logger.warning(f"[Recheck] Attempt 1 failed ({exc}), retrying in 2s…")
                    time.sleep(2)

        raise last_exc

    def web_verify_record(self, prompt: str) -> dict:
        """
        Phase W: like recheck_record, but fed live web-search snippets
        instead of a stored source_excerpt, and the model IS allowed to
        propose a corrected value per finding (it has independent ground
        truth to draw from — recheck_record's source text does not).

        Runs on the 14B reasoning model via _web_verify_client() (300s
        timeout — its own dedicated client, longer than the plain
        recheck's 180s, since this prompt embeds several full search-result
        snippets and measurably runs longer; see that method's docstring).
        One retry with backoff on failure, mirroring classify_startup — a
        single slow/transient call shouldn't lose the whole batch record.

        Returns {"identity_match": bool, "summary": str, "findings": [...]}.
        Raises on repeated failure — the caller (processing/web_verifier.py)
        decides how to handle an unreachable/failing Ollama.
        """
        from reasoning.prompts import SYSTEM_WEB_VERIFIER

        messages = [
            {"role": "system", "content": SYSTEM_WEB_VERIFIER},
            {"role": "user", "content": prompt},
        ]
        last_exc: Exception = RuntimeError("no attempts made")
        for attempt in range(2):
            try:
                with self._semaphore:
                    response = self._web_verify_client().chat(
                        model=self.model,
                        messages=messages,
                        format=_WEB_VERIFICATION_SCHEMA,
                        think=False,
                        options={"temperature": 0, "num_predict": 3000, "num_ctx": 8192},
                    )
                content = self._strip_thinking(response["message"]["content"])
                return json.loads(content)
            except Exception as exc:
                last_exc = exc
                if attempt == 0:
                    logger.warning(f"[WebVerify] Attempt 1 failed ({exc}), retrying in 2s…")
                    time.sleep(2)

        raise last_exc

    def classify_startup(self, name: str, one_liner: str, description: str) -> dict:
        """
        Phase V-2: classify a startup into config/taxonomy.yaml's controlled
        industry + tech_cluster — never free text. The schema's enum lists are
        built fresh from the taxonomy on every call (hot-reloaded config), so
        editing taxonomy.yaml takes effect on the next classification without
        a restart.

        Runs on the small 7B extraction model (settings.ollama_extract_model),
        not the 14B reasoning model — this is a cheap categorical pick, not
        deep reasoning, and it runs once per startup at ingest plus across the
        whole reclassify backlog, so cost matters.

        Returns {"industry": str|None, "tech_cluster": str|None,
        "business_model": str|None}. tech_cluster is nulled (not the model's
        raw pick) if it doesn't actually belong under the chosen industry —
        a deterministic Python cross-check, since JSON-schema enums alone
        can't express "cluster must belong to industry" as a hard grammar
        constraint. industry stays set either way; losing tech_cluster to an
        inconsistent pick is the same correct-and-less-over-wrong-and-more
        tradeoff as H-1 grounding. business_model (Phase Q1, 29 Jul) is the
        owner's stated scouting priority (B2B > B2C) — classified in this
        SAME call, no extra Ollama round-trip.

        Raises on failure — the caller (ingest path / processing/reclassifier.py)
        decides how to handle an unreachable/failing Ollama.
        """
        from config.thesis_loader import get_taxonomy
        from reasoning.prompts import SYSTEM_CLASSIFIER, CLASSIFICATION_PROMPT

        tax = get_taxonomy()
        industries = tax["industries"]
        all_clusters = tax["all_clusters"]
        if not industries or not all_clusters:
            return {"industry": None, "tech_cluster": None, "business_model": None}

        schema = {
            "type": "object",
            "properties": {
                "industry":       {"type": "string", "enum": industries},
                "tech_cluster":   {"type": "string", "enum": all_clusters},
                "business_model": {"type": "string", "enum": ["B2B", "B2C", "B2B2C", "Unclear"]},
            },
            "required": ["industry", "tech_cluster", "business_model"],
        }
        taxonomy_text = "\n".join(
            f"- {industry}: " + ", ".join(clusters)
            for industry, clusters in tax["tech_clusters"].items()
        )
        prompt = CLASSIFICATION_PROMPT.format(
            name=name, one_liner=one_liner or "", description=description or "",
            taxonomy=taxonomy_text,
        )
        messages = [
            {"role": "system", "content": SYSTEM_CLASSIFIER},
            {"role": "user", "content": prompt},
        ]
        last_exc: Exception = RuntimeError("no attempts made")
        for attempt in range(2):
            try:
                with self._semaphore:
                    response = self._classify_client().chat(
                        model=settings.ollama_extract_model,
                        messages=messages,
                        format=schema,
                        options={"temperature": 0, "num_predict": 250},
                    )
                data = json.loads(response["message"]["content"])
                industry = data.get("industry")
                cluster = data.get("tech_cluster")
                business_model = data.get("business_model")
                if cluster and tax["cluster_to_industry"].get(cluster) != industry:
                    cluster = None
                return {"industry": industry, "tech_cluster": cluster, "business_model": business_model}
            except Exception as exc:
                last_exc = exc
                if attempt == 0:
                    logger.warning(f"[Classify] Attempt 1 failed ({exc}), retrying in 2s…")
                    time.sleep(2)

        raise last_exc

    def decide_site_strategy(self, url: str, signals: dict, deterministic: dict, own_pattern: str) -> list:
        """
        Phase R-3: ask the small model to confirm or correct the deterministic
        page-shape verdict (Phase R-1) for one already-probed page, given only
        its STRUCTURAL SIGNALS — never raw HTML, never a second fetch. Cheap
        enough to cache indefinitely per (domain, url_pattern): this call
        happens once per source pattern, not once per crawl.

        `signals` / `deterministic` are StructuralSignals.to_dict() /
        PageStrategy.to_dict() for the SAME probe (see
        ingestion/site_inspector.py, ingestion/strategy.py). `own_pattern` is
        the normalized url_pattern this specific page represents (see
        processing/site_profile_store.py::normalize_path_pattern).

        expected_entity_count is deliberately never asked for — the model
        must never invent a count (the H-1 grounding lesson applied one layer
        up). Counts stay purely structural, recomputed fresh per page per
        crawl by the caller, never from this call's output.

        Returns a list of 1-3 profile dicts (see the schema below); the
        first always describes `own_pattern` itself, and an optional second
        may describe a detail_link_pattern this page links to — how "one
        call per source" survives a domain whose listing and detail pages
        need different handling.

        Raises on repeated failure — the caller
        (processing/site_profile_store.py) always has the deterministic
        strategy as a safe, non-LLM fallback and decides whether to use it;
        this call is an adjudication, never a requirement.

        Runs on the 14B REASONING model (see _site_strategy_client's
        docstring for why: this is a genuine judgment task the 7B extraction
        model demonstrably fails at, confirmed live on real pages).
        """
        from ingestion.strategy import PAGE_SHAPES, TEXT_MODES, CHUNK_MODES
        from reasoning.prompts import SYSTEM_SITE_STRATEGIST, SITE_STRATEGY_PROMPT

        schema = {
            "type": "object",
            "properties": {
                "profiles": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 3,
                    "items": {
                        "type": "object",
                        "properties": {
                            "url_pattern":             {"type": "string"},
                            "page_shape":              {"type": "string", "enum": list(PAGE_SHAPES)},
                            "text_extraction":         {"type": "string", "enum": list(TEXT_MODES)},
                            "chunking":                {"type": "string", "enum": list(CHUNK_MODES)},
                            "needs_render":            {"type": "boolean"},
                            "paginate":                {"type": "boolean"},
                            "follow_detail_links":     {"type": "boolean"},
                            "bypass_candidate_filter": {"type": "boolean"},
                            "confidence":              {"type": "string", "enum": ["high", "medium", "low"]},
                            "reason":                  {"type": "string"},
                        },
                        "required": [
                            "url_pattern", "page_shape", "text_extraction", "chunking",
                            "needs_render", "paginate", "follow_detail_links",
                            "bypass_candidate_filter", "confidence", "reason",
                        ],
                    },
                },
            },
            "required": ["profiles"],
        }

        prompt = SITE_STRATEGY_PROMPT.format(**_site_strategy_context(url, signals, deterministic, own_pattern))
        messages = [
            {"role": "system", "content": SYSTEM_SITE_STRATEGIST},
            {"role": "user", "content": prompt},
        ]
        last_exc: Exception = RuntimeError("no attempts made")
        for attempt in range(2):
            try:
                with self._semaphore:
                    response = self._site_strategy_client().chat(
                        model=self.model,
                        messages=messages,
                        format=schema,
                        think=False,
                        # num_predict generous like recheck_record's, not classify_startup's
                        # 250: Qwen3's <think> block can run several hundred tokens before
                        # the model reaches the actual JSON, and a tight cap here was the
                        # documented cause of empty/truncated output on other 14B calls.
                        options={"temperature": 0, "num_predict": 1500, "num_ctx": 8192},
                    )
                content = self._strip_thinking(response["message"]["content"])
                data = json.loads(content)
                profiles = data.get("profiles") or []
                if not profiles:
                    raise ValueError("model returned an empty profiles array")
                return profiles
            except Exception as exc:
                last_exc = exc
                if attempt == 0:
                    logger.warning(f"[SiteStrategy] Attempt 1 failed ({exc}), retrying in 2s…")
                    time.sleep(2)

        raise last_exc

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
                    think=False,
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

    def compare_startups(self, target: Dict, similar: List[Dict]) -> str:
        """
        Phase P-4: a focused head-to-head verdict over a small group of
        startups doing basically the same thing as `target` — "which is the
        stronger candidate to suggest, and why." Distinct from
        synthesize_scout_results (that's a broad search-result summary, not
        a comparison). Reuses the same generate() primitive + persona.
        """
        from reasoning.prompts import COMPARISON_PROMPT, SYSTEM_VC_ANALYST

        def _fmt(s: Dict) -> str:
            return (
                f"**{s.get('name', 'Unknown')}** ({s.get('city', '')}, {s.get('country', '')})\n"
                f"Industry: {s.get('industry', '')} / {s.get('tech_cluster', '')}\n"
                f"Stage: {s.get('funding_stage', 'unknown')} | Employees: {s.get('employee_count', 'unknown')}\n"
                f"Score: {s.get('enrichment_score', 'n/a')} ({s.get('score_tier', 'unscored')}) | "
                f"Verification: {s.get('verification_status', 'unverified')}\n"
                f"Description: {str(s.get('description') or s.get('short_description') or '')[:250]}"
            )

        prompt = COMPARISON_PROMPT.format(
            count=len(similar),
            target=_fmt(target),
            similar_list="\n\n".join(_fmt(s) for s in similar),
        )
        # max_tokens=800 (first attempt, 29 Jul) silently produced an EMPTY
        # verdict live — no exception, just "". Same bug class already
        # documented on recheck_record/extract_startups: Qwen3's <think>
        # reasoning alone can run several hundred tokens, and a tight cap
        # exhausts the whole budget before the model ever reaches the actual
        # answer. Bumped to 2000 to match that established lesson.
        return self.generate(prompt, system=SYSTEM_VC_ANALYST, temperature=0.2, max_tokens=2000)

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
