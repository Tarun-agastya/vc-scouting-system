"""
Research for one-pagers built from the HubDrive database (no pitch deck).

The database record is a lead, not a source — too thin to write a one-pager
from, and sometimes wrong (see sitereader.py). So this works from what a
one-pager NEEDS (FORMAT.md) and goes looking for whatever is missing:

  REQUIREMENTS — each item a one-pager needs, how to tell whether the
  material already covers it (a transparent pattern check — no model call,
  no credits), and the web search that fills the gap.

Order, cheapest and most reliable first:
  1. the record (lead) and its source article, cut to the paragraphs that
     name the company (free);
  2. the company's own website — homepage, about/team/product pages and the
     Impressum (free). Missing from the record? Found from the first search's
     results, but only a domain that IS the company's name;
  3. web searches ONLY for the items still uncovered, through the guarded
     cache -> SearXNG -> Tavily path, at most MAX_PAID_SEARCHES per startup;
  4. whatever is still missing is reported item by item, so a person knows
     exactly what to add in "Your input" — never filled with a guess.

Same isolation contract as the rest of this tool: no pipeline imports, no
database access (the API hands the record over as a JSON file).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import sitereader
import websearch

# More than the deck flow's 2: here the web is the main source, not a
# supplement. Still per startup, still under the monthly cap and the pipeline
# reserve in websearch.py.
MAX_PAID_SEARCHES = int(os.environ.get("ONEPAGER_RECORD_MAX_SEARCHES", "4"))

_FIG = r"\d[\d.,]*\s*(%|prozent|percent|t\b|tonnen|tons?|kwh|mwh|gwh|€|eur|euro|chf|\$|mio|million|x\b|-?fach|stunden|hours|minuten|minutes|tage|days)"

# How sources state a founding year — "founded in", "gegründet", but also
# "born in 2023" and "a 2023 ETH Zurich spin-off" (REEcover, Oct 2026), which
# the first version of this pattern missed while the year was right there.
_FOUNDED_WORDS = (r"(gegründet|gegruendet|founded|gründung|gruendung|established|incorporated|"
                  r"seit|since|born|launched|spin-?off|spin-?out|ausgründung|ausgruendung|"
                  r"ausgegründet|ausgegruendet)")
FOUNDED_PATTERN = (rf"{_FOUNDED_WORDS}\D{{0,25}}(19|20)\d\d|"
                   rf"(19|20)\d\d\D{{0,30}}{_FOUNDED_WORDS}")

# key, label (shown to a person), coverage pattern (de + en), query, where it lands
REQUIREMENTS = [
    {"key": "product", "label": "What the product is and how it works",
     "pattern": r"(platform|plattform|software|technolog|verfahren|process|develops|entwickelt|"
                r"system|solution|lösung|produkt|product|anlage|device|gerät)",
     "query": "{name} product technology how it works", "section": "loesung"},
    {"key": "figures", "label": "Concrete figures for 'Mehrwerte' (savings, %, volumes)",
     "pattern": _FIG,
     "query": "{name} results savings percent impact", "section": "mehrwerte"},
    {"key": "competition", "label": "Competitors or the usual alternative",
     "pattern": r"(wettbewerb|competitor|konkurren|alternative|im gegensatz|unlike|compared to|"
                r"instead of|anstatt|statt |herkömmlich|conventional|traditional)",
     "query": "{name} competitors alternatives", "section": "usp"},
    {"key": "traction", "label": "Customers, pilots, partners or other traction",
     "pattern": r"(kunde|customer|client|pilot|partner|\bloi\b|absichtserkl|referenz|reference|"
                r"nutzer|users|deployed|im einsatz|auftrag|contract|award|preis gewonnen|won)",
     "query": "{name} customers pilot partners", "section": "zielgruppe"},
    {"key": "business_model", "label": "Business model (what is sold, to whom)",
     "pattern": r"(pricing|preis|price|abo|subscription|lizenz|licen[cs]|saas|verkauf|verkauft|"
                r"sells|sale|leasing|fee|gebühr|per month|pro monat|revenue|umsatz|b2b|b2c)",
     "query": "{name} business model pricing", "section": "geschaeftsmodell"},
    {"key": "founded", "label": "Founding year",
     "pattern": FOUNDED_PATTERN,
     "query": "{name} founded year founders", "section": "meta"},
    {"key": "location", "label": "Location",
     "pattern": r"(\b\d{4,5}\s+[A-ZÄÖÜ][a-zäöüß]+|headquarter|based in|sitz in|mit sitz|standort|"
                r"located in|aus [A-ZÄÖÜ][a-zäöüß]+\b)",
     "query": "{name} startup founded founders location team", "section": "meta"},
    {"key": "team", "label": "Team size",
     "pattern": r"(\b\d+\s*(mitarbeit|employees|people|personen|köpfe|team members|fte)|"
                r"team (of|von|aus) \d+|\d+\s*[-–]\s*\d+\s*(employees|mitarbeiter))",
     "query": "{name} team size employees", "section": "meta"},
]


# The first search for every startup: the facts a record most often lacks.
FACTS_QUERY = "{name} startup founded founders location team"


def coverage(text: str) -> Dict[str, bool]:
    t = text or ""
    return {r["key"]: bool(re.search(r["pattern"], t, re.I)) for r in REQUIREMENTS}


def _label(key: str) -> str:
    return next(r["label"] for r in REQUIREMENTS if r["key"] == key)


@dataclass
class Research:
    name: str
    website: Optional[str] = None
    website_via: Optional[str] = None          # "database" | "web search" | "your input"
    site: Optional[sitereader.SiteRead] = None
    article_url: Optional[str] = None
    article: str = ""
    web: Optional[websearch.SearchReport] = None
    covered: Dict[str, bool] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    @property
    def missing(self) -> List[str]:
        return [k for k, ok in self.covered.items() if not ok]

    @property
    def summary(self) -> dict:
        return {"covered": [k for k, ok in self.covered.items() if ok],
                "missing": self.missing,
                "website_found_via": self.website_via,
                "searches": {"paid": self.web.paid if self.web else 0,
                             "cached": self.web.cached if self.web else 0,
                             "free": self.web.free if self.web else 0}}


def record_text(rec: dict) -> str:
    """The database record as a lead block for the model. Founders and tags
    are left out on purpose: they are the fields most often wrong in practice
    (a politician listed as Atira's founder; "Investment, Security" as
    REEcover's tags), and a one-pager doesn't print either."""
    lines = []
    for key, label in (("name", "Name"), ("short_description", "Short description"),
                       ("description", "Description"), ("industry", "Industry"),
                       ("business_model", "Business model"), ("city", "City"),
                       ("country", "Country"), ("address", "Address"),
                       ("founded_year", "Founded"), ("employee_count", "Employees"),
                       ("funding_stage", "Funding stage"), ("total_funding_usd", "Total funding (USD)")):
        v = rec.get(key)
        if v not in (None, "", []):
            lines.append(f"{label}: {v}")
    return "\n".join(lines)


def run(rec: dict, *, website: Optional[str] = None, website_via: str = "your input",
        allow_web: bool = True, allow_paid: bool = True, log=print) -> Research:
    """Gather the material for one startup. Never raises."""
    name = str(rec.get("name") or "").strip()
    res = Research(name=name)

    # 1. Website: yours > the record's (if it really is the company's own) > search.
    if website:
        res.website, res.website_via = website, website_via
    elif rec.get("website") and sitereader.is_official_domain(rec["website"], name):
        res.website, res.website_via = rec["website"], "database"

    # 2. The source article behind the record, name-filtered. A record whose
    # "website" is really an article about the company (Atira's) counts too.
    # Its links are the first (free) place to find a missing website.
    for u in (rec.get("source_url"), rec.get("website")):
        if u and u != res.website and str(u).startswith("http") and not res.article:
            text, links = sitereader.read_article(u, name)
            if text:
                res.article_url, res.article = u, text
            if not res.website:
                found = sitereader.discover_website(name, links)
                if found:
                    res.website, res.website_via = found, "a link in the source article"
                    log(f"✓ Website found in the source article: {found}")

    # 3. First search round: the company facts. Also how a missing website is
    # found — and if that round didn't turn it up, one search asking for it.
    if allow_web:
        res.web = websearch.search(name, [FACTS_QUERY.format(name=name)], res.website,
                                   allow_paid=allow_paid, max_paid=MAX_PAID_SEARCHES)
        if not res.website:
            hint = " ".join(str(rec.get(k) or "") for k in ("city", "industry")).strip()
            for q in ([] if sitereader.discover_website(name, websearch.all_result_urls(res.web))
                      else [f"{name} {hint} official website".replace("  ", " ")]):
                websearch.search(name, [q], None, allow_paid=allow_paid,
                                 max_paid=MAX_PAID_SEARCHES, rep=res.web)
            found = sitereader.discover_website(name, websearch.all_result_urls(res.web))
            if found:
                res.website, res.website_via = found, "web search"
                websearch.rebuild(res.web, name, found)
                log(f"✓ Website found via web search: {found}")

    # 4. The company's own site (free, the richest source).
    if res.website:
        res.site = sitereader.read_site(res.website)
        if res.site.ok:
            log(f"✓ Website read: {len(res.site.pages)} page(s), {len(res.site.text)} characters")
        else:
            res.notes.append(res.site.note or "The website could not be read.")
            log(f"! {res.site.note}")
    else:
        res.notes.append("No official website found (the record has none, and no search result's "
                         "domain matches the company name). Add it in 'Your input' and regenerate.")
        log("! No official website found")

    # 5. Search only for what is still missing, most important first.
    res.covered = coverage(material(rec, res))
    if allow_web and res.missing:
        queries = []
        for r in REQUIREMENTS:
            q = r["query"].format(name=name)
            if r["key"] in res.missing and q not in queries and q not in res.web.queries:
                queries.append(q)
        if queries:
            log(f"• Researching gaps: {', '.join(_label(k) for k in res.missing)}")
            websearch.search(name, queries, res.website, allow_paid=allow_paid,
                             max_paid=MAX_PAID_SEARCHES, rep=res.web)
        res.covered = coverage(material(rec, res))
    if res.web:
        res.notes.extend(res.web.notes)
    return res


def material(rec: dict, res: Research, *, with_web: bool = True) -> str:
    """Everything gathered, labelled by how far to trust it."""
    parts = [f"[HubDrive database record — collected automatically, may be incomplete or wrong]\n{record_text(rec)}"]
    if res.article:
        parts.append(f"[Source article: {res.article_url}]\n{res.article}")
    if res.site and res.site.ok:
        parts.append(f"[Company website: {res.website}]\n{res.site.text}")
    if with_web and res.web and res.web.text:
        parts.append(f"[Web search — third-party sources]\n{res.web.text}")
    return "\n\n".join(parts)


def missing_questions(res: Research) -> List[str]:
    """One open question per item still uncovered, saying where to add it."""
    out = []
    for key in res.missing:
        if key in ("founded", "location", "team"):
            continue            # generate.build_yaml asks for these, field by field
        out.append(f"Not found anywhere (database, website, web search): {_label(key)}. "
                   f"Add it in 'Your input' and regenerate, or the section stays thin.")
    return out
