"""
All LLM prompts in one place.
Edit prompts here without touching business logic.
"""

# ── System Personas ───────────────────────────────────────────────────────────

SYSTEM_VC_ANALYST = """You are SCOUT, an elite VC intelligence analyst.
You have deep knowledge of startup ecosystems, venture capital, technology trends,
and the European / DACH entrepreneurial landscape.
You provide precise, actionable investment intelligence.
Speak like a senior VC partner — analytical, direct, insightful.
Never fabricate information. If data is missing, say so explicitly."""

SYSTEM_EXTRACTOR = """You are a structured data extraction system.
Extract startup information from text with high precision.
Return ONLY valid JSON. No markdown fences, no explanation, no commentary."""

# ── Extraction Prompt (structured-output path, used with format= schema) ──────

EXTRACTION_PROMPT = """Extract every technology startup or scale-up mentioned in the text below.

INCLUDE: {include_rules}

EXCLUDE:
{exclude_rules}

Field instructions:
- one_liner: exactly 1 sentence — what the company does and who it serves. This is the first thing a VC reads to decide if they are interested. Be specific, never generic.
- description: 2-3 sentences with more context (product, traction, differentiation).
- tech_cluster: the specific technology domain, e.g. "AI/ML Infrastructure", "ClimateTech - Carbon Capture", "FinTech - Payments", "DeepTech - Robotics", "B2B SaaS - HR Tech". Be precise.
- employee_count: one of exactly: "1-10", "11-50", "51-200", "201-500", "500+" — use "" if not mentioned.
- address: full street address if mentioned; city + country is acceptable if no street address is given; "" if unknown.
- contact_info: email address or LinkedIn URL if mentioned, else "".

For all other unknown fields use "" (strings) or 0 (founded_year). Never guess a value.
Return an empty startups list only if the text contains absolutely no matching companies.

Text:
{text}"""

# ── Legacy extraction prompt (kept for newsletter_ingestor until Phase 2) ─────

NEWSLETTER_EXTRACTION_PROMPT = """Extract every startup mentioned in the text below.

Return a JSON array. Each element must follow this schema exactly:
[
  {{
    "name": "startup name (required)",
    "description": "what they do in 1-2 sentences",
    "website": "URL if mentioned, else null",
    "industry": "primary sector (e.g. AI, Fintech, Climatetech, SaaS, Deeptech, Logistics, PropTech)",
    "sub_industry": "more specific niche if identifiable",
    "country": "country if mentioned, else null",
    "city": "city if mentioned, else null",
    "funding_stage": "Pre-seed / Seed / Series A / Series B / Series C / Growth / null",
    "funding_amount": "amount raised if mentioned, else null",
    "founded_year": "4-digit year as integer if mentioned, else null",
    "contact_info": "email address or LinkedIn URL if mentioned, else null",
    "published_date": "ISO 8601 date string of the article/newsletter publish date if identifiable, else null",
    "founders": ["founder name 1", "founder name 2"],
    "tags": ["tag1", "tag2"]
  }}
]

STRICT EXCLUSION RULE:
DO NOT extract startups operating in medicine, biotech, e-commerce, or food
(unless the startup is strictly related to packaging technology).
If a startup falls into any of these excluded categories, ignore it entirely and do not include it in the output.

Additional Rules:
- Only include companies that are clearly startups or scale-ups.
- Do NOT include large corporations, VCs, or media outlets.
- If a field is unknown, use null — never guess.
- Return an empty array [] if no startups are found.

Text:
{text}"""

# ── Analysis Prompts ──────────────────────────────────────────────────────────

STARTUP_ANALYSIS_PROMPT = """Analyze this startup as a senior VC analyst.

Name: {name}
Industry: {industry}
Description: {description}
Location: {city}, {country}
Stage: {funding_stage}
Website: {website}

Provide:
1. Investment thesis (2-3 sentences — why an investor should care)
2. Key strengths (3 specific bullet points)
3. Key risks (2 specific bullet points)
4. Market opportunity (1 sentence)

Be specific and evidence-based. No generic statements."""

SCOUT_SYNTHESIS_PROMPT = """You are a senior VC analyst.
A client asked: "{query}"

You retrieved {count} relevant startups from the database. Here are the most relevant:

{startup_list}

Write a maximum of 2 sentences as an introduction summarising the search results.
Then output the startup data directly — no executive summaries, no market trend sections.
Be direct and concise."""

MATCHMAKING_RATIONALE_PROMPT = """Explain in 2-3 sentences why this startup is a strong match
for the investor profile below. Be specific — cite exact alignment points.

Investor:
- Focus: {industries}
- Stages: {stages}
- Regions: {regions}
- Thesis: {thesis}

Startup:
- Name: {name}
- Industry: {industry}
- Stage: {stage}
- Location: {country}
- Description: {description}"""

SECTOR_REPORT_PROMPT = """Generate a VC sector intelligence briefing for: {sector}

Startups in database ({count} total):
{startup_list}

Format as a professional investor briefing:
1. **Sector Overview** — key dynamics and growth drivers
2. **Notable Players** — standout companies and why
3. **White Spaces** — underserved niches and opportunities
4. **Geographic Hotspots** — where activity is concentrating
5. **Risk Factors** — macro and sector-specific risks
6. **Investment Recommendation** — 1 paragraph verdict

Keep it sharp and evidence-based."""
