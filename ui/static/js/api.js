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
const post = (p, body) => request("POST", p, { body });
const patch = (p, body) => request("PATCH", p, { body });
const del = (p, params) => request("DELETE", p + qs(params));

export const api = {
  health: () => get("/health"),

  // ── Startups ──────────────────────────────────────────────────────────
  listStartups: (filters) => get("/scout/list", filters),
  getStartup: (id) => get(`/scout/startup/${id}`),
  editStartup: (id, changes) => patch(`/scout/startup/${id}`, changes),
  deleteStartup: (id) => del(`/scout/startup/${id}`, { confirm: "true" }),
  /** Semantic (vector) search — different endpoint + shape from listStartups. */
  semanticSearch: (query, opts = {}) =>
    post("/scout/search", { query, limit: opts.limit ?? 30, ...opts }),

  // ── Reviews ───────────────────────────────────────────────────────────
  listReviews: (filters) => get("/reviews", filters),
  getReview: (id) => get(`/reviews/${id}`),
  approveReview: (id) => post(`/reviews/${id}/approve`),
  rejectReview: (id) => post(`/reviews/${id}/reject`),

  // ── Sources ───────────────────────────────────────────────────────────
  listSources: () => get("/sources"),
  addWebSource: (src) => post("/sources/web", src),
  addRssFeed: (feed) => post("/sources/rss", feed),
  deleteWebSource: (id) => del(`/sources/web/${id}`),

  // ── Ingestion ─────────────────────────────────────────────────────────
  ingestionStatus: (runId) => get("/ingestion/status", runId ? { run_id: runId } : {}),
  runAll: () => post("/ingestion/run-all"),
  runRss: () => post("/ingestion/rss", { max_entries: 50 }),
  runNewsletters: () => post("/ingestion/newsletters"),
  runAccelerators: () => post("/ingestion/scrape-accelerators"),
  runUniversities: () => post("/ingestion/scrape-universities"),
  /** Targeted single-source run -> {status, run_id} */
  runTargeted: (target) => post("/ingestion/targeted", target),
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
