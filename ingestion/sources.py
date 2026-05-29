"""
All startup intelligence sources: RSS feeds, accelerators,
incubators, university spinoff pages, and hubs.
Strictly focused on the DACH region and Europe.
"""

# ── RSS Feeds ─────────────────────────────────────────────────────────────────
RSS_FEEDS = [
    # Europe-focused
    {"name": "EU-Startups",             "url": "https://eu-startups.com/feed/",                     "region": "europe",  "type": "directory"},
    {"name": "Sifted",                  "url": "https://sifted.eu/feed",                            "region": "europe",  "type": "news"},
    {"name": "Tech.eu",                 "url": "https://tech.eu/feed/",                             "region": "europe",  "type": "news"},

    # DACH
    {"name": "Gruenderszene",           "url": "https://www.gruenderszene.de/feed",                 "region": "dach",    "type": "news"},
    {"name": "StartupTicker.ch",        "url": "https://www.startupticker.ch/en/news/rss-feed",     "region": "dach",    "type": "news"},
]

# ── Accelerator Portfolio Pages ───────────────────────────────────────────────
ACCELERATOR_SOURCES = [
    {"name": "Entrepreneur First",              "url": "https://www.joinef.com/portfolio/",                 "region": "europe"},
    {"name": "Plug and Play Europe",            "url": "https://www.plugandplaytechcenter.com/europe/",     "region": "europe"},
    {"name": "High-Tech Gruenderfonds",         "url": "https://www.htgf.de/en/portfolio/",                "region": "dach"},
    {"name": "UnternehmerTUM",                  "url": "https://www.unternehmertum.de/en/portfolio",       "region": "dach"},
    {"name": "Atlantic Labs",                   "url": "https://www.atlanticlabs.de/portfolio/",            "region": "dach"},
    {"name": "Axel Springer Plug and Play",     "url": "https://aspp.com/portfolio",                       "region": "dach"},
    {"name": "Factory Berlin",                  "url": "https://factoryberlin.com/community/",              "region": "dach"},
    {"name": "Station F",                       "url": "https://stationf.co/startups/",                    "region": "europe"},
    {"name": "D.Z.S. Digitales Zentrum Schwaben", "url": "https://schwaben.digital",                       "region": "dach"},
]

# ── University & Research Spinoff Pages ───────────────────────────────────────
UNIVERSITY_SOURCES = [
    {"name": "TU Munich Startups",              "url": "https://www.tum.de/en/innovation/startups",                                 "region": "dach"},
    {"name": "ETH Zurich Spinoffs",             "url": "https://ethz.ch/en/industry/entrepreneurship/startups.html",               "region": "dach"},
    {"name": "LMU Munich Startups",             "url": "https://www.lmu.de/en/research/transfer/startup/",                         "region": "dach"},
    {"name": "KIT Startups",                    "url": "https://www.kit.edu/kit/english/innnovations-transfer.php",                "region": "dach"},
    {"name": "Oxford University Innovation",    "url": "https://innovation.ox.ac.uk/companies/spinout-companies/",                 "region": "europe"},
    {"name": "Imperial College London",         "url": "https://www.imperial.ac.uk/enterprise/staff/start-a-business/",           "region": "europe"},
    {"name": "Cambridge Enterprise",            "url": "https://www.enterprise.cam.ac.uk/cambridge-start-ups/",                   "region": "europe"},
    {"name": "EPFL Innovation Park",            "url": "https://innovationpark.ch/en/startups/",                                  "region": "dach"},
    {"name": "StartHub Augsburg",               "url": "https://www.uni-augsburg.de/de/organisation/einrichtungen/starthub/startseite/", "region": "dach"},
]

# ── Startup Hubs & Databases ──────────────────────────────────────────────────
HUB_SOURCES = [
    {"name": "Dealroom.co",     "url": "https://dealroom.co",                   "region": "europe",  "requires_api": True},
]

# ── Newsletter Keyword Filters ─────────────────────────────────────────────────
NEWSLETTER_KEYWORDS = [
    "startup", "raises", "funding", "seed round", "series a", "series b",
    "series c", "founders", "launch", "venture", "investment", "accelerator",
    "incubator", "AI startup", "fintech", "healthtech", "climate tech",
    "deeptech", "SaaS", "b2b startup", "early stage", "pre-seed",
    "portfolio company", "batch", "cohort", "demo day",
]

# ── Default Search Prompts for Sector Intelligence ────────────────────────────
SECTOR_PROMPTS = {
    "ai": "AI machine learning deep learning neural networks LLM artificial intelligence",
    "fintech": "fintech financial technology payments banking neobank insurtech",
    "healthtech": "healthtech medtech digital health biotech genomics diagnostics",
    "climatetech": "climate tech cleantech green energy sustainability carbon",
    "deeptech": "deep tech hardware robotics quantum computing semiconductors",
    "saas": "SaaS software B2B enterprise cloud platform workflow automation",
    "ecommerce": "ecommerce marketplace D2C retail commerce logistics",
}
