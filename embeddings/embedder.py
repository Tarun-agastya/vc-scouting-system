import logging
from typing import List, Dict
from config import settings

logger = logging.getLogger(__name__)

# nomic-embed-text produces 768-dimensional vectors
EMBEDDING_DIM = 768


class Embedder:
    """
    Generates embeddings via Ollama (nomic-embed-text).
    Lightweight: never loads a GPU model — runs on CPU.
    """

    def __init__(self):
        self.model = settings.ollama_embed_model

    def _client(self):
        import ollama
        return ollama.Client(host=settings.ollama_base_url)

    def embed(self, text: str) -> List[float]:
        """
        Generate a single embedding vector.
        Raises RuntimeError if the embedding service is unavailable —
        callers must handle this rather than silently receiving a zero vector
        (which would corrupt search results).
        """
        if not text or not text.strip():
            raise ValueError("Cannot embed empty text")
        try:
            response = self._client().embeddings(
                model=self.model,
                prompt=text[:2000],  # Stay within context limit
            )
            return response["embedding"]
        except Exception as exc:
            logger.error(f"[Embedder] Failed to embed text: {exc}")
            raise RuntimeError(
                f"Embedding failed — is Ollama running with '{self.model}'? Error: {exc}"
            ) from exc

    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        """Generate embeddings for a list of texts."""
        return [self.embed(t) for t in texts]

    def build_startup_text(self, startup: Dict) -> str:
        """
        Construct a rich, searchable text representation of a startup.
        This is what gets embedded — pack in all relevant fields.
        """
        parts = [
            f"Company: {startup.get('name', '')}",
            f"Industry: {startup.get('industry', '')}",
            f"Description: {startup.get('description', '')}",
            f"Location: {startup.get('city', '')}, {startup.get('country', '')}",
            f"Stage: {startup.get('funding_stage', '')}",
            f"Business Model: {startup.get('business_model', '')}",
        ]

        if startup.get("tags"):
            tags = startup["tags"]
            if isinstance(tags, list):
                parts.append(f"Tags: {', '.join(tags)}")

        if startup.get("founders"):
            founders = startup["founders"]
            if isinstance(founders, list):
                parts.append(f"Founders: {', '.join(founders)}")
            elif isinstance(founders, str):
                parts.append(f"Founders: {founders}")

        if startup.get("short_description"):
            parts.append(f"Summary: {startup['short_description']}")

        return "\n".join(p for p in parts if p.split(": ", 1)[-1].strip())

    def build_investor_text(self, investor: Dict) -> str:
        """Build embedding text for an investor profile."""
        parts = [
            f"Investor: {investor.get('name', '')}",
            f"Type: {investor.get('type', '')}",
            f"Focus industries: {', '.join(investor.get('focus_industries', []))}",
            f"Investment stages: {', '.join(investor.get('focus_stages', []))}",
            f"Geographic focus: {', '.join(investor.get('focus_regions', []))}",
        ]
        if investor.get("thesis"):
            parts.append(f"Thesis: {investor['thesis']}")
        return "\n".join(p for p in parts if p.split(": ", 1)[-1].strip())


embedder = Embedder()
