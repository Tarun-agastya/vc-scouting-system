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
    python3 templates/one_pager/render.py templates/one_pager/data/ligaro.de.yaml
    python3 templates/one_pager/render.py templates/one_pager/data/*.yaml --out-dir /tmp/onepagers
    python3 templates/one_pager/render.py data/hula_earth.de.yaml --check   # validate only
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
import brand  # noqa: E402
import i18n  # noqa: E402

# The five sections, in their fixed order. Order is the reading argument: what
# it is -> what it's worth -> why not a competitor -> who buys it -> how it
# earns. Never reorder or extend. The headings come from i18n by the file's
# `lang:` — the page is either all English or all German, never mixed.

REQUIRED_TOP = ["claim", "name", "location", "founded", "team_size"]


def validate(data: dict, path: Path) -> list:
    """Return a list of problems. Empty list means the file is renderable."""
    problems = []
    lang = str(data.get("lang") or i18n.LEGACY_LANG).lower()
    if lang not in i18n.LANGS:
        problems.append(f"lang is {lang!r}, must be one of {', '.join(i18n.LANGS)}")
        lang = i18n.LEGACY_LANG

    for field in REQUIRED_TOP:
        if not str(data.get(field) or "").strip():
            problems.append(f"missing required field: {field}")

    claim = str(data.get("claim") or "")
    if claim.endswith("."):
        problems.append("claim ends with a period — it must be a noun phrase, not a sentence")
    if len(claim) > 70:
        problems.append(f"claim is {len(claim)} chars, spec says <= 70 (it must fit one line)")

    sections = data.get("sections") or {}
    for key, heading in i18n.sections(lang):
        if not str(sections.get(key) or "").strip():
            problems.append(f"missing required section: {key} ({heading})")

    # Section 2 without a digit has failed its job — see FORMAT.md rule 2.
    mehrwerte = str(sections.get("mehrwerte") or "")
    if mehrwerte and not any(c.isdigit() for c in mehrwerte):
        problems.append(f"'{i18n.labels(lang)['sections']['mehrwerte']}' contains no number — spec requires a concrete figure")

    visuals = data.get("visuals") or {}
    for key, _label in i18n.visuals(lang):
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


def _font_src(path: Path, base_dir: Path, embed: bool) -> str:
    if not embed:
        return os.path.relpath(path, base_dir)
    mime = "font/woff2" if path.suffix == ".woff2" else "font/otf" if path.suffix == ".otf" else "font/ttf"
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def _font_faces(base_dir: Path, embed: bool) -> str:
    """Work Sans (bundled, OFL) always; PP Neue Machina only if a licensed file
    has been dropped in (see brand.py). A missing file just falls back to the
    next font in the stack — never an error."""
    fmt = {".ttf": "truetype", ".otf": "opentype", ".woff2": "woff2"}
    faces = []
    for weight, path in brand.WORK_SANS.items():
        if path.exists():
            faces.append(
                f"@font-face {{ font-family: 'Work Sans'; font-weight: {weight}; font-style: normal; "
                f"src: url('{_font_src(path, base_dir, embed)}') format('{fmt[path.suffix]}'); }}")
    if brand.HEADLINE_IS_LICENSED:
        p = brand.HEADLINE_FONT_FILE
        faces.append(
            f"@font-face {{ font-family: 'PP Neue Machina'; font-weight: 400; "
            f"src: url('{_font_src(p, base_dir, embed)}') format('{fmt.get(p.suffix, 'woff2')}'); }}")
    return "\n  ".join(faces)


def _r(short_side: float) -> str:
    """Corner radius per the style guide: short side / 24 (brand.corner_radius)."""
    return f"{brand.corner_radius(short_side):.1f}px"


def _visual_box(slot: dict, default_label: str, base_dir: Path, height: int, embed: bool) -> str:
    label = html.escape(str(slot.get("label") or default_label))
    radius = _r(min(height, brand.RIGHT_W))
    image = slot.get("image")
    if image:
        # Style guide p.8 "Bildcontainer": the rounded shape is the image's mask.
        src = _img_src(image, base_dir, embed)
        return (
            f'<figure class="vbox vbox--img" style="height:{height}px;border-radius:{radius}">'
            f'<img src="{html.escape(src)}" alt="{label}">'
            f"</figure>"
        )
    placeholder = html.escape(str(slot.get("placeholder") or ""))
    return (
        f'<figure class="vbox vbox--ph" style="height:{height}px;border-radius:{radius}">'
        f'<span class="vbox__label">{label}</span>'
        f'<span class="vbox__hint">{placeholder}</span>'
        f"</figure>"
    )


def _initials(name: str) -> str:
    words = [w for w in str(name).replace("-", " ").split() if w]
    return "".join(w[0] for w in words[:2]).upper() or "?"


def render(data: dict, base_dir: Path, *, embed: bool = False, draft_mark: bool = False) -> str:
    """
    One GT Hub one-pager as a self-contained 16:9 HTML page, following
    241023_GTHub_Styleguide.pdf (all tokens and geometry from brand.py):
      * the real GT Hub lockup (logo + name), top right;
      * Work Sans throughout; the claim in the headline face;
      * a lime identity block with the startup's own logo, name, location /
        founding year / team, and its website as a link;
      * images in rounded containers, placeholders as purple blocks;
      * every corner radius = that element's short side / 24.
    Only print-safe colour pairs from the guide's p.6 are used: black on
    white/lime/grey, white on black/purple. (Lime-on-black and purple-on-white
    are "digital only" there, and a one-pager gets printed.)
    """
    lang = i18n.lang_of(data)
    L = i18n.labels(lang)
    meta = data.get("meta") or {}
    page_label = html.escape(str(meta.get("page_label") or L["page_label"]))
    page_number = html.escape(str(meta.get("page_number") or ""))

    name = html.escape(str(data.get("name") or ""))
    claim = html.escape(str(data.get("claim") or ""))
    metaline = " / ".join([
        f"{L['location']}: {html.escape(str(data.get('location') or L['unknown']))}",
        f"{L['founded']}: {html.escape(str(data.get('founded') or L['unknown']))}",
        f"{L['team']}: {html.escape(str(data.get('team_size') or L['unknown']))}",
    ])

    website = data.get("website")
    website_html = (
        f'<a class="web" href="{html.escape(brand.href(website))}">'
        f'{html.escape(brand.display_url(website))}</a>' if website else ""
    )

    logo = data.get("logo")
    if logo:
        tile_w = brand.logo_tile_width(base_dir / str(logo))
        logo_html = (f'<div class="id__logo" style="flex-basis:{tile_w}px">'
                     f'<img src="{html.escape(_img_src(logo, base_dir, embed))}" alt="{name}"></div>')
    else:
        logo_html = f'<div class="id__logo id__logo--text">{html.escape(_initials(data.get("name") or ""))}</div>'

    sections = data.get("sections") or {}
    section_html = "".join(
        f'<section class="sec"><h3>{html.escape(heading)}</h3>'
        f'<p>{_para(sections.get(key) or "")}</p></section>'
        for key, heading in i18n.sections(lang)
    )

    visuals = data.get("visuals") or {}
    visual_html = "".join(
        _visual_box(visuals.get(key) or {}, label, base_dir, h, embed)
        for (key, label), h in zip(i18n.visuals(lang), (brand.VIS_TOP_H, brand.VIS_BOTTOM_H))
    )

    # Opt-in only. The watermark is our own bookkeeping, not part of the GT Hub
    # format, so it must never appear on a page destined for the real deck. The
    # audit trail lives in the YAML (`review.status` + `open_questions`).
    status = ((data.get("review") or {}).get("status") or "").lower()
    draft_ribbon = (
        '<div class="draft">' + html.escape(L["draft"]) + '</div>'
        if (draft_mark and status != "approved") else ""
    )

    headline = ("'PP Neue Machina', " if brand.HEADLINE_IS_LICENSED else "") + "'Work Sans', Arial, sans-serif"
    # Without the licensed headline face, Work Sans SemiBold with tighter
    # tracking is the closest stand-in for Neue Machina's squared grotesk.
    headline_weight = 400 if brand.HEADLINE_IS_LICENSED else 600

    B = brand
    return f"""<!doctype html>
<html lang="{lang}"><head><meta charset="utf-8">
<title>{name} — GT Hub One-Pager</title>
<style>
  {_font_faces(base_dir, embed)}
  @page {{ size: 338.7mm 190.5mm; margin: 0; }}
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; padding: 0; background: #E6E6E6; }}
  body {{ font-family: 'Work Sans', Arial, Helvetica, sans-serif; color: {B.BLACK}; }}
  .slide {{
    position: relative; width: {B.PAGE_W}px; height: {B.PAGE_H}px; margin: 24px auto;
    background: {B.WHITE}; padding: {B.PAD_TOP}px {B.PAD_X}px {B.PAD_BOTTOM}px;
    overflow: hidden;
  }}
  @media print {{ html, body {{ background: {B.WHITE}; }} .slide {{ margin: 0; }} }}

  .top {{ display: flex; justify-content: space-between; align-items: flex-start;
          gap: 28px; height: {B.TOP_H}px; }}
  .label {{ font-size: 12px; font-weight: 600; letter-spacing: .02em; display: flex; gap: 18px; }}
  .claim {{ font-family: {headline}; font-weight: {headline_weight}; font-size: 30px;
            line-height: 1.1; letter-spacing: -.02em; margin: 8px 0 0; text-wrap: balance; }}
  .gtlogo {{ flex: 0 0 auto; height: {B.GT_LOGO_H}px; }}
  .gtlogo svg {{ height: {B.GT_LOGO_H}px; width: auto; display: block; }}

  .cols {{ position: absolute; left: {B.PAD_X}px; top: {B.COLS_TOP}px; right: {B.PAD_X}px;
           height: {B.COLS_H}px; display: flex; gap: {B.COL_GAP}px; }}
  .left {{ flex: 0 0 {B.LEFT_W}px; display: flex; flex-direction: column; min-width: 0; }}

  .id {{ background: {B.LIME}; border-radius: {_r(B.ID_H)}; height: {B.ID_H}px;
         padding: 12px 18px 12px 12px; display: flex; gap: 16px; align-items: center; }}
  .id__logo {{ flex: 0 0 {B.LOGO_TILE}px; height: {B.LOGO_TILE}px; background: {B.WHITE};
               border-radius: {_r(B.LOGO_TILE)}; display: flex; align-items: center;
               justify-content: center; padding: 8px; overflow: hidden; }}
  .id__logo img {{ max-width: 100%; max-height: 100%; object-fit: contain; display: block; }}
  .id__logo--text {{ background: {B.BLACK}; color: {B.WHITE}; font-weight: 600; font-size: 24px; }}
  .id__text {{ min-width: 0; }}
  .id h2 {{ font-size: 21px; font-weight: 600; margin: 0 0 3px; line-height: 1.15; }}
  .meta {{ font-size: 12px; margin: 0; }}
  .web {{ font-size: 12px; color: {B.BLACK}; text-decoration: underline;
          text-underline-offset: 2px; display: inline-block; margin-top: 4px; }}

  .sec {{ margin-top: 11px; }}
  .sec h3 {{ font-size: 12.5px; font-weight: 600; margin: 0 0 2px; }}
  .sec p {{ font-size: 11.6px; line-height: 1.42; margin: 0; }}

  .vis {{ flex: 1; display: flex; flex-direction: column; gap: {B.VIS_GAP}px; min-width: 0; }}
  .vbox {{ margin: 0; overflow: hidden; flex: 0 0 auto; display: flex;
           align-items: center; justify-content: center; }}
  .vbox--img img {{ width: 100%; height: 100%; object-fit: cover; display: block; }}
  .vbox--ph {{ flex-direction: column; gap: 8px; text-align: center;
               background: {B.PURPLE}; color: {B.WHITE}; padding: 18px 26px; }}
  .vbox__label {{ font-size: 15px; font-weight: 600; }}
  .vbox__hint {{ font-size: 11px; max-width: 82%; line-height: 1.4; }}

  .draft {{ position: absolute; top: 0; left: 50%; transform: translateX(-50%);
            background: {B.BLACK}; color: {B.WHITE}; font-size: 10.5px; font-weight: 600;
            letter-spacing: .12em; padding: 4px 20px; border-radius: 0 0 4px 4px; }}
</style></head>
<body>
<div class="slide">
  {draft_ribbon}
  <div class="top">
    <div>
      <div class="label"><span>{page_label}</span><span>{page_number}</span></div>
      <h1 class="claim">{claim}</h1>
    </div>
    <div class="gtlogo">{brand.logo_svg_markup()}</div>
  </div>
  <div class="cols">
    <div class="left">
      <div class="id">
        {logo_html}
        <div class="id__text"><h2>{name}</h2><p class="meta">{metaline}</p>{website_html}</div>
      </div>
      <div class="secs">{section_html}</div>
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
    ap.add_argument("--allow-incomplete", action="store_true",
                    help="render even if validation fails (for previewing a draft); "
                         "implies --draft-mark. Export (export_pptx.py) still validates.")
    ap.add_argument("--draft-mark", action="store_true",
                    help="stamp a DRAFT watermark on pages whose review.status is not 'approved'")
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
        if problems and not (args.allow_incomplete and not args.check):
            print(f"  ✗ {path.name}: {len(problems)} problem(s)")
            for p in problems:
                print(f"      - {p}")
            exit_code = 1
            continue
        if problems:
            print(f"  ! {path.name}: rendering anyway (--allow-incomplete), {len(problems)} problem(s):")
            for p in problems:
                print(f"      - {p}")

        status = ((data.get("review") or {}).get("status") or "draft").lower()
        open_q = (data.get("review") or {}).get("open_questions") or []

        if args.check:
            print(f"  ✓ {path.name}: valid  [{status}]" + (f", {len(open_q)} open question(s)" if open_q else ""))
            continue

        out_dir = Path(args.out_dir).resolve() if args.out_dir else path.parent
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"{path.stem}_onepager.html"
        try:
            page = render(data, path.parent, embed=args.embed,
                          draft_mark=args.draft_mark or args.allow_incomplete)
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
