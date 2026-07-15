import logging
from typing import List, Optional, Dict, Any
from config import settings

logger = logging.getLogger(__name__)

# Must match embedding model output dimension (nomic-embed-text = 768)
VECTOR_DIM = 768


class QdrantStore:
    """
    Manages two Qdrant collections:
      - startups  : all scouted startup profiles
      - investors : investor profiles for matchmaking
    """

    def __init__(self):
        self._client = None
        self._initialized = False

    def _get_client(self):
        if self._client is None:
            from qdrant_client import QdrantClient
            self._client = QdrantClient(
                host=settings.qdrant_host,
                port=settings.qdrant_port,
                timeout=10,
            )
        return self._client

    def ensure_collections(self):
        """Create collections if they do not yet exist."""
        from qdrant_client.models import Distance, VectorParams

        client = self._get_client()
        existing = {c.name for c in client.get_collections().collections}

        for name in ("startups", "investors"):
            if name not in existing:
                client.create_collection(
                    collection_name=name,
                    vectors_config=VectorParams(
                        size=VECTOR_DIM, distance=Distance.COSINE
                    ),
                )
                logger.info(f"[Qdrant] Created collection: {name}")

        self._initialized = True

    def _ensure_ready(self):
        if not self._initialized:
            self.ensure_collections()

    # ── Startup Operations ────────────────────────────────────────────────────

    def upsert_startup(self, startup_id: str, vector: List[float], payload: Dict):
        """Insert or update a startup vector + payload."""
        from qdrant_client.models import PointStruct
        self._ensure_ready()
        self._get_client().upsert(
            collection_name="startups",
            points=[PointStruct(id=startup_id, vector=vector, payload=payload)],
        )

    def search_startups(
        self,
        query_vector: List[float],
        limit: int = 20,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List:
        """
        Semantic search over startups.
        filters: dict of exact-match field → value pairs (optional).
        """
        from qdrant_client.models import Filter, FieldCondition, MatchValue
        self._ensure_ready()

        search_filter = None
        if filters:
            conditions = [
                FieldCondition(key=k, match=MatchValue(value=v))
                for k, v in filters.items()
                if v is not None
            ]
            if conditions:
                search_filter = Filter(must=conditions)

        return self._get_client().search(
            collection_name="startups",
            query_vector=query_vector,
            limit=limit,
            query_filter=search_filter,
            with_payload=True,
        )

    def get_startup_count(self) -> int:
        """Return total number of startups in the collection."""
        self._ensure_ready()
        try:
            result = self._get_client().count(collection_name="startups", exact=False)
            return result.count
        except Exception:
            return 0

    def delete_startup(self, startup_id: str) -> None:
        """Remove a single startup point by its stable UUID."""
        self._ensure_ready()
        self._get_client().delete(
            collection_name="startups",
            points_selector=[startup_id],
        )

    # ── Investor Operations ───────────────────────────────────────────────────

    def upsert_investor(self, investor_id: str, vector: List[float], payload: Dict):
        """Insert or update an investor vector + payload."""
        from qdrant_client.models import PointStruct
        self._ensure_ready()
        self._get_client().upsert(
            collection_name="investors",
            points=[PointStruct(id=investor_id, vector=vector, payload=payload)],
        )

    def search_investors(
        self,
        query_vector: List[float],
        limit: int = 10,
    ) -> List:
        """Semantic search over investors."""
        self._ensure_ready()
        return self._get_client().search(
            collection_name="investors",
            query_vector=query_vector,
            limit=limit,
            with_payload=True,
        )


qdrant_store = QdrantStore()
