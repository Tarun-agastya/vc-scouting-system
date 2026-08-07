"""
Regional company register (Phase RC, 6 Aug 2026).

Established SMEs (100-4000 employees) within a radius of Memmingen, tracked as
GreenTech Hub membership prospects. Replaces the hand-maintained
"Potenzielle Mitglieder" Excel sheet's data-gathering half while leaving its
outreach-tracking half as a human-owned CRM.

Deliberately separate from the VC-scouting pipeline at every layer — its own
table, its own API router, its own dashboard page. See REGIONAL_SME_PLAN.md
§4 for why, and database/models.py::RegionalCompany for the data/CRM split.

Reuses rather than rebuilds: processing.deduplicator.normalize_company_name
and rapidfuzz for matching, the Phase J implausible-name filter for junk,
ingestion.web_search for discovery, and processing.web_verifier for field
enrichment with citations.
"""
