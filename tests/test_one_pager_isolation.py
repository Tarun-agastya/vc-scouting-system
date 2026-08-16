"""
The one-pager tooling must stay independent of the VC-scouting pipeline.

WHY THIS TEST IS THE POINT. The owner asked for this subsystem to be built so
that "if that breaks it shouldn't affect anything else" — while away for a
month, with nobody watching. Isolation stated in a docstring is a convention;
the next person to touch this can break it in one line, with the best of
intentions, by reaching for `reasoning.qwen_client` or `config.settings`
because they are right there and already work. This makes the isolation a
property the main test suite enforces on every run.

It is an AST scan, not an import: it never executes the modules, so it works
even if Ollama, Postgres and Qdrant are all down — which is exactly the
condition under which the isolation matters most.

press_monitor/ is this repo's other isolated subsystem and it DOES import three
project symbols (config.settings, ingestion.gmail_auth, reasoning.qwen_client).
The one-pager tooling deliberately holds a stricter line — zero — because
unlike press_monitor it has no scheduled job and no credential to share, so
there is nothing it needs from the pipeline at all.
"""
import ast
from pathlib import Path

import pytest

ONE_PAGER_DIR = Path(__file__).resolve().parent.parent / "templates" / "one_pager"

# Every top-level package of the VC-scouting pipeline. Importing any of these
# from the one-pager tooling would couple the two failure domains.
PIPELINE_PACKAGES = {
    "processing", "ingestion", "api", "database", "vector_db",
    "reasoning", "config", "matchmaking", "embeddings", "regional",
    "press_monitor", "instagram_insights", "scripts", "discord_bot",
}


def _python_files():
    files = sorted(ONE_PAGER_DIR.glob("*.py"))
    assert files, f"no python files found under {ONE_PAGER_DIR} — did the directory move?"
    return files


def _imported_roots(path: Path):
    """Every root module name imported by `path`, including function-local imports."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            # level > 0 is a relative import (from . import x) — never a
            # pipeline package by definition.
            if node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
    return roots


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: p.name)
def test_no_pipeline_imports(path):
    leaked = _imported_roots(path) & PIPELINE_PACKAGES
    assert not leaked, (
        f"{path.name} imports {sorted(leaked)} from the VC-scouting pipeline. "
        f"The one-pager tooling must stay standalone so a pipeline breakage or "
        f"refactor cannot reach it — see templates/one_pager/FORMAT.md §7. If you "
        f"need something from there, copy the few lines you need instead."
    )


def test_no_database_or_http_api_access(path=None):
    """
    Belt-and-braces beyond the import check: nothing may reach the scouting
    database, Qdrant, or the local API, by any spelling.
    """
    forbidden = ("SessionLocal", "sqlalchemy", "qdrant", "psycopg", "localhost:8000")
    for f in _python_files():
        src = f.read_text(encoding="utf-8")
        # Strip comments/docstrings so prose explaining the rule doesn't trip it.
        tree = ast.parse(src, filename=str(f))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                src = src.replace(node.value, "")
        hits = [tok for tok in forbidden if tok.lower() in src.lower()]
        assert not hits, f"{f.name} references {hits} — the one-pager tooling must never touch pipeline storage"


def test_the_guard_itself_would_catch_a_violation():
    """
    A guard that cannot fail is worse than none. Prove the AST scan actually
    detects a pipeline import rather than silently passing on everything.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        bad = Path(d) / "bad.py"
        bad.write_text("def f():\n    from processing.storage import upsert_startup\n")
        assert _imported_roots(bad) & PIPELINE_PACKAGES == {"processing"}

        good = Path(d) / "good.py"
        good.write_text("import yaml\nimport httpx\nfrom pathlib import Path\n")
        assert not (_imported_roots(good) & PIPELINE_PACKAGES)
