"""
Dashboard access to the one-pager generator — via SUBPROCESS, never import.

WHY SUBPROCESS AND NOT A FUNCTION CALL. templates/one_pager/ is deliberately
isolated from this pipeline (see FORMAT.md §7): the owner's requirement was
that if the one-pager tooling breaks, nothing else is affected. Importing
generate.py into this FastAPI process would throw that away in one line — a
hang, a crash, an unbounded memory allocation or a bad third-party import
inside the generator would then take the whole API down with it, dashboard
and scouting pipeline included.

Running it as a child process keeps the failure domain intact:
  * a crash is an exit code, not an exception in our process;
  * a hang is bounded by `timeout=` and killed, not an event-loop stall;
  * the generator's imports never enter this interpreter at all.

The only thing this module knows about the generator is its FILE PATH and its
command-line flags. tests/test_one_pager_isolation.py asserts that this file
imports nothing from templates/one_pager/, so the boundary cannot be quietly
removed later.

Every subprocess call is dispatched through run_in_executor. subprocess.run is
blocking, and blocking the event loop inside an async handler is the exact bug
that froze the whole dashboard during ingestion (fixed 14 Aug in
ingestion/worker_queue.py) — it must not be reintroduced here.
"""
import asyncio
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

import yaml
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse

router = APIRouter()
logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_TOOL_DIR = _REPO_ROOT / "templates" / "one_pager"
_DATA_DIR = _TOOL_DIR / "data"

# Generous but bounded. Drafting the whole deck on gemma4:12b plus translating
# runs ~2-3 min (longer if an ingestion run holds the GPU); the ceiling exists
# so a wedged model can never hold a request open indefinitely.
_GENERATE_TIMEOUT_S = 600
_RENDER_TIMEOUT_S = 120

_MAX_UPLOAD_BYTES = 60 * 1024 * 1024   # a pitch deck well past any realistic size
_ALLOWED_SUFFIXES = {".pdf", ".pptx"}

# Every one-pager exists in both languages: data/<slug>.de.yaml + <slug>.en.yaml.
# German is the final, exported version, so it is the default everywhere a
# language is not given. Duplicated from templates/one_pager/i18n.py on purpose:
# this module must not import the tool (see the docstring above).
_LANGS = ("de", "en")
_FINAL_LANG = "de"


def _run(cmd: list, timeout: int) -> subprocess.CompletedProcess:
    """Blocking; always called via run_in_executor."""
    return subprocess.run(
        cmd, cwd=str(_REPO_ROOT), capture_output=True, text=True, timeout=timeout,
    )


async def _run_async(cmd: list, timeout: int) -> subprocess.CompletedProcess:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: _run(cmd, timeout))


def _split_name(path: Path) -> tuple:
    """data/ligaro.de.yaml -> ("ligaro", "de"); a pre-two-language ligaro.yaml -> ("ligaro", "de")."""
    stem = path.name[: -len(".yaml")]
    base, _, lang = stem.rpartition(".")
    if base and lang in _LANGS:
        return base, lang
    return stem, _FINAL_LANG


def _read_version(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        logger.warning(f"[OnePager] could not read {path.name}: {exc}")
        return {"name": None, "claim": "", "status": "unreadable",
                "open_questions": [str(exc)], "updated_at": path.stat().st_mtime}
    review = data.get("review") or {}
    return {
        "name": data.get("name"),
        "claim": data.get("claim") or "",
        "status": review.get("status") or "draft",
        "open_questions": review.get("open_questions") or [],
        "updated_at": path.stat().st_mtime,
    }


def _list_drafts() -> list:
    """One entry per startup, with each language version it has."""
    if not _DATA_DIR.is_dir():
        return []
    groups = {}
    for p in sorted(_DATA_DIR.glob("*.yaml")):
        base, lang = _split_name(p)
        groups.setdefault(base, {}).setdefault(lang, _read_version(p))
    out = []
    for base, versions in groups.items():
        lead = versions.get(_FINAL_LANG) or next(iter(versions.values()))
        out.append({
            "slug": base,
            "name": lead["name"] or base,
            "claim": lead["claim"],
            "versions": versions,
            "missing": [lang for lang in _LANGS if lang not in versions],
            "updated_at": max(v["updated_at"] for v in versions.values()),
        })
    out.sort(key=lambda d: d["updated_at"], reverse=True)
    return out


@router.get("")
async def list_one_pagers():
    """Every startup with a draft, newest first, with its German and English versions."""
    return {"one_pagers": _list_drafts(), "final_lang": _FINAL_LANG}


@router.get("/search-budget")
async def search_budget():
    """
    What a one-pager web search may spend right now: Tavily's real balance, the
    reserve kept for the scouting pipeline, and the one-pager's own monthly use.
    Asked of the tool by subprocess (this module never imports it).
    """
    try:
        proc = await _run_async([sys.executable, str(_TOOL_DIR / "websearch.py")], 30)
        import json
        return json.loads(proc.stdout)
    except Exception as exc:
        logger.warning(f"[OnePager] search budget unavailable: {exc}")
        return {"tavily": None, "paid_searches_available": None, "error": "unavailable"}


@router.post("/generate")
async def generate_one_pager(
    deck: UploadFile = File(...),
    name: str = Form(...),
    url: Optional[str] = Form(None),
    no_llm: bool = Form(False),
    force: bool = Form(False),
    draft_lang: str = Form(_FINAL_LANG),
    web_search: bool = Form(True),
    paid_search: bool = Form(True),
):
    """
    Upload a pitch deck (.pdf or .pptx) and generate a draft one-pager.

    Always writes BOTH languages. `draft_lang` (German by default, the exported
    version) is drafted from the deck; the other is translated from it.

    Returns the generator's own stdout plus the parsed draft, so the dashboard
    can show exactly what a terminal run would have shown — including the
    open-questions list, which is the part that actually needs a human.
    """
    suffix = Path(deck.filename or "").suffix.lower()
    if suffix not in _ALLOWED_SUFFIXES:
        raise HTTPException(
            status_code=422,
            detail=(f"'{deck.filename}' is not a supported deck. Use .pdf or .pptx — "
                    f"a legacy .ppt must be re-saved first."),
        )
    if not name.strip():
        raise HTTPException(status_code=422, detail="A startup name is required.")
    if draft_lang not in _LANGS:
        raise HTTPException(status_code=422, detail=f"draft_lang must be one of {', '.join(_LANGS)}.")

    tmpdir = Path(tempfile.mkdtemp(prefix="onepager_upload_"))
    try:
        payload = await deck.read()
        if len(payload) > _MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="Deck is larger than 60 MB.")
        if not payload:
            raise HTTPException(status_code=422, detail="Uploaded deck is empty.")
        deck_path = tmpdir / f"deck{suffix}"
        deck_path.write_bytes(payload)

        cmd = [sys.executable, str(_TOOL_DIR / "generate.py"),
               "--deck", str(deck_path), "--name", name.strip(), "--draft-lang", draft_lang]
        if url and url.strip():
            cmd += ["--url", url.strip()]
        if no_llm:
            cmd.append("--no-llm")
        if force:
            cmd.append("--force")
        if not web_search:
            cmd.append("--no-web-search")
        elif not paid_search:
            cmd.append("--no-paid-search")

        try:
            proc = await _run_async(cmd, _GENERATE_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            raise HTTPException(
                status_code=504,
                detail=(f"The generator did not finish within {_GENERATE_TIMEOUT_S}s and was "
                        f"stopped. The API itself is unaffected. Try again, or tick \"without AI "
                        f"draft\" if the local model is busy with an ingestion run."),
            )

        if proc.returncode != 0:
            # The generator prints a human-readable reason for every hard
            # failure (deck unreadable, legacy .ppt, target exists) — surface
            # that verbatim rather than a generic 500.
            detail = (proc.stdout or "").strip().splitlines()
            reason = next((ln for ln in detail if ln.startswith("✗")), None) \
                or (proc.stderr or "").strip()[-400:] or "generation failed"
            reason = reason.lstrip("✗ ").strip()
            # The CLI's own wording talks about --force/--out, which mean
            # nothing to someone clicking a button. Translate that one case.
            if "already exists" in reason:
                reason = (f"A draft for \"{name.strip()}\" already exists. "
                          f"Tick \"overwrite existing draft\" to replace it.")
            raise HTTPException(status_code=422, detail=reason)

        # Locate what it wrote. The generator prints one absolute path per language.
        written = []
        for line in (proc.stdout or "").splitlines():
            if line.startswith("✓ Written (") and "): " in line:
                written.append(Path(line.split("): ", 1)[1].strip()))
        if not written or not all(p.exists() for p in written):
            raise HTTPException(status_code=500,
                                detail="Generator reported success but the YAML files were not found.")

        base = _split_name(written[0])[0]
        draft = next((d for d in _list_drafts() if d["slug"] == base), None)
        return {"status": "ok", "log": proc.stdout, "draft": draft}
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


@router.post("/{slug}/translate")
async def translate_one_pager(slug: str, from_lang: str = _FINAL_LANG, force: bool = False):
    """
    (Re)create one language version from the other — e.g. bring the English
    version up to date after the German one was edited. Subprocess, like the rest.
    """
    src = _safe_yaml(slug, from_lang)
    cmd = [sys.executable, str(_TOOL_DIR / "translate.py"), str(src)]
    if force:
        cmd.append("--force")
    try:
        proc = await _run_async(cmd, _GENERATE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504,
                            detail=f"Translation did not finish within {_GENERATE_TIMEOUT_S}s and was stopped.")
    if proc.returncode != 0:
        lines = (proc.stdout or "").strip().splitlines()
        reason = next((ln for ln in lines if ln.startswith("✗")), None) \
            or (proc.stderr or "").strip()[-400:] or "translation failed"
        reason = reason.lstrip("✗ ").strip()
        if "already exists" in reason:
            reason = "That language version already exists. Confirm to overwrite it."
        raise HTTPException(status_code=422, detail=reason)
    draft = next((d for d in _list_drafts() if d["slug"] == slug), None)
    return {"status": "ok", "log": proc.stdout, "draft": draft}


@router.get("/{slug}/preview", response_class=HTMLResponse)
async def preview_one_pager(slug: str, lang: str = _FINAL_LANG):
    """
    Render one language version to self-contained HTML for an inline preview.
    A draft renders even while incomplete (with the DRAFT ribbon) — validation
    gates the PowerPoint export, not looking at the work in progress.
    """
    src = _safe_yaml(slug, lang)
    out_dir = Path(tempfile.mkdtemp(prefix="onepager_preview_"))
    try:
        proc = await _run_async(
            [sys.executable, str(_TOOL_DIR / "render.py"), str(src),
             "--embed", "--allow-incomplete", "--out-dir", str(out_dir)],
            _RENDER_TIMEOUT_S,
        )
        rendered = out_dir / f"{src.stem}_onepager.html"
        if proc.returncode != 0 or not rendered.exists():
            raise HTTPException(status_code=422,
                                detail=(proc.stdout or proc.stderr or "render failed")[-500:])
        return HTMLResponse(rendered.read_text(encoding="utf-8"))
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


@router.get("/{slug}/pptx")
async def download_pptx(slug: str, lang: str = _FINAL_LANG):
    """Export one language version (German by default) to editable PowerPoint."""
    src = _safe_yaml(slug, lang)
    out_dir = Path(tempfile.mkdtemp(prefix="onepager_pptx_"))
    proc = await _run_async(
        [sys.executable, str(_TOOL_DIR / "export_pptx.py"), str(src), "--out-dir", str(out_dir)],
        _RENDER_TIMEOUT_S,
    )
    built = out_dir / f"{src.stem}_onepager.pptx"
    if proc.returncode != 0 or not built.exists():
        shutil.rmtree(out_dir, ignore_errors=True)
        raise HTTPException(status_code=422,
                            detail=(proc.stdout or proc.stderr or "export failed")[-500:])
    # FileResponse streams after this handler returns, so the temp dir is
    # cleaned by a background task rather than a finally block.
    from starlette.background import BackgroundTask
    return FileResponse(
        built, filename=f"{_split_name(src)[0]}_onepager_{lang}.pptx",
        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        background=BackgroundTask(shutil.rmtree, out_dir, ignore_errors=True),
    )


@router.get("/{slug}/yaml")
async def get_yaml(slug: str, lang: str = _FINAL_LANG):
    """Raw YAML, so the draft can be read (and copied out) from the browser."""
    return {"slug": slug, "lang": lang, "yaml": _safe_yaml(slug, lang).read_text(encoding="utf-8")}


def _safe_yaml(slug: str, lang: str = _FINAL_LANG) -> Path:
    """
    Resolve <slug>.<lang>.yaml inside the data dir, refusing anything that
    escapes it. Slugs come from the URL, so path traversal has to be impossible
    here. A German request also finds a pre-two-language <slug>.yaml.
    """
    if lang not in _LANGS:
        raise HTTPException(status_code=422, detail=f"lang must be one of {', '.join(_LANGS)}")
    root = _DATA_DIR.resolve()
    name = Path(slug).name
    candidates = [root / f"{name}.{lang}.yaml"]
    if lang == _FINAL_LANG:
        candidates.append(root / f"{name}.yaml")
    for candidate in candidates:
        candidate = candidate.resolve()
        if os.path.commonpath([candidate, root]) != str(root):
            raise HTTPException(status_code=400, detail="invalid slug")
        if candidate.exists():
            return candidate
    label = "German" if lang == "de" else "English"
    raise HTTPException(status_code=404, detail=f"no {label} version of one-pager {slug!r}")
