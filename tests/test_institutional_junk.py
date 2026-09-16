"""
Phase J (4 Aug, extended same day) — deterministic post-extraction gate for
names that aren't a plausible single company: institutions/incumbents,
fabricated generic noun phrases, article headlines, and comma-concatenated
multi-company lists.

Backs up the prompt-level EXCLUDE rules, which proved unreliable on their
own on real pages: schwaben.digital's /presse archive, hochschule-
biberach.de's "unser-netzwerk" partner page, and a munich-startup.de crawl
(all confirmed live 31 Jul-4 Aug) each got non-startups extracted as
"startups" despite the prompt already excluding them.
"""
from reasoning.qwen_client import _is_implausible_startup_name
from config.tuning_loader import get_institutional_junk_config

_CFG = get_institutional_junk_config()


def test_confirmed_institutional_and_incumbent_names_are_caught():
    names = [
        "VR-Bank", "Sparkasse Schwaben-Bodensee", "Sparkassen Bezirksverband Schwaben",
        "Stadtsparkasse Augsburg", "IHK Schwaben", "Handwerkskammer Augsburg",
        "Schmid Frank Rechtsanwälte", "Techniker Krankenkasse",
        "Liebherr", "PERI", "Goldbeck", "Ed. Züblin AG", "Wolff und Müller",
        "Eine Digitalagentur", "ein Games Studio", "ein Server", "ein Telemedizinanbieter",
    ]
    for name in names:
        assert _is_implausible_startup_name(name, _CFG), f"expected {name!r} to be flagged as junk"


def test_confirmed_headline_and_multi_entity_names_are_caught():
    names = [
        "Industrie 4.0: Wie Münchner Startups die Industrie digitalisieren",
        "Urban Mobility: Wie kommen wir künftig durch die Stadt?",
        "Healthtech: Vier Münchner Startups und ihre Lösungen",
        "Fünf Münchner Startups und ihre Lösungen für autonomes Fahren",
        "sechs Münchner Startup die Bauwirtschaft digitalisieren",
        "Sechs Münchner Startup-Gründerinnen und ein Mobility-Investor",
        "Vier Münchner Fintech-Startups und ihre Erfolge",
        "Cassio-P, Novoviz, Solar Loop",
        "Kibaudi, Newsense Engineering, Datfid",
        "menstruflow, Nouxx, nghty berlin, Olena Scent",
        "Xplore Freerider, Xplore Market Pioneer, Xplore Venture Creator",
        "Münchner Startups",
        "Münchner Healthtech-Startups",
        "Wie Münchner Startups Drohnen abheben lassen",
        "5 Münchner E-Commerce-Startups im Blick",
        "US-Connection für Münchner Robotik-Startups",
    ]
    for name in names:
        assert _is_implausible_startup_name(name, _CFG), f"expected {name!r} to be flagged as junk"


def test_real_startups_are_never_false_positives():
    names = [
        "Sherpa", "Voiceline", "Onyx Biotech", "Furo", "Ororatech", "Porters",
        "BLP Digital", "Zollhof", "4Screen", "Kutter", "Peripheral Vision AI",
        "Two Sigma", "Six Flags", "One Medical", "ElevenLabs", "Mistral AI",
        "Stocard", "ConciergeBeauty (Digital Concierge Solutions GmbH)",
        "Quintos Grundbesitzverwaltung Vermögensverwaltung 60 UG (haftungsbeschränkt) & Co. KG",
        "gate Garchinger Technologie- und Gründerzentrum GmbH",
        "Center for Digital Technology and Management (CDTM)",
    ]
    for name in names:
        assert not _is_implausible_startup_name(name, _CFG), f"expected {name!r} to NOT be flagged"


def test_disabled_via_config_never_flags_anything():
    cfg = {**_CFG, "enabled": False}
    assert not _is_implausible_startup_name("VR-Bank", cfg)


def test_empty_name_is_not_junk():
    assert not _is_implausible_startup_name("", _CFG)
    assert not _is_implausible_startup_name(None, _CFG)


def test_single_comma_is_allowed():
    # a single comma (e.g. a legal-suffix pattern like "Acme, Inc.") must not
    # be enough alone to reject a name — only 2+ commas signal a concatenated
    # list of multiple entities.
    assert not _is_implausible_startup_name("Acme, Inc.", _CFG)


def test_university_office_and_chair_titles_are_caught():
    """
    16 Sep 2026: a crawl registered against hochschule-biberach.de's (now
    404) Gründung URL wandered into /studium/bachelorstudium/* and stored 11
    "startups" that were course subjects and faculty roles. These three are
    the ones a name-shaped rule can catch safely.
    """
    names = [
        "Wissenschaftliche Leitung Modellbauwerkstatt",
        "Qualitätsmanagement / Medienausschuss",
        "Stud.Dekan Master / Öffentlichkeitsarbeit / Alumni",
        "Studiendekanat Bauingenieurwesen",
        "Prodekanin Architektur",
        "Lehrstuhl für Baukonstruktion",
        "Prüfungsausschuss Master",
    ]
    for name in names:
        assert _is_implausible_startup_name(name, _CFG), f"expected {name!r} to be flagged as junk"


def test_academic_office_rule_does_not_eat_real_company_names():
    """
    The rule drops the WHOLE record, so a false positive is expensive. These
    are names that share vocabulary with the pattern but are real companies —
    the rule is anchored at the start of the name precisely so they survive.
    """
    keep = [
        "Qualitest GmbH",              # 'qualit...' but not Qualitätsmanagement
        "Dekade Labs",                 # 'deka...' prefix, not 'Dekan'
        "Professional Robotics",       # 'profes...' prefix, not 'Professur'
        "Rektorat Software UG",        # leading token is not one of the offices
        "Leitungswerk GmbH",           # 'Leitung' compound, not 'Wissenschaftliche Leitung'
        "Ligaro",
        "Hula Earth",
    ]
    for name in keep:
        assert not _is_implausible_startup_name(name, _CFG), \
            f"{name!r} is a plausible company name and must not be dropped"
