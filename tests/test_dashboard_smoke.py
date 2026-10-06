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


def _fresh_page(browser):
    """
    A page with its own empty HTTP cache.

    The browser fixture is module-scoped, so pages share a cache — and the
    dashboard's ES modules are cached aggressively. After editing a .js file
    a test could keep exercising the PREVIOUS version and pass (or, worse,
    fail against code that is already fixed on disk). A fresh context per
    test removes that whole class of false result. Caller must close the
    returned context.
    """
    ctx = browser.new_context()
    return ctx, ctx.new_page()


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


def test_per_field_controls_render_and_history_loads(browser):
    """
    Per-field resolve: each field in a grouped review gets its own Apply /
    Reject and an expandable history. Clicking into a group and opening the
    history panel is the part `node --check` cannot see — the toggle fetches
    and renders on demand, so a bad selector or a bad response shape only
    shows up here.

    Skips rather than fails when the queue has no grouped field_update rows
    left: an empty inbox is a good outcome, not a broken dashboard.
    """
    ctx, page = _fresh_page(browser)
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(f"console.error: {m.text}")
            if m.type == "error" else None)

    page.goto(f"{BASE}/dashboard/#/reviews", wait_until="networkidle", timeout=45000)
    page.wait_for_timeout(3000)

    # Grouped (field_update) rows only — singles have no per-field controls.
    page.select_option("#f-type", "field_update")
    page.wait_for_timeout(3000)

    rows = page.locator("#review-list .row-item, #review-list [data-entry-id]")
    if rows.count() == 0:
        ctx.close()
        pytest.skip("no pending grouped field_update reviews to exercise")

    rows.first.click()
    page.wait_for_timeout(1800)

    apply_btns = page.locator("[data-apply-field]").count()
    reject_btns = page.locator("[data-reject-field]").count()
    toggles = page.locator("[data-history-toggle]")

    assert apply_btns > 0, "no per-field Apply button rendered"
    assert reject_btns == apply_btns, "Apply/Reject buttons are not paired per field"

    # Open the history panel — the on-demand fetch + render path.
    toggles.first.click()
    page.wait_for_timeout(2000)
    panel_text = page.locator("[data-history-panel]").first.inner_text()

    ctx.close()
    assert not errors, f"per-field controls raised: {errors[:3]}"
    assert "loading…" not in panel_text, "history panel never resolved"
    assert "Couldn't load history" not in panel_text, f"history fetch failed: {panel_text[:120]}"


def test_merge_field_by_field_actually_renders(browser):
    """
    The reported bug: clicking "Merge field by field…" showed a spinner
    forever. draw() was defined and then only re-entered from its own
    show-matching-fields toggle, so it was never called once — the preview
    request succeeded and nothing consumed it. Zero JS errors, zero syntax
    errors, permanently blank screen.

    So this clicks the button and asserts the table is really on screen.
    """
    ctx, page = _fresh_page(browser)
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(f"console.error: {m.text}")
            if m.type == "error" else None)

    page.goto(f"{BASE}/dashboard/#/reviews", wait_until="networkidle", timeout=45000)
    page.wait_for_timeout(3000)
    page.select_option("#f-type", "possible_duplicate")
    page.wait_for_timeout(3000)

    rows = page.locator("#review-list [data-entry-id]")
    merge_btn = None
    for i in range(min(rows.count(), 8)):
        rows.nth(i).click()
        page.wait_for_timeout(1200)
        if page.locator("#merge-fields-btn").count():
            merge_btn = page.locator("#merge-fields-btn")
            break
    if merge_btn is None:
        ctx.close()
        pytest.skip("no pending duplicate with an incoming record to merge")

    merge_btn.click()
    page.wait_for_timeout(2500)

    body = page.inner_text("body")
    has_table = page.locator("#m-go").count()      # the merge submit button
    ctx.close()

    assert not errors, f"merge screen raised: {errors[:3]}"
    assert has_table == 1, "merge screen never rendered past the spinner"
    assert "Merge field by field" in body, "merge screen heading missing"
    assert "NaN" not in body, "a NaN% bar rendered in the evidence panel"


def test_field_pick_survives_the_background_poll(browser):
    """
    The 10s poll repaints the detail pane. An unsubmitted radio pick used to
    be reset to its default on that repaint — silent loss of work. Pick a
    non-default candidate, wait past a poll tick, and check it is still picked.
    """
    ctx, page = _fresh_page(browser)
    page.goto(f"{BASE}/dashboard/#/reviews", wait_until="networkidle", timeout=45000)
    page.wait_for_timeout(3000)
    page.select_option("#f-type", "field_update")
    page.wait_for_timeout(3000)

    rows = page.locator("#review-list [data-entry-id]")
    if rows.count() == 0:
        ctx.close()
        pytest.skip("no grouped field_update reviews")
    rows.first.click()
    page.wait_for_timeout(1500)

    reject = page.locator('input[type="radio"][value="__reject__"]').first
    if reject.count() == 0:
        ctx.close()
        pytest.skip("no field picker in this group")
    reject.check()
    name = reject.get_attribute("name")
    page.wait_for_timeout(12000)   # > one 10s poll tick

    still = page.locator(f'input[name="{name}"][value="__reject__"]').first.is_checked()
    ctx.close()
    assert still, "the poll repaint reset an unsubmitted field pick"


def test_recent_merges_are_listed_with_an_undo_button(browser):
    """
    The nightly job merges without asking, so 'every merge can be undone' is
    only true if the button is where a person will look. It was missing: the
    only Undo was a bar that appeared after a MANUAL merge, and the card even
    claimed otherwise. Skips when nothing has been merged yet.
    """
    ctx, page = _fresh_page(browser)
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f"{BASE}/dashboard/#/reviews", wait_until="networkidle", timeout=45000)
    page.wait_for_timeout(3500)
    if page.locator("#merge-list").count() == 0:
        ctx.close()
        pytest.skip("no merges have happened yet")
    page.locator("#merge-list summary").click()
    page.wait_for_timeout(500)
    undo_buttons = page.locator("[data-undo-merge]").count()
    visible = page.locator("#merge-list [data-undo-merge]").first.is_visible()
    ctx.close()
    assert not errors, f"merge list raised: {errors[:3]}"
    assert undo_buttons > 0 and visible, "the merge list has no visible Undo button"


def test_browse_deduplicate_panel_previews_all_and_selected(browser):
    """
    The Deduplicate button: opens a panel, offers All / Only-selected, and a
    Preview that writes nothing. Deliberately NEVER clicks the merge button —
    this runs against the live database, and applying would merge real records.
    """
    ctx, page = _fresh_page(browser)
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(f"console.error: {m.text}") if m.type == "error" else None)
    page.goto(f"{BASE}/dashboard/#/browse", wait_until="networkidle", timeout=45000)
    page.wait_for_timeout(3500)

    page.locator("#dedup-toggle").click()
    assert page.locator("#dedup-panel").inner_text().strip(), "the panel did not open"
    # nothing selected yet: 'Only the selected' must be unavailable
    assert page.locator('input[name="dd-scope"][value="selected"]').is_disabled()

    page.locator("#dd-preview").click()
    page.wait_for_selector("#dedup-panel :text('safe to merge')", timeout=60000)
    all_text = page.locator("#dedup-panel").inner_text()
    assert "of " in all_text and "records" in all_text

    # select one row: the option unlocks and a selected-scope preview works
    page.locator("table.table tbody tr .row-select").first.check()
    page.wait_for_timeout(600)
    assert not page.locator('input[name="dd-scope"][value="selected"]').is_disabled()
    assert "Only the 1 selected" in page.locator("#dedup-panel").inner_text()
    page.locator('input[name="dd-scope"][value="selected"]').check()
    page.locator("#dd-preview").click()
    page.wait_for_selector("#dedup-panel :text('safe to merge')", timeout=60000)

    # changing the selection must discard the preview, so a stale one can't be applied
    page.locator("table.table tbody tr .row-select").nth(1).check()
    page.wait_for_timeout(600)
    assert page.locator("#dd-apply").count() == 0, "a preview survived a selection change"
    ctx.close()
    assert not errors, f"deduplicate panel raised: {errors[:3]}"
