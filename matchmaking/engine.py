import logging
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)


class MatchmakingEngine:
    """
    Matches investor profiles to startups using:
    1. Semantic similarity (Qdrant vector search)
    2. Rule-based scoring (stage, geography, industry alignment)
    3. AI-generated rationale (Qwen, top-5 matches only)
    """

    def match_investor_to_startups(
        self,
        investor_profile: Dict,
        limit: int = 10,
    ) -> List[Dict]:
        """
        Main entry: find and rank startups for an investor.
        Returns list of {startup, match_score, semantic_score, match_rationale}.
        """
        from embeddings.embedder import embedder
        from vector_db.qdrant_store import qdrant_store

        # Build investor embedding
        profile_text = embedder.build_investor_text(investor_profile)
        investor_vector = embedder.embed(profile_text)

        # Vector search — retrieve 3× limit so we have room to re-rank
        raw_results = qdrant_store.search_startups(
            query_vector=investor_vector,
            limit=limit * 3,
        )

        # Score each result
        matches = []
        for result in raw_results:
            startup = result.payload
            score = self._composite_score(investor_profile, startup, result.score)
            matches.append(
                {
                    "startup": startup,
                    "match_score": round(score, 3),
                    "semantic_score": round(result.score, 3),
                    "match_rationale": None,
                }
            )

        # Re-rank and trim
        matches.sort(key=lambda x: x["match_score"], reverse=True)
        matches = matches[:limit]

        # AI rationale for top-5 (kept small to protect Qwen latency)
        for match in matches[:5]:
            match["match_rationale"] = self._generate_rationale(
                investor_profile, match["startup"]
            )

        return matches

    def match_startup_to_investors(
        self,
        startup: Dict,
        limit: int = 5,
    ) -> List[Dict]:
        """Find investors who fit a given startup."""
        from embeddings.embedder import embedder
        from vector_db.qdrant_store import qdrant_store

        startup_text = embedder.build_startup_text(startup)
        startup_vector = embedder.embed(startup_text)

        results = qdrant_store.search_investors(
            query_vector=startup_vector, limit=limit
        )

        return [
            {
                "investor": r.payload,
                "match_score": round(r.score, 3),
            }
            for r in results
        ]

    # ── Scoring ───────────────────────────────────────────────────────────────

    def _composite_score(
        self,
        investor: Dict,
        startup: Dict,
        semantic_score: float,
    ) -> float:
        """
        Weighted composite:
          50% semantic similarity
          20% stage alignment
          15% industry alignment
          15% geography alignment
        """
        score = semantic_score * 0.50

        # Stage alignment
        focus_stages = [s.lower() for s in investor.get("focus_stages", [])]
        startup_stage = (startup.get("funding_stage") or "").lower()
        if focus_stages and startup_stage:
            if any(fs in startup_stage or startup_stage in fs for fs in focus_stages):
                score += 0.20

        # Industry alignment
        focus_industries = [i.lower() for i in investor.get("focus_industries", [])]
        startup_industry = (startup.get("industry") or "").lower()
        if focus_industries and startup_industry:
            if any(
                fi in startup_industry or startup_industry in fi
                for fi in focus_industries
            ):
                score += 0.15

        # Geography alignment
        focus_regions = [r.lower() for r in investor.get("focus_regions", [])]
        startup_country = (startup.get("country") or "").lower()
        startup_city = (startup.get("city") or "").lower()
        if focus_regions and (startup_country or startup_city):
            if any(
                fr in startup_country
                or fr in startup_city
                or startup_country in fr
                for fr in focus_regions
            ):
                score += 0.15

        return min(score, 1.0)

    def _generate_rationale(self, investor: Dict, startup: Dict) -> Optional[str]:
        """Generate AI match rationale. Returns None on failure."""
        from reasoning.qwen_client import qwen_client
        from reasoning.prompts import MATCHMAKING_RATIONALE_PROMPT

        try:
            prompt = MATCHMAKING_RATIONALE_PROMPT.format(
                industries=", ".join(investor.get("focus_industries", [])),
                stages=", ".join(investor.get("focus_stages", [])),
                regions=", ".join(investor.get("focus_regions", [])),
                thesis=investor.get("thesis", "not specified"),
                name=startup.get("name", ""),
                industry=startup.get("industry", ""),
                stage=startup.get("funding_stage", ""),
                country=startup.get("country", ""),
                description=str(startup.get("description", ""))[:200],
            )
            return qwen_client.generate(prompt, temperature=0.1, max_tokens=200)
        except Exception as exc:
            logger.error(f"[Matchmaking] Rationale generation failed: {exc}")
            return None


matchmaking_engine = MatchmakingEngine()
