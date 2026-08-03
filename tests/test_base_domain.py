"""
Phase R-7 — _base_domain www-stripping fix.

Found live profiling techfounders.com: its registered primary_url is
"https://www.techfounders.com" but the site 301-redirects to the bare
"techfounders.com" and every internal link uses the bare form — an exact
netloc comparison made every internal link look like a different domain,
so the crawl could never expand past the entry page at all.
"""
from ingestion.web_scraper import _base_domain


def test_base_domain_strips_www():
    assert _base_domain("https://www.techfounders.com") == "techfounders.com"


def test_base_domain_bare_domain_unchanged():
    assert _base_domain("https://techfounders.com/") == "techfounders.com"


def test_base_domain_www_and_bare_now_compare_equal():
    assert _base_domain("https://www.example.com/a") == _base_domain("https://example.com/b")


def test_base_domain_lowercases():
    assert _base_domain("https://WWW.Example.COM/x") == "example.com"


def test_base_domain_does_not_strip_www_from_a_real_subdomain_prefix():
    # "wwwx.example.com" is not "www.example.com" — must not be mangled.
    assert _base_domain("https://wwwx.example.com/") == "wwwx.example.com"
