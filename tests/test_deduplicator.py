"""Pure identity-resolution functions — fast, no DB."""
from processing.deduplicator import (
    normalize_company_name, extract_domain, generate_fingerprint, name_to_stable_uuid,
)


def test_normalize_strips_legal_suffixes_and_case():
    assert normalize_company_name("DeepDrive GmbH") == "deepdrive"
    assert normalize_company_name("Deep-Drive AG") == "deep-drive"
    assert normalize_company_name("DEEPDRIVE, Inc.") == "deepdrive"
    assert normalize_company_name("") == ""


def test_extract_domain_variants():
    assert extract_domain("https://www.acme.eu/about") == "acme.eu"
    assert extract_domain("acme.eu") == "acme.eu"
    assert extract_domain("") == ""


def test_fingerprint_stable_and_domain_sensitive():
    a = generate_fingerprint("DeepDrive GmbH", "deepdrive.eu")
    b = generate_fingerprint("DeepDrive", "https://www.deepdrive.eu")
    assert a == b  # same normalized name + domain -> same fingerprint
    c = generate_fingerprint("DeepDrive", "other.com")
    assert a != c  # different domain -> different fingerprint
    assert generate_fingerprint("", "x.com") == ""


def test_stable_uuid_deterministic():
    u1 = name_to_stable_uuid("Celonis", "celonis.com")
    u2 = name_to_stable_uuid("celonis", "https://celonis.com")
    assert u1 == u2 and u1 is not None
    assert name_to_stable_uuid("", "") is None
