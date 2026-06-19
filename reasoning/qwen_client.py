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
                    "description":    {"type": "string"},
                    "website":        {"type": "string"},
                    "industry":       {"type": "string"},
                    "sub_industry":   {"type": "string"},
                    "country":        {"type": "string"},
                    "city":           {"type": "string"},
                    "funding_stage":  {"type": "string"},
                    "funding_amount": {"type": "string"},
                    "founded_year":   {"type": "integer"},
                    "contact_info":   {"type": "string"},
                    "published_date": {"type": "string"},
                    "founders":       {"type": "array", "items": {"type": "string"}},
                    "tags":           {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "name", "description", "website", "industry", "sub_industry",
                    "country", "city", "funding_stage", "funding_amount",
                    "founded_year", "contact_info", "published_date",
                    "founders", "tags",
                ],
            },
        }
    },
    "required": ["startups"],
}

# String fields that should be None (not "") when the model returns empty/zero.
_NULLABLE_STR_FIELDS = (
    "description", "website", "industry", "sub_industry", "country", "city",
    "funding_stage", "funding_amount", "contact_info", "published_date",
)


def _normalize_startup(s: dict) -> dict:
    """Convert empty-string sentinels to None so upsert_startup sees null values."""
    for field in _NULLABLE_STR_FIELDS:
        if s.get(field) == "":
            s[field] = None
    if s.get("founded_year") == 0:
        s["founded_year"] = None
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

    def extract_startups(self, text: str) -> list:
        """
        Extract startup entities from text using the small fast extraction model.

        Uses Ollama structured output (format= JSON schema) so the response is
        guaranteed-valid JSON — no <think> stripping or parse-repair needed.
        Retries once after a 2-second pause on transient failure, then re-raises
        so the caller (worker_queue) can count the failure and move on.
        """
        from reasoning.prompts import EXTRACTION_PROMPT, SYSTEM_EXTRACTOR

        prompt = EXTRACTION_PROMPT.format(text=text)
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
                return [_normalize_startup(s) for s in data.get("startups", [])]
            except Exception as exc:
                last_exc = exc
                if attempt == 0:
                    logger.warning(
                        f"[Extract] Attempt 1 failed ({exc}), retrying in 2s…"
                    )
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

    @staticmethod
    def parse_json_array(response: str) -> list:
        """
        Robustly extract a JSON array from an LLM response.

        Handles the two most common LLM formatting mistakes:
          - Trailing commas before ] or }  e.g. {"a": 1,}
          - Extra prose / markdown wrapping the array

        Returns an empty list (never raises) if all attempts fail.
        """
        # Strip think blocks first
        if "<think>" in response and "</think>" in response:
            response = response.split("</think>", 1)[-1].strip()

        start = response.find("[")
        end = response.rfind("]") + 1
        if start == -1 or end <= start:
            logger.debug(
                "[Qwen] parse_json_array: NO ARRAY BRACKETS found — "
                "raw_response=%r", response[:300]
            )
            return []

        json_str = response[start:end]

        # Attempt 1: direct parse
        try:
            result = json.loads(json_str)
            if not result:
                logger.debug(
                    "[Qwen] parse_json_array: EMPTY ARRAY returned — "
                    "json_str=%r", json_str[:200]
                )
            return result
        except json.JSONDecodeError as exc:
            logger.debug(
                "[Qwen] parse_json_array: INVALID JSON (attempt 1) — %s — "
                "json_str=%r", exc, json_str[:300]
            )

        # Attempt 2: strip trailing commas (most common LLM mistake)
        repaired = re.sub(r",\s*([\]}])", r"\1", json_str)
        try:
            result = json.loads(repaired)
            logger.debug("[Qwen] JSON repaired (trailing commas removed)")
            return result
        except json.JSONDecodeError as exc:
            logger.warning(
                "[Qwen] parse_json_array: PARSE FAILED even after repair — "
                "%s — json_str=%r", exc, json_str[:300]
            )
            return []


qwen_client = QwenClient()
