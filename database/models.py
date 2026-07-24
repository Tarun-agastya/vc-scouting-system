from sqlalchemy import (
    Column, String, DateTime, Float, Integer,
    Text, JSON, ForeignKey, Boolean
)
from sqlalchemy.dialects.postgresql import UUID, ARRAY
from sqlalchemy.orm import relationship, declarative_base
import uuid
from datetime import datetime

Base = declarative_base()


class Startup(Base):
    __tablename__ = "startups"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(255), nullable=False, index=True)
    website = Column(String(500))
    description = Column(Text)
    short_description = Column(String(500))

    # Categorization
    industry = Column(String(100), index=True)
    sub_industry = Column(String(100))
    tech_cluster = Column(String(200))
    tags = Column(ARRAY(String))
    business_model = Column(String(100))

    # Location
    country = Column(String(100), index=True)
    city = Column(String(100))
    region = Column(String(100))
    address = Column(String(500))

    # Funding
    funding_stage = Column(String(50), index=True)
    total_funding_usd = Column(Float)
    last_funding_date = Column(DateTime)

    # Publication tracking
    published_at = Column(DateTime, nullable=True)   # the source article/newsletter's own date
    extracted_at = Column(DateTime, nullable=True)   # when THIS pipeline captured the record (date + time)

    # Company details
    founded_year = Column(Integer)
    employee_count = Column(String(50))

    # Source tracking
    source = Column(String(200))
    source_url = Column(String(500))
    source_type = Column(String(100))

    # Source attribution history — never overwritten, only appended
    # Format: [{"source": "...", "url": "...", "date": "..."}, ...]
    source_history = Column(JSON, default=list)

    # Deduplication identity
    normalized_name = Column(String(255), index=True)
    fingerprint = Column(String(64), unique=True, nullable=True, index=True)

    # Contact
    contact_info = Column(String(500))
    linkedin = Column(String(500))

    # Enrichment tracking
    enrichment_score  = Column(Float, default=0.0)
    source_confidence = Column(Float, default=0.0)    # Phase 3: extraction trust score
    score_tier        = Column(String(50), nullable=True, index=True)  # Phase 3: tier label
    score_breakdown   = Column(JSON, nullable=True)   # Phase 3: explainable breakdown
    last_enriched_at  = Column(DateTime, nullable=True)

    # Verification / recheck state (Phase H-2/H-3) — a record's TRUST state,
    # distinct from enrichment_score (which measures completeness, not
    # truthfulness). Every record starts "unverified"; a Phase H-3 recheck
    # pass (deterministic re-ground + LLM deep-recheck against source_excerpt)
    # sets it to "verified" (no unsupported claims found) or "flagged" (a
    # field contradicted its own source text). Never auto-corrected —
    # flagged records wait for a human, consistent with the S-3b model.
    verification_status   = Column(String(20), default="unverified", index=True)  # unverified | verified | flagged
    verification_notes    = Column(Text, nullable=True)   # human/LLM-readable recheck summary
    verification_evidence = Column(JSON, nullable=True)   # per-field grounded/nulled/flagged detail
    verified_at            = Column(DateTime, nullable=True)
    source_excerpt          = Column(Text, nullable=True)  # the extraction chunk this record came from (Phase H-1)

    # AI-generated insights
    ai_summary = Column(Text)
    investment_thesis = Column(Text)
    strengths = Column(JSON)
    weaknesses = Column(JSON)

    # Vector DB reference
    embedding_id = Column(String(100))

    # Metadata
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    raw_data = Column(JSON)

    # Relationships
    founders = relationship("Founder", back_populates="startup", cascade="all, delete-orphan")
    funding_rounds = relationship("FundingRound", back_populates="startup", cascade="all, delete-orphan")


class Founder(Base):
    __tablename__ = "founders"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    startup_id = Column(UUID(as_uuid=True), ForeignKey("startups.id", ondelete="CASCADE"))

    name = Column(String(255), nullable=False)
    role = Column(String(100))
    linkedin_url = Column(String(500))
    bio = Column(Text)
    background = Column(String(200))

    created_at = Column(DateTime, default=datetime.utcnow)

    startup = relationship("Startup", back_populates="founders")


class FundingRound(Base):
    __tablename__ = "funding_rounds"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    startup_id = Column(UUID(as_uuid=True), ForeignKey("startups.id", ondelete="CASCADE"))

    round_type = Column(String(50))  # Pre-seed, Seed, Series A, etc.
    amount_usd = Column(Float)
    date = Column(DateTime)
    investors = Column(JSON)  # List of investor names
    lead_investor = Column(String(255))

    created_at = Column(DateTime, default=datetime.utcnow)

    startup = relationship("Startup", back_populates="funding_rounds")


class Investor(Base):
    __tablename__ = "investors"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(255), nullable=False, index=True)
    type = Column(String(100))  # VC, Angel, Family Office, Corporate

    focus_industries = Column(ARRAY(String))
    focus_stages = Column(ARRAY(String))
    focus_regions = Column(ARRAY(String))

    description = Column(Text)
    website = Column(String(500))
    thesis = Column(Text)

    # Vector DB reference
    embedding_id = Column(String(100))

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ScoutingSession(Base):
    __tablename__ = "scouting_sessions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    query = Column(Text, nullable=False)
    filters = Column(JSON)
    results = Column(JSON)
    result_count = Column(Integer)
    source = Column(String(100))  # discord, api, manual

    created_at = Column(DateTime, default=datetime.utcnow)


class NewsletterEntry(Base):
    __tablename__ = "newsletter_entries"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    subject = Column(String(500))
    sender = Column(String(255))
    received_at = Column(DateTime)
    raw_text = Column(Text)
    extracted_startups = Column(JSON)
    startup_count = Column(Integer, default=0)
    processed = Column(Boolean, default=False)

    created_at = Column(DateTime, default=datetime.utcnow)


class DuplicateReview(Base):
    """
    A data-stewardship review item (Phase S-3b). The pipeline NEVER auto-merges
    or auto-overwrites; instead every change to an existing master and every
    possible-duplicate is staged here for a human to approve/reject in the
    dashboard "Review Inbox". The incoming data is never lost.

    review_type:
      field_update       — the same master, but a field changed. `master_id` is
                           the record; `proposed_changes` is the diff. The master
                           is NOT modified until a human approves. `incoming_id`
                           is NULL (no separate row — it's the same startup).
      possible_duplicate — an incoming record resembles an existing master but
                           identity is not exact. Incoming was inserted as its
                           own master (`incoming_id`), the pair is flagged.
      anomaly            — layers contradict (e.g. shared/multi-tenant domain +
                           mismatched name/founders). Handled like possible_duplicate.

    status transitions:
      pending  → approved  (field_update → apply diff to master; duplicate → merge rows)
      pending  → rejected  (discard; record suppression so it isn't re-flagged)
      pending  → deleted   (neither merge nor keep — permanently remove the
                            master and/or incoming record itself; 23 Jul)
    """
    __tablename__ = "duplicate_reviews"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    review_type = Column(String(30), default="possible_duplicate", index=True)

    # The existing master this review concerns
    master_id   = Column(UUID(as_uuid=True), index=True)
    master_name = Column(String(255))

    # The incoming record. incoming_id is set only for possible_duplicate/anomaly
    # (where incoming was inserted as its own row); NULL for field_update.
    incoming_id   = Column(UUID(as_uuid=True), nullable=True, index=True)
    incoming_name = Column(String(255))
    incoming_data = Column(JSON)        # full incoming extraction (apply on approval, no re-scrape)

    # For field_update: {field: {old, new, incoming_source, incoming_extracted_at}}
    proposed_changes = Column(JSON)
    evidence         = Column(JSON)     # full per-signal scorecard (all signals separately)
    risk_level       = Column(String(20), default="low", index=True)  # low | high | anomaly
    confidence       = Column(Float)    # aggregate score (ordering only, not a gate)
    llm_explanation  = Column(Text, nullable=True)  # nightly prose summary of evidence — NOT a verdict

    # Provenance of the incoming observation
    source  = Column(String(200))
    run_id  = Column(String(64), nullable=True)

    status     = Column(String(30), default="pending", index=True)  # pending | approved | rejected | deleted

    created_at  = Column(DateTime, default=datetime.utcnow)
    resolved_at = Column(DateTime, nullable=True)


class SuppressedMatch(Base):
    """
    Records human 'reject' decisions so the same thing is not re-flagged on
    every twice-weekly sweep (Phase S-3b volume guardrail).

    kind:
      known_different — a (master_id, other_id) pair a human confirmed are
                        different companies; the matcher must not flag them again.
      rejected_value  — a (master_id, field, value) a human rejected; an
                        identical future proposal is auto-suppressed.
    """
    __tablename__ = "suppressed_matches"

    id   = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    kind = Column(String(30), index=True)

    master_id = Column(UUID(as_uuid=True), index=True)
    other_id  = Column(UUID(as_uuid=True), nullable=True, index=True)  # known_different
    field     = Column(String(100), nullable=True)                     # rejected_value
    value     = Column(Text, nullable=True)                            # rejected_value

    created_at = Column(DateTime, default=datetime.utcnow)
