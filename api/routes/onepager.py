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
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import List, Optional

import yaml
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

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
        "website": data.get("website") or "",
        "location": data.get("location") or "",
        "founded": data.get("founded") or "",
        "team_size": data.get("team_size") or "",
        "manual": data.get("manual") or {},
        "has_logo": bool(data.get("logo")),
        "research": data.get("research") or None,
        "source_record": data.get("source_record") or None,
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
            "has_deck": (_DECKS_DIR.is_dir() and any(_DECKS_DIR.glob(f"{base}.*")))
                        or bool((lead.get("source_record") or {}).get("id")),
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


_DECKS_DIR = _DATA_DIR / "decks"          # kept by generate.py --save-deck (gitignored)
_MAX_LOGO_BYTES = 5 * 1024 * 1024
_LOGO_SUFFIXES = {".png", ".jpg", ".jpeg", ".svg", ".webp", ".gif"}


# ── One-pagers from the HubDrive database ────────────────────────────────────
# The tool never opens the database (FORMAT.md §7): this side reads the record
# and hands it over as a JSON file, exactly as it hands over an uploaded deck.

def startup_record(startup_id: str) -> dict:
    """The fields a one-pager can use as LEADS — the tool verifies them on the
    web (templates/one_pager/research.py). Founders and tags are not passed:
    they are the fields most often wrong, and a one-pager prints neither."""
    from database.connection import SessionLocal
    from database.models import Startup

    db = SessionLocal()
    try:
        s = db.get(Startup, startup_id)
        if s is None:
            raise HTTPException(status_code=404, detail=f"no startup with id {startup_id}")
        return {
            "id": str(s.id), "name": s.name, "website": s.website,
            "short_description": s.short_description, "description": s.description,
            "industry": s.industry, "business_model": s.business_model,
            "city": s.city, "country": s.country, "address": s.address,
            "founded_year": s.founded_year, "employee_count": s.employee_count,
            "funding_stage": s.funding_stage, "total_funding_usd": s.total_funding_usd,
            "source_url": s.source_url,
        }
    finally:
        db.close()


# How many startups one batch may take. Measured on this Mac mini (M4, 24 GB,
# Oct 2026): research ~10 s + drafting and translating on gemma4:12b ~90 s =
# ~2 min per startup, ONE AT A TIME — running them in parallel would only make
# them fight over the one GPU and the ~8 GB the model needs, while the
# scouting pipeline's own 14B model wants ~9 GB of the same 24. Five is about
# ten minutes of GPU, handed back between startups so ingestion can interleave.
MAX_BATCH = 5
_batch: dict = {}                 # the current or last batch; one at a time
_batch_task = None                # held so the event loop can't garbage-collect it mid-run


class FromDatabase(BaseModel):
    startup_ids: List[str]
    draft_lang: str = _FINAL_LANG
    web_search: bool = True
    paid_search: bool = True
    force: bool = False


def _batch_names(ids: List[str]) -> dict:
    from database.connection import SessionLocal
    from database.models import Startup

    db = SessionLocal()
    try:
        return {str(s.id): s.name for s in db.query(Startup).filter(Startup.id.in_(ids)).all()}
    finally:
        db.close()


@router.get("/batch")
async def batch_status():
    """The current (or last) batch of one-pagers from the database."""
    return {"max_batch": MAX_BATCH, "batch": _batch or None}


@router.post("/from-database")
async def create_from_database(body: FromDatabase):
    """
    Create one-pagers for up to MAX_BATCH startups from the HubDrive database.
    Runs in the background, one startup at a time, each holding the GPU lock
    the scouting pipeline uses — so a batch never fights ingestion for the
    model. Poll GET /onepager/batch for progress.
    """
    ids = list(dict.fromkeys(i.strip() for i in body.startup_ids if i and i.strip()))
    if not ids:
        raise HTTPException(status_code=422, detail="Select at least one startup.")
    if len(ids) > MAX_BATCH:
        raise HTTPException(status_code=422,
                            detail=f"At most {MAX_BATCH} startups per batch — each takes about 2 minutes "
                                   f"of the machine's GPU. Run the rest as a second batch.")
    if body.draft_lang not in _LANGS:
        raise HTTPException(status_code=422, detail=f"draft_lang must be one of {', '.join(_LANGS)}.")
    if _batch and not _batch.get("finished_at"):
        raise HTTPException(status_code=409, detail="A batch is already running — wait for it to finish.")
    names = _batch_names(ids)
    unknown = [i for i in ids if i not in names]
    if unknown:
        raise HTTPException(status_code=404, detail=f"Unknown startup id(s): {', '.join(unknown)}")

    import datetime as _dt
    _batch.clear()
    _batch.update({
        "id": uuid.uuid4().hex[:8],
        "started_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "finished_at": None,
        "options": body.dict(),
        "items": [{"id": i, "name": names[i], "status": "queued", "message": "", "slug": None,
                   "seconds": None, "missing": [], "open_items": None} for i in ids],
    })
    global _batch_task
    _batch_task = asyncio.create_task(_run_batch(dict(_batch["options"])))
    return {"max_batch": MAX_BATCH, "batch": _batch}


async def _run_batch(opts: dict) -> None:
    import datetime as _dt
    import json as _json
    import time as _time
    from processing.scout_controller import scout_controller

    for item in _batch["items"]:
        tmpdir = Path(tempfile.mkdtemp(prefix="onepager_record_"))
        t0 = _time.monotonic()
        try:
            rec = startup_record(item["id"])
            rec_path = tmpdir / "record.json"
            rec_path.write_text(_json.dumps(rec, default=str), encoding="utf-8")
            cmd = [sys.executable, str(_TOOL_DIR / "generate.py"), "--record", str(rec_path),
                   "--draft-lang", opts["draft_lang"]]
            if opts.get("force"):
                cmd.append("--force")
            cmd += _search_flags(opts.get("web_search", True), opts.get("paid_search", True))

            lock = scout_controller.gpu_mutex
            if lock.locked():
                item["status"], item["message"] = "waiting", "Waiting for the GPU — an ingestion run is using it."
            async with lock:
                item["status"], item["message"] = "running", "Researching and drafting…"
                proc = await _run_async(cmd, _GENERATE_TIMEOUT_S)
            out = proc.stdout or ""
            if proc.returncode != 0:
                reason = next((ln for ln in out.splitlines() if ln.startswith("✗")), None) \
                    or (proc.stderr or "").strip()[-300:] or "generation failed"
                reason = reason.lstrip("✗ ").strip()
                if "already exists" in reason:
                    item["status"] = "skipped"
                    item["message"] = "A one-pager for this startup already exists — tick “overwrite” to replace it."
                else:
                    item["status"], item["message"] = "failed", reason
                continue
            written = [Path(ln.split("): ", 1)[1].strip()) for ln in out.splitlines()
                       if ln.startswith("✓ Written (") and "): " in ln]
            item["slug"] = _split_name(written[0])[0] if written else None
            draft = next((d for d in _list_drafts() if d["slug"] == item["slug"]), None)
            v = (draft or {}).get("versions", {}).get(_FINAL_LANG, {})
            item["open_items"] = len(v.get("open_questions") or [])
            item["missing"] = (v.get("research") or {}).get("missing", [])
            item["status"], item["message"] = "done", ""
        except subprocess.TimeoutExpired:
            item["status"], item["message"] = "failed", f"Stopped after {_GENERATE_TIMEOUT_S}s."
        except HTTPException as exc:
            item["status"], item["message"] = "failed", str(exc.detail)
        except Exception as exc:                       # a batch must never die half-way silently
            logger.error(f"[OnePager] batch item {item['name']}: {type(exc).__name__}: {exc}")
            item["status"], item["message"] = "failed", f"{type(exc).__name__}: {exc}"
        finally:
            item["seconds"] = round(_time.monotonic() - t0)
            shutil.rmtree(tmpdir, ignore_errors=True)
    _batch["finished_at"] = _dt.datetime.now().isoformat(timespec="seconds")


def _input_flags(location, founded, team_size, notes) -> list:
    """Your own input -> generate.py flags (only the ones actually given)."""
    flags = []
    for flag, value in (("--location", location), ("--founded", founded),
                        ("--team-size", team_size), ("--notes", notes)):
        if value is not None:
            flags += [flag, str(value)]
    return flags


async def _save_logo(logo: Optional[UploadFile], tmpdir: Path) -> Optional[Path]:
    if logo is None or not (logo.filename or "").strip():
        return None
    suffix = Path(logo.filename).suffix.lower()
    if suffix not in _LOGO_SUFFIXES:
        raise HTTPException(status_code=422,
                            detail=f"'{logo.filename}' is not a supported logo. Use PNG, JPG, SVG or WebP.")
    data = await logo.read()
    if not data:
        return None
    if len(data) > _MAX_LOGO_BYTES:
        raise HTTPException(status_code=413, detail="Logo is larger than 5 MB.")
    path = tmpdir / f"logo{suffix}"
    path.write_bytes(data)
    return path


async def _run_generator(cmd: list, name: str) -> dict:
    """Run generate.py, turn its failures into readable HTTP errors, return the draft."""
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
        # The generator prints a human-readable reason for every hard failure
        # (deck unreadable, legacy .ppt, target exists) — surface it verbatim.
        detail = (proc.stdout or "").strip().splitlines()
        reason = next((ln for ln in detail if ln.startswith("✗")), None) \
            or (proc.stderr or "").strip()[-400:] or "generation failed"
        reason = reason.lstrip("✗ ").strip()
        # The CLI's wording talks about --force, which means nothing to someone
        # clicking a button. Translate that one case.
        if "already exists" in reason:
            reason = (f"A draft for \"{name}\" already exists. "
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


def _search_flags(web_search: bool, paid_search: bool) -> list:
    if not web_search:
        return ["--no-web-search"]
    return [] if paid_search else ["--no-paid-search"]


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
    location: Optional[str] = Form(None),
    founded: Optional[str] = Form(None),
    team_size: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
    logo: Optional[UploadFile] = File(None),
):
    """
    Upload a pitch deck (.pdf or .pptx) and generate a draft one-pager.

    Always writes BOTH languages. `draft_lang` (German by default, the exported
    version) is drafted from the deck; the other is translated from it.

    Your own input — location / founded / team_size, free-text `notes` (extra
    facts and instructions), a `logo` file — beats every other source. Fields
    left empty keep whatever was given on an earlier run. The deck is kept
    (gitignored) so the draft can be regenerated with new input, no re-upload.
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
        # Keep the original file name: it is what the YAML's sources cite.
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(deck.filename).stem)[:80] or "deck"
        deck_path = tmpdir / f"{safe}{suffix}"
        deck_path.write_bytes(payload)
        logo_path = await _save_logo(logo, tmpdir)

        cmd = [sys.executable, str(_TOOL_DIR / "generate.py"),
               "--deck", str(deck_path), "--name", name.strip(), "--draft-lang", draft_lang,
               "--save-deck"]
        if url and url.strip():
            cmd += ["--url", url.strip()]
        if no_llm:
            cmd.append("--no-llm")
        if force:
            cmd.append("--force")
        cmd += _search_flags(web_search, paid_search)
        # Empty fields are not sent: on an overwrite they keep earlier input.
        cmd += _input_flags(*(v.strip() if v and v.strip() else None
                              for v in (location, founded, team_size, notes)))
        if logo_path:
            cmd += ["--logo", str(logo_path)]
        return await _run_generator(cmd, name.strip())
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


@router.post("/{slug}/regenerate")
async def regenerate_one_pager(
    slug: str,
    url: Optional[str] = Form(None),
    draft_lang: str = Form(_FINAL_LANG),
    web_search: bool = Form(True),
    paid_search: bool = Form(True),
    location: Optional[str] = Form(""),
    founded: Optional[str] = Form(""),
    team_size: Optional[str] = Form(""),
    notes: Optional[str] = Form(""),
    logo: Optional[UploadFile] = File(None),
):
    """
    Redraft an existing one-pager from its kept deck with new input. The
    dashboard sends the input panel exactly as shown (prefilled with what was
    given before), so here an EMPTY field clears that input (--manual-replace).
    Overwrites both language files; web searches are usually cached (free).
    """
    src = _safe_yaml(slug, _FINAL_LANG)
    if draft_lang not in _LANGS:
        raise HTTPException(status_code=422, detail=f"draft_lang must be one of {', '.join(_LANGS)}.")
    decks = sorted(_DECKS_DIR.glob(f"{Path(slug).name}.*")) if _DECKS_DIR.is_dir() else []
    data = yaml.safe_load(src.read_text(encoding="utf-8")) or {}
    record_id = (data.get("source_record") or {}).get("id")
    if not decks and not record_id:
        raise HTTPException(
            status_code=409,
            detail=("This one-pager's deck wasn't kept (it was created before regeneration "
                    "existed). Upload the deck again above, with \"overwrite existing draft\" ticked."))
    name = str(data.get("name") or slug)

    tmpdir = Path(tempfile.mkdtemp(prefix="onepager_regen_"))
    try:
        logo_path = await _save_logo(logo, tmpdir)
        if decks:
            source = ["--deck", str(decks[0])]
        else:
            # Built from the database: re-read the record (it may have improved).
            import json as _json
            rec_path = tmpdir / "record.json"
            rec_path.write_text(_json.dumps(startup_record(record_id), default=str), encoding="utf-8")
            source = ["--record", str(rec_path)]
        cmd = [sys.executable, str(_TOOL_DIR / "generate.py"), *source,
               "--name", name, "--draft-lang", draft_lang, "--force", "--manual-replace"]
        website = (url or "").strip() or data.get("website")
        if website:
            cmd += ["--url", str(website)]
        cmd += _search_flags(web_search, paid_search)
        cmd += _input_flags(*((v or "").strip() for v in (location, founded, team_size, notes)))
        if logo_path:
            cmd += ["--logo", str(logo_path)]
        return await _run_generator(cmd, name)
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
