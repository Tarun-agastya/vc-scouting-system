"""logo_fetch: which candidate wins, and which are refused. No network."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "templates" / "one_pager"))

import logo_fetch as lf  # noqa: E402

PAGE = """<html><head>
<link rel="icon" href="/media/Studio-Tectonic-Dusts_35.ico">
<link rel="shortcut icon" href="/favicon-32x32.png">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
</head><body><header><img src="/img/hero.jpg" alt="Hero"><img src="/img/acme-logo.svg" alt="Acme"></header>
</body></html>"""


def test_candidates_come_best_first():
    got = [u for u, _ in lf._candidates(PAGE, "https://acme.de/")]
    assert got[0] == "https://acme.de/apple-touch-icon.png"
    assert got[1] == "https://acme.de/img/acme-logo.svg"
    assert "https://acme.de/favicon-32x32.png" in got
    assert got[-1] == "https://acme.de/favicon.ico"


def test_a_favicon_that_is_really_a_photo_is_refused():
    """Eidola's site pointed its favicon at a renamed project photo."""
    got = [u for u, _ in lf._candidates(PAGE, "https://acme.de/")]
    assert not any("Tectonic" in u for u in got)


def test_tiny_and_banner_images_are_rejected(tmp_path, monkeypatch):
    from io import BytesIO
    from PIL import Image

    def png(w, h):
        b = BytesIO()
        Image.new("RGB", (w, h)).save(b, "PNG")
        return b.getvalue()

    class Resp:
        def __init__(self, data): self.content, self.headers = data, {"content-type": "image/png"}
        def raise_for_status(self): pass

    class Client:
        data = b""
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def get(self, url): return Resp(Client.data)

    import httpx
    monkeypatch.setattr(httpx, "Client", Client)
    out = tmp_path / "logo.png"
    for w, h, ok in ((1, 1, False), (900, 60, False), (180, 180, True)):
        Client.data = png(w, h)
        assert bool(lf._try_save("https://acme.de/x.png", out, "test")) is ok, (w, h)


def test_an_uploaded_svg_logo_is_normalised_to_png(tmp_path):
    svg = tmp_path / "logo.svg"
    svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
                   '<rect width="10" height="10" fill="#7c57fc"/></svg>')
    out = tmp_path / "out.png"
    assert lf.save_local(svg, out) and out.read_bytes()[:4] == b"\x89PNG"
