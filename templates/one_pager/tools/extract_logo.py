"""
One-off tool: pull the GT Hub logo out of the style guide PDF as real vector
paths, and write assets/gt_hub_logo.svg + gt_hub_mark.svg (+ their rasterized
.png twins, for PowerPoint).

WHY THIS EXISTS, rather than just committing the SVGs and deleting the tool:
so the logo can be re-extracted if the style guide is ever reissued, and so
it's auditable that the shape is a pixel-accurate pull from the guide's own
page 1 cover (which draws the lockup as filled bezier paths, not a raster
image) — not a redrawing or approximation.

Usage:
    python3 templates/one_pager/tools/extract_logo.py /path/to/styleguide.pdf

Not run automatically by anything; the checked-in SVG/PNG files under
assets/ are what every other file in this tool actually uses.
"""
from __future__ import annotations

import sys
from pathlib import Path

import fitz

HERE = Path(__file__).resolve().parent.parent
ASSETS = HERE / "assets"


def _pt(p, d=2) -> str:
    return f"{p.x:.{d}f},{p.y:.{d}f}"


def _path_d(items, decimals: int = 2, eps: float = 0.05) -> str:
    """
    PyMuPDF gives each drawing as a flat list of line/curve segments, each
    carrying its own start point — convenient, but a naive walk glues
    separate CONTOURS together with a spurious connecting line (visible as a
    small hook artifact on letters with a counter, like 'b'). Insert a fresh
    "M" wherever a segment's start point doesn't match where the pen actually
    is, which is the real signal that PDF meant a new subpath (e.g. a hole).
    """
    parts: list[str] = []
    cur = None

    def close_if_open():
        if cur is not None:
            parts.append("Z")

    for it in items:
        op = it[0]
        if op == "re":
            close_if_open()
            r = it[1]
            parts.append(f"M{_pt(r.tl, decimals)} L{_pt(r.tr, decimals)} "
                        f"L{_pt(r.br, decimals)} L{_pt(r.bl, decimals)} Z")
            cur = None
            continue
        p0 = it[1]
        if cur is None or abs(p0.x - cur[0]) > eps or abs(p0.y - cur[1]) > eps:
            close_if_open()
            parts.append(f"M{_pt(p0, decimals)}")
        if op == "l":
            p1 = it[2]
            parts.append(f"L{_pt(p1, decimals)}")
            cur = (p1.x, p1.y)
        elif op == "c":
            c1, c2, p3 = it[2], it[3], it[4]
            parts.append(f"C{_pt(c1, decimals)} {_pt(c2, decimals)} {_pt(p3, decimals)}")
            cur = (p3.x, p3.y)
        elif op == "qu":
            pts = it[1]
            parts.append("L" + " L".join(_pt(q, decimals) for q in pts[1:]))
            cur = (pts[-1].x, pts[-1].y)
    close_if_open()
    return " ".join(parts)


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 1
    doc = fitz.open(sys.argv[1])
    page = doc[0]                       # the cover: icon + wordmark, nothing else
    drawings = page.get_drawings()

    # drawing[1] = the black icon shape (rounded box + the cut-out "arrow"
    # swoosh, one compound path). drawings[2:7] = the five lime wordmark
    # letters ("G", "T", " Hub" as drawn shapes) — found by inspecting the
    # extracted drawing list once by hand; re-check if the guide is reissued.
    icon = drawings[1]
    letters = drawings[2:7]
    if icon["fill"] is None or icon["fill"][0] > 0.1:
        print("! drawings[1] doesn't look like the black icon any more — "
              "the style guide's layout changed. Re-inspect page.get_drawings().")
        return 1

    icon_d = _path_d(icon["items"])
    letter_ds = [_path_d(d["items"]) for d in letters]
    r = icon["rect"]
    x0, y0, w, h = r.x0, r.y0, r.x1 - r.x0, r.y1 - r.y0

    ASSETS.mkdir(parents=True, exist_ok=True)
    lockup_svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{x0:.2f} {y0:.2f} {w:.2f} {h:.2f}" '
        f'role="img" aria-label="GT Hub">\n  <path d="{icon_d}" fill="#000000"/>\n'
        + "".join(f'  <path d="{p}" fill="#D7F159"/>\n' for p in letter_ds) + "</svg>\n"
    )
    mark_svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{x0:.2f} {y0:.2f} {w:.2f} {h:.2f}" '
        f'role="img" aria-label="GT Hub">\n  <path d="{icon_d}" fill="#000000"/>\n</svg>\n'
    )
    (ASSETS / "gt_hub_logo.svg").write_text(lockup_svg, encoding="utf-8")
    (ASSETS / "gt_hub_mark.svg").write_text(mark_svg, encoding="utf-8")

    for name, svg in (("gt_hub_logo.png", lockup_svg), ("gt_hub_mark.png", mark_svg)):
        rdoc = fitz.open(stream=svg.encode(), filetype="svg")
        pix = rdoc[0].get_pixmap(alpha=True, matrix=fitz.Matrix(6, 6))  # ~3600px tall
        pix.save(str(ASSETS / name))
        print(f"✓ {name}  {pix.width}x{pix.height}")

    print("✓ gt_hub_logo.svg / gt_hub_mark.svg")
    return 0


if __name__ == "__main__":
    sys.exit(main())
