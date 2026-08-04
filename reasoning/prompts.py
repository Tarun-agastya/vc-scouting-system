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

CRITICAL — per-company grounding (if the text mentions more than one company):
- Every field you report for a company must come from the SAME sentence or
  paragraph that names that company. Never borrow a fact (founding year,
  funding amount, funding stage, headcount) from a different company's
  paragraph, even if it appears right next to this one.
- If a specific fact (founding year, headcount, funding amount) is not
  literally stated for THIS company, output "" or 0 for it. A blank field is
  always correct; a guessed one is not — do not infer, estimate, round, or
  pattern-match a "plausible" value from general knowledge or from context.

CRITICAL — never merge two different companies into one name:
- If a sentence names two or more DIFFERENT companies together (they shared
  an award, were co-founded, appear in a list like "X and Y", "X, Y and Z"),
  extract EACH one as its own separate startup record with its own name.
  Never concatenate two company names into a single combined name field
  (e.g. a headline reading "X and Y took the top prize" is TWO records —
  "X" and "Y" — never one record named "X and Y").
- Watch especially for a nearby paragraph mentioning a similarly-spelled or
  partially-overlapping name (e.g. "BLP" in one sentence and "BLP Digital"
  in the next) — these may be the SAME company (extract once) or may be two
  DIFFERENT companies that happen to share a word (extract as two). Decide
  from what the text actually says about each; never default to merging
  just because the names look similar.

BARE NAME LISTS (portfolio / logo grids):
If the text says it is a list of company names shown as logos in a portfolio
grid with no further description, then it is exactly that — a curated list of
real companies, already vetted by the incubator or accelerator whose page it
came from. In that case:
- Extract every entry that is plausibly a COMPANY name, one record per name.
- Fill `name` only; leave every other field "" or 0. That is the correct,
  expected output here, not a failure — you have no other information, and a
  name-only record is genuinely useful.
- Do NOT skip a name merely because you don't recognise it or can't tell what
  the company does. An unfamiliar company name is still a company name, and
  the page's own curation is the evidence.
- DO skip entries that are plainly not companies. These lists are harvested
  from image alt text, so they often contain non-company noise mixed in:
    * image/file names ("Medium_20250128-raum-orange-0003", "Xlarge_2023",
      "Article_20191505-dzs-rf-zuschnitt", "Handshake-simple-solid")
    * individual people ("stefan.schimpfle", "clara.fischer", "Anna Treischl")
    * the host organisation itself, its own site name or its programmes
    * public bodies and institutions — cities, districts, universities,
      chambers of commerce, ministries ("Stadt Augsburg", "IHK Schwaben",
      "Technische Hochschule Augsburg", "Landkreis Augsburg")
    * sponsors/partners that are established firms rather than startups —
      banks, insurers, law firms, utilities ("VR-Bank", "Sonntag und
      Partner", "Maucher Jenkins")
    * navigation, pagination, or call-to-action link text that happens to
      sit inside the same repeating grid as the real company cards ("Bring
      me back to the incubation program", "Load more", "View all",
      "Back to overview", "Zurück zur Übersicht") — this is UI chrome, not
      a company, no matter how sentence-like or button-like it reads.
  Skipping these is correct and expected; the EXCLUDE rules above still apply
  to every entry in the list.
- Do NOT invent an industry, description, location, or year to fill the gap.
- If the list contains genuine company names, returning an empty list is wrong.

For all other unknown fields use "" (strings) or 0 (founded_year). Never guess a value.
Return an empty startups list only if the text contains absolutely no matching companies.

Text:
{text}"""

# Phase R-4 — same prompt minus the BARE NAME LISTS section, for chunks the
# adaptive pipeline already knows are NOT a bare name list (ordinary prose,
# or a structural card carrying real per-company text) — the strategy
# object tells the pipeline this directly, so the model no longer needs ~30
# lines instructing it how to recognise a shape this chunk isn't. Used only
# when settings.adaptive_pipeline_enabled; every legacy call path keeps
# using EXTRACTION_PROMPT unconditionally, unchanged.
EXTRACTION_PROMPT_PROSE = """Extract every technology startup or scale-up mentioned in the text below.

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

CRITICAL — per-company grounding (if the text mentions more than one company):
- Every field you report for a company must come from the SAME sentence or
  paragraph that names that company. Never borrow a fact (founding year,
  funding amount, funding stage, headcount) from a different company's
  paragraph, even if it appears right next to this one.
- If a specific fact (founding year, headcount, funding amount) is not
  literally stated for THIS company, output "" or 0 for it. A blank field is
  always correct; a guessed one is not — do not infer, estimate, round, or
  pattern-match a "plausible" value from general knowledge or from context.

CRITICAL — never merge two different companies into one name:
- If a sentence names two or more DIFFERENT companies together (they shared
  an award, were co-founded, appear in a list like "X and Y", "X, Y and Z"),
  extract EACH one as its own separate startup record with its own name.
  Never concatenate two company names into a single combined name field
  (e.g. a headline reading "X and Y took the top prize" is TWO records —
  "X" and "Y" — never one record named "X and Y").
- Watch especially for a nearby paragraph mentioning a similarly-spelled or
  partially-overlapping name (e.g. "BLP" in one sentence and "BLP Digital"
  in the next) — these may be the SAME company (extract once) or may be two
  DIFFERENT companies that happen to share a word (extract as two). Decide
  from what the text actually says about each; never default to merging
  just because the names look similar.

For all other unknown fields use "" (strings) or 0 (founded_year). Never guess a value.
Return an empty startups list only if the text contains absolutely no matching companies.

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

# ── Compare similar startups (Phase P-4) ────────────────────────────────────
# Distinct from SCOUT_SYNTHESIS_PROMPT: that summarizes a broad search result
# set; this does a focused head-to-head over a SMALL group of startups doing
# basically the same thing, to help decide which one (if any) to suggest to
# a stakeholder. Data quality caveats matter here — say so if evidence is thin.

COMPARISON_PROMPT = """A user is deciding which of these startups — all doing
basically the same thing — is the strongest candidate to suggest to a
stakeholder. Here is the target startup and {count} similar ones found in
the database:

TARGET:
{target}

SIMILAR STARTUPS:
{similar_list}

Write a short, direct verdict (max 5 sentences):
1. Name which one (if any) looks like the stronger candidate to suggest, and why —
   cite concrete differentiators (score, funding stage, verification status,
   completeness of the profile, what the description actually says they do
   differently).
2. If the group is too similar to call, or the data is too thin/unverified
   to trust a recommendation, say so plainly instead of forcing a pick.
3. Do not invent facts not present above. If a startup shows verification_status
   'unverified' or 'flagged', treat its data as less trustworthy and say so."""

# ── Verification recheck (Phase H-3) ────────────────────────────────────────
# Distinct from every prompt above: this one produces a VERDICT (which
# fields does the source text actually support), not investor commentary.
# It never suggests a replacement value — Layer 1 (deterministic) is the
# only thing allowed to null a field; this only classifies support.

SYSTEM_VERIFIER = """You are a fact-checker for a startup intelligence database.
Given a stored record and the exact source text it was extracted from, your
only job is to say which fields the source text actually supports. You do
NOT judge the company as an investment, and you NEVER invent, correct, or
suggest a replacement value — you only report support/contradiction.
Return ONLY valid JSON matching the required schema. No markdown, no prose
outside the JSON."""

VERIFICATION_RECHECK_PROMPT = """Source text (the ONLY evidence — judge every field against this and nothing else):
\"\"\"
{source_excerpt}
\"\"\"

Stored record for "{name}":
{fields}

For each field ask: does the source text state this, contradict it, or
simply not mention it?

Report:
- identity_match: true if the source text is clearly ABOUT this named
  company (even briefly); false if the text doesn't describe this company
  at all, describes a different company, or the name doesn't genuinely
  match what the text is about.
- summary: 1-3 plain sentences a human reviewer can read in five seconds —
  say what's solid and what to double check.
- unsupported_fields: field names the source text simply does NOT mention.
  This is NOT necessarily wrong — the value may be correct from an earlier
  sighting of this company — it just isn't confirmable from THIS text.
- contradicted_fields: field names where the source text states something
  DIFFERENT from the stored value. This is the strong signal — the record
  is likely wrong here.

Do not guess. Do not propose what the correct value should be. Only classify."""

# ── Web verification (Phase W, 23 Jul) ──────────────────────────────────────
# Distinct from SYSTEM_VERIFIER/VERIFICATION_RECHECK_PROMPT above: THAT pass
# only classifies support against a stored source_excerpt and is deliberately
# forbidden from proposing a value. This pass has independent ground truth
# (live web search results) for records that have no source_excerpt at all —
# so unlike the recheck pass, it CAN and SHOULD state what the correct value
# actually is when it finds one, always citing which search result it came
# from. Every other rule carries over: never invent, never guess, cite
# everything, only report what the evidence actually supports.

SYSTEM_WEB_VERIFIER = """You are a fact-checker for a startup intelligence database.
You are given a stored record and a set of live web search results about the
named company. Your job is to check each stored field against what the
search results actually say, and — where a result clearly gives a better or
different value — state the correct value and which result supports it.

IMPORTANT — name collisions are common: search results for a company name
often include a DIFFERENT, unrelated company that merely shares or resembles
the name (confirmed live 24 Jul: a search for "bup system", a German
fashion-tech startup, returned one genuine result plus four results about
"bUp Systems"/"B-Up Systems", an unrelated US marketing-software company —
and those got wrongly used to "correct" the real company's industry).
Before using ANY result as evidence, confirm it is genuinely about THIS
specific company — same business, same rough location/industry if stated —
not just a similar name. Silently discard any result about a different
entity; never blend facts from a different company into your findings, even
if those results outnumber the genuine ones.

Never invent a fact that isn't in the search results. If the results don't
mention a field, leave it alone. If NONE of the results are genuinely about
this company, say so via identity_match=false.
Return ONLY valid JSON matching the required schema. No markdown, no prose
outside the JSON."""

WEB_VERIFICATION_PROMPT = """Web search results for "{name}" ({context}):
{search_results}

Stored record for "{name}":
{fields}

Step 0 — first check that "{name}" is itself a real, single company name,
not two different companies merged into one. This happens when extraction
pulls a sentence naming two co-mentioned companies (e.g. "Porters and BLP
Digital took the top prize", "X and Y both raised funding this week") and
concatenates them into one bogus combined name instead of recognising them
as two separate entities. Signs of this: the search results consistently
treat "{name}"'s two halves as two distinct, separately-covered companies
rather than one; or you cannot find a single result referring to the whole
name as one entity, only to a piece of it. If this looks like the case,
set identity_match=false and say so plainly in summary, naming the two
likely-separate companies if you can tell them apart from the results —
do not attempt to silently pick one half or invent a merged profile for
the combined name.

Step 1 — for EACH search result above, decide whether it is genuinely about
THIS specific company (matching business/industry/location where stated), or
a different company that merely shares or resembles the name. This matters —
search results frequently mix in an unrelated company with a similar name.
Only results that are genuinely about this company may be used as evidence
in Step 2 — ignore every other result completely, even if they outnumber the
genuine ones.

Step 2 — using ONLY the genuinely-matching results from Step 1, check each
stored field:
- If a result confirms the stored value, do nothing (no finding needed).
- If a result gives a genuinely DIFFERENT value (e.g. a different founding
  year, a different city), report it as a finding with the correct value
  and cite the source_url of the result that supports it.
- If the results simply don't mention a field, leave it alone — that's not
  a finding, just missing evidence.
- If your OWN genuinely-matching results disagree with EACH OTHER on a
  field (not just vs. the stored value) — e.g. one says Boston, another
  says Greece — that is a conflict, not a finding: do not silently pick
  one. Prefer the company's own official channel (its own website or
  company LinkedIn page) over a third-party article or an unrelated
  mention (a founder's nationality or academic affiliation is NOT the
  company's location). If you still can't resolve it confidently, omit
  the finding entirely rather than guess — say so in summary instead.
- city, country, and address must describe ONE consistent real-world
  place. Never report city/country/address findings that would put the
  company in two different places at once (a Boston address with a
  Greece country is exactly this kind of self-contradiction) — resolve
  them together as a single location using the same official-source
  preference above, or omit all three rather than mix values pulled from
  different, disagreeing sources.
- website is usually stored EMPTY — that is not "nothing to report," it
  is the normal case you should try to fill in. If a genuinely-matching
  result reveals the company's OWN official homepage (its own domain,
  not a profile page on someone else's platform), report it as a finding
  even though the stored value was empty. If no result reveals the
  company's own domain, leave it alone like any other unmentioned field.
- short_description and description are ALSO commonly stored empty, for
  the same reason: many records come from portfolio/logo-grid pages that
  list only a company name and nothing else. Treat them like website —
  an empty value is an invitation to fill it in, not a field to skip.
  If the genuinely-matching results describe what this company actually
  does, report it:
    - short_description: ONE plain sentence saying what the company does
      ("Builds AI-powered logistics routing software for freight
      forwarders.").
    - description: 2-4 sentences with more substance — what it does, who
      it serves, and anything notable that the results actually state
      (technology, market, traction). Never pad it with speculation.
  Write both in neutral third-person prose, stating only what the results
  genuinely support. If the results reveal nothing about what the company
  does, omit these two findings rather than inventing a description.
  If a stored description already exists and the results contradict it,
  report the corrected version exactly as you would any other field.

Report:
- identity_match: true if at least one result is genuinely about this named
  company (per Step 1); false if every result is about a different company,
  nothing relevant came back at all, or the name itself looks like two
  merged companies (per Step 0).
- summary: 1-3 plain sentences a human reviewer can read in five seconds —
  say explicitly if you had to discard results about a different,
  similarly-named company, or if the stored name looks like two companies
  merged into one (name both, if identifiable, so a human can split them).
- findings: a list of {{field, verdict, correct_value, source_url}} — one
  entry per field where a genuinely-matching result contradicts the stored
  value. verdict is always "contradicted" (only report fields you're
  correcting — don't list fields that matched or weren't mentioned).
  field must be one of the exact stored field names shown above.
  correct_value must be ONLY the corrected value itself, in the same bare,
  short format the stored field already uses — put any reasoning in summary
  instead, never inside correct_value. If you cannot state a clean value
  with no explanation attached, do not report a finding for that field at
  all. Specifically:
    - founded_year: a bare 4-digit year ("2022"), or omit the finding.
    - funding_stage: a short stage name only ("Seed", "Series C",
      "Bootstrapped") — NEVER an amount or valuation ("$13.7B raised" is
      wrong; omit it or say "Series C").
    - employee_count: a short range or number ("2-10", "50").
    - city / country: the plain place name only.
    - website: the bare homepage URL of the company's OWN domain only
      (e.g. "https://www.example.com"). NEVER propose a LinkedIn,
      Crunchbase, Tracxn, PitchBook, Dealroom, EU-Startups, Wikipedia,
      news-article, or any other third-party-platform URL as website —
      those are not the company's own site, even when they're the only
      result you have. If you cannot identify the company's own domain
      with confidence, omit the finding.
    - short_description: exactly one sentence of prose.
    - description: 2-4 sentences of prose.
  For every field EXCEPT short_description and description, a correct_value
  longer than a few words is almost always wrong — omit it. Those two are
  the deliberate exception: they are prose fields and are expected to be
  full sentences.
  source_url must be one of the results you confirmed in Step 1 is
  genuinely about this company — never cite a discarded result.

Do not guess. Do not report a finding sourced from a result you haven't
confirmed is genuinely about this company."""

# ── Classification (Phase V-2, 27 Jul) ──────────────────────────────────────
# Distinct from extraction's free-form industry/tech_cluster (which produced
# noise like "Proptech - Fashion Tech"): this is a dedicated, structured-output
# call whose industry/tech_cluster are ENUM-constrained by the caller to
# config/taxonomy.yaml's controlled vocabulary, so the model can only return a
# value that's already valid — no post-hoc cleanup, no free text.

SYSTEM_CLASSIFIER = """You are a precise company classifier for a startup
intelligence database. You are given a company's name and description, and a
fixed list of valid industries and tech clusters. Pick the single best-fitting
industry and the single best-fitting tech cluster FROM THAT LIST — never
invent a new category, never combine two, never leave either blank if a
reasonable fit exists. The tech cluster you pick must genuinely belong under
the industry you pick. Also classify the business model (B2B/B2C/B2B2C/Unclear).
Return ONLY valid JSON matching the required schema. No markdown, no prose
outside the JSON."""

CLASSIFICATION_PROMPT = """Company: {name}
One-liner: {one_liner}
Description: {description}

Valid industries and their tech clusters (pick industry, then a tech_cluster
that is listed under that SAME industry):
{taxonomy}

Classify this company:
- industry: the single best-fitting industry from the list above, exactly as written.
- tech_cluster: the single best-fitting tech cluster from the list above,
  exactly as written, and it MUST be one of the clusters listed under the
  industry you chose.
- business_model: who the company actually sells to.
  "B2B" — sells to other businesses/organizations (most enterprise software,
  industrial tech, manufacturing equipment, professional services tooling).
  "B2C" — sells directly to individual consumers (consumer apps, retail
  products, personal finance apps for individuals).
  "B2B2C" — sells to businesses who then serve end consumers through it
  (e.g. a platform businesses use to serve their own customers).
  "Unclear" — the description genuinely doesn't say enough to tell. Do not
  guess — most B2B SaaS/industrial/enterprise companies really are B2B, but
  if there's no real signal either way, say Unclear rather than defaulting.

If the company is genuinely generic or doesn't fit any specific tech cluster,
use "Other / Uncategorized" for both industry and tech_cluster. Do not guess a
specific-sounding category that doesn't actually match the description."""

# ── Phase R-3: site strategist (adaptive web extraction) ───────────────────
# One cached call per source. The deterministic structural inspector (Phase
# R-1) always runs first and produces a verdict on its own — this call only
# confirms or corrects it, and is never load-bearing (Ollama down / a
# malformed response / the settings.site_strategy_llm_enabled kill switch all
# fall back to the deterministic verdict with no loss of function). See
# ingestion/site_inspector.py::derive_strategy_deterministic and
# processing/site_profile_store.py for the callers.

SYSTEM_SITE_STRATEGIST = """You are a web-page structure classifier for a
startup-scouting crawler. You are given a compact STATISTICAL SUMMARY of a
page's structure — counts, ratios, and a few sample item names — never the
raw HTML. You are not browsing the page; reason only from the numbers and
sample names given. Return ONLY valid JSON matching the required schema. No
markdown, no prose outside the JSON."""

SITE_STRATEGY_PROMPT = """A page was structurally analyzed for a startup-scouting crawler.

URL: {url}
This page's own pattern (always include a profile for this exact pattern): {own_pattern}

Fetch: {text_len} chars of text, render gain over a plain static fetch: {render_gain}
Prose density: {prose_density} (higher = more running text, lower = more structural/linky)
Link density: {link_density} (share of text that is inside links)
JSON-LD: {jsonld_summary}
Pagination detected: {pagination_kind}

Primary repeating group: {group_summary}

Other candidate groups on the page (weaker, did not win): {other_groups_summary}

Detail links: {detail_link_pattern} (coverage {detail_link_coverage} of the primary group's items)

DETERMINISTIC VERDICT (a rules-based classifier already produced this — your
job is to confirm or correct it, citing what in the numbers/names above
changes your mind if you disagree):
  page_shape: {det_page_shape}
  text_extraction: {det_text_extraction}
  chunking: {det_chunking}
  needs_render: {det_needs_render}
  reason: {det_reason}

page_shape options and what each means:
- logo_grid: a repeating group of items that carry ONLY a name — no real
  per-item description (a portfolio wall, a sponsor strip).
- card_directory: a repeating group whose items each carry real content (a
  name plus a description, price, or other substantive text) — a genuine
  directory of distinct entities.
- detail_page: this page describes ONE entity in depth, not a list.
- prose_listing: entities are mentioned in running prose/paragraphs, not a
  repeating structural group.
- article_feed: a repeating group of NEWS, EVENTS, or BLOG POSTS — the
  sample names read as headlines or event titles, not proper-noun entity
  names, even though the group structurally repeats just like a real
  directory would. This is the single most common mistake to correct: a
  clean, well-scored repeating group is not automatically a company
  directory.
- mixed: a genuine blend of more than one of the above on the same page.
- non_content: navigation, legal, contact, marketing/CTA blocks — nothing
  worth extracting here.

Before deciding, read the sample names ONE AT A TIME and ask of each: "is
this the proper name of a distinct company?" A high score, a high
distinct-names percentage, and a clean structural match mean the PAGE
STRUCTURE is a real repeating group — they say NOTHING about whether its
CONTENTS are companies. A hero/CTA section, a site's own footer menu, or a
"quick links" block can score just as well structurally as a real portfolio
grid; the sample names are the only signal that tells them apart, so they
must win over the structural score whenever the two disagree.

Worked example — do not copy this verbatim, it illustrates the reasoning:
  sample names: ['StartHub-Titelbild', 'Der StartHub ist die zentrale
  Anlaufstelle für alle Gründungsinteressierten...', 'Newsletter', 'Social
  Media', 'Imagefilm']
  -> None of these is a company name. "Newsletter" and "Social Media" are
  generic UI labels; the long sentence is page copy, not an entity name;
  "StartHub-Titelbild"/"Imagefilm" are asset/section labels. Correct verdict:
  non_content — regardless of how high the group's structural score was.
  Compare with a page whose sample names are ['Nouma Autonomy', 'WeSort.AI
  GmbH', 'e-laborate', 'SmartCitySystems'] — each IS a distinct, plausible
  company name (even though some are unfamiliar) — THAT is a real logo_grid
  or card_directory, correctly kept as such.

Return:
- profiles: 1 to 3 entries, in this exact order:
  1. ALWAYS first: your adjudication of THIS page (the verdict above).
     Set url_pattern to exactly the literal text shown after "This page's own
     pattern" near the top — copy it character-for-character, including if
     it is the literal text "(domain default)".
  2. OPTIONAL second entry: only if a detail_link_pattern was reported above
     AND you believe it leads to individual entity pages (not e.g. news
     articles) — describing what a detail page there would likely look like
     (typically page_shape "detail_page", text_extraction "main_prose" or
     "full_text"). Set its url_pattern to that detail_link_pattern value,
     copied character-for-character. Only add this if the evidence above
     genuinely supports it — do not invent a pattern with nothing backing it.
  - Do not add a third entry unless you have equally concrete evidence for
    it.
  - The FIRST entry is always read as "this page" regardless of what its
    url_pattern says — position 1 in the array matters, not just the text.
  - text_extraction: card_structured (read each card's own text) |
    alt_harvest (name-only items, e.g. logo_grid) | main_prose (extract the
    page's main article text, e.g. a single detail_page or genuine
    prose_listing) | full_text (fallback, whole-page plain text).
  - chunking: per_card | name_batch | sliding_window | blurb.
  - needs_render / paginate / follow_detail_links / bypass_candidate_filter:
    booleans. bypass_candidate_filter should be true only for logo_grid or
    card_directory (structurally-curated content that doesn't need a
    relevance heuristic); false for article_feed/prose_listing/non_content.
  - confidence: high | medium | low.
  - reason: one short sentence.
- Never state, imply, or estimate how many entities a page contains. That is
  measured separately and freshly from the live page on every crawl — you
  are not given that number and must not invent one."""
