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

# Generous but bounded. A 7B draft over a dense deck runs ~20-60s; the ceiling
# exists so a wedged model can never hold a request open indefinitely.
_GENERATE_TIMEOUT_S = 300
_RENDER_TIMEOUT_S = 120

_MAX_UPLOAD_BYTES = 60 * 1024 * 1024   # a pitch deck well past any realistic size
_ALLOWED_SUFFIXES = {".pdf", ".pptx"}


def _run(cmd: list, timeout: int) -> subprocess.CompletedProcess:
    """Blocking; always called via run_in_executor."""
    return subprocess.run(
        cmd, cwd=str(_REPO_ROOT), capture_output=True, text=True, timeout=timeout,
    )


async def _run_async(cmd: list, timeout: int) -> subprocess.CompletedProcess:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: _run(cmd, timeout))


def _read_draft(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    review = data.get("review") or {}
    return {
        "slug": path.stem,
        "name": data.get("name") or path.stem,
        "claim": data.get("claim") or "",
        "status": review.get("status") or "draft",
        "open_questions": review.get("open_questions") or [],
        "updated_at": path.stat().st_mtime,
    }


@router.get("")
async def list_one_pagers():
    """Every draft on disk, newest first."""
    if not _DATA_DIR.is_dir():
        return {"one_pagers": []}
    out = []
    for p in sorted(_DATA_DIR.glob("*.yaml")):
        try:
            out.append(_read_draft(p))
        except Exception as exc:
            logger.warning(f"[OnePager] could not read {p.name}: {exc}")
            out.append({"slug": p.stem, "name": p.stem, "claim": "",
                        "status": "unreadable", "open_questions": [str(exc)],
                        "updated_at": p.stat().st_mtime})
    out.sort(key=lambda d: d["updated_at"], reverse=True)
    return {"one_pagers": out}


@router.post("/generate")
async def generate_one_pager(
    deck: UploadFile = File(...),
    name: str = Form(...),
    url: Optional[str] = Form(None),
    no_llm: bool = Form(False),
    force: bool = Form(False),
):
    """
    Upload a pitch deck (.pdf or .pptx) and generate a draft one-pager.

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
               "--deck", str(deck_path), "--name", name.strip()]
        if url and url.strip():
            cmd += ["--url", url.strip()]
        if no_llm:
            cmd.append("--no-llm")
        if force:
            cmd.append("--force")

        try:
            proc = await _run_async(cmd, _GENERATE_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            raise HTTPException(
                status_code=504,
                detail=(f"The generator did not finish within {_GENERATE_TIMEOUT_S}s and was "
                        f"stopped. The API itself is unaffected. Try again, or use --no-llm "
                        f"from the terminal if the local model is busy with an ingestion run."),
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
                reason = (f"Ein Entwurf für „{name.strip()}\" existiert bereits. "
                          f"Setze „vorhandenen Entwurf überschreiben\", um ihn zu ersetzen.")
            raise HTTPException(status_code=422, detail=reason)

        # Locate what it wrote. The generator prints the absolute path.
        written = None
        for line in (proc.stdout or "").splitlines():
            if "Geschrieben:" in line:
                written = Path(line.split("Geschrieben:", 1)[1].strip())
                break
        if written is None or not written.exists():
            raise HTTPException(status_code=500,
                                detail="Generator reported success but no YAML was found.")

        return {"status": "ok", "log": proc.stdout, "draft": _read_draft(written)}
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


@router.get("/{slug}/preview", response_class=HTMLResponse)
async def preview_one_pager(slug: str):
    """Render the draft to self-contained HTML for an inline preview."""
    src = _safe_yaml(slug)
    out_dir = Path(tempfile.mkdtemp(prefix="onepager_preview_"))
    try:
        proc = await _run_async(
            [sys.executable, str(_TOOL_DIR / "render.py"), str(src),
             "--embed", "--out-dir", str(out_dir)],
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
async def download_pptx(slug: str):
    """Export to editable PowerPoint and hand it back as a download."""
    src = _safe_yaml(slug)
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
        built, filename=f"{src.stem}_onepager.pptx",
        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        background=BackgroundTask(shutil.rmtree, out_dir, ignore_errors=True),
    )


@router.get("/{slug}/yaml")
async def get_yaml(slug: str):
    """Raw YAML, so the draft can be read (and copied out) from the browser."""
    return {"slug": slug, "yaml": _safe_yaml(slug).read_text(encoding="utf-8")}


def _safe_yaml(slug: str) -> Path:
    """
    Resolve <slug>.yaml inside the data dir, refusing anything that escapes it.
    Slugs come from the URL, so path traversal has to be impossible here.
    """
    candidate = (_DATA_DIR / f"{Path(slug).name}.yaml").resolve()
    if os.path.commonpath([candidate, _DATA_DIR.resolve()]) != str(_DATA_DIR.resolve()):
        raise HTTPException(status_code=400, detail="invalid slug")
    if not candidate.exists():
        raise HTTPException(status_code=404, detail=f"no one-pager named {slug!r}")
    return candidate
