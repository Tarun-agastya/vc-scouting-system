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
    published_at = Column(DateTime, nullable=True)

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
