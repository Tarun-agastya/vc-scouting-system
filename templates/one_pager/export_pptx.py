"""
Export a GT Hub one-pager to an EDITABLE PowerPoint slide (.pptx).

Same YAML source as render.py — this is just the other output format. Use it when
the page needs to be changed by hand or dropped into the Matchmaking deck:

  * every text block is a real text box  -> click and type
  * every image is a real picture        -> right-click > Change Picture
  * the slide is 16:9                    -> paste straight into the deck

Nothing here is a flattened image. Opens in PowerPoint, Keynote, LibreOffice and
Google Slides.

Usage:
    python3 templates/one_pager/export_pptx.py templates/one_pager/data/ligaro.de.yaml
    python3 templates/one_pager/export_pptx.py templates/one_pager/data/*.yaml --out-dir ~/Desktop
    python3 templates/one_pager/export_pptx.py data/*.yaml --combine deck.pptx   # all pages, one file

Requires: pip3 install python-pptx pillow
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, MSO_AUTO_SIZE, PP_ALIGN
from pptx.util import Emu, Inches, Pt

sys.path.insert(0, str(Path(__file__).resolve().parent))
import brand  # noqa: E402
import i18n  # noqa: E402
from render import validate  # noqa: E402  — single source of truth

# Geometry comes from brand.py in CSS pixels (the HTML page is 1280 x 720);
# a 13.333 x 7.5 in slide is exactly that at 96 px/in, so px / 96 = inches and
# this slide is the HTML preview's layout by construction.
SLIDE_W, SLIDE_H = brand.PAGE_W / 96, brand.PAGE_H / 96


def _in(px: float):
    return Inches(px / 96)


def _rgb(hex_color: str) -> RGBColor:
    return RGBColor.from_string(hex_color.lstrip("#").upper())


LIME, BLACK, PURPLE, WHITE = (_rgb(c) for c in (brand.LIME, brand.BLACK, brand.PURPLE, brand.WHITE))
# Fonts are referenced by name, not embedded (python-pptx can't embed fonts).
# Work Sans is free: install it from templates/one_pager/assets/fonts/ on any
# machine that edits these files, or PowerPoint substitutes a similar sans.
BODY_FONT = brand.BODY_FONT_NAME
HEADLINE_FONT = brand.HEADLINE_FONT_NAME


def _txbox(slide, x, y, w, h):
    """Position and size in CSS px (see brand.py)."""
    tb = slide.shapes.add_textbox(_in(x), _in(y), _in(w), _in(h))
    tf = tb.text_frame
    tf.word_wrap = True
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    return tb, tf


def _run(p, text, *, size, bold=False, underline=False, color=BLACK, font=None):
    r = p.add_run()
    r.text = text
    r.font.size = Pt(size)
    r.font.bold = bold
    r.font.underline = underline
    r.font.color.rgb = color
    r.font.name = font or BODY_FONT
    # Tell the viewer this is a SANS-SERIF face (pitchFamily 34 = variable
    # pitch, "swiss" family). Without the hint, a machine lacking Work Sans
    # substitutes Times — measured with macOS Quick Look; with it, Helvetica/Arial.
    from pptx.oxml.ns import qn
    latin = r.font._rPr.find(qn("a:latin"))
    if latin is not None:
        latin.set("pitchFamily", "34")
        latin.set("charset", "0")
    return r


def _set_corner(shape_or_pic, w, h) -> None:
    """Corner radius = short side / 24 (style guide p.9). In DrawingML the
    rounded-rectangle 'adj' is the radius as a fraction of the short side, in
    1/100000ths, so it is the same 1/24 for every size."""
    from pptx.oxml.ns import qn

    sp_pr = shape_or_pic._element.spPr
    geom = sp_pr.find(qn("a:prstGeom"))
    if geom is None:
        return
    geom.set("prst", "roundRect")
    av = geom.find(qn("a:avLst"))
    if av is None:
        av = geom.makeelement(qn("a:avLst"), {})
        geom.append(av)
    for gd in list(av):
        av.remove(gd)
    gd = av.makeelement(qn("a:gd"), {"name": "adj", "fmla": f"val {round(100000 / brand.CORNER_DIVISOR)}"})
    av.append(gd)


def _block(slide, x, y, w, h, *, fill):
    """A brand rounded rectangle (style guide p.8 'Hintergrundfläche')."""
    s = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, _in(x), _in(y), _in(w), _in(h))
    s.shadow.inherit = False
    s.fill.solid()
    s.fill.fore_color.rgb = fill
    s.line.fill.background()
    _set_corner(s, w, h)
    return s


def _fit_cover(src: Path, box_w: float, box_h: float, tmp_dir: Path) -> Path:
    """Centre-crop `src` to the box aspect ratio so the picture fills its
    rounded container (style guide p.8 'Bildcontainer'), matching the HTML's
    object-fit: cover. The original on disk is never modified."""
    from PIL import Image

    im = Image.open(src)
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.split()[-1])
        im = bg
    else:
        im = im.convert("RGB")

    target = box_w / box_h
    w, h = im.size
    if w / h > target:                       # too wide -> trim the sides
        new_w = int(h * target)
        im = im.crop(((w - new_w) // 2, 0, (w - new_w) // 2 + new_w, h))
    else:                                    # too tall -> trim top/bottom
        new_h = int(w / target)
        im = im.crop((0, (h - new_h) // 2, w, (h - new_h) // 2 + new_h))

    tmp_dir.mkdir(parents=True, exist_ok=True)
    out = tmp_dir / f"{src.stem}_fit.jpg"
    im.save(out, "JPEG", quality=88, optimize=True)
    return out


def _visual(slide, slot: dict, default_label: str, base_dir: Path, tmp_dir: Path, y: float, h: float):
    """One image container: a real picture, masked to the brand rounded shape,
    or the purple placeholder block carrying the label so an unfilled slot is
    obviously unfilled."""
    x, w = brand.RIGHT_X, brand.RIGHT_W
    image = slot.get("image")
    label = str(slot.get("label") or default_label)

    if image:
        src = (base_dir / str(image)).resolve()
        if src.exists():
            pic = _fit_cover(src, w, h, tmp_dir)
            p = slide.shapes.add_picture(str(pic), _in(x), _in(y), _in(w), _in(h))
            _set_corner(p, w, h)
            return
        print(f"      ! image not found, using placeholder: {src}")

    _block(slide, x, y, w, h, fill=PURPLE)
    tb, tf = _txbox(slide, x + 34, y + h / 2 - 44, w - 68, 88)
    tf.vertical_anchor = MSO_ANCHOR.MIDDLE
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.CENTER
    _run(p, label, size=11.5, bold=True, color=WHITE)
    if slot.get("placeholder"):
        p2 = tf.add_paragraph()
        p2.alignment = PP_ALIGN.CENTER
        p2.space_before = Pt(6)
        _run(p2, str(slot["placeholder"]), size=8, color=WHITE)


def _initials(name: str) -> str:
    words = [w for w in str(name).replace("-", " ").split() if w]
    return "".join(w[0] for w in words[:2]).upper() or "?"


def build_slide(prs: Presentation, data: dict, base_dir: Path, tmp_dir: Path) -> None:
    """The same page as render.py: GT Hub lockup top right, claim, lime
    identity block (startup logo, name, meta line, website link), five
    sections, two rounded image containers. Every text block is a real,
    editable text box; every image a real picture."""
    B = brand
    slide = prs.slides.add_slide(prs.slide_layouts[6])          # blank layout
    bg = slide.background.fill
    bg.solid()
    bg.fore_color.rgb = WHITE

    lang = i18n.lang_of(data)
    L = i18n.labels(lang)
    meta = data.get("meta") or {}

    # ── top: page label, claim, GT Hub lockup ────────────────────────────────
    logo_w = B.GT_LOGO_H / B.LOGO_ASPECT
    text_w = B.PAGE_W - 2 * B.PAD_X - logo_w - 28
    tb, tf = _txbox(slide, B.PAD_X, B.PAD_TOP, text_w, 16)
    label = str(meta.get("page_label") or L["page_label"])
    num = str(meta.get("page_number") or "")
    _run(tf.paragraphs[0], f"{label}      {num}".rstrip(), size=9, bold=True)

    tb, tf = _txbox(slide, B.PAD_X, B.PAD_TOP + 22, text_w, B.TOP_H - 22)
    tf.vertical_anchor = MSO_ANCHOR.TOP
    _run(tf.paragraphs[0], str(data.get("claim") or ""), size=22.5,
         bold=not B.HEADLINE_IS_LICENSED, font=HEADLINE_FONT)

    slide.shapes.add_picture(str(B.LOGO_PNG), _in(B.PAGE_W - B.PAD_X - logo_w), _in(B.PAD_TOP),
                             _in(logo_w), _in(B.GT_LOGO_H))

    # ── identity block ────────────────────────────────────────────────────────
    x0, y0 = B.PAD_X, B.COLS_TOP
    _block(slide, x0, y0, B.LEFT_W, B.ID_H, fill=LIME)
    tile_x, tile_y = x0 + 12, y0 + (B.ID_H - B.LOGO_TILE) / 2

    logo = data.get("logo")
    lp = (base_dir / str(logo)).resolve() if logo else None
    if lp is not None and lp.exists():
        tile_w = B.logo_tile_width(lp)
        _block(slide, tile_x, tile_y, tile_w, B.LOGO_TILE, fill=WHITE)
        from PIL import Image
        iw, ih = Image.open(lp).size
        inner_w, inner_h = tile_w - 16, B.LOGO_TILE - 16
        scale = min(inner_w / iw, inner_h / ih)
        w, h = iw * scale, ih * scale
        slide.shapes.add_picture(str(lp), _in(tile_x + (tile_w - w) / 2), _in(tile_y + (B.LOGO_TILE - h) / 2),
                                 _in(w), _in(h))
    else:
        if lp is not None:
            print(f"      ! logo not found: {lp}")
        tile_w = B.LOGO_TILE
        _block(slide, tile_x, tile_y, tile_w, B.LOGO_TILE, fill=BLACK)
        tb, tf = _txbox(slide, tile_x, tile_y, tile_w, B.LOGO_TILE)
        tf.vertical_anchor = MSO_ANCHOR.MIDDLE
        tf.paragraphs[0].alignment = PP_ALIGN.CENTER
        _run(tf.paragraphs[0], _initials(data.get("name") or ""), size=18, bold=True, color=WHITE)

    text_x = tile_x + tile_w + 16
    text_w = x0 + B.LEFT_W - 18 - text_x
    tb, tf = _txbox(slide, text_x, y0 + 14, text_w, B.ID_H - 24)
    tf.vertical_anchor = MSO_ANCHOR.MIDDLE
    _run(tf.paragraphs[0], str(data.get("name") or ""), size=15.5, bold=True)
    p = tf.add_paragraph()
    p.space_before = Pt(2)
    _run(p, (f"{L['location']}: {data.get('location') or L['unknown']} / "
             f"{L['founded']}: {data.get('founded') or L['unknown']} / "
             f"{L['team']}: {data.get('team_size') or L['unknown']}"), size=9)
    website = data.get("website")
    if website:
        p = tf.add_paragraph()
        p.space_before = Pt(3)
        r = _run(p, B.display_url(website), size=9, underline=True)
        r.hyperlink.address = B.href(website)

    # ── five sections ─────────────────────────────────────────────────────────
    # One text box on purpose: edited text reflows instead of overflowing a
    # fixed frame, which is the whole point of the .pptx export. Auto-shrink on
    # top, so an added sentence nudges the type down rather than spilling.
    sec_y = y0 + B.ID_H + 12
    tb, tf = _txbox(slide, x0 + 2, sec_y, B.LEFT_W - 4, B.COLS_TOP + B.COLS_H - sec_y)
    tf.auto_size = MSO_AUTO_SIZE.TEXT_TO_FIT_SHAPE
    sections = data.get("sections") or {}
    for i, (key, heading) in enumerate(i18n.sections(lang)):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        if i:
            p.space_before = Pt(8)
        _run(p, heading, size=9.5, bold=True)
        pb = tf.add_paragraph()
        pb.space_before = Pt(1.5)
        _run(pb, " ".join(str(sections.get(key) or "").split()), size=8.7)

    # ── two image containers ───────────────────────────────────────────────────
    visuals = data.get("visuals") or {}
    slots = zip(i18n.visuals(lang),
                (B.COLS_TOP, B.COLS_TOP + B.VIS_TOP_H + B.VIS_GAP),
                (B.VIS_TOP_H, B.VIS_BOTTOM_H))
    for (key, default_label), y, h in slots:
        _visual(slide, visuals.get(key) or {}, default_label, base_dir, tmp_dir, y, h)


def new_deck() -> Presentation:
    prs = Presentation()
    prs.slide_width = Inches(SLIDE_W)
    prs.slide_height = Inches(SLIDE_H)
    return prs


def main() -> int:
    ap = argparse.ArgumentParser(description="Export GT Hub one-pagers to editable .pptx")
    ap.add_argument("data", nargs="+")
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--combine", default=None,
                    help="write every page into ONE .pptx at this path instead of one file each")
    args = ap.parse_args()

    tmp_dir = Path(".onepager_tmp")
    combined = new_deck() if args.combine else None
    code = 0

    for raw in args.data:
        path = Path(raw).resolve()
        if not path.exists():
            print(f"  ✗ {raw}: not found")
            code = 1
            continue
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        problems = validate(data, path)
        if problems:
            print(f"  ✗ {path.name}: {len(problems)} problem(s)")
            for p in problems:
                print(f"      - {p}")
            code = 1
            continue

        if combined is not None:
            build_slide(combined, data, path.parent, tmp_dir)
            print(f"  ✓ {path.name} -> slide added")
        else:
            prs = new_deck()
            build_slide(prs, data, path.parent, tmp_dir)
            out_dir = Path(args.out_dir).resolve() if args.out_dir else path.parent
            out_dir.mkdir(parents=True, exist_ok=True)
            out = out_dir / f"{path.stem}_onepager.pptx"
            prs.save(str(out))
            print(f"  ✓ {path.name} -> {out}")

    if combined is not None:
        out = Path(args.combine).resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        combined.save(str(out))
        print(f"  ✓ combined deck -> {out}")

    if tmp_dir.exists():
        for f in tmp_dir.iterdir():
            f.unlink()
        tmp_dir.rmdir()
    return code


if __name__ == "__main__":
    sys.exit(main())
