"""
ValidationSession — append-only JSONL capture for extraction provenance.

One session = one scrape run against one source URL.

Lifecycle
---------
  with ValidationSession(source_url=url) as vsession:
      await web_scraper.scrape_source(url=url, ..., validation_session=vsession)
  # artifacts are in validation/{run_id}/capture.jsonl

Record anatomy
--------------
  One record is written per *extracted company*, not per chunk.
  If a chunk yields 0 companies, one record is written with
  extracted_company_name = "(none)" so coverage gaps are visible.

Thread safety
-------------
  storage_worker_task runs in the asyncio event loop and calls record()
  serially (storage is single-worker by design).  qwen_worker_task also
  calls record() for the empty-extraction case from the event loop.
  A threading.Lock guards the file handle for correctness if the worker
  count is ever raised above 1.
"""

import json
import logging
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Root artifacts directory — sibling of the project root (not committed to Git)
_VALIDATION_DIR = Path(__file__).resolve().parent.parent / "validation"


class ValidationSession:
    """
    Context manager that opens ``validation/{run_id}/capture.jsonl`` on entry
    and flushes + closes it on exit.

    Parameters
    ----------
    source_url : str
        The top-level URL being scraped (used as a grouping key in every record).
    run_id : str, optional
        Supply a fixed UUID to resume / overwrite a previous session.  Defaults
        to a new UUID4.
    """

    def __init__(self, source_url: str, run_id: Optional[str] = None) -> None:
        self.run_id    = run_id or str(uuid.uuid4())
        self.source_url = source_url
        self._dir  = _VALIDATION_DIR / self.run_id
        self._path = self._dir / "capture.jsonl"
        self._fh   = None
        self._lock = threading.Lock()

    # ── Context manager protocol ──────────────────────────────────────────────

    def __enter__(self) -> "ValidationSession":
        self._dir.mkdir(parents=True, exist_ok=True)
        self._fh = open(self._path, "a", encoding="utf-8")
        logger.info(
            f"[Validation] Session {self.run_id} started — writing to {self._path}"
        )
        return self

    def __exit__(self, *_) -> None:
        if self._fh:
            self._fh.flush()
            self._fh.close()
            self._fh = None
        logger.info(
            f"[Validation] Session {self.run_id} closed — "
            f"capture complete ({self._path})"
        )

    # ── Public API ────────────────────────────────────────────────────────────

    def record(
        self,
        *,
        page_url:      str,
        chunk_num:     int,
        total_chunks:  int,
        chunk_preview: str,
        company_name:  str,            # "" → written as "(none)"
        startup_dict:  dict,
        qwen_duration_s: float,
        stored:        bool,
        record_id:     Optional[str],
    ) -> None:
        """
        Append one extraction provenance record to the JSONL file.

        Safe to call from the asyncio event loop or any thread.
        Silently no-ops if the session has been closed (should never happen
        in normal operation but guards against mis-ordering in tests).
        """
        if not self._fh:
            return

        chunk_id = f"{page_url}#chunk_{chunk_num}_of_{total_chunks}"
        entry = {
            "run_id":                 self.run_id,
            "source_url":             self.source_url,
            "page_url":               page_url,
            "chunk_id":               chunk_id,
            "chunk_preview":          chunk_preview[:200],
            "extracted_company_name": company_name if company_name else "(none)",
            "extracted_data":         json.dumps(startup_dict, ensure_ascii=False),
            "qwen_duration_s":        round(qwen_duration_s, 2),
            "stored":                 stored,
            "record_id":              record_id,
            "captured_at":            datetime.utcnow().isoformat(),
        }
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with self._lock:
            if self._fh:
                self._fh.write(line)
