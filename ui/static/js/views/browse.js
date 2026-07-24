/* ══════════════════════════════════════════════════════════════════════════
   SCOUT — Browse & Search
   Keyword + semantic search, filters, sortable table, and an inline detail
   panel (profile, provenance timeline, score breakdown, edit, delete).
   Preserves every Streamlit Browse capability, plus semantic search toggle
   and CSV export.
   ══════════════════════════════════════════════════════════════════════════ */

import { api, fmt, esc } from "../api.js";
import { toast, confirmAction } from "../router.js";

const COLUMNS = [
  ["name", "Name", false],
  ["industry", "Industry", true],
  ["tech_cluster", "Cluster", true],
  ["city", "City", true],
  ["country", "Country", true],
  ["funding_stage", "Stage", true],
  ["employee_count", "Employees", false],
  ["score_tier", "Tier", true],
  ["enrichment_score", "Score", true],
  ["verification_status", "Verified", false],
  ["source_url", "Source", false],
];

/** "https://www.munich-startup.de/en/x" -> "munich-startup.de" (falls back to the coarse source type). */
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
      q: "",
      filters: { industry: "", country: "", city: "", tech_cluster: "", funding_stage: "", score_tier: "", employee_count: "", verification_status: "", source_url: "" },
      sort: "created_at", order: "desc",
      limit: 50, offset: 0,
      expandedId: null,
      lastRows: [], lastTotal: 0,
      aiAnalysis: null,
      sourceSites: null,   // [{label, count}] — fetched once, populates the source-website filter
    };

    el.innerHTML = `
      <div class="stack">
        <div class="card" id="search-card"></div>
        <div id="results-region"></div>
      </div>`;

    const searchCard = el.querySelector("#search-card");
    const resultsRegion = el.querySelector("#results-region");

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
            <select class="select" style="max-width:200px" id="f-source_url" title="Filter to startups extracted from one source website — useful for a manual verification pass, site by site">
              <option value="">${state.sourceSites ? "All source websites" : "Loading sources…"}</option>
              ${(state.sourceSites || []).map((s) =>
                `<option value="${esc(s.label)}" ${state.filters.source_url === s.label ? "selected" : ""}>${esc(s.label)} (${s.count})</option>`).join("")}
            </select>
            ${Object.values(state.filters).some(Boolean) ? `<button class="btn btn--ghost btn--sm" id="clear-filters">Clear filters</button>` : ""}
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
        searchCard.querySelector("#clear-filters")?.addEventListener("click", () => {
          for (const k in state.filters) state.filters[k] = "";
          state.offset = 0;
          buildSearchCard();
          load();
        });
      }
    }

    async function runSemantic() {
      const q = searchCard.querySelector("#q-input").value.trim();
      if (!q) { toast("Enter a query first", "error"); return; }
      resultsRegion.innerHTML = `<div class="card row" style="justify-content:center;padding:40px;gap:10px">
        <span class="spinner"></span><span class="dim">Asking the local AI model — this can take up to a minute…</span></div>`;
      try {
        const res = await api.semanticSearch(q, { limit: 30 });
        state.aiAnalysis = res.ai_analysis;
        state.lastRows = (res.startups || []).map((s) => ({ ...s, id: s.id }));
        state.lastTotal = res.total_found ?? state.lastRows.length;
        renderResults();
      } catch (err) {
        resultsRegion.innerHTML = `<div class="empty"><div class="empty__title">Search failed</div><div>${esc(err.message)}</div></div>`;
      }
    }

    async function load() {
      resultsRegion.innerHTML = `<div class="table-wrap"><div class="skeleton" style="height:300px"></div></div>`;
      try {
        const filters = Object.fromEntries(Object.entries(state.filters).filter(([, v]) => v));
        const res = await api.listStartups({
          q: state.q || undefined, ...filters,
          sort: state.sort, order: state.order,
          limit: state.limit, offset: state.offset,
        });
        state.lastRows = res.startups || [];
        state.lastTotal = res.total ?? 0;
        renderResults();
      } catch (err) {
        resultsRegion.innerHTML = `<div class="empty"><div class="empty__title">Couldn't load startups</div><div>${esc(err.message)}</div></div>`;
      }
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

      const wrap = document.createElement("div");
      wrap.className = "table-wrap";
      wrap.innerHTML = `
        <table class="table">
          <thead><tr>
            ${COLUMNS.map(([key, label, sortable]) => `
              <th ${sortable ? `data-sort="${key}"` : ""}>
                ${esc(label)}${state.sort === key ? (state.order === "asc" ? " ↑" : " ↓") : ""}
              </th>`).join("")}
          </tr></thead>
          <tbody>
            ${rows.map((s) => `
              <tr data-id="${esc(s.id)}">
                <td><strong>${esc(s.name)}</strong></td>
                <td class="dim">${esc(s.industry, "—")}</td>
                <td class="dim">${esc(s.tech_cluster, "—")}</td>
                <td class="dim">${esc(s.city, "—")}</td>
                <td class="dim">${esc(s.country, "—")}</td>
                <td class="dim">${esc(s.funding_stage, "—")}</td>
                <td class="dim">${esc(s.employee_count, "—")}</td>
                <td>${s.score_tier ? `<span class="chip ${tierChipClass(s.score_tier)}">${esc(s.score_tier.replace(/_/g, " "))}</span>` : "—"}</td>
                <td class="mono">${s.enrichment_score ?? "—"}</td>
                <td>${verificationBadge(s.verification_status)}</td>
                <td class="dim truncate" style="max-width:160px">${s.source_url
                  ? `<a href="${esc(s.source_url)}" target="_blank" rel="noopener" title="${esc(s.source_url)}" onclick="event.stopPropagation()">${esc(sourceLabel(s.source_url, s.source))}</a>`
                  : esc(sourceLabel(s.source_url, s.source))}</td>
              </tr>
              <tr class="detail-row hidden" data-detail-for="${esc(s.id)}"><td colspan="${COLUMNS.length}"></td></tr>
            `).join("")}
          </tbody>
        </table>`;
      resultsRegion.appendChild(wrap);

      if (!state.aiAnalysis) {
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

      wrap.querySelectorAll("tbody tr[data-id]").forEach((tr) => tr.addEventListener("click", () => {
        const id = tr.dataset.id;
        state.expandedId = state.expandedId === id ? null : id;
        wrap.querySelectorAll(".detail-row").forEach((dr) => dr.classList.add("hidden"));
        if (state.expandedId) {
          const dr = wrap.querySelector(`.detail-row[data-detail-for="${CSS.escape(id)}"]`);
          dr.classList.remove("hidden");
          openDetail(dr.querySelector("td"), id);
        }
      }));
    }

    async function openDetail(cell, id) {
      cell.innerHTML = `<div class="row" style="padding:16px;gap:8px"><span class="spinner"></span><span class="dim">Loading…</span></div>`;
      let s;
      try { s = await api.getStartup(id); }
      catch (err) { cell.innerHTML = `<div class="empty">${esc(err.message)}</div>`; return; }

      const breakdown = s.score_breakdown?.categories || {};
      cell.innerHTML = `
        <div class="stack" style="padding:16px;gap:16px;background:var(--surface-2);border-radius:var(--radius-sm)">
          <div class="grid-2">
            <div class="card">
              <div class="card__head"><span class="card__title">Profile</span></div>
              <div class="stack" style="gap:6px;font-size:13px">
                <div><span class="dim">Website:</span> ${s.website ? `<a href="${esc(s.website)}" target="_blank" rel="noopener">${esc(s.website)}</a>` : "—"}</div>
                <div><span class="dim">One-liner:</span> ${esc(s.short_description, "—")}</div>
                <div><span class="dim">Description:</span> ${esc(s.description, "—")}</div>
                <div><span class="dim">Location:</span> ${esc(s.city, "—")}, ${esc(s.country, "—")}</div>
                <div><span class="dim">Founded:</span> ${esc(s.founded_year, "—")} · <span class="dim">Employees:</span> ${esc(s.employee_count, "—")}</div>
                <div><span class="dim">Stage:</span> ${esc(s.funding_stage, "—")}</div>
                <div><span class="dim">Contact:</span> ${esc(s.contact_info, "—")}</div>
                <div><span class="dim">Tags:</span> ${(s.tags || []).map((t) => `<span class="chip" style="margin-right:4px">${esc(t)}</span>`).join("") || "—"}</div>
              </div>
            </div>
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
    }

    buildSearchCard();
    load();

    // Fetched once per mount, separately from load() — it's the list of
    // distinct sites, not startup results, and rarely changes mid-session.
    api.listSourceSites().then((res) => {
      state.sourceSites = res.sites || [];
      if (state.mode === "keyword") buildSearchCard();
    }).catch(() => { state.sourceSites = []; });
  },
};
