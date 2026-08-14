"""
Render a GT Hub startup one-pager from a YAML data file.

The format spec lives in templates/one_pager/FORMAT.md and the data contract in
templates/one_pager/schema.yaml. This script is the executable half: data in,
finished 16:9 page out. Adding a startup means writing one YAML file.

Output is a self-contained HTML file sized to a 16:9 slide. Open it and use the
browser's "Print -> Save as PDF" (margins: none, background graphics: on) to get
a PDF, or screenshot it to drop into the deck.

Validation is deliberately strict: a missing required field aborts with a clear
message rather than emitting a half-filled page. A one-pager is outward-facing —
a silently incomplete one is worse than none, the same reasoning behind the
pipeline's staged-review model.

Usage:
    python3 templates/one_pager/render.py templates/one_pager/data/ligaro.yaml
    python3 templates/one_pager/render.py templates/one_pager/data/*.yaml --out-dir /tmp/onepagers
    python3 templates/one_pager/render.py data/hula_earth.yaml --check   # validate only
"""
from __future__ import annotations

import argparse
import base64
import html
import mimetypes
import os
import sys
from pathlib import Path

import yaml

# The five sections, in their fixed order, with their verbatim headings.
# Order is the reading argument: what it is -> what it's worth -> why not a
# competitor -> who buys it -> how it earns. Never reorder or extend.
SECTIONS = [
    ("loesung", "Lösung & Funktionalität"),
    ("mehrwerte", "Mehrwerte & Leistungen"),
    ("usp", "USP & Abgrenzung vom Wettbewerb"),
    ("zielgruppe", "Zielgruppe & Kunden"),
    ("geschaeftsmodell", "Geschäftsmodell"),
]

VISUALS = [
    ("visual_solution", "Visualisierung der Lösung"),
    ("visual_how_it_works", "So funktioniert die Lösung"),
]

REQUIRED_TOP = ["claim", "name", "location", "founded", "team_size"]

ACCENT = "#6C5CE7"


def validate(data: dict, path: Path) -> list:
    """Return a list of problems. Empty list means the file is renderable."""
    problems = []

    for field in REQUIRED_TOP:
        if not str(data.get(field) or "").strip():
            problems.append(f"missing required field: {field}")

    claim = str(data.get("claim") or "")
    if claim.endswith("."):
        problems.append("claim ends with a period — it must be a noun phrase, not a sentence")
    if len(claim) > 70:
        problems.append(f"claim is {len(claim)} chars, spec says <= 70 (it must fit one line)")

    sections = data.get("sections") or {}
    for key, heading in SECTIONS:
        if not str(sections.get(key) or "").strip():
            problems.append(f"missing required section: {key} ({heading})")

    # Section 2 without a digit has failed its job — see FORMAT.md rule 2.
    mehrwerte = str(sections.get("mehrwerte") or "")
    if mehrwerte and not any(c.isdigit() for c in mehrwerte):
        problems.append("'Mehrwerte & Leistungen' contains no number — spec requires a concrete figure")

    visuals = data.get("visuals") or {}
    for key, _label in VISUALS:
        slot = visuals.get(key) or {}
        if not isinstance(slot, dict) or not (slot.get("image") or slot.get("placeholder")):
            problems.append(f"visual slot '{key}' needs either an `image:` or a `placeholder:`")

    return problems


def _para(text: str) -> str:
    """YAML folded blocks arrive with hard newlines; collapse to flowing text."""
    return html.escape(" ".join(str(text).split()))


def _img_src(image: str, base_dir: Path, embed: bool) -> str:
    """Resolve an image reference to something a browser can load.

    With embed=True the bytes are inlined as a data: URI, which is what makes a
    rendered page a single self-contained file — survives being emailed, moved,
    or published somewhere that blocks external requests. Remote URLs are passed
    through untouched.
    """
    raw = str(image)
    if raw.startswith(("http://", "https://", "data:")):
        return raw
    resolved = (base_dir / raw).resolve()
    if not embed:
        return os.path.relpath(resolved, base_dir)
    if not resolved.exists():
        raise FileNotFoundError(f"image not found: {resolved}")
    mime = mimetypes.guess_type(resolved.name)[0] or "image/jpeg"
    return f"data:{mime};base64," + base64.b64encode(resolved.read_bytes()).decode("ascii")


def _visual_box(slot: dict, default_label: str, base_dir: Path, grow: int, embed: bool) -> str:
    label = html.escape(str(slot.get("label") or default_label))
    image = slot.get("image")
    if image:
        src = _img_src(image, base_dir, embed)
        return (
            f'<figure class="vbox vbox--img" style="flex:{grow}">'
            f'<img src="{html.escape(src)}" alt="{label}">'
            f"</figure>"
        )
    placeholder = html.escape(str(slot.get("placeholder") or ""))
    return (
        f'<figure class="vbox vbox--ph" style="flex:{grow}">'
        f'<span class="vbox__label">{label}</span>'
        f'<span class="vbox__hint">{placeholder}</span>'
        f"</figure>"
    )


def render(data: dict, base_dir: Path, *, embed: bool = False, draft_mark: bool = False) -> str:
    meta = data.get("meta") or {}
    page_label = html.escape(str(meta.get("page_label") or "Matchmaking-Startups"))
    page_number = html.escape(str(meta.get("page_number") or ""))

    name = html.escape(str(data["name"]))
    claim = html.escape(str(data["claim"]))
    metaline = " / ".join([
        f"Ort: {html.escape(str(data['location']))}",
        f"Gründung: {html.escape(str(data['founded']))}",
        f"Team: {html.escape(str(data['team_size']))}",
    ])

    logo = data.get("logo")
    if logo:
        logo_html = f'<div class="logo"><img src="{html.escape(_img_src(logo, base_dir, embed))}" alt="{name}"></div>'
    else:
        logo_html = f'<div class="logo logo--text">{name}</div>'

    sections = data["sections"]
    section_html = "".join(
        f'<section class="sec"><h3>{html.escape(heading)}</h3>'
        f'<p>{_para(sections[key])}</p></section>'
        for key, heading in SECTIONS
    )

    visuals = data["visuals"]
    # 40/60 split: the "how it works" box usually carries a denser graphic.
    visual_html = "".join(
        _visual_box(visuals.get(key) or {}, label, base_dir, grow, embed)
        for (key, label), grow in zip(VISUALS, (40, 60))
    )

    # Opt-in only. The watermark is our own bookkeeping, not part of the GT Hub
    # format, so it must never appear on a page destined for the real deck. The
    # audit trail lives in the YAML (`review.status` + `open_questions`), which
    # is where it belongs — a stamp on the artwork is not the same as a record.
    status = ((data.get("review") or {}).get("status") or "").lower()
    draft_ribbon = (
        '<div class="draft">ENTWURF — nicht freigegeben</div>'
        if (draft_mark and status != "approved") else ""
    )

    return f"""<!doctype html>
<html lang="de"><head><meta charset="utf-8">
<title>{name} — GT Hub One-Pager</title>
<style>
  @page {{ size: 338.7mm 190.5mm; margin: 0; }}
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; padding: 0; background: #999; }}
  body {{ font-family: Arial, Helvetica, "Helvetica Neue", system-ui, sans-serif; }}
  .slide {{
    position: relative; width: 1280px; height: 720px; margin: 24px auto;
    background: #F2F2F2; color: #111; padding: 26px 34px 30px;
    display: flex; flex-direction: column; overflow: hidden;
  }}
  @media print {{ body {{ background: #fff; }} .slide {{ margin: 0; }} }}

  .hdr {{ display: flex; justify-content: space-between; align-items: baseline;
          font-size: 11px; letter-spacing: .01em; }}
  .hdr__r {{ display: flex; gap: 26px; }}
  .rule {{ border-bottom: 1px solid #111; margin: 5px 0 16px; }}

  .claim {{ font-size: 34px; line-height: 1.12; font-weight: 400;
            color: {ACCENT}; margin: 0 0 16px; letter-spacing: -.01em; }}

  .cols {{ display: flex; gap: 20px; flex: 1; min-height: 0; }}
  .card {{ flex: 0 0 48.5%; background: #fff; border: 1px solid #111;
           padding: 15px 17px; display: flex; flex-direction: column; overflow: hidden; }}
  .idhead {{ display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }}
  .idhead h2 {{ font-size: 19px; margin: 0 0 2px; font-weight: 700; }}
  .metaline {{ font-size: 11.5px; font-style: italic; margin: 0; }}
  .logo {{ flex: 0 0 auto; height: 46px; min-width: 104px; display: flex;
           align-items: center; justify-content: center; overflow: hidden; }}
  .logo img {{ max-height: 46px; max-width: 150px; object-fit: contain; }}
  .logo--text {{ background: {ACCENT}; color: #fff; font-weight: 700;
                 font-size: 13px; padding: 0 12px; letter-spacing: .04em; }}

  .sec {{ margin-top: 11px; }}
  .sec h3 {{ font-size: 12.5px; margin: 0 0 2px; font-weight: 700;
             text-decoration: underline; text-underline-offset: 2px; }}
  .sec p {{ font-size: 11.8px; line-height: 1.42; margin: 0; }}

  .vis {{ flex: 1; display: flex; flex-direction: column; gap: 14px; min-width: 0; }}
  .vbox {{ margin: 0; border: 1px solid #111; background: #fff;
           display: flex; align-items: center; justify-content: center;
           overflow: hidden; min-height: 0; }}
  .vbox--img img {{ width: 100%; height: 100%; object-fit: contain; }}
  .vbox--ph {{ flex-direction: column; gap: 8px; text-align: center;
               background: {ACCENT}; color: #fff; padding: 18px 26px;
               border: 1px dashed rgba(255,255,255,.75); }}
  .vbox__label {{ font-size: 15px; font-weight: 500; }}
  .vbox__hint {{ font-size: 11px; opacity: .92; max-width: 82%; line-height: 1.4; }}

  .draft {{ position: absolute; top: 20px; left: 50%; transform: translateX(-50%);
            background: #E8E24A; color: #111; font-size: 11px; font-weight: 700;
            letter-spacing: .1em; padding: 3px 22px; }}
</style></head>
<body>
<div class="slide">
  {draft_ribbon}
  <div class="hdr"><span>GT Hub</span><span class="hdr__r"><span>{page_label}</span><span>{page_number}</span></span></div>
  <div class="rule"></div>
  <h1 class="claim">{claim}</h1>
  <div class="cols">
    <div class="card">
      <div class="idhead">
        <div><h2>{name}</h2><p class="metaline">{metaline}</p></div>
        {logo_html}
      </div>
      {section_html}
    </div>
    <div class="vis">{visual_html}</div>
  </div>
</div>
</body></html>"""


def main() -> int:
    ap = argparse.ArgumentParser(description="Render a GT Hub startup one-pager from YAML.")
    ap.add_argument("data", nargs="+", help="one or more YAML data files")
    ap.add_argument("--out-dir", default=None, help="where to write HTML (default: alongside the YAML)")
    ap.add_argument("--check", action="store_true", help="validate only, write nothing")
    ap.add_argument("--embed", action="store_true",
                    help="inline images as data: URIs so the HTML is a single self-contained file")
    ap.add_argument("--draft-mark", action="store_true",
                    help="stamp an ENTWURF watermark on pages whose review.status is not 'approved'")
    args = ap.parse_args()

    exit_code = 0
    for raw in args.data:
        path = Path(raw).resolve()
        if not path.exists():
            print(f"  ✗ {raw}: file not found")
            exit_code = 1
            continue

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        problems = validate(data, path)
        if problems:
            print(f"  ✗ {path.name}: {len(problems)} problem(s)")
            for p in problems:
                print(f"      - {p}")
            exit_code = 1
            continue

        status = ((data.get("review") or {}).get("status") or "draft").lower()
        open_q = (data.get("review") or {}).get("open_questions") or []

        if args.check:
            print(f"  ✓ {path.name}: valid  [{status}]" + (f", {len(open_q)} open question(s)" if open_q else ""))
            continue

        out_dir = Path(args.out_dir).resolve() if args.out_dir else path.parent
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"{path.stem}_onepager.html"
        try:
            page = render(data, path.parent, embed=args.embed, draft_mark=args.draft_mark)
        except FileNotFoundError as exc:
            print(f"  ✗ {path.name}: {exc}")
            exit_code = 1
            continue
        out.write_text(page, encoding="utf-8")

        print(f"  ✓ {path.name} -> {out}  [{status}]")
        if open_q:
            print(f"      {len(open_q)} open question(s) still need a human:")
            for q in open_q:
                print(f"      - {' '.join(str(q).split())[:150]}")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
