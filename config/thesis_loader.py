"""
Scouting-thesis + taxonomy loader (Phase V-1, 24 Jul) — hot-reloaded.

Reads config/taxonomy.yaml (the controlled industry/tech_cluster vocabulary)
and config/theses.yaml (stakeholder + ad-hoc interest profiles) with an mtime
cache, exactly like config/tuning_loader.py — the files are only re-parsed when
they actually change on disk, so callers can read them freely on a hot path.

Safety: a malformed/missing file falls back to the last good parse, then to an
empty-but-valid structure, so a bad edit never crashes the pipeline.
"""
from __future__ import annotations

import io
import os
import re
import logging
from datetime import datetime, timezone
from typing import List, Optional

import yaml
from pydantic import BaseModel, ValidationError, field_validator
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

logger = logging.getLogger(__name__)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TAXONOMY_YAML_PATH = os.path.join(_PROJECT_ROOT, "config", "taxonomy.yaml")
THESES_YAML_PATH = os.path.join(_PROJECT_ROOT, "config", "theses.yaml")

_ruamel = YAML()
_ruamel.preserve_quotes = True
_ruamel.indent(mapping=2, sequence=4, offset=2)
_ruamel.width = 100_000  # disable line-wrap folding — mirrors config/source_loader.py

# ── mtime caches (one per file — they change independently) ───────────────────

_tax_mtime: Optional[float] = None
_tax_cache: Optional[dict] = None
_theses_mtime: Optional[float] = None
_theses_cache: Optional[list] = None


def _load_yaml(path: str):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# ── Taxonomy ─────────────────────────────────────────────────────────────────

def _load_taxonomy() -> dict:
    """
    Return the parsed taxonomy, re-reading only when the file's mtime changed.
    Shape: {"industries": [str, ...], "tech_clusters": {industry: [str, ...]}}.
    Never raises.
    """
    global _tax_mtime, _tax_cache
    try:
        mtime = os.path.getmtime(TAXONOMY_YAML_PATH)
    except OSError:
        if _tax_cache is None:
            logger.warning("[Theses] config/taxonomy.yaml missing — taxonomy is empty")
        return _tax_cache or {"industries": [], "tech_clusters": {}}

    if _tax_cache is not None and mtime == _tax_mtime:
        return _tax_cache

    try:
        raw = _load_yaml(TAXONOMY_YAML_PATH)
        if not isinstance(raw, dict):
            raise ValueError("root of taxonomy.yaml must be a mapping")
        parsed = {
            "industries": list(raw.get("industries") or []),
            "tech_clusters": dict(raw.get("tech_clusters") or {}),
        }
        _tax_cache = parsed
        _tax_mtime = mtime
        return parsed
    except Exception as exc:
        logger.error(f"[Theses] taxonomy.yaml invalid ({exc}) — using last-known-good")
        return _tax_cache or {"industries": [], "tech_clusters": {}}


def get_taxonomy() -> dict:
    """
    Full taxonomy plus derived views for convenience:
      industries         : [str, ...] top-level buckets
      tech_clusters      : {industry: [cluster, ...]} grouped
      all_clusters       : [str, ...] flat, deduped (for enum-constrained
                           classification in Phase V-2)
      cluster_to_industry: {cluster: industry} reverse map (for the thesis
                           tag-boost in Phase V-3)
    """
    tax = _load_taxonomy()
    industries = tax["industries"]
    grouped = tax["tech_clusters"]

    all_clusters: list = []
    cluster_to_industry: dict = {}
    seen = set()
    for industry, clusters in grouped.items():
        for c in (clusters or []):
            if c not in seen:
                seen.add(c)
                all_clusters.append(c)
            cluster_to_industry.setdefault(c, industry)

    return {
        "industries": industries,
        "tech_clusters": grouped,
        "all_clusters": all_clusters,
        "cluster_to_industry": cluster_to_industry,
    }


# ── Theses ───────────────────────────────────────────────────────────────────

def _load_theses() -> list:
    """
    Return the parsed thesis list, re-reading only when the file's mtime
    changed. Each thesis is a dict with at least id/name/kind/summary. Never
    raises.
    """
    global _theses_mtime, _theses_cache
    try:
        mtime = os.path.getmtime(THESES_YAML_PATH)
    except OSError:
        if _theses_cache is None:
            logger.warning("[Theses] config/theses.yaml missing — no theses configured")
        return _theses_cache or []

    if _theses_cache is not None and mtime == _theses_mtime:
        return _theses_cache

    try:
        raw = _load_yaml(THESES_YAML_PATH)
        if not isinstance(raw, dict):
            raise ValueError("root of theses.yaml must be a mapping")
        theses = raw.get("theses") or []
        if not isinstance(theses, list):
            raise ValueError("'theses' must be a list")
        # Normalize each to a stable shape so callers never KeyError.
        normalized = []
        for t in theses:
            if not isinstance(t, dict) or not t.get("id"):
                continue
            normalized.append({
                "id":            str(t["id"]),
                "name":          t.get("name") or str(t["id"]),
                "kind":          t.get("kind") or "theme",
                "active":        bool(t.get("active", True)),
                "summary":       (t.get("summary") or "").strip(),
                "industries":    list(t.get("industries") or []),
                "tech_clusters": list(t.get("tech_clusters") or []),
                "keywords":      list(t.get("keywords") or []),
                "exclude_keywords": list(t.get("exclude_keywords") or []),
                # Lower-weighted "useful but not the focus" interest (Phase
                # V-3 boosts these less than the primary fields above) — e.g.
                # admin/back-office/HR tooling every partner can use but none
                # of them scout for specifically. Optional; defaults empty.
                "secondary_industries":    list(t.get("secondary_industries") or []),
                "secondary_tech_clusters": list(t.get("secondary_tech_clusters") or []),
                "secondary_keywords":      list(t.get("secondary_keywords") or []),
            })
        _theses_cache = normalized
        _theses_mtime = mtime
        return normalized
    except Exception as exc:
        logger.error(f"[Theses] theses.yaml invalid ({exc}) — using last-known-good")
        return _theses_cache or []


def get_theses(active_only: bool = True) -> list:
    """All configured theses (active ones only by default)."""
    theses = _load_theses()
    return [t for t in theses if t["active"]] if active_only else list(theses)


def get_thesis(thesis_id: str) -> Optional[dict]:
    """One thesis by id, or None if unknown/misspelled."""
    for t in _load_theses():
        if t["id"] == thesis_id:
            return t
    return None


# ── Writes (comment-preserving round-trip, Phase V-4 — ad-hoc themes) ─────────
#
# Mirrors config/source_loader.py's add_web_source/delete_source pattern
# exactly: validate -> ruamel round-trip load -> append/remove -> dump, so a
# programmatic edit from the dashboard never disturbs human-written comments
# or the existing stakeholder theses' YAML-anchor secondary-interest block.

_ID_RE = re.compile(r"^[a-z0-9_]+$")
_ATTRIBUTION_STAMP_RE = re.compile(r"^\s*# Added via dashboard on \d{4}-\d{2}-\d{2}\s*$")


class ThesisEntry(BaseModel):
    id: str
    name: str
    summary: str
    industries: List[str] = []
    tech_clusters: List[str] = []
    keywords: List[str] = []
    exclude_keywords: List[str] = []

    @field_validator("id")
    @classmethod
    def _valid_id(cls, v: str) -> str:
        if not _ID_RE.match(v):
            raise ValueError("id must be lowercase letters, digits, underscores only")
        return v

    @field_validator("name", "summary")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be empty")
        return v


def _strip_orphaned_attribution_stamps(text: str) -> str:
    """Drop an auto-generated stamp left above a now-deleted entry — never touches a human comment."""
    lines = text.split("\n")
    kept = []
    for i, line in enumerate(lines):
        if _ATTRIBUTION_STAMP_RE.match(line):
            next_line = lines[i + 1] if i + 1 < len(lines) else ""
            if re.match(r"^\s*-\s", next_line):
                kept.append(line)
            continue
        kept.append(line)
    return "\n".join(kept)


def _read_document():
    if not os.path.exists(THESES_YAML_PATH):
        raise FileNotFoundError(f"{THESES_YAML_PATH} does not exist")
    with open(THESES_YAML_PATH, "r", encoding="utf-8") as f:
        doc = _ruamel.load(f)
    return doc if doc is not None else {}


def _write_document(doc) -> None:
    buf = io.StringIO()
    _ruamel.dump(doc, buf)
    text = _strip_orphaned_attribution_stamps(buf.getvalue())
    with open(THESES_YAML_PATH, "w", encoding="utf-8") as f:
        f.write(text)
    global _theses_mtime
    _theses_mtime = None  # force a re-read on the next get_theses()/get_thesis() call


def add_thesis(entry: dict) -> dict:
    """
    Validate and append a new ad-hoc theme (kind='adhoc', active=True).
    Raises ValueError on invalid input, a duplicate id (including the four
    stakeholder theses — ids are global), or an industry/tech_cluster that
    isn't in the controlled taxonomy (config/taxonomy.yaml) — ad-hoc themes
    stay on the same controlled vocabulary as classification/matching, per
    the Phase V design; free-form tags would defeat the point.
    """
    try:
        validated = ThesisEntry(**entry)
    except ValidationError as exc:
        raise ValueError(str(exc)) from exc

    if get_thesis(validated.id) is not None:
        raise ValueError(f"thesis id '{validated.id}' already exists")

    tax = get_taxonomy()
    bad_industries = [i for i in validated.industries if i not in tax["industries"]]
    bad_clusters = [c for c in validated.tech_clusters if c not in tax["all_clusters"]]
    if bad_industries or bad_clusters:
        raise ValueError(
            f"not in the controlled taxonomy — industries: {bad_industries or 'ok'}, "
            f"tech_clusters: {bad_clusters or 'ok'}"
        )

    doc = _read_document()
    doc.setdefault("theses", [])
    fields = validated.model_dump()
    fields["kind"] = "adhoc"
    fields["active"] = True
    stamped = CommentedMap(fields)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    stamped.yaml_set_start_comment(f"Added via dashboard on {stamp}")
    doc["theses"].append(stamped)
    _write_document(doc)
    logger.info(f"[Theses] Added ad-hoc theme '{validated.name}' ({validated.id})")
    return fields


def delete_thesis(thesis_id: str) -> bool:
    """
    Remove an ad-hoc theme by id. Refuses to delete a 'stakeholder' thesis
    (the four partner theses) — those are edited in config/theses.yaml
    directly, not deletable from the dashboard, to avoid an accidental
    click losing a partner's whole profile. Returns True if found+removed,
    False if the id doesn't exist.
    """
    existing = get_thesis(thesis_id)
    if existing is None:
        return False
    if existing["kind"] == "stakeholder":
        raise ValueError("stakeholder theses cannot be deleted from the dashboard")

    doc = _read_document()
    theses = doc.get("theses") or []
    remaining = [t for t in theses if t.get("id") != thesis_id]
    doc["theses"] = remaining
    _write_document(doc)
    logger.info(f"[Theses] Deleted ad-hoc theme '{thesis_id}'")
    return True
