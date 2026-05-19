import logging
from typing import Optional, List, Dict
from config import settings

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
        self._ollama_client = None  # Lazy, cached — avoid re-creating on every call

    def _client(self):
        if self._ollama_client is None:
            import ollama
            self._ollama_client = ollama.Client(host=self.base_url)
        return self._ollama_client

    def generate(
        self,
        prompt: str,
        system: Optional[str] = None,
        temperature: float = 0.1,
        max_tokens: int = 1500,
    ) -> str:
        """
        Single-turn generation. No conversation history.
        Returns clean text with <think> blocks stripped.
        """
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        try:
            response = self._client().chat(
                model=self.model,
                messages=messages,
                options={
                    "temperature": temperature,
                    "num_ctx": 4096,
                    "num_predict": max_tokens,
                },
            )
            content: str = response["message"]["content"]
            return self._strip_thinking(content)

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
