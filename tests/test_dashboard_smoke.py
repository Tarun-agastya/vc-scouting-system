"""
Does the dashboard actually RUN?

`node --check` proves a file parses. It cannot see a ReferenceError to a
function that was never defined, or an insertBefore against a node that is not
yet in the DOM — and both shipped in one day:

  * reviews.js called load(), which does not exist in that module. It would
    have thrown on every successful merge and undo, AFTER the write had
    committed, so the data was correct while the screen appeared to hang.
  * browse.js inserted the column bar before a <table> wrapper it had not yet
    appended, which threw during render and left Browse showing
    "Couldn't load startups". Reported by the owner, not by any check here.

Both are invisible to syntax checking and obvious the moment a page is loaded,
so this loads the pages.

Skipped when the API is not running — it is a smoke test against the live
dashboard, not a unit test, and a missing server is not a test failure.
"""
import urllib.error
import urllib.request

import pytest

BASE = "http://localhost:8000"
VIEWS = ["browse", "reviews", "overview", "ingestion", "sources", "regional"]


def _api_up() -> bool:
    try:
        with urllib.request.urlopen(f"{BASE}/health", timeout=4) as r:
            return r.status == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _api_up(), reason="dashboard API not running")


@pytest.fixture(scope="module")
def browser():
    pw = pytest.importorskip("playwright.sync_api")
    with pw.sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        yield b
        b.close()


def _load(browser, route):
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(f"console.error: {m.text}")
            if m.type == "error" else None)
    page.goto(f"{BASE}/dashboard/#/{route}", wait_until="networkidle", timeout=45000)
    page.wait_for_timeout(2500)
    body = page.inner_text("body")
    page.close()
    return errors, body


@pytest.mark.parametrize("route", VIEWS)
def test_view_loads_without_a_runtime_error(browser, route):
    errors, body = _load(browser, route)
    assert not errors, f"{route} raised: {errors[:3]}"
    # The banner the owner actually saw. A view can render with zero JS errors
    # and still be showing a failure message from a caught exception.
    assert "Couldn't load" not in body, f"{route} shows a load-failure banner"


def test_browse_renders_rows_and_the_column_picker(browser):
    """
    The specific regression: Browse threw during render, so the table never
    appeared at all.
    """
    page = browser.new_page()
    page.goto(f"{BASE}/dashboard/#/browse", wait_until="networkidle", timeout=45000)
    page.wait_for_timeout(3500)
    rows = page.locator("table.table tbody tr").count()
    picker = page.locator("#col-toggle").count()
    page.close()
    assert rows > 0, "Browse rendered no rows"
    assert picker == 1, "the Columns button is missing"
