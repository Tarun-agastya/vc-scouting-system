# GT Hub Startup One-Pager — Format Specification

The canonical spec for the **Matchmaking-Startups** one-pager. Derived from the
blank GT Hub template plus two filled references — **ONOX** (S. 5) and
**Arctory** (S. 4) — cross-checked against each other on 14 Aug 2026.

> **Where the blank template and the filled pages disagree, the filled pages
> win.** The blank template's meta line reads
> `Ort: XXX / Gründungsjahr: XXXX / Teamgröße: XXX`, but both real one-pagers use
> the shorter `Ort: … / Gründung: … / Team: …`. That is the convention.

This exists to be executed, not just read: `schema.yaml` is the data contract,
`data/*.yaml` are real filled examples, and two exporters turn a data file into a
finished page. Adding a startup means writing one YAML file — nothing else.

## Two ways to change a one-pager

Pick whichever suits the change. They are not in conflict: YAML is the source of
truth for *generating* a page, PowerPoint is where a page gets *finished*.

**A — Edit the PowerPoint directly.** For "swap this screenshot", "fix this
sentence", "move that box". No terminal, no Python.

```bash
python3 templates/one_pager/export_pptx.py templates/one_pager/data/ligaro.yaml
```

Every text block is a real text box (click and type) and every image is a real
picture (right-click → **Change Picture**). The slide is 16:9, so it pastes
straight into the Matchmaking deck. Opens in PowerPoint, Keynote, LibreOffice and
Google Slides. The section text box has **shrink-on-overflow** enabled, so adding
a sentence scales the type down instead of spilling out of the card.

> Edits made in PowerPoint do **not** flow back into the YAML. If a change should
> survive a re-export — a corrected figure, a better claim — put it in the YAML
> too, or the next generated page will quietly reintroduce the old wording.

**B — Edit the YAML and re-generate.** For content changes you want to keep, and
the path automation will use.

```bash
python3 templates/one_pager/render.py    data/ligaro.yaml            # HTML (review / print-to-PDF)
python3 templates/one_pager/render.py    data/*.yaml --embed         # HTML, images inlined, one file
python3 templates/one_pager/export_pptx.py data/*.yaml --combine deck.pptx   # all pages, one deck
python3 templates/one_pager/render.py    data/*.yaml --check         # validate only, write nothing
```

**To swap an image**, drop the new file in `data/assets/<startup>/` and point the
`image:` key at it. Any format Pillow reads works; it is centre-cropped to fill
the frame. Set `placeholder:` instead of `image:` to go back to a labelled empty
box.

---

## 1. Layout

16:9 landscape (matches the deck it lives in). Three bands:

```
┌──────────────────────────────────────────────────────────────────┐
│ GT Hub                                    Matchmaking-Startups  N │  header rule
├──────────────────────────────────────────────────────────────────┤
│ 1-Satz Value Proposition / Claim                    (violet, XL)  │  claim
├────────────────────────────────┬─────────────────────────────────┤
│ Startup-Name          [ logo ] │ ┌─────────────────────────────┐ │
│ Ort / Gründung / Team (italic) │ │ Visualisierung der Lösung   │ │
│                                │ └─────────────────────────────┘ │
│ Lösung & Funktionalität        │                                 │
│ Mehrwerte & Leistungen         │ ┌─────────────────────────────┐ │
│ USP & Abgrenzung …             │ │ So funktioniert die Lösung  │ │
│ Zielgruppe & Kunden            │ │                             │ │
│ Geschäftsmodell                │ └─────────────────────────────┘ │
└────────────────────────────────┴─────────────────────────────────┘
      left card ~48%                    right column ~48%
```

- **Left** — one white card, thin border, holding name + meta + the five sections.
- **Right** — two stacked image boxes, roughly 40/60 height split.
- Everything is in **German**. The audience is German-speaking matchmaking partners.

## 2. The five sections — fixed, in this order

Never reorder, never rename, never add a sixth. The order is the reading argument:
*what it is → what it's worth → why not a competitor → who buys it → how it earns.*

| # | Heading (verbatim) | Answers | Target length |
|---|---|---|---|
| 1 | `Lösung & Funktionalität` | What the product is and how it works | 2–3 sentences |
| 2 | `Mehrwerte & Leistungen` | KPIs / performance — **must carry numbers** | 1–2 sentences |
| 3 | `USP & Abgrenzung vom Wettbewerb` | Why not the incumbent or the other startup | 2–3 sentences |
| 4 | `Zielgruppe & Kunden` | Who buys, **plus traction** | 2–3 sentences |
| 5 | `Geschäftsmodell` | How money is made | 1–2 sentences |

Headings render **bold + underlined**. In the blank template each carries an italic
`→ Erklärung der …` hint; those hints are authoring guidance and are **dropped**
once the section is filled (see the ONOX reference).

## 3. Writing rules

Both reference pages obey all six. Follow them or the pages stop looking like a set.

1. **Claim is a noun phrase, not a sentence.** Product category + the one
   differentiator. No verb, no period, no adjectives like *innovativ*.
   ✅ `Elektrischer Traktor mit Wechselbatterien` (ONOX)
   ✅ `KI-basiertes Shopfloor-Management` (Arctory)
   ❌ `ONOX revolutioniert die Landwirtschaft mit innovativer Technologie`
2. **Numbers live in section 2 and section 4.** A `Mehrwerte` block with no figure
   has failed. ONOX: `11t/a CO2`, `8-Jahres-Vergleich`. Arctory:
   `~2 Applikationen pro Monat pro Fabrik`. Traction is a number or a named
   customer: ONOX `3 Kaufanfragen, über 20 Probefahrten`; Arctory names
   `Venti Oelde` and `ams OSRAM`.
3. **Section 3 names the alternative.** "Nicht nur X, sondern Y" is the house
   construction — Arctory closes with the parenthetical
   `(Kein No-Code-Baukasten, sondern Tailored Apps)`, ONOX with
   `Nicht nur E-Traktor, sondern integriertes Energie- und Fahrzeugkonzept`.
   Naming real competitors and real incumbent workarounds (`Excel-Workarounds`)
   is expected, not rude.
4. **No marketing voice.** Plain declarative German. Delete *führend*,
   *einzigartig* (unless literally proven by a comparison table), *revolutionär*.
   Trailing periods on the body paragraphs are optional — both references drop
   them on some sections; be consistent within a page.
5. **Every claim must be traceable** to the deck or a cited source. This is the
   same rule the ingestion pipeline runs on: evidence, not verdicts. If a fact
   isn't in the source, it does not go on the page — use `k. A.` and flag it.
6. **Meta line is exact:** `Ort: … / Gründung: … / Team: …`. Unknown → `k. A.`,
   never a guess. If the founding year is ambiguous (research start vs. GmbH),
   say which one.

## 4. Image slots

Two, both pulled from the startup's own deck — never stock photography.

| Slot | Purpose | Good source slide | In the references |
|---|---|---|---|
| `visual_solution` (top) | What it looks like / system overview | Solution or hero slide | ONOX: barn-with-PV solution diagram · Arctory: platform-architecture diagram (ERP layer → platform → factory) |
| `visual_how_it_works` (bottom) | How it works / range / variants | Product-breakdown or UI slide | ONOX: three-product breakdown (Traktor / Batteriemodule / Energiemanagement) · Arctory: real product UI screenshot (dashboard + mobile) |

Pattern worth copying: the top box shows **the system in context**, the bottom box
shows **the actual thing you get** — either the product range or a real screenshot.
A screenshot of the working software is the strongest possible bottom box.

In YAML each slot takes either an `image:` path or, when the asset isn't in hand
yet, a `placeholder:` string naming the exact slide to drop in. The renderer draws
a labelled dashed box for placeholders so an unfinished page is obviously
unfinished rather than quietly wrong.

## 5. Colors & type

| Token | Value | Use |
|---|---|---|
| Accent violet | `#6C5CE7` | Claim headline, image-box fill, logo plate |
| Ink | `#111111` | Body text, headings |
| Page ground | `#F2F2F2` | Slide background |
| Card | `#FFFFFF` | Left card, image frames |
| Hairline | `#111111` @ 1px | Header rule, card border |

Type: one sans family throughout (Arial/Helvetica in PowerPoint; the renderer uses
a system stack). Claim ~34px, startup name ~19px bold, meta ~12px italic,
section heading ~12.5px bold underlined, body ~12px.

## 6. Automation notes

Where this is heading — a startup in the scouting DB becomes a one-pager without
a human writing prose.

- **Data contract** is `schema.yaml`. Renderer validates against it and fails loudly
  on a missing required field rather than emitting a half page.
- **Drafting** should run on the local extraction model, one section per call,
  each constrained to text present in the source (deck text, `source_excerpt`,
  or a cited page). Same grounding gate as `reasoning/qwen_client._ground_startup`
  — an unsupported number gets nulled, not printed.
- **Never auto-publish.** A one-pager is outward-facing. Generate → human approves
  → export. Treat it exactly like the Review Inbox: staged, never silently applied.
- **Fields the DB already has** (`name`, `city`, `founded_year`, `website`,
  `short_description`, `industry`) map straight onto the meta line and give
  section 1 its first draft. `employee_count` fills `Team`.
- **Fields the DB does not have** and that a deck or interview must supply:
  traction specifics, the competitor comparison, the business model, and both
  images. These are the human-in-the-loop parts; don't let a model invent them.
