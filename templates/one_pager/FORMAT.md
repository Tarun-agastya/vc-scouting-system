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

## 6. The generator

`generate.py` turns a pitch deck into a filled-in draft of everything above.

```bash
python3 templates/one_pager/generate.py --deck ~/Desktop/ONOX.pdf  --name ONOX
python3 templates/one_pager/generate.py --deck deck.pptx --name LIGARO --url https://ligaro.org
python3 templates/one_pager/generate.py --deck deck.pdf  --name X --no-llm   # no drafting
```

**Accepts `.pdf` and `.pptx`.** A legacy binary `.ppt` is rejected with an
instruction to re-save — there is no reliable pure-python reader for it.

What it does, in order:
1. Reads the deck slide by slide (`deck.py`), keeping slide numbers so every
   fact stays traceable.
2. Extracts **every** embedded image plus a full-page render per slide into
   `data/assets/<slug>/`, named `slide07_img2.jpg`.
3. Optionally pulls the company website (`--url`) for extra context.
4. Drafts the claim, the meta line and all five sections on the local 7B model
   (`llm.py`), constrained to the deck text.
5. Applies the grounding checks in §8.
6. Writes `data/<slug>.yaml` as `status: draft` with an `open_questions` list.

**It deliberately stops there.** It never picks the two images, never exports,
and never marks anything approved — same staged model as the Review Inbox, for
the same reason: a one-pager is outward-facing.

> A fresh draft **will** fail `render.py --check` if the model could not fill
> everything. That is correct, not a bug: validation gates *export*, not
> drafting.

### Two things it will not do, and why

- **It does not pick the images.** Choosing a good product shot is visual
  judgement. On LIGARO's own material the largest, most prominent assets were
  funder and partner logos (EXIST, Leibniz Universität, Future Greentech
  Incubator); the only real product shot was the poster frame of a site video.
  Anything automatic would have confidently chosen wrong.
- **It does not read the scouting database.** An earlier version of this
  section proposed pre-filling `name`/`city`/`founded_year` from it. That is now
  forbidden — see §7.

## 7. Isolation contract

The one-pager tooling is **standalone**. It imports nothing from
`processing/`, `ingestion/`, `api/`, `database/`, `vector_db/`, `reasoning/` or
`config/`, opens no database, and writes only inside `templates/one_pager/`.

This is deliberately stricter than `press_monitor/`, the repo's other isolated
subsystem, which imports three project symbols. The one-pager tooling has no
scheduled job and no shared credential, so it needs nothing from the pipeline
at all — and the owner's requirement was explicit: if this breaks, nothing else
may be affected.

| Decision | Reason |
|---|---|
| Own Ollama client (`llm.py`, raw `httpx` to `/api/chat`) rather than `reasoning/qwen_client.py` | Keeps the zero-import line, and sidesteps a real trap: the repo's `venv/` carries `ollama==0.2.1`, whose `chat()` has no `think` parameter and only accepts `format: Literal['','json']`. A raw POST has no such coupling. |
| Config from `os.environ`, not `config.settings` | `OLLAMA_BASE_URL` (default `http://localhost:11434`), `ONEPAGER_MODEL` (default `qwen2.5:7b-instruct`), `ONEPAGER_TIMEOUT` (75s). |
| No launchd job, no scheduler | Human-triggered only. Nothing fires unattended, so nothing fails unattended. |
| Enforced by `tests/test_one_pager_isolation.py` | An AST scan, not an import — it passes with Ollama, Postgres and Qdrant all down, which is when isolation matters most. |

**The one shared resource is the local Ollama server**, and the cost is honest:
a separate process does not share `QwenClient`'s single-worker semaphore, so
running the generator during an ingestion sweep makes both slower. It cannot
produce wrong data — only a slow run.

Failure model — only an unreadable deck is fatal:

| Condition | Behaviour |
|---|---|
| Deck missing / unreadable / legacy `.ppt` | Clear error, exit 1 |
| Ollama down, busy, or timing out | Sections left empty, reason in `open_questions`, YAML still written, exit 0 |
| `--url` unreachable | Warn, continue from the deck alone |
| No images extractable | Placeholders kept, flagged |
| Number in the draft not in the deck | Flagged in `open_questions`, never silently deleted |
| Target YAML exists | Refuses without `--force` |

## 8. Grounding — what the generator will and will not check

Follows the discipline in `reasoning/qwen_client.py::_ground_startup` (mirrored,
not imported): *gate only what is both fabrication-prone and checkable; never
gate paraphrase, because nulling a correct paraphrase is worse than leaving a
wrong one.*

- **The five sections are paraphrase → never auto-nulled.** Rewriting the deck's
  message in GT Hub's voice is the entire job.
- **Numbers inside them are checkable → verified.** Every figure in drafted prose
  must appear in the deck text, compared digits-only so `95.861` and `95861`
  don't read as different. A number with no counterpart is **flagged, not
  deleted** — `Mehrwerte` is *required* to carry a real figure, so a silent strip
  would leave a page that looks finished but isn't.
  *This is not theoretical:* on the first live run, from a deck stating
  `121.300 EUR` and `95.861 EUR`, the model wrote *"25.439 EUR gesenkt"* —
  arithmetic it did itself, correct but stated nowhere in the source.
- **Meta-line fields are checked the same way** and fall back to `k. A.` rather
  than a guess. This is exactly why Hula Earth's `Team` is `k. A.` today.
- Everything ships `status: draft`. Nothing is auto-approved or auto-exported.
