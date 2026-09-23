/* ══════════════════════════════════════════════════════════════════════════
   SCOUT — Browse & Search
   Keyword + semantic search, filters, sortable table, and an inline detail
   panel (profile, provenance timeline, score breakdown, edit, delete).
   Preserves every Streamlit Browse capability, plus semantic search toggle
   and CSV export.
   ══════════════════════════════════════════════════════════════════════════ */

import { api, fmt, esc } from "../api.js";
import { toast, confirmAction, navigate, recordBatch } from "../router.js";

/* The column catalogue (Phase 1, 23 Sep 2026).
   Every column the list endpoint can fill, with the ones shown by default
   flagged. Name is not listed because it is always first and never optional —
   a row you cannot identify is not a row.

   `sortable` is true only where the BACKEND can order by that key. Marking a
   column sortable that the API does not understand puts an arrow on the header
   and silently sorts by date, which is exactly the bug fixed earlier today. */
const ALL_COLUMNS = [
  ["short_description", "What they do", false, false],
  ["city",              "Location",     true,  true],
  ["industry",          "Industry",     true,  true],
  ["sub_industry",      "Sub-industry", true,  false],
  ["tech_cluster",      "Cluster",      true,  false],
  ["enrichment_score",  "Score",        true,  true],
  ["verification_status", "State",      true,  true],
  ["interest_status",   "Interest",     true,  false],
  ["funding_stage",     "Stage",        true,  false],
  ["employee_count",    "Employees",    true,  false],
  ["founded_year",      "Founded",      true,  false],
  ["business_model",    "Model",        true,  false],
  ["total_funding_usd", "Funding",      false, false],
  ["tags",              "Tags",         false, false],
  ["website",           "Website",      false, false],
  ["source_url",        "Source",       false, false],
];

const COLUMN_STORAGE_KEY = "browse.columns.v1";

function defaultColumnKeys() {
  return ALL_COLUMNS.filter(([, , , on]) => on).map(([k]) => k);
}

function loadColumnKeys() {
  try {
    const saved = JSON.parse(localStorage.getItem(COLUMN_STORAGE_KEY) || "null");
    if (!Array.isArray(saved) || !saved.length) return defaultColumnKeys();
    // Drop anything no longer in the catalogue, so removing a column in code
    // cannot leave a saved preference rendering a blank stripe forever.
    const known = new Set(ALL_COLUMNS.map(([k]) => k));
    const kept = saved.filter((k) => known.has(k));
    return kept.length ? kept : defaultColumnKeys();
  } catch { return defaultColumnKeys(); }
}

function saveColumnKeys(keys) {
  try { localStorage.setItem(COLUMN_STORAGE_KEY, JSON.stringify(keys)); } catch { /* private mode */ }
}

/* One renderer per column. Keeping them here rather than inline in the row
   template is what makes the set configurable at all — the row builds itself
   from whichever keys are active. */
const CELL = {
  short_description: (s) => `<span class="dim">${esc(s.short_description, "—")}</span>`,
  city:              (s) => `<span class="dim">${esc([s.city, s.country].filter(Boolean).join(", "), "—")}</span>`,
  industry:          (s) => `<span class="dim">${esc(s.industry, "—")}</span>`,
  sub_industry:      (s) => `<span class="dim">${esc(s.sub_industry, "—")}</span>`,
  tech_cluster:      (s) => `<span class="dim">${esc(s.tech_cluster, "—")}</span>`,
  enrichment_score:  (s) => `<span class="mono score-n">${s.enrichment_score ?? "—"}</span>${s.score_tier ? ` <span class="chip ${tierChipClass(s.score_tier)}">${esc(s.score_tier.replace(/_/g, " ").toLowerCase())}</span>` : ""}`,
  verification_status: (s) => verificationBadge(s.verification_status),
  interest_status:   (s) => s.interest_status ? interestBadge(s.interest_status) : `<span class="dim">—</span>`,
  funding_stage:     (s) => `<span class="dim">${esc(s.funding_stage, "—")}</span>`,
  employee_count:    (s) => `<span class="dim">${esc(s.employee_count, "—")}</span>`,
  founded_year:      (s) => `<span class="dim">${esc(s.founded_year, "—")}</span>`,
  business_model:    (s) => `<span class="dim">${esc(s.business_model, "—")}${s.is_gmbh ? " · GmbH" : ""}</span>`,
  // fmt has no money helper, and adding one for a single column is more
  // surface than it earns. Compact and local to the renderer.
  total_funding_usd: (s) => {
    const v = s.total_funding_usd;
    if (!v) return `<span class="dim">—</span>`;
    const m = v >= 1e9 ? `${(v / 1e9).toFixed(1)}B` : v >= 1e6 ? `${(v / 1e6).toFixed(1)}M`
            : v >= 1e3 ? `${(v / 1e3).toFixed(0)}k` : String(v);
    return `<span class="mono dim">$${m}</span>`;
  },
  tags:              (s) => (s.tags || []).length ? (s.tags || []).slice(0, 3).map((t) => `<span class="chip">${esc(t)}</span>`).join(" ") : `<span class="dim">—</span>`,
  website:           (s) => s.website ? `<a href="${esc(s.website)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">${esc(s.website.replace(/^https?:\/\/(www\.)?/, ""))}</a>` : `<span class="dim">—</span>`,
  source_url:        (s) => s.source_url
    ? `<a href="${esc(s.source_url)}" target="_blank" rel="noopener" title="${esc(s.source_url)}" onclick="event.stopPropagation()">${esc(sourceLabel(s.source_url, s.source))}</a>`
    : `<span class="dim">${esc(sourceLabel(s.source_url, s.source))}</span>`,
};

/* Phase V-3: only shown while a thesis is selected — relevance sort takes
   over from the normal `sort`/`order` columns, so it's not clickable-sortable
   like the rest (the backend already returns it pre-ranked). */
const RELEVANCE_COLUMN = ["relevance_score", "Relevant to", false];

/** "https://www.munich-startup.de/en/x" -> "munich-startup.de" (falls back to the coarse source type). */
/* One-liner on hover (23 Sep 2026).
   Rows are single-height so the eye can run down the column without the
   rhythm breaking; what a company does lives in a tooltip instead.

   The tooltip is ONE element on <body>, moved on hover, not one per row.
   .table-wrap sets overflow-x:auto, which makes overflow-y compute to auto
   too — a tooltip positioned inside it would be clipped exactly at the row
   edge where it needs to appear. pointer-events:none so it can never swallow
   the row click that opens the detail panel. */
let _rowTip = null;

function attachRowTooltip(wrap) {
  if (!_rowTip) {
    _rowTip = document.createElement("div");
    _rowTip.className = "row-tip";
    document.body.appendChild(_rowTip);
  }
  const hide = () => _rowTip.classList.remove("is-on");

  wrap.querySelectorAll("tbody tr[data-tip]").forEach((tr) => {
    const text = tr.dataset.tip;
    if (!text) return;
    tr.addEventListener("mouseenter", () => {
      _rowTip.textContent = text;
      _rowTip.classList.add("is-on");
      const r = tr.getBoundingClientRect();
      // Measure after showing, so a long one-liner flips above the row
      // instead of running off the bottom of the window.
      const h = _rowTip.offsetHeight;
      const below = r.bottom + 6;
      _rowTip.style.top = (below + h > window.innerHeight ? r.top - h - 6 : below) + "px";
      _rowTip.style.left = Math.min(r.left + 34, window.innerWidth - _rowTip.offsetWidth - 14) + "px";
    });
    tr.addEventListener("mouseleave", hide);
  });
  wrap.addEventListener("scroll", hide, { passive: true });
}

/* The change timeline (Phase 2).
   Fetched separately from the record itself: it is the one part of the panel
   that can be slow on a much-recrawled company, and nobody should wait on it
   to read the profile. An empty history is stated plainly rather than hidden,
   because "nothing has changed" and "we weren't recording yet" look identical
   in an empty list and mean different things. */
const CHANGE_SOURCE_LABEL = {
  crawl: "crawl", newsletter: "newsletter", rss: "RSS", review: "approved review",
  merge: "merge", undo: "undo", manual: "manual edit", web_verify: "web check",
  system: "unattributed",
};

async function loadHistory(cell, id) {
  const body = cell.querySelector("#history-body");
  if (!body) return;
  let rows;
  try { rows = (await api.startupHistory(id, 60)).history; }
  catch (err) { body.innerHTML = `<div class="dim" style="font-size:12px">Couldn't load history (${esc(err.message)})</div>`; return; }

  if (!rows.length) {
    body.innerHTML = `<div class="dim" style="font-size:12px">
      No changes recorded. Field history started on 23 Sep 2026 — anything before that
      wasn't kept, so an older record shows nothing here until it next changes.</div>`;
    return;
  }

  body.innerHTML = `<div class="stack" style="gap:9px">
    ${rows.map((h) => `
      <div style="font-size:12px;border-left:2px solid var(--border);padding-left:9px">
        <div class="row" style="gap:6px;align-items:baseline">
          <strong style="font-size:11.5px">${esc(h.field.replace(/_/g, " "))}</strong>
          <span class="dim" style="font-size:11px">${esc(CHANGE_SOURCE_LABEL[h.source] || h.source)}</span>
          <span class="grow"></span>
          <span class="dim" style="font-size:11px">${fmt.dateTime(h.changed_at)}</span>
        </div>
        <div style="margin-top:2px;line-height:1.5">
          ${h.old ? `<span class="dim" style="text-decoration:line-through">${esc(h.old)}</span> ` : `<span class="dim">(empty)</span> `}
          <span style="color:var(--text-dim)">→</span>
          ${h.new ? ` ${esc(h.new)}` : ` <span class="dim">(cleared)</span>`}
        </div>
        ${h.detail ? `<div class="dim truncate" style="font-size:10.5px;margin-top:2px">${esc(h.detail)}</div>` : ""}
      </div>`).join("")}
  </div>`;
}

function sourceLabel(sourceUrl, source) {
  if (sourceUrl) {
    try {
      const host = new URL(sourceUrl).hostname;
      return host.startsWith("www.") ? host.slice(4) : host;
    } catch { /* not a valid URL */ }
  }
  return source || "—";
}

/* Phase H-3: trust-state badge — unverified (neutral) / verified (lime) /
   flagged (red), so a wrong-data record is visible right in the table,
   not just in the detail drawer. */
function verificationBadge(status) {
  const s = status || "unverified";
  const cls = s === "verified" ? "chip--brand" : s === "flagged" ? "chip--danger" : "";
  const label = s === "verified" ? "✓ verified" : s === "flagged" ? "🚩 flagged" : "⚠ unverified";
  return `<span class="chip ${cls}">${label}</span>`;
}

/* Phase Q3: manual Interested/Not Interested badge — distinct from the
   verification badge (that's about data trust; this is a human's business
   judgment). Unset renders as a dim dash, never hidden. */
function interestBadge(status) {
  if (status === "interested") return `<span class="chip chip--brand">👍 interested</span>`;
  if (status === "not_interested") return `<span class="chip">👎 not interested</span>`;
  return `<span class="dim">—</span>`;
}

const EDITABLE_FIELDS = [
  ["name", "Name", "text"], ["website", "Website", "text"],
  ["short_description", "One-liner", "text"], ["description", "Description", "textarea"],
  ["industry", "Industry", "text"], ["tech_cluster", "Tech cluster", "text"],
  ["funding_stage", "Funding stage", "text"], ["city", "City", "text"],
  ["country", "Country", "text"], ["address", "Address", "text"],
  ["employee_count", "Employees", "text"], ["founded_year", "Founded year", "number"],
  ["contact_info", "Contact", "text"], ["linkedin", "LinkedIn", "text"],
];

function debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

function tierChipClass(tier) {
  if (tier === "PRIORITY" || tier === "HIGH_QUALITY_LEAD") return "chip--brand";
  if (tier === "WEAK_SIGNAL") return "";
  return "";
}

function csvEscape(v) {
  const s = v === null || v === undefined ? "" : String(v);
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

function downloadCsv(rows) {
  if (!rows.length) { toast("Nothing to export", "error"); return; }
  const cols = ["name", "industry", "tech_cluster", "country", "city", "funding_stage",
                "employee_count", "score_tier", "enrichment_score", "source", "source_url", "verification_status"];
  const lines = [cols.join(",")];
  for (const r of rows) lines.push(cols.map((c) => csvEscape(r[c])).join(","));
  const blob = new Blob([lines.join("\n")], { type: "text/csv" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `scout-startups-${new Date().toISOString().slice(0, 10)}.csv`;
  a.click();
  URL.revokeObjectURL(a.href);
}

export default {
  title: "Browse & Search",

  mount(el) {
    const state = {
      mode: "keyword",           // "keyword" | "semantic"
      columns: loadColumnKeys(),  // Phase 1: which columns are shown, per browser
      q: "",
      filters: { industry: "", country: "", city: "", tech_cluster: "", funding_stage: "", score_tier: "", employee_count: "", verification_status: "", source_url: "", interest_status: "", business_model: "", is_gmbh: "" },
      thesis: "",           // Phase V-3: selected thesis id — "" means normal sort/order browsing
      priorityFirst: false, // Phase P-1: sort=priority — startups matching a priority thesis (e.g. SÜDPACK) first
      sort: "name", order: "asc",
      limit: 50, offset: 0,
      expandedId: null,
      lastRows: [], lastTotal: 0,
      aiAnalysis: null,
      sourceSites: null,   // [{label, count}] — fetched once, populates the source-website filter
      theses: null,        // [{id, name, kind, summary}] — fetched once, populates the "Relevant to" dropdown
      selectedIds: new Set(), // Phase Q2/Q3: row-checkbox selection, shared by bulk verify/recheck + bulk interest marking
    };

    el.innerHTML = `
      <div class="stack">
        <div class="card" id="search-card"></div>
        <div id="selection-toolbar"></div>
        <div id="results-region"></div>
      </div>`;

    const searchCard = el.querySelector("#search-card");
    const selectionToolbar = el.querySelector("#selection-toolbar");
    const resultsRegion = el.querySelector("#results-region");

    /* Phase Q2/Q3: shared bulk-action toolbar — appears only when ≥1 row is
       selected. Q3's mark-interest buttons are wired here now; Q2's bulk
       verify/recheck buttons slot into the same toolbar. */
    function renderSelectionToolbar() {
      const n = state.selectedIds.size;
      if (!n) { selectionToolbar.innerHTML = ""; return; }
      selectionToolbar.innerHTML = `
        <div class="card row wrap" style="gap:10px;align-items:center;background:var(--surface-2)">
          <strong style="font-size:13px">${n} selected</strong>
          <button class="btn btn--sm" id="sel-mark-interested">👍 Mark Interested</button>
          <button class="btn btn--sm" id="sel-mark-not-interested">👎 Mark Not Interested</button>
          <button class="btn btn--ghost btn--sm" id="sel-clear-interest" title="Clear interest marking back to unset">— Clear marking</button>
          <span style="width:1px;height:20px;background:var(--border)"></span>
          <button class="btn btn--sm" id="sel-recheck" title="Re-run the deterministic + AI verification pass on exactly these startups, even if already verified">🔁 Recheck selected</button>
          <button class="btn btn--sm" id="sel-web-verify" title="Search the live web to verify/correct these startups — results stage in the Review Inbox, tagged to this batch">🌐 Web-verify selected</button>
          <span class="grow"></span>
          <button class="btn btn--ghost btn--sm" id="sel-clear">Clear selection</button>
        </div>`;

      const markInterest = async (status) => {
        const ids = [...state.selectedIds];
        try {
          const res = await api.markInterest(ids, status);
          toast(`Marked ${res.updated} startup${res.updated === 1 ? "" : "s"}`);
          state.selectedIds.clear();
          load();
        } catch (err) {
          toast(`Mark failed: ${err.message}`, "error");
        }
      };
      selectionToolbar.querySelector("#sel-mark-interested").addEventListener("click", () => markInterest("interested"));
      selectionToolbar.querySelector("#sel-mark-not-interested").addEventListener("click", () => markInterest("not_interested"));
      selectionToolbar.querySelector("#sel-clear-interest").addEventListener("click", () => markInterest(null));
      selectionToolbar.querySelector("#sel-recheck").addEventListener("click", () => runSelectedVerification("recheck"));
      selectionToolbar.querySelector("#sel-web-verify").addEventListener("click", () => runSelectedVerification("web-verify"));
      selectionToolbar.querySelector("#sel-clear").addEventListener("click", () => {
        state.selectedIds.clear();
        renderResults();
      });
    }

    /* Phase Q2: bulk verify/recheck on the human-selected set. Both trigger
       endpoints are fire-and-forget (queue on the GPU mutex, no synchronous
       run_id) — the SAME convention every other trigger in this dashboard
       already follows, so we discover the real run_id by polling
       /ingestion/status right after triggering, matching current_run.kind.
       Once found it's stashed via recordBatch() so the Review Inbox can
       offer it as a "recent batch" to filter by (see reviews.js). */
    async function runSelectedVerification(kind) {
      const ids = [...state.selectedIds];
      const n = ids.length;
      const isWebVerify = kind === "web-verify";
      const runKind = isWebVerify ? "web_verify_selected" : "recheck_selected";
      const label = isWebVerify ? "Web-verify" : "Recheck";

      try {
        await (isWebVerify ? api.webVerifySelected(ids) : api.recheckSelected(ids));
      } catch (err) {
        toast(`${label} failed to start: ${err.message}`, "error");
        return;
      }
      toast(`${label} queued for ${n} startup${n === 1 ? "" : "s"} — finding its batch id…`);
      state.selectedIds.clear();
      renderResults();

      // Poll briefly for the run to appear as current_run — it may already
      // be queued behind another GPU-mutex job, so tolerate a short wait.
      let runId = null;
      for (let i = 0; i < 10 && !runId; i++) {
        await new Promise((r) => setTimeout(r, 1000));
        try {
          const status = await api.ingestionStatus();
          if (status.current_run?.kind === runKind) runId = status.current_run.run_id;
        } catch { /* transient — keep trying */ }
      }

      if (runId) {
        recordBatch({ run_id: runId, kind: runKind, label, count: n });
        toast(`${label} batch queued (${n} startup${n === 1 ? "" : "s"}) — click to watch it in the Review Inbox`, "ok", () => {
          navigate(`#/reviews?run_id=${runId}`);
        });
      } else {
        toast(`${label} is running — check the Ingestion page for progress, then filter the Review Inbox by batch once it completes`);
      }
    }

    function buildSearchCard() {
      searchCard.innerHTML = `
        <div class="row wrap" style="gap:10px">
          <div class="row" style="background:var(--surface-2);border-radius:var(--radius-sm);padding:2px;flex:none">
            <button class="btn btn--sm ${state.mode === "keyword" ? "btn--primary" : "btn--ghost"}" data-mode="keyword">Keyword</button>
            <button class="btn btn--sm ${state.mode === "semantic" ? "btn--primary" : "btn--ghost"}" data-mode="semantic">Semantic (AI)</button>
          </div>
          <input class="input grow" id="q-input" placeholder="${state.mode === "semantic"
            ? "Describe what you're looking for — e.g. 'climate startups in Munich raising seed'"
            : "Search name, summary, description, tags…"}" value="${esc(state.q)}" style="min-width:240px">
          ${state.mode === "semantic" ? `<button class="btn btn--primary" id="semantic-go">Search</button>` : ""}
          <button class="btn" id="export-csv">⬇ Export CSV</button>
        </div>
        ${state.mode === "keyword" ? `
          <div class="row wrap" style="gap:8px;margin-top:10px">
            <select class="select" style="max-width:200px" id="f-thesis" title="Rank &amp; filter by relevance to a stakeholder's interests or an ad-hoc theme">
              <option value="">${state.theses ? "Relevant to…" : "Loading theses…"}</option>
              ${(state.theses || []).map((t) =>
                `<option value="${esc(t.id)}" ${state.thesis === t.id ? "selected" : ""}>${esc(t.name)}</option>`).join("")}
            </select>
            ${!state.thesis ? `
              <button class="btn btn--sm ${state.priorityFirst ? "btn--primary" : "btn--ghost"}" id="priority-toggle"
                title="Sort by priority signals first: a stakeholder thesis match (e.g. SÜDPACK's packaging focus), B2B, and GmbH each count — more matches ranks higher">
                ⭐ Priority first
              </button>` : ""}
            <input class="input" style="max-width:150px" id="f-industry" placeholder="Industry" value="${esc(state.filters.industry)}">
            <input class="input" style="max-width:130px" id="f-country" placeholder="Country" value="${esc(state.filters.country)}">
            <input class="input" style="max-width:130px" id="f-city" placeholder="City" value="${esc(state.filters.city)}">
            <input class="input" style="max-width:150px" id="f-tech_cluster" placeholder="Tech cluster" value="${esc(state.filters.tech_cluster)}">
            <input class="input" style="max-width:140px" id="f-funding_stage" placeholder="Funding stage" value="${esc(state.filters.funding_stage)}">
            <select class="select" style="max-width:170px" id="f-score_tier">
              <option value="">All tiers</option>
              ${["PRIORITY", "HIGH_QUALITY_LEAD", "INTERESTING", "EARLY_DISCOVERY", "WEAK_SIGNAL"].map((t) =>
                `<option value="${t}" ${state.filters.score_tier === t ? "selected" : ""}>${t.replace(/_/g, " ")}</option>`).join("")}
            </select>
            <input class="input" style="max-width:110px" id="f-employee_count" placeholder="Employees" value="${esc(state.filters.employee_count)}">
            <select class="select" style="max-width:150px" id="f-verification_status">
              <option value="">Any verification</option>
              ${["unverified", "verified", "flagged"].map((s) =>
                `<option value="${s}" ${state.filters.verification_status === s ? "selected" : ""}>${s}</option>`).join("")}
            </select>
            <select class="select" style="max-width:150px" id="f-interest_status" title="Manual Interested/Not Interested marking">
              <option value="">Any interest</option>
              ${[["interested", "👍 Interested"], ["not_interested", "👎 Not interested"], ["unset", "— Unmarked"]].map(([v, label]) =>
                `<option value="${v}" ${state.filters.interest_status === v ? "selected" : ""}>${label}</option>`).join("")}
            </select>
            <select class="select" style="max-width:130px" id="f-business_model" title="B2B is the stated scouting priority">
              <option value="">Any model</option>
              ${["B2B", "B2C", "B2B2C", "Unclear"].map((v) =>
                `<option value="${v}" ${state.filters.business_model === v ? "selected" : ""}>${v}</option>`).join("")}
            </select>
            <select class="select" style="max-width:130px" id="f-is_gmbh" title="GmbH is the stated scouting priority">
              <option value="">Any legal form</option>
              <option value="true" ${state.filters.is_gmbh === "true" ? "selected" : ""}>🏢 GmbH only</option>
              <option value="false" ${state.filters.is_gmbh === "false" ? "selected" : ""}>Non-GmbH only</option>
            </select>
            <select class="select" style="max-width:200px" id="f-source_url" title="Filter to startups extracted from one source website — useful for a manual verification pass, site by site">
              <option value="">${state.sourceSites ? "All source websites" : "Loading sources…"}</option>
              ${(state.sourceSites || []).map((s) =>
                `<option value="${esc(s.label)}" ${state.filters.source_url === s.label ? "selected" : ""}>${esc(s.label)} (${s.count})</option>`).join("")}
            </select>
            ${Object.values(state.filters).some(Boolean) || state.thesis || state.priorityFirst ? `<button class="btn btn--ghost btn--sm" id="clear-filters">Clear filters</button>` : ""}
          </div>` : ""}`;

      searchCard.querySelectorAll("[data-mode]").forEach((btn) =>
        btn.addEventListener("click", () => {
          state.mode = btn.dataset.mode;
          state.aiAnalysis = null;
          buildSearchCard();
          if (state.mode === "keyword") load();
          else resultsRegion.innerHTML = `<div class="empty">Type a query above and press Search</div>`;
        }));

      const qInput = searchCard.querySelector("#q-input");
      qInput.addEventListener("input", debounce(() => {
        state.q = qInput.value;
        if (state.mode === "keyword") { state.offset = 0; load(); }
      }, 300));
      qInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && state.mode === "semantic") runSemantic();
      });
      searchCard.querySelector("#semantic-go")?.addEventListener("click", runSemantic);
      searchCard.querySelector("#export-csv").addEventListener("click", () => downloadCsv(state.lastRows));

      if (state.mode === "keyword") {
        for (const key of Object.keys(state.filters)) {
          const input = searchCard.querySelector(`#f-${key}`);
          if (!input) continue;
          const evt = input.tagName === "SELECT" ? "change" : "input";
          input.addEventListener(evt, debounce(() => {
            state.filters[key] = input.value;
            state.offset = 0;
            load();
          }, evt === "input" ? 300 : 0));
        }
        searchCard.querySelector("#f-thesis")?.addEventListener("change", (e) => {
          state.thesis = e.target.value;
          if (state.thesis) state.priorityFirst = false; // thesis ranking already surfaces priority via priority_match
          state.offset = 0;
          buildSearchCard();
          load();
        });
        searchCard.querySelector("#priority-toggle")?.addEventListener("click", () => {
          state.priorityFirst = !state.priorityFirst;
          state.offset = 0;
          buildSearchCard();
          load();
        });
        searchCard.querySelector("#clear-filters")?.addEventListener("click", () => {
          for (const k in state.filters) state.filters[k] = "";
          state.thesis = "";
          state.priorityFirst = false;
          state.offset = 0;
          buildSearchCard();
          load();
        });
      }
    }

    async function runSemantic() {
      const q = searchCard.querySelector("#q-input").value.trim();
      if (!q) { toast("Enter a query first", "error"); return; }

      // Two phases, because the two halves of this search cost wildly
      // different amounts. The vector lookup is ~60ms; the AI report is a
      // 14B call that queues on the same GPU mutex as ingestion, so during a
      // sweep it genuinely takes minutes. Previously both were one request,
      // which meant the RESULTS waited on the REPORT and a mid-sweep search
      // looked broken for minutes while working perfectly. Now the matches
      // paint immediately and the report fills in behind them.
      state.aiAnalysis = null;
      resultsRegion.innerHTML = `<div class="table-wrap"><div class="skeleton" style="height:240px"></div></div>`;

      let rows;
      try {
        const fast = await api.semanticSearch(q, {
          limit: 30, synthesize: false, searchTimeout: 45000,
        });
        rows = (fast.startups || []).map((s) => ({ ...s, id: s.id }));
        state.lastRows = rows;
        state.lastTotal = fast.total_found ?? rows.length;
        renderResults();
      } catch (err) {
        resultsRegion.innerHTML = `<div class="empty"><div class="empty__title">Search failed</div><div>${esc(err.message)}</div></div>`;
        return;
      }

      if (!rows.length) return;   // nothing to write a report about

      // Phase 2. The wait note still matters — it's now the only thing the
      // user is actually waiting for, and it says whether the queue is the
      // reason rather than leaving someone guessing.
      let waitNote = "this can take up to a minute…";
      try {
        const st = await api.ingestionStatus();
        if (st.current_run) waitNote = "an ingestion run is in progress, so this may take several minutes — it's queued, not stuck…";
      } catch { /* status check is best-effort; fall back to the default note */ }

      const pending = document.createElement("div");
      pending.className = "card";
      pending.style.marginBottom = "var(--gap)";
      pending.innerHTML = `<div class="row" style="gap:10px;padding:6px 0">
        <span class="spinner"></span>
        <span class="dim">Writing the AI analysis — ${waitNote}</span></div>`;
      resultsRegion.prepend(pending);

      try {
        const full = await api.semanticSearch(q, { limit: 30 });
        // Ignore a late response if the user has since searched for
        // something else — otherwise a slow report lands on new results.
        if (searchCard.querySelector("#q-input").value.trim() !== q) return;
        state.aiAnalysis = full.ai_analysis;
        renderResults();
      } catch (err) {
        pending.innerHTML = `<div class="dim" style="padding:6px 0">
          AI analysis unavailable (${esc(err.message)}) — the ${rows.length} matches above are unaffected.</div>`;
      }
    }

    async function load() {
      resultsRegion.innerHTML = `<div class="table-wrap"><div class="skeleton" style="height:300px"></div></div>`;
      try {
        const filters = Object.fromEntries(Object.entries(state.filters).filter(([, v]) => v));
        const res = await api.listStartups({
          q: state.q || undefined, ...filters,
          thesis: state.thesis || undefined,
          sort: state.priorityFirst ? "priority" : state.sort, order: state.order,
          limit: state.limit, offset: state.offset,
        });
        state.lastRows = res.startups || [];
        state.lastTotal = res.total ?? 0;
        renderResults();
      } catch (err) {
        resultsRegion.innerHTML = `<div class="empty"><div class="empty__title">Couldn't load startups</div><div>${esc(err.message)}</div></div>`;
      }
    }


    /* Column picker (Phase 1).
       Checkboxes only — no drag-to-reorder. Columns render in catalogue order
       whatever order they were ticked, which keeps the layout stable and
       predictable; adding and removing is what people actually want, and
       reordering is a much larger interaction for much less benefit.

       Name is deliberately absent from the list: it is always first and never
       optional, because a row you cannot identify is not a row. */
    function buildColumnBar() {
      const bar = document.createElement("div");
      bar.className = "row";
      bar.style.cssText = "gap:8px;align-items:center;margin-bottom:8px";
      const chosen = new Set(state.columns);
      bar.innerHTML = `
        <span class="grow"></span>
        <div style="position:relative">
          <button class="btn btn--ghost btn--sm" id="col-toggle">⚙ Columns (${chosen.size})</button>
          <div id="col-menu" class="card hidden"
               style="position:absolute;right:0;top:calc(100% + 5px);z-index:40;min-width:230px;
                      padding:10px;max-height:340px;overflow:auto;box-shadow:0 8px 26px rgba(0,0,0,.35)">
            <div class="stack" style="gap:5px">
              ${ALL_COLUMNS.map(([key, label]) => `
                <label class="row" style="gap:7px;cursor:pointer;font-size:12.5px">
                  <input type="checkbox" data-col="${esc(key)}" ${chosen.has(key) ? "checked" : ""}>
                  <span>${esc(label)}</span>
                </label>`).join("")}
            </div>
            <div class="row" style="gap:6px;margin-top:9px;padding-top:9px;border-top:1px solid var(--border)">
              <button class="btn btn--ghost btn--sm" id="col-reset">Reset to default</button>
            </div>
          </div>
        </div>`;

      const menu = bar.querySelector("#col-menu");
      bar.querySelector("#col-toggle").addEventListener("click", (e) => {
        e.stopPropagation();
        menu.classList.toggle("hidden");
      });
      menu.addEventListener("click", (e) => e.stopPropagation());
      // Close on an outside click, once — re-registered each render, so it is
      // removed with the element rather than accumulating listeners.
      document.addEventListener("click", () => menu.classList.add("hidden"), { once: true });

      menu.querySelectorAll("input[data-col]").forEach((cb) =>
        cb.addEventListener("change", () => {
          const keys = [...menu.querySelectorAll("input[data-col]:checked")].map((x) => x.dataset.col);
          if (!keys.length) {
            toast("Keep at least one column besides the name", "error");
            cb.checked = true;
            return;
          }
          state.columns = keys;
          saveColumnKeys(keys);
          renderResults();
        }));
      menu.querySelector("#col-reset").addEventListener("click", () => {
        state.columns = defaultColumnKeys();
        saveColumnKeys(state.columns);
        renderResults();
      });
      return bar;
    }

    function renderResults() {
      resultsRegion.innerHTML = "";

      if (state.aiAnalysis) {
        const aiCard = document.createElement("div");
        aiCard.className = "card";
        aiCard.style.marginBottom = "var(--gap)";
        aiCard.innerHTML = `<div class="card__head"><span class="card__title">AI analysis</span></div>
                             <div style="white-space:pre-wrap;font-size:13px;line-height:1.6">${esc(state.aiAnalysis)}</div>`;
        resultsRegion.appendChild(aiCard);
      }

      const rows = state.lastRows;
      if (!rows.length) {
        resultsRegion.insertAdjacentHTML("beforeend",
          `<div class="empty"><div class="empty__title">No startups match</div><div>Try a different search or clear filters</div></div>`);
        return;
      }

      // Relevance only applies to keyword-mode results (semantic-search rows
      // come back with a different shape and never carry relevance_score) —
      // gate on both so a thesis selected earlier doesn't leave a stale,
      // always-"—" column showing once the user switches to semantic search.
      const thesisActive = Boolean(state.thesis) && state.mode === "keyword";
      const sortLocked = thesisActive || state.priorityFirst; // both override the clickable-column sort with their own ordering
      // Name is always first and never optional. Everything after it comes
      // from the saved selection, in catalogue order so the layout stays
      // stable however the boxes were ticked.
      const chosen = new Set(state.columns);
      const active = ALL_COLUMNS.filter(([k]) => chosen.has(k));
      const cols = [["name", "Company", true], ...(thesisActive ? [RELEVANCE_COLUMN] : []), ...active];

      // Phase Q2/Q3: a checkbox column, not part of `cols` (which drives the
      // sortable-header logic) — prepended directly in the markup, +1 on
      // every colspan. Selection is shared infrastructure: Q3's bulk
      // interest-marking uses it now, Q2's bulk verify/recheck reuses the
      // same state.selectedIds and toolbar.
      const allOnPageSelected = rows.length > 0 && rows.every((s) => state.selectedIds.has(s.id));

      const wrap = document.createElement("div");
      wrap.className = "table-wrap";
      wrap.innerHTML = `
        <table class="table">
          <thead><tr>
            <th style="width:28px"><input type="checkbox" id="select-all-page" ${allOnPageSelected ? "checked" : ""}></th>
            ${cols.map(([key, label, sortable]) => `
              <th ${sortable && !sortLocked ? `data-sort="${key}"` : ""}>
                ${esc(label)}${!sortLocked && state.sort === key ? (state.order === "asc" ? " ↑" : " ↓") : ""}
              </th>`).join("")}
          </tr></thead>
          <tbody>
            ${rows.map((s) => `
              <tr data-id="${esc(s.id)}" data-tip="${esc(s.short_description, "")}">
                <td><input type="checkbox" class="row-select" data-id="${esc(s.id)}" ${state.selectedIds.has(s.id) ? "checked" : ""}></td>
                <td class="cell-company">
                  ${s.priority_match ? `<span title="Matches a priority thesis">⭐</span>` : ""}${s.business_model === "B2B" ? `<span title="B2B">🤝</span>` : ""}${s.is_gmbh ? `<span title="GmbH">🏢</span>` : ""}<strong>${esc(s.name)}</strong>
                </td>
                ${thesisActive ? `<td class="mono" title="${esc((s.matched_signals || []).join('; '), 'semantic match only')}">${s.relevance_score?.toFixed(2) ?? "—"}</td>` : ""}
                ${active.map(([key]) => `<td class="${key === "short_description" || key === "tags" ? "" : "nowrap"}">${(CELL[key] || (() => "—"))(s)}</td>`).join("")}
              </tr>
            `).join("")}
          </tbody>
        </table>`;
      resultsRegion.insertBefore(buildColumnBar(), wrap);
      resultsRegion.appendChild(wrap);

      // Detail/edit panel: a sibling of .table-wrap, not a colspan row inside
      // the <table> (see app.css's .detail-panel comment for why — an
      // in-table detail row forced the WHOLE table wider to fit the wide
      // edit form, and its own horizontal scrollbar ended up unreachable at
      // the bottom of a huge inflated table).
      const detailPanel = document.createElement("div");
      detailPanel.className = "detail-panel hidden";
      resultsRegion.appendChild(detailPanel);

      renderSelectionToolbar();

      wrap.querySelector("#select-all-page").addEventListener("click", (e) => {
        e.stopPropagation();
        if (e.target.checked) rows.forEach((s) => state.selectedIds.add(s.id));
        else rows.forEach((s) => state.selectedIds.delete(s.id));
        renderResults();
      });
      wrap.querySelectorAll(".row-select").forEach((cb) => {
        cb.addEventListener("click", (e) => e.stopPropagation());
        cb.addEventListener("change", (e) => {
          const id = e.target.dataset.id;
          if (e.target.checked) state.selectedIds.add(id);
          else state.selectedIds.delete(id);
          renderSelectionToolbar();
          wrap.querySelector("#select-all-page").checked = rows.every((s) => state.selectedIds.has(s.id));
        });
      });

      // Guard on the MODE, not on the absence of an AI report. Those meant
      // the same thing until semantic search became two-phase (16 Sep 2026) —
      // now `aiAnalysis` is legitimately null while phase 1's matches are on
      // screen, and keying off it rendered offset pagination over a single
      // 30-row vector result whose Prev/Next called load() and silently
      // replaced the semantic matches with keyword ones.
      if (state.mode === "keyword") {
        const footer = document.createElement("div");
        footer.className = "row";
        footer.style.cssText = "justify-content:space-between;margin-top:10px;font-size:12px";
        const from = state.offset + 1, to = Math.min(state.offset + state.limit, state.lastTotal);
        footer.innerHTML = `
          <span class="dim">${state.lastTotal} total · showing ${from}–${to}</span>
          <span class="row" style="gap:6px">
            <button class="btn btn--sm" id="prev-page" ${state.offset === 0 ? "disabled" : ""}>← Prev</button>
            <button class="btn btn--sm" id="next-page" ${to >= state.lastTotal ? "disabled" : ""}>Next →</button>
          </span>`;
        resultsRegion.appendChild(footer);
        footer.querySelector("#prev-page")?.addEventListener("click", () => {
          state.offset = Math.max(0, state.offset - state.limit); load();
        });
        footer.querySelector("#next-page")?.addEventListener("click", () => {
          state.offset += state.limit; load();
        });
      }

      wrap.querySelectorAll("th[data-sort]").forEach((th) => th.addEventListener("click", () => {
        const key = th.dataset.sort;
        if (state.sort === key) state.order = state.order === "asc" ? "desc" : "asc";
        else { state.sort = key; state.order = "desc"; }
        if (state.mode === "keyword") load(); else renderResults();
      }));

      attachRowTooltip(wrap);

      wrap.querySelectorAll("tbody tr[data-id]").forEach((tr) => tr.addEventListener("click", () => {
        const id = tr.dataset.id;
        state.expandedId = state.expandedId === id ? null : id;
        wrap.querySelectorAll("tbody tr[data-id]").forEach((r) => r.classList.toggle("is-selected", r.dataset.id === state.expandedId));
        if (state.expandedId) {
          detailPanel.classList.remove("hidden");
          openDetail(detailPanel, id);
        } else {
          detailPanel.classList.add("hidden");
          detailPanel.innerHTML = "";
        }
      }));
    }

    /**
     * Phase P-3: render the proposed-changes panel from a web-verify
     * response. Show-changes-first, one-click apply — never writes anything
     * on its own; "Apply selected" reuses api.editStartup (the same
     * "a human action applies directly" path as manual Edit), never the
     * Review Inbox.
     */
    function renderWebVerifyPanel(panel, id, res) {
      const proposed = res.proposed || {};
      const fields = Object.entries(proposed);

      if (res.identity_match === false) {
        panel.innerHTML = `<div class="card" style="background:var(--surface-2)">
          <div style="font-size:13px;line-height:1.5">⚠️ Could not confirm this is the right company: ${esc(res.summary)}</div>
        </div>`;
        return;
      }

      if (!fields.length) {
        panel.innerHTML = `<div class="card" style="background:var(--surface-2)">
          <div style="font-size:13px;line-height:1.5">✅ ${esc(res.summary || "Confirmed via web search — no corrections needed.")}</div>
        </div>`;
        return;
      }

      panel.innerHTML = `
        <div class="card" style="background:var(--surface-2)">
          <div style="font-size:13px;line-height:1.5;margin-bottom:10px">${esc(res.summary)}</div>
          <div class="table-wrap">
            <table class="table">
              <thead><tr><th></th><th>Field</th><th>Current</th><th>Proposed</th><th>Source</th></tr></thead>
              <tbody>
                ${fields.map(([field, c]) => `
                  <tr>
                    <td><input type="checkbox" class="wv-check" data-field="${esc(field)}" checked></td>
                    <td><strong>${esc(field.replace(/_/g, " "))}</strong></td>
                    <td class="dim">${esc(c.old, "—")}</td>
                    <td>${esc(c.new, "—")}</td>
                    <td style="font-size:12px">${c.source_url ? `<a href="${esc(c.source_url)}" target="_blank" rel="noopener">source</a>` : "—"}</td>
                  </tr>`).join("")}
              </tbody>
            </table>
          </div>
          <div class="row" style="gap:8px;margin-top:10px">
            <button class="btn btn--primary btn--sm" id="wv-apply-btn">✅ Apply selected</button>
            <button class="btn btn--ghost btn--sm" id="wv-discard-btn">Discard</button>
          </div>
        </div>`;

      panel.querySelector("#wv-discard-btn").addEventListener("click", () => { panel.innerHTML = ""; });
      panel.querySelector("#wv-apply-btn").addEventListener("click", async () => {
        const checked = [...panel.querySelectorAll(".wv-check:checked")].map((c) => c.dataset.field);
        if (!checked.length) { toast("Nothing selected"); return; }
        const changed = {};
        for (const field of checked) {
          let val = proposed[field].new;
          if (field === "founded_year") val = val ? Number(val) : null;
          changed[field] = val;
        }
        try {
          await api.editStartup(id, changed);
          toast(`Applied: ${checked.join(", ")}`);
          load();
        } catch (err) {
          toast(`Apply failed: ${err.message}`, "error");
        }
      });
    }

    /** Phase P-4: render the similar-startups table + AI verdict from a compare response. */
    function renderComparePanel(panel, res) {
      const rows = res.similar || [];
      if (!rows.length) {
        panel.innerHTML = `<div class="dim" style="font-size:12px;padding:8px 0">${esc(res.ai_verdict || "No similar startups found in the database yet.")}</div>`;
        return;
      }
      panel.innerHTML = `
        <div class="table-wrap">
          <table class="table">
            <thead><tr><th>Name</th><th>Industry / Cluster</th><th>Location</th><th>Stage</th><th>Score</th><th>Verified</th></tr></thead>
            <tbody>
              ${rows.map((s) => `
                <tr>
                  <td><strong>${esc(s.name)}</strong>${s.website ? ` <a href="${esc(s.website)}" target="_blank" rel="noopener" style="font-size:11px">↗</a>` : ""}</td>
                  <td class="dim">${esc(s.industry, "—")} / ${esc(s.tech_cluster, "—")}</td>
                  <td class="dim">${esc(s.city, "—")}, ${esc(s.country, "—")}</td>
                  <td class="dim">${esc(s.funding_stage, "—")}</td>
                  <td class="mono">${s.enrichment_score ?? "—"}</td>
                  <td>${verificationBadge(s.verification_status)}</td>
                </tr>`).join("")}
            </tbody>
          </table>
        </div>
        <div class="card" style="background:var(--surface-2);margin-top:10px">
          <div class="dim" style="font-size:11px;text-transform:uppercase;letter-spacing:.06em;margin-bottom:6px">🤖 AI verdict — which to suggest</div>
          <div style="font-size:13px;line-height:1.6;white-space:pre-wrap">${esc(res.ai_verdict, "—")}</div>
        </div>`;
    }

    async function openDetail(cell, id) {
      cell.innerHTML = `<div class="row" style="padding:16px;gap:8px"><span class="spinner"></span><span class="dim">Loading…</span></div>`;
      let s;
      try { s = await api.getStartup(id); }
      catch (err) { cell.innerHTML = `<div class="empty">${esc(err.message)}</div>`; return; }

      const breakdown = s.score_breakdown?.categories || {};
      cell.innerHTML = `
        <div class="stack" style="padding:16px;gap:16px;background:var(--surface-2);border-radius:var(--radius-sm)">
          <div class="row" style="gap:10px;align-items:center">
            ${interestBadge(s.interest_status)}
            <span class="grow"></span>
            <button class="btn btn--sm" id="mark-interested-btn">👍 Interested</button>
            <button class="btn btn--sm" id="mark-not-interested-btn">👎 Not interested</button>
            ${s.interest_status ? `<button class="btn btn--ghost btn--sm" id="mark-clear-btn">— Clear</button>` : ""}
          </div>
          <div class="card">
            <div class="dt-head" style="margin-bottom:14px">
              <div class="stack" style="gap:3px;min-width:0">
                <span class="dt-title">${esc(s.name)}</span>
                ${s.website ? `<a href="${esc(s.website)}" target="_blank" rel="noopener" style="font-size:12.5px">${esc(s.website.replace(/^https?:\/\//, ""))}</a>` : `<span class="dim" style="font-size:12.5px">no website</span>`}
              </div>
              <span class="grow"></span>
              <div class="row" style="gap:6px;flex-wrap:wrap">
                ${verificationBadge(s.verification_status)}
                ${s.score_tier ? `<span class="chip ${tierChipClass(s.score_tier)}">${esc(s.score_tier.replace(/_/g, " ").toLowerCase())}</span>` : ""}
              </div>
            </div>

            ${s.short_description ? `<div class="dt-prose" style="margin-bottom:13px"><strong>${esc(s.short_description)}</strong></div>` : ""}
            ${s.description ? `<div class="dt-prose dim" style="margin-bottom:15px">${esc(s.description)}</div>` : ""}

            <div class="dt-grid">
              <div class="dt-field"><span class="dt-label">Location</span><span class="dt-value">${esc([s.city, s.country].filter(Boolean).join(", "), "\u2014")}</span></div>
              <div class="dt-field"><span class="dt-label">Industry</span><span class="dt-value">${esc(s.industry, "\u2014")}</span></div>
              <div class="dt-field"><span class="dt-label">Cluster</span><span class="dt-value">${esc(s.tech_cluster, "\u2014")}</span></div>
              <div class="dt-field"><span class="dt-label">Founded</span><span class="dt-value">${esc(s.founded_year, "\u2014")}</span></div>
              <div class="dt-field"><span class="dt-label">Employees</span><span class="dt-value">${esc(s.employee_count, "\u2014")}</span></div>
              <div class="dt-field"><span class="dt-label">Stage</span><span class="dt-value">${esc(s.funding_stage, "\u2014")}</span></div>
              <div class="dt-field"><span class="dt-label">Model</span><span class="dt-value">${esc(s.business_model, "\u2014")}${s.is_gmbh ? " \u00b7 GmbH" : ""}</span></div>
              <div class="dt-field"><span class="dt-label">Contact</span><span class="dt-value">${esc(s.contact_info, "\u2014")}</span></div>
            </div>

            ${(s.tags || []).length ? `
              <div class="dt-field" style="margin-top:15px">
                <span class="dt-label" style="margin-bottom:5px">Tags</span>
                <div class="row" style="gap:5px;flex-wrap:wrap">
                  ${(s.tags || []).map((t) => `<span class="chip">${esc(t)}</span>`).join("")}
                </div>
              </div>` : ""}
          </div>

          <div class="grid-2">
            <div class="card">
              <div class="card__head"><span class="card__title">Score breakdown</span></div>
              <div class="stack" style="gap:8px">
                <div class="row"><strong style="font-size:20px">${s.enrichment_score ?? "—"}</strong>
                  <span class="chip ${tierChipClass(s.score_tier)}">${esc((s.score_tier || "unscored").replace(/_/g, " "))}</span>
                  <span class="dim" style="margin-left:auto;font-size:12px">confidence ${s.source_confidence ?? "—"}</span></div>
                ${Object.entries(breakdown).map(([key, cat]) => `
                  <div>
                    <div class="row" style="font-size:12px"><span class="dim">${esc(key.replace(/_/g, " "))}</span>
                      <span class="mono" style="margin-left:auto">${cat.score}/${cat.max}</span></div>
                    <div style="background:var(--surface);border-radius:4px;height:6px;overflow:hidden;margin-top:3px">
                      <span style="display:block;height:100%;width:${(cat.score / cat.max) * 100}%;background:var(--brand-lime)"></span>
                    </div>
                  </div>`).join("") || `<div class="dim" style="font-size:12px">Not yet scored</div>`}
              </div>
            </div>
          </div>

          <div class="card" id="history-card">
            <div class="card__head">
              <span class="card__title">History</span>
              <span class="dim" style="margin-left:auto;font-size:12px">what changed, newest first</span>
            </div>
            <div id="history-body"><div class="dim" style="font-size:12px">Loading…</div></div>
          </div>

          <div class="card">
            <div class="card__head">
              <span class="card__title">Provenance</span>
              <span class="dim" style="margin-left:auto;font-size:12px">Extracted ${fmt.dateTime(s.extracted_at)}</span>
            </div>
            ${(s.source_history || []).length ? `
              <div class="stack" style="gap:8px">
                ${s.source_history.map((h) => `
                  <div class="row" style="font-size:12px;align-items:flex-start">
                    <span class="chip" style="flex:none">${esc(h.source || "?")}</span>
                    <span class="grow">${h.url
                      ? `<a href="${esc(h.url)}" target="_blank" rel="noopener">${esc(h.source_name || h.sender || h.url)}</a>`
                      : esc(h.source_name || h.sender || "")}${h.subject ? ` — "${esc(h.subject)}"` : ""}</span>
                    <span class="dim" style="flex:none">${fmt.dateTime(h.extracted_at || h.date)}</span>
                  </div>`).join("")}
              </div>` : `<div class="dim" style="font-size:12px">No source history</div>`}
          </div>

          <div class="card">
            <div class="card__head">
              <span class="card__title">Verification</span>
              ${verificationBadge(s.verification_status)}
              <span class="dim" style="margin-left:auto;font-size:12px">${s.verified_at ? `Last checked ${fmt.dateTime(s.verified_at)}` : "Not yet rechecked"}</span>
            </div>
            ${s.verification_notes
              ? `<div style="font-size:13px;line-height:1.6">${esc(s.verification_notes)}</div>`
              : `<div class="dim" style="font-size:12px">${s.source_excerpt
                  ? "Awaiting recheck — press “Recheck now” on the Ingestion page."
                  : "No source excerpt on file (predates the grounding system) — will be flagged for manual review on next recheck."}</div>`}
            <div class="row" style="margin-top:10px">
              <button class="btn btn--sm" id="web-verify-btn" title="Search the live web and check this one record now — shows proposed corrections here, applies only what you approve">
                🌐 Verify now (web)
              </button>
            </div>
            <div id="web-verify-panel" style="margin-top:10px"></div>
          </div>

          <div class="card">
            <div class="card__head">
              <span class="card__title">Compare similar</span>
              <button class="btn btn--sm" style="margin-left:auto" id="compare-btn" title="Find other startups doing basically the same thing, with an AI verdict on which is stronger to suggest">
                ⚖️ Compare similar
              </button>
            </div>
            <div id="compare-panel"></div>
          </div>

          <div class="card" id="edit-card">
            <div class="card__head"><span class="card__title">Edit</span></div>
            <form id="edit-form" class="stack" style="gap:10px">
              <div class="grid-2">
                ${EDITABLE_FIELDS.map(([field, label, type]) => `
                  <div class="field" ${type === "textarea" ? 'style="grid-column:1/-1"' : ""}>
                    <label class="field__label">${esc(label)}</label>
                    ${type === "textarea"
                      ? `<textarea class="textarea" name="${field}">${esc(s[field])}</textarea>`
                      : `<input class="input" type="${type}" name="${field}" value="${esc(s[field])}">`}
                  </div>`).join("")}
              </div>
              <div class="row" style="justify-content:space-between">
                <button type="submit" class="btn btn--primary">💾 Save changes</button>
                <button type="button" class="btn btn--danger" id="delete-btn">🗑 Delete startup</button>
              </div>
            </form>
          </div>
        </div>`;

      cell.querySelector("#edit-form").addEventListener("submit", async (e) => {
        e.preventDefault();
        const form = e.target;
        const changed = {};
        for (const [field] of EDITABLE_FIELDS) {
          const el = form.elements[field];
          let val = el.value;
          if (field === "founded_year") val = val ? Number(val) : null;
          if (String(s[field] ?? "") !== String(val ?? "")) changed[field] = val;
        }
        if (!Object.keys(changed).length) { toast("No changes to save"); return; }
        try {
          await api.editStartup(id, changed);
          toast(`Saved: ${Object.keys(changed).join(", ")}`);
          load();
        } catch (err) {
          toast(`Save failed: ${err.message}`, "error");
        }
      });

      cell.querySelector("#delete-btn").addEventListener("click", async () => {
        if (!confirmAction(`Permanently delete "${s.name}"? This cannot be undone.`)) return;
        try {
          await api.deleteStartup(id);
          toast(`Deleted "${s.name}"`);
          state.expandedId = null;
          load();
        } catch (err) {
          toast(`Delete failed: ${err.message}`, "error");
        }
      });

      const markOne = async (status) => {
        try {
          await api.editStartup(id, { interest_status: status });
          toast(status ? `Marked ${status.replace("_", " ")}` : "Cleared marking");
          load();
        } catch (err) {
          toast(`Mark failed: ${err.message}`, "error");
        }
      };
      loadHistory(cell, id);

      cell.querySelector("#mark-interested-btn").addEventListener("click", () => markOne("interested"));
      cell.querySelector("#mark-not-interested-btn").addEventListener("click", () => markOne("not_interested"));
      cell.querySelector("#mark-clear-btn")?.addEventListener("click", () => markOne(null));

      cell.querySelector("#web-verify-btn").addEventListener("click", async (e) => {
        const btn = e.currentTarget;
        const panel = cell.querySelector("#web-verify-panel");
        btn.disabled = true;
        btn.textContent = "🌐 Searching & checking… (up to a few minutes)";
        panel.innerHTML = `<div class="row" style="gap:8px;padding:8px 0"><span class="spinner"></span><span class="dim" style="font-size:12px">Live web search + local model check in progress…</span></div>`;
        try {
          const res = await api.webVerifyStartup(id);
          renderWebVerifyPanel(panel, id, res);
        } catch (err) {
          panel.innerHTML = `<div class="dim" style="font-size:12px">Verify failed: ${esc(err.message)}</div>`;
        } finally {
          btn.disabled = false;
          btn.textContent = "🌐 Verify now (web)";
        }
      });

      cell.querySelector("#compare-btn").addEventListener("click", async (e) => {
        const btn = e.currentTarget;
        const panel = cell.querySelector("#compare-panel");
        btn.disabled = true;
        btn.textContent = "⚖️ Comparing… (up to a few minutes)";
        panel.innerHTML = `<div class="row" style="gap:8px;padding:8px 0"><span class="spinner"></span><span class="dim" style="font-size:12px">Finding similar startups + AI verdict in progress…</span></div>`;
        try {
          const res = await api.compareStartup(id);
          renderComparePanel(panel, res);
        } catch (err) {
          panel.innerHTML = `<div class="dim" style="font-size:12px">Compare failed: ${esc(err.message)}</div>`;
        } finally {
          btn.disabled = false;
          btn.textContent = "⚖️ Compare similar";
        }
      });
    }

    buildSearchCard();
    load();

    // Fetched once per mount, separately from load() — it's the list of
    // distinct sites, not startup results, and rarely changes mid-session.
    api.listSourceSites().then((res) => {
      state.sourceSites = res.sites || [];
      if (state.mode === "keyword") buildSearchCard();
    }).catch(() => { state.sourceSites = []; });

    api.listTheses().then((res) => {
      state.theses = res.theses || [];
      if (state.mode === "keyword") buildSearchCard();
    }).catch(() => { state.theses = []; });
  },
};
