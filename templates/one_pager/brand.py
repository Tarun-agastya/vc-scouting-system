"""
GT Hub brand tokens — colors, fonts, the logo, the corner-radius rule.

Source: 241023_GTHub_Styleguide.pdf (the owner's style guide). Page references
below are to that PDF. WHY THIS FILE EXISTS: render.py (HTML) and
export_pptx.py (PowerPoint) are two renderers for the same page; without one
shared source of these constants they drift, exactly the way the visual box
split once did (export_pptx.py had a 50/50 split with a comment wrongly
claiming parity with a 40/60 reference — see its own history). Every brand
constant is defined once, here.

ISOLATION. Like every file in templates/one_pager/, this imports nothing from
the VC-scouting pipeline (FORMAT.md §7) — only stdlib.
"""
from __future__ import annotations

from pathlib import Path

HERE = Path(__file__).resolve().parent
ASSETS = HERE / "assets"
FONTS_DIR = ASSETS / "fonts"

# ── Colors (style guide p.5) ──────────────────────────────────────────────────
LIME = "#D7F159"      # PANTONE 388U — the brand's signature accent
BLACK = "#000000"
PURPLE = "#7C57FC"
WHITE = "#FFFFFF"
GREY = "#BBBBBB"       # "Neutral Grey"

# Which text color is legible on which fill (style guide p.6, "Farbkontraste"):
# black and white both take lime or purple text for emphasis; lime, grey and
# purple take only black or white body text — never lime-on-purple or similar.
ON_DARK = WHITE        # body text on black or purple
ON_LIGHT = BLACK       # body text on lime, grey or white

# ── Type (style guide p.7) ─────────────────────────────────────────────────────
# Headline: PP Neue Machina. That is a paid Pangram Pangram font this repo has
# no license for, so it is never downloaded or bundled — doing so would ship
# pirated type. Drop a licensed file at HEADLINE_FONT_FILE (woff2 preferred, or
# otf/ttf) and both renderers pick it up automatically; until then the
# headline falls back to Work Sans Bold with tightened tracking, the closest
# unlicensed approximation to the guide's squared-off grotesk.
HEADLINE_FONT_FILE = FONTS_DIR / "PPNeueMachina-Regular.woff2"
HEADLINE_FONT_NAME = "PP Neue Machina" if HEADLINE_FONT_FILE.exists() else "Work Sans"
HEADLINE_IS_LICENSED = HEADLINE_FONT_FILE.exists()

# Subline / body: Work Sans (SIL Open Font License — free to bundle and
# embed), fetched once from Google Fonts and checked into assets/fonts/.
BODY_FONT_NAME = "Work Sans"
WORK_SANS = {
    400: FONTS_DIR / "WorkSans-Regular.ttf",
    600: FONTS_DIR / "WorkSans-SemiBold.ttf",
    700: FONTS_DIR / "WorkSans-Bold.ttf",
}

# ── The logo (style guide p.3-4) ────────────────────────────────────────────────
# Extracted as real vector paths from the style guide PDF itself (page 1's
# cover, which draws the exact "Hauptlogo" — icon + wordmark — as filled
# bezier paths, not a raster image) — a pixel-accurate reproduction, not a
# redrawing. See scripts/extract_gt_hub_logo.py for how these were pulled.
LOGO_SVG = ASSETS / "gt_hub_logo.svg"      # icon + "GT Hub" wordmark (Hauptlogo)
MARK_SVG = ASSETS / "gt_hub_mark.svg"      # icon only (Bildmarke), no wordmark
LOGO_PNG = ASSETS / "gt_hub_logo.png"      # same, rasterized (for PowerPoint)
MARK_PNG = ASSETS / "gt_hub_mark.png"
# The logo's natural aspect ratio (height / width) — both SVGs share this
# box (style guide p.3's "a x a" construction), so one ratio covers either.
LOGO_ASPECT = 667.0 / 605.19


def logo_svg_markup() -> str:
    """The lockup's inline <svg>...</svg> markup, for embedding directly in HTML
    (crisp at any size, no raster artifacts — unlike a <img> of the PNG)."""
    return LOGO_SVG.read_text(encoding="utf-8")


# ── The rounded-rectangle rule (style guide p.9) ────────────────────────────────
# "Eckenradius = kurze Formatseite / 24" — every rounded container's corner
# radius is a fixed fraction of its OWN shorter side, not a flat pixel value,
# so a small card and a full-page block read as the same brand shape at any
# size. Verified against the guide's own worked example: a 1080x1920 asset
# with a 45px radius -> 1080/45 = 24.
CORNER_DIVISOR = 24


def corner_radius(short_side: float) -> float:
    return short_side / CORNER_DIVISOR


# ── Page geometry, shared by render.py and export_pptx.py ───────────────────────
# One 16:9 page, in CSS pixels (1280 x 720). export_pptx.py divides by 96 to get
# inches, so the HTML preview and the PowerPoint are the same layout by
# construction rather than by two people keeping two sets of numbers in step.
PAGE_W, PAGE_H = 1280, 720
PAD_X, PAD_TOP, PAD_BOTTOM = 34, 26, 28
TOP_H = 84                 # page label + claim (room for a two-line claim)
GT_LOGO_H = 72             # GT Hub lockup, top right
COLS_TOP = PAD_TOP + TOP_H + 14
COLS_H = PAGE_H - PAD_BOTTOM - COLS_TOP
LEFT_W = 640               # identity block + five sections
COL_GAP = 18
RIGHT_X = PAD_X + LEFT_W + COL_GAP
RIGHT_W = PAGE_W - PAD_X - RIGHT_X
ID_H = 96                  # the lime identity block (logo, name, meta, website)
LOGO_TILE = 72             # the startup's logo sits on a white tile inside it
LOGO_TILE_MAX_W = 176      # ...which widens for a wide wordmark (up to this)


def logo_tile_width(image_path) -> int:
    """
    Width of the white tile for a startup logo: square for a square mark,
    wider for a wordmark, so a wide logo isn't shrunk to a sliver (LIGARO's
    wordmark was unreadable in a square tile). Falls back to square if the
    image can't be read.
    """
    try:
        from PIL import Image

        with Image.open(image_path) as im:
            w, h = im.size
        inner_h = LOGO_TILE - 16                       # 8px padding each side
        return int(max(LOGO_TILE, min(LOGO_TILE_MAX_W, inner_h * w / max(1, h) + 16)))
    except Exception:
        return LOGO_TILE
VIS_GAP = 14
VIS_TOP_H = round((COLS_H - VIS_GAP) * 0.40)   # 40/60: "how it works" gets more room
VIS_BOTTOM_H = COLS_H - VIS_GAP - VIS_TOP_H


def display_url(url: str) -> str:
    """'https://www.ligaro.org/' -> 'ligaro.org' for printing on the page."""
    u = str(url or "").strip()
    for prefix in ("https://", "http://"):
        if u.lower().startswith(prefix):
            u = u[len(prefix):]
    if u.lower().startswith("www."):
        u = u[4:]
    return u.rstrip("/")


def href(url: str) -> str:
    u = str(url or "").strip()
    return u if "://" in u else f"https://{u}"
