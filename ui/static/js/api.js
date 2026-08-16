/* ══════════════════════════════════════════════════════════════════════════
   SCOUT — API client
   Thin wrapper over the existing FastAPI endpoints. The dashboard is served
   BY that same API (/dashboard), so requests are same-origin: no CORS, no
   base URL config, no auth headers.
   ══════════════════════════════════════════════════════════════════════════ */

const BASE = ""; // same origin

/** Drop null/undefined/empty/"(all)" so we never send noise filters. */
function qs(params = {}) {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === null || v === undefined || v === "" || v === "(all)") continue;
    p.append(k, v);
  }
  const s = p.toString();
  return s ? `?${s}` : "";
}

async function request(method, path, { body, timeout = 30000 } = {}) {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), timeout);
  try {
    const res = await fetch(`${BASE}${path}`, {
      method,
      signal: ctrl.signal,
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!res.ok) {
      // FastAPI puts the reason in {detail: ...}
      let detail = `${res.status} ${res.statusText}`;
      try {
        const j = await res.json();
        if (j.detail) detail = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
      } catch { /* non-JSON error body */ }
      throw new Error(detail);
    }
    return res.status === 204 ? null : res.json();
  } catch (err) {
    if (err.name === "AbortError") throw new Error("Request timed out — is the API running?");
    if (err instanceof TypeError) throw new Error("Cannot reach the backend. Is the API running?");
    throw err;
  } finally {
    clearTimeout(t);
  }
}

const get = (p, params) => request("GET", p + qs(params));
const post = (p, body, opts = {}) => request("POST", p, { body, ...opts });
const patch = (p, body) => request("PATCH", p, { body });
const del = (p, params) => request("DELETE", p + qs(params));

export const api = {
  health: () => get("/health"),

  // ── Startups ──────────────────────────────────────────────────────────
  listStartups: (filters) => get("/scout/list", filters),
  getStartup: (id) => get(`/scout/startup/${id}`),
  /** Distinct source websites actually present in the DB, with counts — powers the Browse source filter. */
  listSourceSites: () => get("/scout/source-sites"),
  editStartup: (id, changes) => patch(`/scout/startup/${id}`, changes),
  /** Phase Q3: bulk Interested/Not Interested marking. status: "interested" | "not_interested" | null (clears back to unset). */
  markInterest: (ids, status) => post("/scout/mark-interest", { ids, status }),
  deleteStartup: (id) => del(`/scout/startup/${id}`, { confirm: "true" }),
  /**
   * Semantic (vector) search — different endpoint + shape from listStartups.
   * Its synthesis step queues on the SAME gpu_mutex as ingestion (see
   * /scout/search's own docstring) — if a scrape/sweep is mid-run, this
   * waits behind the whole thing, not just a moment. Found live 14 Aug 2026:
   * the default 30s client timeout meant search reliably errored out
   * whenever ingestion happened to be running, even though the request was
   * queued fine server-side and would have succeeded. Extended to match the
   * timeout already used for /startup/{id}/web-verify and /compare — the
   * other two endpoints that queue on this same mutex for a single LLM call.
   */
  semanticSearch: (query, opts = {}) =>
    post("/scout/search", { query, limit: opts.limit ?? 30, ...opts }, { timeout: 320000 }),
  /**
   * Phase P-3: on-demand web verification for ONE startup. Runs a live
   * search + a 14B verdict call (100-300s observed) — extended timeout,
   * well past the 30s default. Returns proposed changes for an inline
   * confirm; never writes anything itself (see api.editStartup to apply).
   */
  webVerifyStartup: (id) => post(`/scout/startup/${id}/web-verify`, null, { timeout: 320000 }),
  /** Phase P-4: similar-startup table + AI "which is stronger to suggest" verdict. Same extended timeout as web-verify — one LLM call, 100-300s. */
  compareStartup: (id, limit = 5) => post(`/scout/startup/${id}/compare?limit=${limit}`, null, { timeout: 320000 }),

  // ── Reviews ───────────────────────────────────────────────────────────
  listReviews: (filters) => get("/reviews", filters),
  getReview: (id) => get(`/reviews/${id}`),
  /** Phase Y (5 Aug): the Review Inbox collapsed to one entry per startup. */
  listReviewsGrouped: (filters) => get("/reviews/grouped", filters),
  resolveGroupedReviews: (masterId, selections) =>
    post(`/reviews/grouped/${masterId}/resolve`, { selections }),
  approveReview: (id) => post(`/reviews/${id}/approve`),
  rejectReview: (id) => post(`/reviews/${id}/reject`),
  /** Reverses an approved possible_duplicate/anomaly merge — reinserts the deleted row, leaves the master untouched. */
  undoMerge: (id) => post(`/reviews/${id}/undo-merge`),
  /** Phase Q4: approve/reject a human-selected set of reviews in one call — still 100% human-triggered, just fewer clicks. */
  bulkApproveReviews: (ids) => post("/reviews/bulk-approve", { ids }),
  bulkRejectReviews: (ids) => post("/reviews/bulk-reject", { ids }),
  /** target: "incoming" | "master" | "both" — neither merge nor keep, just remove the data */
  deleteReview: (id, target) => post(`/reviews/${id}/delete?target=${target}`),
  /** Exact SQL-aggregated risk-level breakdown (Phase Z-4) — never disagrees
   *  with the Pending KPI tile the way a capped client-side sample could. */
  reviewCounts: (status = "pending") => get("/reviews/counts", { status }),
  /**
   * Phase Z-4 (12 Aug): act on EVERY review matching a filter, not just a
   * loaded page — the queue-clearing endpoints. Both re-embed/re-score per
   * row they touch, so give them real headroom past the 30s default; always
   * call with dry_run:true first (the default) to get a real count for a
   * confirm dialog before ever passing dry_run:false.
   */
  bulkResolveFiltered: (filters, action, dryRun = true) =>
    post("/reviews/bulk-resolve-filtered", { ...filters, action, dry_run: dryRun }, { timeout: 120000 }),
  bulkResolveGrouped: (filters, dryRun = true) =>
    post("/reviews/grouped/bulk-resolve", { ...filters, dry_run: dryRun }, { timeout: 120000 }),

  // ── Regional register (Phase RC) ──────────────────────────────────────
  /** Established local employers tracked for membership outreach. A separate
   *  dataset from startups — deliberately never mixed into Browse/search. */
  listRegional: (filters) => get("/regional", filters),
  regionalStats: () => get("/regional/stats"),
  regionalFacets: () => get("/regional/facets"),
  /** Background enrichment progress — inferred from the data, so it is
   *  accurate whoever started the run. */
  regionalEnrichmentStatus: () => get("/regional/enrichment-status"),
  getRegional: (id) => get(`/regional/${id}`),
  /** Only the CRM columns are writable; machine-gathered fields carry
   *  citations and are owned by the enrichment pipeline. */
  editRegional: (id, changes) => patch(`/regional/${id}`, changes),
  /** Server-side CSV of the CURRENT filter set (not just the loaded page),
   *  semicolon-delimited + BOM so German Excel opens it with umlauts intact. */
  regionalExportUrl: (filters) => `/regional/export.csv${qs(filters)}`,

  // ── One-pager generator ───────────────────────────────────────────────
  /** Drafts on disk (templates/one_pager/data/*.yaml). */
  listOnePagers: () => get("/onepager"),
  getOnePagerYaml: (slug) => get(`/onepager/${encodeURIComponent(slug)}/yaml`),
  /** Rendered preview + editable PowerPoint are plain URLs (iframe / download). */
  onePagerPreviewUrl: (slug) => `/onepager/${encodeURIComponent(slug)}/preview`,
  onePagerPptxUrl: (slug) => `/onepager/${encodeURIComponent(slug)}/pptx`,
  /**
   * Upload a pitch deck and generate a draft. multipart/form-data, so this
   * bypasses the JSON `request()` helper. The server runs the generator as a
   * subprocess (see api/routes/onepager.py) — a local 7B draft over a dense
   * deck takes ~20-60s, and longer if an ingestion run is holding the GPU,
   * so this gets the same generous ceiling as the other LLM-bound calls.
   */
  async generateOnePager({ file, name, url, noLlm, force }) {
    const form = new FormData();
    form.append("deck", file);
    form.append("name", name);
    if (url) form.append("url", url);
    form.append("no_llm", noLlm ? "true" : "false");
    form.append("force", force ? "true" : "false");

    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), 320000);
    try {
      const res = await fetch("/onepager/generate", { method: "POST", body: form, signal: ctrl.signal });
      if (!res.ok) {
        let detail = `${res.status} ${res.statusText}`;
        try {
          const j = await res.json();
          if (j.detail) detail = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
        } catch { /* non-JSON error body */ }
        throw new Error(detail);
      }
      return res.json();
    } catch (err) {
      if (err.name === "AbortError") throw new Error("Generation timed out — is the local model busy with an ingestion run?");
      throw err;
    } finally {
      clearTimeout(t);
    }
  },

  // ── Sources ───────────────────────────────────────────────────────────
  listSources: () => get("/sources"),
  addWebSource: (src) => post("/sources/web", src),
  addRssFeed: (feed) => post("/sources/rss", feed),
  deleteWebSource: (id) => del(`/sources/web/${id}`),

  // ── Site profiles (Phase R-2 — adaptive pipeline) ───────────────────────
  /** What the structural inspector has learned about each source's page shape. */
  listSiteProfiles: () => get("/sources/profiles"),
  /** Force a fresh probe now, bypassing the cache. Pass {source_id} or {url}. */
  reinspectSource: (target) => post("/sources/profiles/reinspect", target, { timeout: 60000 }),
  pinSiteProfile: (id) => post(`/sources/profiles/${id}/pin`),
  unpinSiteProfile: (id) => post(`/sources/profiles/${id}/unpin`),
  /** "Profile all sources" — fire-and-forget; poll batchProfileStatus. */
  batchProfileSources: () => post("/sources/profiles/batch"),
  batchProfileStatus: () => get("/sources/profiles/batch-status"),

  // ── Ingestion ─────────────────────────────────────────────────────────
  ingestionStatus: (runId) => get("/ingestion/status", runId ? { run_id: runId } : {}),
  runAll: () => post("/ingestion/run-all"),
  runRss: () => post("/ingestion/rss", { max_entries: 50 }),
  /** days: search window — omit for the routine 14-day top-up, or pass e.g. 3650 for a full backfill. */
  runNewsletters: (days) => post(`/ingestion/newsletters${days ? `?days=${days}` : ""}`),
  runAccelerators: () => post("/ingestion/scrape-accelerators"),
  runUniversities: () => post("/ingestion/scrape-universities"),
  /** Targeted single-source run -> {status, run_id} */
  runTargeted: (target) => post("/ingestion/targeted", target),
  /** Cancel the currently-running ingestion (or a specific run_id). */
  stopIngestion: (runId) => post(`/ingestion/stop${runId ? `?run_id=${runId}` : ""}`),

  // ── Verification (Phase H-3 / Phase W) ───────────────────────────────
  verificationStatus: () => get("/verification/status"),
  runRecheck: (limit = 20) => post(`/verification/recheck?limit=${limit}`),
  runWebVerify: (limit = 15) => post(`/verification/web-verify?limit=${limit}`),
  /** Phase Q2: run recheck/web-verify on an explicit human-selected set of startups from Browse (fire-and-forget; find the run via /ingestion/status). */
  recheckSelected: (ids) => post("/verification/recheck-selected", { ids }),
  webVerifySelected: (ids) => post("/verification/web-verify-selected", { ids }),

  // ── Classification (Phase V-2) ────────────────────────────────────────
  classificationStatus: () => get("/classification/status"),
  runReclassify: (limit = 20) => post(`/classification/reclassify?limit=${limit}`),

  // ── Theses (Phase V-3 / V-4) ──────────────────────────────────────────
  listTheses: () => get("/theses"),
  thesesTaxonomy: () => get("/theses/taxonomy"),
  addThesis: (thesis) => post("/theses", thesis),
  deleteThesis: (id) => del(`/theses/${id}`),
};

/* ── Formatting helpers (shared by all views) ─────────────────────────── */

export const fmt = {
  /** "2026-07-17T13:04:22" -> "17 Jul, 13:04" */
  dateTime(v) {
    if (!v) return "—";
    const d = new Date(v.endsWith?.("Z") ? v : v + "Z");
    if (isNaN(d)) return "—";
    return d.toLocaleString(undefined, {
      day: "numeric", month: "short", hour: "2-digit", minute: "2-digit",
    });
  },
  date(v) {
    if (!v) return "—";
    const d = new Date(v.endsWith?.("Z") ? v : v + "Z");
    return isNaN(d) ? "—" : d.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
  },
  /** Seconds since an ISO timestamp -> "1m 12s" (for live elapsed). */
  elapsed(since) {
    if (!since) return "—";
    const start = new Date(since.endsWith?.("Z") ? since : since + "Z");
    const s = Math.max(0, Math.floor((Date.now() - start.getTime()) / 1000));
    return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`;
  },
  num: (n) => (n === null || n === undefined ? "—" : Number(n).toLocaleString()),
  text: (v, fallback = "—") => (v === null || v === undefined || v === "" ? fallback : String(v)),
};

/** Escape untrusted strings before injecting into innerHTML. Optional fallback
 *  text (itself escaped) is used when v is null/undefined/empty string. */
export function esc(v, fallback = "") {
  const s = (v === null || v === undefined || v === "") ? fallback : String(v);
  return s
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}
