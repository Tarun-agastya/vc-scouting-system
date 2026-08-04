"""
Phase J (4 Aug) — deterministic post-extraction gate for institutions,
established incumbents, and fabricated generic-noun-phrase fragments.

Backs up the prompt-level EXCLUDE rules, which proved unreliable on their
own on real bare-name-list pages: schwaben.digital's /presse archive (31
Jul/3 Aug) and hochschule-biberach.de's "unser-netzwerk" partner-logo page
(4 Aug) both got banks, chambers of commerce, law firms, and large
industrial incumbents (Liebherr, PERI, Goldbeck, Ed. Züblin) extracted as
"startups" despite the prompt already saying not to include them.
"""
from reasoning.qwen_client import _is_institutional_junk
from config.tuning_loader import get_institutional_junk_config

_CFG = get_institutional_junk_config()


def test_confirmed_junk_names_are_caught():
    names = [
        "VR-Bank", "Sparkasse Schwaben-Bodensee", "Sparkassen Bezirksverband Schwaben",
        "Stadtsparkasse Augsburg", "IHK Schwaben", "Handwerkskammer Augsburg",
        "Schmid Frank Rechtsanwälte", "Techniker Krankenkasse",
        "Liebherr", "PERI", "Goldbeck", "Ed. Züblin AG", "Wolff und Müller",
        "Eine Digitalagentur", "ein Games Studio", "ein Server", "ein Telemedizinanbieter",
    ]
    for name in names:
        assert _is_institutional_junk(name, _CFG), f"expected {name!r} to be flagged as junk"


def test_real_startups_are_never_false_positives():
    names = [
        "Sherpa", "Voiceline", "Onyx Biotech", "Furo", "Ororatech", "Porters",
        "BLP Digital", "Zollhof", "4Screen", "Kutter", "Peripheral Vision AI",
    ]
    for name in names:
        assert not _is_institutional_junk(name, _CFG), f"expected {name!r} to NOT be flagged"


def test_disabled_via_config_never_flags_anything():
    cfg = {**_CFG, "enabled": False}
    assert not _is_institutional_junk("VR-Bank", cfg)


def test_empty_name_is_not_junk():
    assert not _is_institutional_junk("", _CFG)
    assert not _is_institutional_junk(None, _CFG)
