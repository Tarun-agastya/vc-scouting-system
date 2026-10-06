/* ══════════════════════════════════════════════════════════════════════════
   SCOUT — Review Inbox
   The pipeline never auto-merges or auto-overwrites; every staged change
   waits here. Two-pane triage (list + detail) with keyboard shortcuts:
   j/k navigate, a approve, r reject — fast review of a growing queue.

   Phase Y (5 Aug): field_update reviews are shown ONE ROW PER STARTUP
   (grouped by master_id via GET /reviews/grouped), since a single startup
   re-sighted across ingestion runs could otherwise show up dozens of times
   (Bliro 11x, Omegga/Alqem 18x each). possible_duplicate/anomaly reviews
   are genuine distinct pairs and stay ungrouped, in the flat /reviews list.
   ══════════════════════════════════════════════════════════════════════════ */

import { api, fmt, esc } from "../api.js";
import { toast, confirmAction, poll, getRecentBatches } from "../router.js";

const RISK = {
  high:    { mark: "🔴", label: "Conflict", chip: "chip--danger" },
  low:     { mark: "🟡", label: "New info", chip: "chip--warning" },
  anomaly: { mark: "⚠️", label: "Anomaly", chip: "chip--warning" },
  none:    { mark: "⚪", label: "—", chip: "" },
};
const TYPE_LABEL = { field_update: "Field change", possible_duplicate: "Possible duplicate", anomaly: "Anomaly" };
const PROFILE_FIELDS = ["name", "description", "website", "city", "country", "funding_stage", "founded_year", "industry"];

function debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

function rowLabel(entry) {
  if (entry.kind === "group") {
    const fieldCount = Object.keys(entry.fields || {}).length;
    return `${esc(entry.master_name)} <span class="dim">· ${entry.review_count} change${entry.review_count === 1 ? "" : "s"} across ${fieldCount} field${fieldCount === 1 ? "" : "s"}</span>`;
  }
  return `${esc(entry.incoming_name)} <span class="dim">~ ${esc(entry.master_name)}</span>`;
}

export default {
  title: "Review Inbox",

  mount(el, params = {}) {
    const PAGE_SIZE = 200;
    const state = {
      status: "pending", type: "", risk: "", q: "", evidenceLevel: "",
      runId: params.run_id || "", // Phase Q2: batch filter — either deep-linked from Browse or picked below
      reviews: [], selectedId: null, busy: false,
      selectedIds: new Set(), // Phase Q4: bulk-select for approve/reject, cleared on any filter change — singles only
      groupTotals: { total_groups: 0, total_reviews: 0 }, // Phase Y: for the KPI strip
      // Phase Z-4: pagination past the first PAGE_SIZE rows — the "primary"
      // list is groups when field_update rows are shown (the dominant case,
      // and the one Z-4 was built to unblock), else singles. A merged
      // groups+singles view (no type filter) paginates groups only, since
      // singles' own total is small enough to sit comfortably on one page —
      // narrow the type filter to page through singles specifically.
      offset: 0, primaryTotal: 0,
      // True while a sub-screen (the field-by-field merge table) owns the
      // detail pane, so the background poll's repaint leaves it alone.
      detailLocked: false,
    };

    el.innerHTML = `
      <div class="stack">
        <div id="resolver-card"></div>
        <div class="kpis" id="counts"></div>
        <div class="card">
          <div class="row wrap" style="gap:8px">
            <input class="input" id="f-q" placeholder="Filter by company…" style="max-width:200px" value="${esc(state.q)}">
            <select class="select" id="f-status" style="max-width:150px">
              <option value="pending">Pending</option>
              <option value="approved">Approved</option>
              <option value="rejected">Rejected</option>
              <option value="deleted">Deleted</option>
            </select>
            <select class="select" id="f-type" style="max-width:190px">
              <option value="">All types</option>
              <option value="field_update">Field change</option>
              <option value="possible_duplicate">Possible duplicate</option>
              <option value="anomaly">Anomaly</option>
            </select>
            <select class="select" id="f-risk" style="max-width:150px">
              <option value="">All risk levels</option>
              <option value="high">🔴 Conflict</option>
              <option value="low">🟡 New info</option>
              <option value="anomaly">⚠️ Anomaly</option>
            </select>
            <select class="select" id="f-evidence" style="max-width:190px" title="possible_duplicate/anomaly reviews where BOTH sides have no description and no website">
              <option value="">All evidence levels</option>
              <option value="minimal">⬜ Minimal evidence (bare stubs)</option>
              <option value="normal">Has real evidence</option>
            </select>
            <span class="dim" style="margin-left:auto;font-size:12px">j/k navigate · a approve · r reject</span>
          </div>
          <div class="row wrap" style="gap:8px;margin-top:8px" id="batch-row"></div>
        </div>
        <div id="queue-actions"></div>
        <div id="bulk-toolbar"></div>
        <div class="inbox-grid" id="inbox-grid" style="align-items:start">
          <div class="card" id="review-list" style="padding:0;max-height:70vh;overflow-y:auto"></div>
          <div class="card" id="review-detail"></div>
        </div>
        <div class="row" id="pagination" style="gap:8px;justify-content:center"></div>
      </div>`;

    const resolverCardEl = el.querySelector("#resolver-card");
    const countsEl = el.querySelector("#counts");
    const queueActions = el.querySelector("#queue-actions");
    const bulkToolbar = el.querySelector("#bulk-toolbar");
    const listEl = el.querySelector("#review-list");
    const detailEl = el.querySelector("#review-detail");
    const paginationEl = el.querySelector("#pagination");

    // Any filter change starts back at the first page — an offset from the
    // old filter's result set is meaningless against a new one.
    function resetAndLoad() { state.offset = 0; loadList(); }

    el.querySelector("#f-status").addEventListener("change", (e) => { state.status = e.target.value; resetAndLoad(); });
    el.querySelector("#f-type").addEventListener("change", (e) => { state.type = e.target.value; resetAndLoad(); });
    el.querySelector("#f-risk").addEventListener("change", (e) => { state.risk = e.target.value; resetAndLoad(); });
    el.querySelector("#f-evidence").addEventListener("change", (e) => { state.evidenceLevel = e.target.value; resetAndLoad(); });
    el.querySelector("#f-q").addEventListener("input", debounce((e) => { state.q = e.target.value; resetAndLoad(); }, 300));

    // Re-render just the batch row (picker selection / clear button visibility)
    // without rebuilding the whole filter card.
    function mountBatchRow() {
      const row = el.querySelector("#batch-row");
      row.innerHTML = `
        <select class="select" id="f-batch-pick" style="max-width:260px" title="Jump to a bulk verify/recheck batch triggered from Browse's selection toolbar">
          <option value="">Filter by recent batch…</option>
          ${getRecentBatches().map((b) =>
            `<option value="${esc(b.run_id)}" ${state.runId === b.run_id ? "selected" : ""}>
              ${esc(b.label)} · ${b.count} startup${b.count === 1 ? "" : "s"} · ${fmt.dateTime(b.ts)}
            </option>`).join("")}
        </select>
        <input class="input mono" id="f-batch-id" placeholder="or paste a batch/run id…" style="max-width:280px;font-size:12px" value="${esc(state.runId)}">
        ${state.runId ? `<button class="btn btn--ghost btn--sm" id="f-batch-clear">✕ Clear batch filter</button>` : ""}
        ${state.runId ? `<span class="chip" style="font-size:11px">Showing only this batch</span>` : ""}`;
      row.querySelector("#f-batch-pick").addEventListener("change", (e) => { state.runId = e.target.value; mountBatchRow(); resetAndLoad(); });
      row.querySelector("#f-batch-id").addEventListener("input", debounce((e) => { state.runId = e.target.value.trim(); resetAndLoad(); }, 300));
      row.querySelector("#f-batch-clear")?.addEventListener("click", () => { state.runId = ""; mountBatchRow(); resetAndLoad(); });
    }

    // Phase 4 (autonomy plan): "what the resolver did last night" — the
    // worker's output is worthless if nobody can see it. Silent (no card at
    // all) if the resolver has never run, rather than an empty/zero card
    // implying it ran and found nothing.
    function resolverRunLine(kind, label, run) {
      if (!run) return "";
      const when = run.finished_at ? fmt.dateTime(run.finished_at) : "—";
      const bits = [`${run.judged} judged`, `${run.auto_closed} closed for you`,
                    `${run.left_pending} left pending`];
      if (kind === "research" && run.searches_used != null) bits.push(`${run.searches_used} searches`);
      if (run.unavailable) bits.push(`${run.unavailable} skipped (model unavailable)`);
      const errBit = run.error ? `<span class="dim" style="color:var(--danger,#c0392b)"> · stopped early: ${esc(run.error)}</span>` : "";
      return `<div class="row" style="gap:6px;font-size:13px">
        <strong>${label}</strong>
        <span class="dim">${when} · ${bits.join(" · ")}</span>${errBit}
      </div>`;
    }

    async function loadResolverCard() {
      try {
        const [runs, aut, mergeList] = await Promise.all([
          api.resolverLastRun(), api.autonomy().catch(() => null),
          api.recentMerges(25).catch(() => null)]);
        const merge = runs.auto_merge
          ? `<div class="row" style="gap:6px;font-size:13px"><strong>🔗 Auto-merge</strong>
               <span class="dim">${runs.auto_merge.finished_at ? fmt.dateTime(runs.auto_merge.finished_at) : "—"} ·
               ${runs.auto_merge.stats && runs.auto_merge.stats.blocked ? "blocked: " + esc(runs.auto_merge.stats.blocked) : "see Recent merges — each has Undo"}</span></div>` : "";
        const backup = runs.backup
          ? `<div class="row" style="gap:6px;font-size:13px"><strong>💾 Backup</strong>
               <span class="dim">${runs.backup.finished_at ? fmt.dateTime(runs.backup.finished_at) : "—"}${runs.backup.error ? " · FAILED: " + esc(runs.backup.error) : " · ok"}</span></div>` : "";
        // What is automated, and the evidence — never a bare "on". A field only
        // appears once a person has settled a review the model judged.
        const earned = aut ? aut.fields.filter((f) => f.earned).map((f) => f.field) : [];
        const autLine = aut
          ? `<div class="row" style="gap:6px;font-size:13px"><strong>🧭 Autonomy</strong>
               <span class="dim">${earned.length ? "auto-applying: " + earned.map(esc).join(", ")
                 : "nothing auto-applied yet — needs " + aut.min_decisions + " human decisions at ≥" + Math.round(aut.min_agreement * 100) + "% agreement per field"}
               ${aut.fields.length ? " · " + aut.fields.map((f) => esc(f.field) + " " + (f.rate == null ? "–" : Math.round(f.rate * 100) + "%") + " (" + f.n + ")").join(", ") : ""}</span></div>` : "";
        const lines = [
          resolverRunLine("resolve", "🌙 Review resolver", runs.resolve),
          resolverRunLine("research", "🔎 Research loop", runs.research),
          merge, backup, autLine,
        ].filter(Boolean);
        // Recent merges, each with its own Undo. The nightly job merges
        // without asking, so "every merge can be undone" is only true if the
        // button is somewhere a person will find it — it used to exist only as
        // a bar that appeared after a MANUAL merge. Newest first, and undo in
        // that order: a keeper that was itself merged away later can't be
        // put back until the later merge is undone.
        const merges = (mergeList && mergeList.merges) || [];
        const mergesHtml = merges.length ? `
          <details style="margin-top:6px" id="merge-list">
            <summary style="cursor:pointer;font-size:13px"><strong>↩ Recent merges</strong>
              <span class="dim">· ${merges.filter((m) => m.can_undo).length} can be undone</span></summary>
            <div class="stack" style="gap:4px;margin-top:6px;max-height:240px;overflow-y:auto">
              ${merges.map((m) => `
                <div class="row" style="gap:8px;font-size:12.5px;align-items:center">
                  <span class="dim" style="min-width:110px">${fmt.dateTime(m.created_at)}</span>
                  <span>${esc(m.loser_name)} → <strong>${esc(m.keeper_name)}</strong></span>
                  ${m.automatic ? `<span class="chip" style="font-size:10.5px">${m.via === "dedup-button" ? "dedup button" : "auto"}</span>` : ""}
                  ${m.fields_taken.length ? `<span class="dim">took ${m.fields_taken.map(esc).join(", ")}</span>` : ""}
                  <span class="grow"></span>
                  ${m.can_undo
                    ? `<button class="btn btn--ghost btn--sm" data-undo-merge="${esc(m.id)}">↩ Undo</button>`
                    : '<span class="dim">undone</span>'}
                </div>`).join("")}
            </div>
          </details>` : "";
        resolverCardEl.innerHTML = lines.length
          ? `<div class="card" style="padding:10px 16px">${lines.join("")}${mergesHtml}</div>` : "";
        resolverCardEl.querySelectorAll("[data-undo-merge]").forEach((btn) =>
          btn.addEventListener("click", async () => {
            const row = btn.closest(".row");
            if (!confirmAction(`Undo this merge?\n\n${row.innerText.replace(/\s+/g, " ").trim()}\n\nThe deleted record comes back exactly as it was.`)) return;
            btn.disabled = true;
            try {
              const r = await api.undoFieldMerge(btn.getAttribute("data-undo-merge"));
              toast(`Undone — ${r.restored} restored`);
              await loadResolverCard(); await loadCounts(); await loadList();
            } catch (err) {
              btn.disabled = false;
              toast(`Undo failed: ${err.message}`, "error");
            }
          }));
      } catch { /* non-fatal — the card is a convenience */ }
    }

    async function loadCounts() {
      try {
        // Phase Z-4: real SQL GROUP BY, not a capped client-side sample — the
        // old `limit: 500` fetch-and-tally silently under-reported the risk
        // tiles once pending passed 500, disagreeing with the Pending tile
        // (which already used the true count.count()).
        const counts = await api.reviewCounts("pending");
        const c = counts.by_risk_level || {};
        // Phase Y: also surface "N startups / M changes" so the grouped view's
        // count doesn't look contradictory next to the flat pending total.
        let groupsKpi = "";
        try {
          const grouped = await api.listReviewsGrouped({ status: "pending", limit: 1 });
          state.groupTotals = { total_groups: grouped.total_groups || 0, total_reviews: grouped.total_reviews || 0 };
          groupsKpi = `<div class="kpi"><div class="kpi__label">Startups w/ changes</div><div class="kpi__value">${state.groupTotals.total_groups}</div></div>`;
        } catch { /* non-fatal */ }
        countsEl.innerHTML = `
          <div class="kpi kpi--accent"><div class="kpi__label">Pending</div><div class="kpi__value">${counts.total ?? 0}</div></div>
          ${groupsKpi}
          <div class="kpi"><div class="kpi__label">🔴 Conflicts</div><div class="kpi__value">${c.high || 0}</div></div>
          <div class="kpi"><div class="kpi__label">🟡 New info</div><div class="kpi__value">${c.low || 0}</div></div>
          <div class="kpi"><div class="kpi__label">⚠️ Anomalies</div><div class="kpi__value">${c.anomaly || 0}</div></div>`;
      } catch { /* non-fatal — counts are a convenience */ }
    }

    async function loadList(preserveSelection = false) {
      const commonFilters = {
        status: state.status || undefined,
        risk_level: state.risk || undefined,
        evidence_level: state.evidenceLevel || undefined,
        q: state.q || undefined,
        run_id: state.runId || undefined,
      };
      // field_update entries come from the grouped endpoint (one row per
      // startup); possible_duplicate/anomaly stay flat. Fetch whichever the
      // type filter allows, in parallel. Groups are the "primary" paginated
      // list whenever they're shown (the dominant, Z-4-motivating case);
      // singles get their own offset only when narrowed to singles alone —
      // see the state.offset comment above.
      const wantGroups = !state.type || state.type === "field_update";
      const wantSingles = state.type !== "field_update";
      const groupsGetOffset = wantGroups;
      const singlesGetOffset = wantSingles && !wantGroups;

      try {
        const [groupsRes, singlesRes] = await Promise.all([
          wantGroups ? api.listReviewsGrouped({ ...commonFilters, limit: PAGE_SIZE, offset: groupsGetOffset ? state.offset : 0 }) : Promise.resolve({ groups: [] }),
          wantSingles ? api.listReviews({ ...commonFilters, review_type: (state.type && state.type !== "field_update") ? state.type : undefined, limit: PAGE_SIZE, offset: singlesGetOffset ? state.offset : 0 }) : Promise.resolve({ reviews: [] }),
        ]);
        state.primaryTotal = groupsGetOffset ? (groupsRes.total_groups || 0) : (singlesRes.total || 0);
        const groupEntries = (groupsRes.groups || []).map((g) => ({
          entryId: g.master_id, kind: "group",
          master_id: g.master_id, master_name: g.master_name,
          review_ids: g.review_ids || [], review_count: g.review_count,
          risk_level: g.risk_level, first_seen: g.first_seen, last_seen: g.last_seen,
          fields: g.fields || {}, current: g.current || {}, master_missing: g.master_missing,
          created_at: g.last_seen,
        }));
        // field_update rows are represented via groups above — drop any
        // stray singles of that type so nothing shows twice.
        const singleEntries = (singlesRes.reviews || [])
          .filter((r) => r.review_type !== "field_update")
          .map((r) => ({ entryId: r.id, kind: "single", ...r }));

        state.reviews = [...groupEntries, ...singleEntries]
          .sort((a, b) => new Date(b.created_at || 0) - new Date(a.created_at || 0));
      } catch (err) {
        listEl.innerHTML = `<div class="empty">${esc(err.message)}</div>`;
        return;
      }

      if (!preserveSelection || !state.reviews.some((r) => r.entryId === state.selectedId)) {
        state.selectedId = state.reviews[0]?.entryId || null;
      }
      // Bulk selection: a filter change starts fresh; a background poll
      // refresh keeps it but drops any id that fell out of the loaded list
      // (resolved by someone else, or no longer matches the filter).
      if (!preserveSelection) {
        state.selectedIds.clear();
      } else {
        const loadedIds = new Set(state.reviews.filter((r) => r.kind === "single").map((r) => r.entryId));
        for (const id of state.selectedIds) if (!loadedIds.has(id)) state.selectedIds.delete(id);
      }
      renderList();
      renderDetail();
      renderQueueActions();
      renderPagination();
    }

    function renderList() {
      // Phase Q4 (29 Jul, after the queue hit 1,010 pending): bulk-select is
      // only meaningful for pending reviews — approve/reject both require
      // status=="pending". Grouped rows (Phase Y) don't have a single
      // approve/reject action, so bulk-select only applies to single rows.
      const bulkEnabled = state.status === "pending";
      const singles = state.reviews.filter((r) => r.kind === "single");

      if (!state.reviews.length) {
        listEl.innerHTML = `<div class="empty" style="padding:24px"><div class="empty__title">Nothing here</div>
                             <div>${state.status === "pending" ? "All clear 🎉" : "No items match these filters"}</div></div>`;
        renderBulkToolbar();
        return;
      }
      const allLoadedSelected = singles.length > 0 && singles.every((rv) => state.selectedIds.has(rv.entryId));
      const header = bulkEnabled && singles.length ? `
        <div class="row" style="padding:8px 12px;border-bottom:1px solid var(--border);gap:8px">
          <input type="checkbox" id="select-all-loaded" ${allLoadedSelected ? "checked" : ""}>
          <span class="dim" style="font-size:11px">Select all ${singles.length} loaded</span>
        </div>` : "";

      listEl.innerHTML = header + state.reviews.map((entry) => {
        const risk = RISK[entry.risk_level] || RISK.none;
        const active = entry.entryId === state.selectedId;
        const showCheckbox = bulkEnabled && entry.kind === "single";
        const subLabel = entry.kind === "group"
          ? `Field change (grouped) · ${fmt.dateTime(entry.last_seen)}`
          : `${TYPE_LABEL[entry.review_type]} · ${fmt.dateTime(entry.created_at)}`;
        return `
          <div class="row" data-entry-id="${esc(entry.entryId)}"
               style="padding:10px 12px;cursor:pointer;border-bottom:1px solid var(--border);gap:8px;
                      ${active ? "background:var(--brand-lime-glow);border-left:3px solid var(--brand-lime)" : "border-left:3px solid transparent"}">
            ${showCheckbox ? `<input type="checkbox" class="row-select" data-id="${esc(entry.entryId)}" ${state.selectedIds.has(entry.entryId) ? "checked" : ""} style="flex:none">` : (bulkEnabled ? `<span style="flex:none;width:13px"></span>` : "")}
            <span style="flex:none">${risk.mark}</span>
            <div class="grow" style="min-width:0">
              <div class="truncate" style="font-size:13px;font-weight:550">${rowLabel(entry)}</div>
              <div class="dim truncate" style="font-size:11px">${subLabel}</div>
            </div>
          </div>`;
      }).join("");

      listEl.querySelectorAll("[data-entry-id]").forEach((row) =>
        row.addEventListener("click", () => {
          // Picking a different row is an explicit "I'm done with whatever
          // was open" — release the pane so the merge screen can't strand it.
          state.detailLocked = false;
          state.selectedId = row.dataset.entryId;
          renderList();
          renderDetail();
        }));

      if (bulkEnabled) {
        listEl.querySelector("#select-all-loaded")?.addEventListener("click", (e) => {
          e.stopPropagation();
          if (e.target.checked) singles.forEach((rv) => state.selectedIds.add(rv.entryId));
          else singles.forEach((rv) => state.selectedIds.delete(rv.entryId));
          renderList();
        });
        listEl.querySelectorAll(".row-select").forEach((cb) => {
          cb.addEventListener("click", (e) => e.stopPropagation());
          cb.addEventListener("change", (e) => {
            const id = e.target.dataset.id;
            if (e.target.checked) state.selectedIds.add(id);
            else state.selectedIds.delete(id);
            renderBulkToolbar();
          });
        });
      }
      renderBulkToolbar();
    }


    /* ── Field-level merge screen (Phase 3) ──────────────────────────────
       Side by side, one radio per field. Fields where both records already
       agree are collapsed behind a toggle: on a real pair most fields match,
       and showing thirty identical rows buries the three that need a
       decision.

       Every merge is reversible — the backend snapshots before touching
       anything — so the toast offers Undo directly rather than making
       someone hunt for it. */
    async function openMergeScreen(reviewId) {
      const host = detailEl;
      const prev = host.innerHTML;
      // Claim the pane before the first await — the poll can fire while the
      // preview request is still in flight.
      state.detailLocked = true;
      host.innerHTML = `<div class="row" style="padding:40px;justify-content:center"><span class="spinner"></span></div>`;

      let pv;
      try { pv = await api.mergePreview(reviewId); }
      catch (err) {
        state.detailLocked = false;
        host.innerHTML = `<div class="empty" style="padding:30px"><div class="empty__title">Can't merge these</div><div>${esc(err.message)}</div></div>`;
        return;
      }

      const choices = {};
      pv.fields.forEach((f) => { choices[f.field] = f.default; });
      let showSame = false;

      const fmtVal = (v) => {
        if (v === null || v === undefined || v === "") return '<span class="dim">—</span>';
        if (Array.isArray(v)) return v.length ? v.map((x) => `<span class="chip">${esc(x)}</span>`).join(" ") : '<span class="dim">—</span>';
        return esc(String(v));
      };

      function draw() {
        const differing = pv.fields.filter((f) => f.differs);
        const same = pv.fields.filter((f) => !f.differs);
        const rowsFor = (list) => list.map((f) => `
          <tr data-field="${esc(f.field)}">
            <td class="dim" style="white-space:nowrap;font-size:11px;text-transform:uppercase;letter-spacing:.05em;vertical-align:top;padding-top:11px">${esc(f.field.replace(/_/g, " "))}</td>
            <td style="vertical-align:top">
              <label class="row" style="gap:7px;align-items:flex-start;cursor:pointer">
                <input type="radio" name="m-${esc(f.field)}" value="keeper" ${choices[f.field] === "keeper" ? "checked" : ""} style="margin-top:3px;flex:none">
                <span style="font-size:12.5px;line-height:1.45">${fmtVal(f.keeper)}</span>
              </label>
            </td>
            <td style="vertical-align:top">
              <label class="row" style="gap:7px;align-items:flex-start;cursor:pointer">
                <input type="radio" name="m-${esc(f.field)}" value="incoming" ${choices[f.field] === "incoming" ? "checked" : ""} style="margin-top:3px;flex:none">
                <span style="font-size:12.5px;line-height:1.45">${fmtVal(f.incoming)}</span>
              </label>
            </td>
          </tr>`).join("");

        host.innerHTML = `
          <div class="stack" style="gap:14px">
            <div class="row" style="gap:10px;align-items:center">
              <strong style="font-size:15px">Merge field by field</strong>
              <span class="grow"></span>
              <button class="btn btn--ghost btn--sm" id="m-cancel">Cancel</button>
            </div>
            <div class="dim" style="font-size:12px">
              Pick which value survives on each row. The record on the right is deleted
              afterwards — and every merge can be undone.
            </div>
            <div class="table-wrap">
              <table class="table">
                <thead><tr>
                  <th style="width:120px">Field</th>
                  <th>Keep — <strong>${esc(pv.keeper.name)}</strong><div class="dim" style="font-weight:400;font-size:11px">survives</div></th>
                  <th>Merged away — <strong>${esc(pv.incoming.name)}</strong><div class="dim" style="font-weight:400;font-size:11px">deleted after merge</div></th>
                </tr></thead>
                <tbody>
                  ${differing.length ? rowsFor(differing) : `<tr><td colspan="3" class="dim" style="padding:14px">Nothing differs between these two.</td></tr>`}
                  ${same.length && showSame ? rowsFor(same) : ""}
                </tbody>
              </table>
            </div>
            ${same.length ? `<button class="btn btn--ghost btn--sm" id="m-toggle-same" style="align-self:flex-start">
                ${showSame ? "Hide" : "Show"} ${same.length} field${same.length === 1 ? "" : "s"} that already match
              </button>` : ""}
            <div class="row" style="gap:10px">
              <button class="btn btn--primary" id="m-go">⚖️ Merge these ${differing.length} decision${differing.length === 1 ? "" : "s"}</button>
              <span class="dim" style="font-size:11px;align-self:center">Reversible — an Undo button appears afterwards</span>
            </div>
          </div>`;

        host.querySelectorAll('input[type="radio"]').forEach((el) =>
          el.addEventListener("change", (e) => {
            const field = e.target.closest("tr").dataset.field;
            choices[field] = e.target.value;
          }));
        host.querySelector("#m-cancel")?.addEventListener("click", () => {
          state.detailLocked = false;
          host.innerHTML = prev;
          renderDetail();
        });
        host.querySelector("#m-toggle-same")?.addEventListener("click", () => { showSame = !showSame; draw(); });
        host.querySelector("#m-go")?.addEventListener("click", doMerge);
      }

      async function doMerge() {
        const btn = host.querySelector("#m-go");
        btn.disabled = true;
        btn.textContent = "Merging…";
        try {
          const res = await api.mergeReview(reviewId, choices);
          const took = res.fields_changed || [];
          toast(took.length
            ? `Merged — took ${took.length} value${took.length === 1 ? "" : "s"} from ${pv.incoming.name}`
            : `Merged — kept every value from ${pv.keeper.name}`);
          state.detailLocked = false;   // merge done — hand the pane back
          offerUndo(res.snapshot_id, pv);
          await loadCounts();
          await loadList();
        } catch (err) {
          state.detailLocked = false;
          btn.disabled = false;
          btn.textContent = "⚖️ Merge";
          toast(`Merge failed: ${err.message}`, "error");
        }
      }

      // Paint it. This call was missing: draw() was defined and then only
      // ever re-entered from its own "show matching fields" toggle, so the
      // screen stopped at the loading spinner and never rendered — the
      // preview request succeeded, nothing consumed it. Another runtime-only
      // bug `node --check` cannot see; tests/test_dashboard_smoke.py now
      // clicks this button.
      draw();
    }

    /* A persistent undo affordance, not a toast that vanishes in 4 seconds.
       Someone who merges the wrong way usually realises a moment later. */
    function offerUndo(snapshotId, pv) {
      const bar = document.createElement("div");
      bar.className = "row";
      bar.style.cssText = "gap:10px;align-items:center;padding:10px 12px;margin-bottom:12px;" +
        "border:1px solid var(--border);border-left:3px solid var(--brand-lime);" +
        "background:var(--surface-2);border-radius:var(--radius-sm)";
      bar.innerHTML = `<span style="font-size:12.5px">Merged <strong>${esc(pv.incoming.name)}</strong> into <strong>${esc(pv.keeper.name)}</strong></span>
        <span class="grow"></span>
        <button class="btn btn--sm" id="undo-now">↩ Undo</button>
        <button class="btn btn--ghost btn--sm" id="undo-dismiss">✕</button>`;
      detailEl.parentElement.insertBefore(bar, detailEl);
      bar.querySelector("#undo-dismiss").addEventListener("click", () => bar.remove());
      bar.querySelector("#undo-now").addEventListener("click", async () => {
        try {
          const r = await api.undoFieldMerge(snapshotId);
          toast(`Undone — ${esc(r.restored)} restored`);
          bar.remove();
          await loadCounts();
          await loadList();
        } catch (err) { toast(`Undo failed: ${err.message}`, "error"); }
      });
    }

    /* ── Phase Z-4: pagination past the first PAGE_SIZE rows ────────────── */
    function renderPagination() {
      const total = state.primaryTotal;
      if (total <= PAGE_SIZE) { paginationEl.innerHTML = ""; return; }
      const from = total ? state.offset + 1 : 0;
      const to = Math.min(state.offset + PAGE_SIZE, total);
      paginationEl.innerHTML = `
        <button class="btn btn--ghost btn--sm" id="page-prev" ${state.offset === 0 ? "disabled" : ""}>← Previous</button>
        <span class="dim" style="font-size:12px;align-self:center">${fmt.num(from)}–${fmt.num(to)} of ${fmt.num(total)}</span>
        <button class="btn btn--ghost btn--sm" id="page-next" ${to >= total ? "disabled" : ""}>Next →</button>`;
      paginationEl.querySelector("#page-prev")?.addEventListener("click", () => {
        state.offset = Math.max(0, state.offset - PAGE_SIZE); loadList();
      });
      paginationEl.querySelector("#page-next")?.addEventListener("click", () => {
        state.offset += PAGE_SIZE; loadList();
      });
    }

    /* ── Phase Z-4: act on EVERY review matching the current filter ─────── */
    function currentFilters() {
      return {
        status: state.status || undefined,
        review_type: state.type || undefined,
        risk_level: state.risk || undefined,
        evidence_level: state.evidenceLevel || undefined,
        q: state.q || undefined,
        run_id: state.runId || undefined,
      };
    }

    function renderQueueActions() {
      // Only meaningful for the pending queue — approve/reject and majority-
      // resolve both require status=="pending", same gate the per-row bulk
      // toolbar uses.
      if (state.status !== "pending") { queueActions.innerHTML = ""; return; }
      const wantGroups = !state.type || state.type === "field_update";
      const wantSingles = state.type !== "field_update";
      queueActions.innerHTML = `
        <div class="card row wrap" style="gap:10px;align-items:center">
          <strong style="font-size:12px" class="dim">Clear the whole queue, not just this page:</strong>
          ${wantSingles ? `
            <button class="btn btn--ghost btn--sm" id="queue-approve-all">✅ Approve all matching filter</button>
            <button class="btn btn--ghost btn--sm" id="queue-reject-all">✋ Reject all matching filter</button>` : ""}
          ${wantGroups ? `
            <button class="btn btn--ghost btn--sm" id="queue-resolve-majority">🎯 Auto-resolve by majority vote (matching filter)</button>` : ""}
        </div>`;

      queueActions.querySelector("#queue-approve-all")?.addEventListener("click", () => queueResolveFiltered("approve"));
      queueActions.querySelector("#queue-reject-all")?.addEventListener("click", () => queueResolveFiltered("reject"));
      queueActions.querySelector("#queue-resolve-majority")?.addEventListener("click", () => queueResolveGrouped());
    }

    async function queueResolveFiltered(action) {
      if (state.busy) return;
      state.busy = true;
      try {
        const dry = await api.bulkResolveFiltered(currentFilters(), action, true);
        if (!dry.matched) { toast("Nothing matches this filter"); return; }
        const verb = action === "approve" ? "Approve" : "Reject";
        const byType = Object.entries(dry.by_review_type || {}).map(([t, n]) => `${n} ${TYPE_LABEL[t] || t}`).join(", ");
        if (!confirmAction(`${verb} ALL ${dry.matched} review${dry.matched === 1 ? "" : "s"} matching the current filter (${byType})? This cannot be selectively undone — check the filter is what you mean before confirming.`)) return;

        const res = await api.bulkResolveFiltered(currentFilters(), action, false);
        const failN = (res.failed || []).length;
        toast(
          failN ? `${verb}d ${res.resolved} of ${res.total} — ${failN} failed (see console)` : `${verb}d ${res.resolved} review${res.resolved === 1 ? "" : "s"}`,
          failN ? "error" : "ok",
        );
        if (failN) console.warn(`[Reviews] bulk-resolve-filtered failures:`, res.failed);
        state.offset = 0;
        await loadCounts();
        await loadList();
      } catch (err) {
        toast(`Bulk ${action} failed: ${err.message}`, "error");
      } finally {
        state.busy = false;
      }
    }

    async function queueResolveGrouped() {
      if (state.busy) return;
      state.busy = true;
      try {
        const dry = await api.bulkResolveGrouped(currentFilters(), true);
        if (!dry.groups_matched) { toast("Nothing matches this filter"); return; }
        if (!dry.groups_resolvable) {
          toast(`All ${dry.groups_matched} matching startup${dry.groups_matched === 1 ? "" : "s"} ${dry.groups_matched === 1 ? "has" : "have"} at least one tied field — none can be auto-resolved. Pick manually per startup.`);
          return;
        }
        if (!confirmAction(
          `Auto-resolve ${dry.groups_resolvable} of ${dry.groups_matched} matching startup${dry.groups_matched === 1 ? "" : "s"} by majority vote (${dry.fields_would_apply} field${dry.fields_would_apply === 1 ? "" : "s"} total)? ` +
          `${dry.groups_skipped_tie} startup${dry.groups_skipped_tie === 1 ? "" : "s"} with a true tie on at least one field will be left pending for you to pick manually — never partially resolved.`
        )) return;

        const res = await api.bulkResolveGrouped(currentFilters(), false);
        const errN = (res.errors || []).length;
        toast(
          errN ? `Resolved ${res.groups_resolved} startups — ${errN} failed (see console)` : `Resolved ${res.groups_resolved} startup${res.groups_resolved === 1 ? "" : "s"} by majority vote`,
          errN ? "error" : "ok",
        );
        if (errN) console.warn(`[Reviews] grouped bulk-resolve failures:`, res.errors);
        state.offset = 0;
        await loadCounts();
        await loadList();
      } catch (err) {
        toast(`Majority resolve failed: ${err.message}`, "error");
      } finally {
        state.busy = false;
      }
    }

    function renderBulkToolbar() {
      const n = state.selectedIds.size;
      if (state.status !== "pending" || !n) { bulkToolbar.innerHTML = ""; return; }
      bulkToolbar.innerHTML = `
        <div class="card row wrap" style="gap:10px;align-items:center;background:var(--surface-2)">
          <strong style="font-size:13px">${n} selected</strong>
          <button class="btn btn--primary btn--sm" id="bulk-approve-btn">✅ Approve selected</button>
          <button class="btn btn--danger btn--sm" id="bulk-reject-btn">✋ Reject selected</button>
          <span class="grow"></span>
          <button class="btn btn--ghost btn--sm" id="bulk-clear-btn">Clear selection</button>
        </div>`;

      bulkToolbar.querySelector("#bulk-clear-btn").addEventListener("click", () => {
        state.selectedIds.clear();
        renderList();
      });
      bulkToolbar.querySelector("#bulk-approve-btn").addEventListener("click", () => bulkAct("approve"));
      bulkToolbar.querySelector("#bulk-reject-btn").addEventListener("click", () => bulkAct("reject"));
    }

    async function bulkAct(kind) {
      if (state.busy) return;
      const ids = [...state.selectedIds];
      const verb = kind === "approve" ? "Approve" : "Reject";
      if (!confirmAction(`${verb} ${ids.length} selected review${ids.length === 1 ? "" : "s"}? This applies to each one individually — same effect as clicking ${kind} on each, just in one step.`)) return;

      state.busy = true;
      try {
        const res = kind === "approve" ? await api.bulkApproveReviews(ids) : await api.bulkRejectReviews(ids);
        const done = kind === "approve" ? res.approved : res.rejected;
        const failN = (res.failed || []).length;
        toast(
          failN
            ? `${verb}d ${done} of ${res.total} — ${failN} failed (see console)`
            : `${verb}d ${done} review${done === 1 ? "" : "s"}`,
          failN ? "error" : "ok",
        );
        if (failN) console.warn(`[Reviews] bulk-${kind} failures:`, res.failed);
        state.selectedIds.clear();
        await loadCounts();
        await loadList();
      } catch (err) {
        toast(`Bulk ${kind} failed: ${err.message}`, "error");
      } finally {
        state.busy = false;
      }
    }

    /* ── Phase Y: grouped detail — one section per field, all candidates ── */
    function renderGroupDetail(entry) {
      const risk = RISK[entry.risk_level] || RISK.none;
      const fieldNames = Object.keys(entry.fields);

      detailEl.innerHTML = `
        <div class="stack" style="gap:16px">
          <div class="row">
            <span style="font-size:18px">${risk.mark}</span>
            <span class="card__title" style="font-size:15px">${esc(entry.master_name)}</span>
            <span class="chip ${risk.chip}">${risk.label}</span>
            <span class="dim" style="margin-left:auto;font-size:12px">${entry.review_count} pending change${entry.review_count === 1 ? "" : "s"} · first seen ${fmt.dateTime(entry.first_seen)}</span>
          </div>

          ${entry.master_missing ? `<div class="card" style="background:var(--surface-2)"><span class="chip chip--danger">Record no longer exists</span></div>` : ""}

          <div class="dim" style="font-size:12px">Decide each field on its own — Apply or Reject settles just that field and leaves the rest pending. Or pick across all of them and use "Apply all selections" at the bottom.</div>

          <div class="stack" style="gap:14px">
            ${fieldNames.map((field) => {
              const candidates = entry.fields[field] || [];
              const current = entry.current ? entry.current[field] : undefined;
              return `
                <div class="card" style="background:var(--surface-2)" data-field-card="${esc(field)}">
                  <div class="row" style="margin-bottom:6px">
                    <div class="dim" style="font-size:11px;text-transform:uppercase;letter-spacing:.06em">${esc(field)}</div>
                    <button class="btn btn--ghost btn--sm" data-history-toggle="${esc(field)}"
                            style="margin-left:auto;font-size:11px;padding:2px 8px">history ▾</button>
                  </div>
                  <div style="font-size:12px;margin-bottom:8px"><span class="dim">Current:</span> ${esc(String(current ?? ""), "—")}</div>
                  <div data-history-panel="${esc(field)}" style="display:none;margin-bottom:8px"></div>
                  <div class="stack" style="gap:6px">
                    ${candidates.map((c, i) => `
                      <label class="row" style="gap:8px;align-items:flex-start;font-size:13px">
                        <input type="radio" name="field-${esc(field)}" value="${i}" ${i === 0 ? "checked" : ""} style="margin-top:3px">
                        <span>${esc(String(c.value), "—")}
                          <span class="dim" style="font-size:11px">
                            ${c.count > 1 ? `× ${c.count} · ` : ""}${esc(c.sources?.[0]?.source, "unknown source")} · ${fmt.dateTime(c.sources?.[0]?.at)}
                          </span>
                        </span>
                      </label>`).join("")}
                    <label class="row" style="gap:8px;font-size:13px">
                      <input type="radio" name="field-${esc(field)}" value="__reject__">
                      <span class="dim">Reject — keep current value</span>
                    </label>
                  </div>
                  <div class="row" style="gap:8px;margin-top:10px">
                    <button class="btn btn--sm" data-apply-field="${esc(field)}">Apply this field</button>
                    <button class="btn btn--ghost btn--sm" data-reject-field="${esc(field)}">Reject</button>
                    <span class="dim" style="font-size:11px;align-self:center">settles only ${esc(field)}</span>
                  </div>
                </div>`;
            }).join("")}
          </div>

          <div class="row wrap" style="gap:10px">
            <button class="btn btn--primary" id="apply-group-btn">✅ Apply all selections</button>
            <span class="dim" style="font-size:12px;align-self:center">Applies every pick above at once and closes all ${entry.review_count} pending change${entry.review_count === 1 ? "" : "s"} for this startup.</span>
          </div>
        </div>`;

      detailEl.querySelector("#apply-group-btn").addEventListener("click", () => applyGroupSelections(entry));

      // Touching a picker is unsaved intent. The 10s poll repaints this pane,
      // which would reset every radio back to its default and throw away
      // picks made across several fields — the same clobber the merge screen
      // hit, just less obvious because the pane looks unchanged afterwards.
      // Cleared once a decision completes, or when another row is selected.
      detailEl.querySelectorAll('input[type="radio"][name^="field-"]').forEach((el) =>
        el.addEventListener("change", () => { state.detailLocked = true; }));

      detailEl.querySelectorAll("[data-apply-field]").forEach((btn) =>
        btn.addEventListener("click", () => {
          const field = btn.getAttribute("data-apply-field");
          const checked = detailEl.querySelector(`input[name="field-${CSS.escape(field)}"]:checked`);
          if (!checked || checked.value === "__reject__") {
            // "Apply" with Reject selected is ambiguous — say so rather than
            // silently doing one of the two.
            toast(`Pick a value for "${field}" first, or use Reject.`, "error");
            return;
          }
          const cand = entry.fields[field][parseInt(checked.value, 10)];
          resolveOneField(entry, field, { value: cand.value });
        }));

      detailEl.querySelectorAll("[data-reject-field]").forEach((btn) =>
        btn.addEventListener("click", () => {
          const field = btn.getAttribute("data-reject-field");
          resolveOneField(entry, field, { reject: true });
        }));

      detailEl.querySelectorAll("[data-history-toggle]").forEach((btn) =>
        btn.addEventListener("click", () => toggleFieldHistory(entry, btn)));
    }

    /* ── Per-field history, loaded on demand ─────────────────────────────── */
    async function toggleFieldHistory(entry, btn) {
      const field = btn.getAttribute("data-history-toggle");
      const panel = detailEl.querySelector(`[data-history-panel="${CSS.escape(field)}"]`);
      if (!panel) return;
      if (panel.style.display !== "none") {
        panel.style.display = "none";
        btn.textContent = "history ▾";
        return;
      }
      panel.style.display = "";
      btn.textContent = "history ▴";
      if (panel.dataset.loaded) return;       // fetch once per open detail
      panel.innerHTML = `<span class="dim" style="font-size:11px">loading…</span>`;
      try {
        const res = await api.fieldHistory(entry.master_id, field);
        const rows = res.history || [];
        panel.dataset.loaded = "1";
        panel.innerHTML = rows.length
          ? `<div class="stack" style="gap:3px;font-size:11px;border-left:2px solid var(--border);padding-left:8px">
               ${rows.map((h) => `
                 <div class="row" style="gap:6px">
                   <span class="dim" style="min-width:104px">${fmt.dateTime(h.changed_at)}</span>
                   <span class="chip" style="font-size:10px">${esc(h.source, "system")}</span>
                   <span>${esc(String(h.old ?? ""), "—")} → <strong>${esc(String(h.new ?? ""), "—")}</strong></span>
                 </div>`).join("")}
             </div>`
          : `<span class="dim" style="font-size:11px">No recorded changes for this field yet. The change log only covers edits made since it shipped — there is no backfill.</span>`;
      } catch (err) {
        panel.innerHTML = `<span class="dim" style="font-size:11px">Couldn't load history: ${esc(err.message)}</span>`;
      }
    }

    async function resolveOneField(entry, field, { value = null, reject = false }) {
      if (state.busy) return;
      const what = reject
        ? `Reject every proposed value for "${field}" and keep the current one?`
        : `Apply "${String(value)}" to ${field}?`;
      if (!confirmAction(`${what}\n\nThis settles only ${field}. Any other pending fields for "${entry.master_name}" stay in the queue.`)) return;

      state.busy = true;
      try {
        const res = await api.resolveSingleField(entry.master_id, field, { value, reject });
        const closed = res.approved_review_ids.length + res.rejected_review_ids.length;
        toast(
          `${res.applied ? `Applied ${field}` : `Rejected ${field}`}` +
          (closed ? ` · closed ${closed} review${closed === 1 ? "" : "s"}` : "") +
          (res.still_pending_review_ids.length
            ? ` · ${res.still_pending_review_ids.length} review${res.still_pending_review_ids.length === 1 ? "" : "s"} still have other fields`
            : "")
        );
        // A deliberate action: hand the pane back so it repaints with this
        // field settled. Other unsubmitted picks reset on that repaint — a
        // visible result of the user's own click, unlike the silent poll wipe.
        state.detailLocked = false;
        await loadCounts();
        await loadList(true);
      } catch (err) {
        toast(`Could not settle ${field}: ${err.message}`, "error");
      } finally {
        state.busy = false;
      }
    }

    async function applyGroupSelections(entry) {
      if (state.busy) return;
      const selections = {};
      for (const field of Object.keys(entry.fields)) {
        const checked = detailEl.querySelector(`input[name="field-${CSS.escape(field)}"]:checked`);
        if (checked && checked.value !== "__reject__") {
          const idx = parseInt(checked.value, 10);
          selections[field] = entry.fields[field][idx].value;
        }
      }
      if (!confirmAction(`Apply the selected values to "${entry.master_name}" and close all ${entry.review_count} pending change${entry.review_count === 1 ? "" : "s"}? Unselected candidates are rejected and won't be re-proposed.`)) return;

      state.busy = true;
      try {
        const res = await api.resolveGroupedReviews(entry.master_id, selections);
        toast(`Applied ${res.applied_fields.length} field${res.applied_fields.length === 1 ? "" : "s"} · closed ${res.approved_review_ids.length + res.rejected_review_ids.length} review${(res.approved_review_ids.length + res.rejected_review_ids.length) === 1 ? "" : "s"}`);
        state.detailLocked = false;
        await loadCounts();
        await loadList();
      } catch (err) {
        toast(`Apply failed: ${err.message}`, "error");
      } finally {
        state.busy = false;
      }
    }

    async function renderDetail() {
      // The detail pane is repainted by the 10s background poll. When the
      // user has opened a sub-screen in it (the field-by-field merge table,
      // mid-decision), that repaint silently destroyed their work-in-progress
      // — reported as "I click merge field by field, it processes, and
      // nothing happens": the screen DID render, then the next poll tick
      // wiped it. Anything that takes over the pane sets state.detailLocked
      // and is responsible for clearing it again.
      if (state.detailLocked) return;
      if (!state.selectedId) {
        detailEl.innerHTML = `<div class="empty" style="padding:40px">Select an item from the list</div>`;
        return;
      }
      const entry = state.reviews.find((r) => r.entryId === state.selectedId);
      if (!entry) {
        detailEl.innerHTML = `<div class="empty" style="padding:40px">Select an item from the list</div>`;
        return;
      }
      if (entry.kind === "group") {
        renderGroupDetail(entry);
        return;
      }

      detailEl.innerHTML = `<div class="row" style="padding:40px;justify-content:center"><span class="spinner"></span></div>`;

      let rv;
      try { rv = await api.getReview(entry.id); }
      catch (err) { detailEl.innerHTML = `<div class="empty">${esc(err.message)}</div>`; return; }

      const risk = RISK[rv.risk_level] || RISK.none;
      const isPending = rv.status === "pending";

      const m = rv.master || {}, inc = rv.incoming || {};
      const bodyHtml = `
        <div class="grid-2">
          <div>
            <div class="row" style="margin-bottom:6px">
              <div class="dim" style="font-size:11px;text-transform:uppercase;letter-spacing:.06em">Existing record</div>
              ${isPending ? `<button class="btn btn--ghost btn--sm" style="margin-left:auto;font-size:11px" id="delete-master-btn" title="Neither merge nor keep — permanently remove this record">🗑 Delete</button>` : ""}
            </div>
            <div class="stack" style="gap:4px;font-size:13px">
              ${PROFILE_FIELDS.map((f) => `<div><span class="dim">${f}:</span> ${esc(m[f], "—")}</div>`).join("")}
            </div>
          </div>
          <div>
            <div class="row" style="margin-bottom:6px">
              <div class="dim" style="font-size:11px;text-transform:uppercase;letter-spacing:.06em">Incoming record</div>
              ${isPending ? `<button class="btn btn--ghost btn--sm" style="margin-left:auto;font-size:11px" id="delete-incoming-btn" title="Neither merge nor keep — permanently remove this record">🗑 Delete</button>` : ""}
            </div>
            <div class="stack" style="gap:4px;font-size:13px">
              ${PROFILE_FIELDS.map((f) => `<div><span class="dim">${f}:</span> ${esc(inc[f], "—")}</div>`).join("")}
            </div>
          </div>
        </div>`;

      // Two unrelated evidence shapes share this field: the duplicate-matcher
      // produces {signal_name: 0.0-1.0, ...} (rendered as % bars below), but
      // web-verification evidence is {web_verdict: {...}, search_results:
      // [...]} — dicts/arrays, not scores. Treating them as numbers silently
      // produced "NaN%" bars and hid exactly the source citations a reviewer
      // needs (found live 29 Jul, reviewing a Boston/Greece location
      // conflict). Detect the shape and render each appropriately.
      const rawEvidence = rv.evidence || {};
      const isWebVerification = "web_verdict" in rawEvidence || "search_results" in rawEvidence;

      let evidenceRows;
      if (isWebVerification) {
        const sources = rawEvidence.search_results || [];
        evidenceRows = `
          <div class="dim" style="font-size:11px;text-transform:uppercase;letter-spacing:.06em;margin-bottom:6px">Sources checked (live web search)</div>
          <div class="stack" style="gap:4px">
            ${sources.length ? sources.map((s) => `
              <div style="font-size:12px">
                <a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.title || s.url)}</a>
              </div>`).join("") : '<div class="dim" style="font-size:12px">No search results recorded</div>'}
          </div>`;
      } else {
        // Only the numeric match signals are percentage bars. evidence carries
        // non-numeric entries too — `evidence_level` is the string "minimal"/
        // "normal", and the resolver writes structured verdict objects
        // (adjudication / field_adjudications / resolved_fields) — and
        // multiplying those by 100 rendered a literal "NaN%" bar, reported
        // from the dashboard. Numbers get a bar, the evidence level gets a
        // chip, and the verdict objects are skipped: their content is already
        // shown in the AI-explanation banner below, in prose.
        const entries = Object.entries(rawEvidence).filter(([k]) => k !== "aggregate_score");
        const numeric = entries.filter(([, v]) => typeof v === "number" && isFinite(v));
        const level = rawEvidence.evidence_level;

        evidenceRows = numeric.map(([k, v]) => `
            <div>
              <div class="row" style="font-size:12px"><span class="dim">${esc(k.replace(/_/g, " "))}</span>
                <span class="mono" style="margin-left:auto">${(v * 100).toFixed(0)}%</span></div>
              <div style="background:var(--surface-2);border-radius:4px;height:6px;overflow:hidden;margin-top:3px">
                <span style="display:block;height:100%;width:${Math.max(0, Math.min(100, v * 100))}%;background:var(--brand-lime)"></span>
              </div>
            </div>`).join("");

        if (level) {
          evidenceRows += `
            <div class="row" style="font-size:12px;margin-top:2px">
              <span class="dim">evidence level</span>
              <span class="chip ${level === "minimal" ? "chip--warning" : ""}" style="margin-left:auto;font-size:11px">
                ${esc(level)}${level === "minimal" ? " — both sides are bare stubs" : ""}
              </span>
            </div>`;
        }
      }

      // For a web-verification review, llm_explanation is never populated
      // (that field is written by the SEPARATE nightly review_explainer job,
      // which only runs for duplicate-matcher reviews) — so the banner
      // always read "No AI explanation yet" even though the model's own
      // verdict summary (which can explicitly call out a conflict, like
      // "Boston vs. Greece") was sitting right there in evidence, unused.
      const explanation = rv.llm_explanation || rawEvidence.web_verdict?.summary || null;
      const explanationLabel = rv.llm_explanation ? "🤖 AI explanation (not a decision)" : "🌐 Web-verification summary";

      detailEl.innerHTML = `
        <div class="stack" style="gap:16px">
          <div class="row">
            <span style="font-size:18px">${risk.mark}</span>
            <span class="card__title" style="font-size:15px">${TYPE_LABEL[rv.review_type]}</span>
            <span class="chip ${risk.chip}">${risk.label}</span>
            <span class="dim" style="margin-left:auto;font-size:12px">via ${esc(rv.source, "unknown source")}</span>
          </div>

          ${explanation ? `
            <div class="card" style="background:var(--surface-2)">
              <div class="dim" style="font-size:11px;text-transform:uppercase;letter-spacing:.06em;margin-bottom:6px">${explanationLabel}</div>
              <div style="font-size:13px;line-height:1.5">${esc(explanation)}</div>
            </div>` : `<div class="dim" style="font-size:12px">No AI explanation yet — added nightly at 02:00</div>`}

          ${bodyHtml}

          <div class="card">
            <div class="card__head"><span class="card__title">${isWebVerification ? "Evidence" : "Match evidence"}</span></div>
            <div class="stack" style="gap:8px">${evidenceRows || '<div class="dim" style="font-size:12px">No evidence recorded</div>'}</div>
          </div>

          ${isPending ? `
            <div class="row wrap" style="gap:10px">
              <button class="btn btn--primary" id="approve-btn">
                ✅ Merge — same company
              </button>
              <button class="btn btn--danger" id="reject-btn">
                ✋ Keep separate — different
              </button>
              ${rv.review_type === "possible_duplicate" && rv.incoming_id ? `
                <button class="btn" id="merge-fields-btn" title="Choose field by field which value survives. Reversible.">
                  ⚖️ Merge field by field…
                </button>` : ""}
            </div>` : `
            <div class="row wrap" style="gap:10px;align-items:center">
              <span class="chip">Already ${esc(rv.status)}</span>
              ${rv.status === "approved" ? `
                <button class="btn btn--ghost btn--sm" id="undo-merge-btn" title="Reinsert the deleted record from this review's saved snapshot; the record it was merged into is left as-is">
                  ↩️ Undo merge
                </button>` : ""}
            </div>`}
        </div>`;

      detailEl.querySelector("#merge-fields-btn")?.addEventListener("click", () => openMergeScreen(entry.id));
      detailEl.querySelector("#approve-btn")?.addEventListener("click", () => act("approve", entry.id));
      detailEl.querySelector("#reject-btn")?.addEventListener("click", () => act("reject", entry.id));
      detailEl.querySelector("#delete-master-btn")?.addEventListener("click", () =>
        act("delete", entry.id, "master", rv.master_name || rv.master?.name));
      detailEl.querySelector("#delete-incoming-btn")?.addEventListener("click", () =>
        act("delete", entry.id, "incoming", rv.incoming_name || rv.incoming?.name));
      detailEl.querySelector("#undo-merge-btn")?.addEventListener("click", () =>
        act("undo-merge", entry.id, null, rv.incoming_name || rv.incoming?.name));
    }

    async function act(kind, reviewId, target, recordName) {
      const id = reviewId || state.selectedId;
      if (state.busy || !id) return;
      if (kind === "delete") {
        const label = recordName ? `"${recordName}"` : "this record";
        if (!confirmAction(`Permanently delete ${label}? This removes it from the database entirely — not a merge, not a reject. This cannot be undone.`)) return;
      }
      if (kind === "undo-merge") {
        const label = recordName ? `"${recordName}"` : "the merged-away record";
        if (!confirmAction(`Undo this merge? ${label} will be reinserted as its own record from the review's saved data. The record it was merged into is left as-is.`)) return;
      }
      state.busy = true;
      try {
        if (kind === "approve") await api.approveReview(id);
        else if (kind === "reject") await api.rejectReview(id);
        else if (kind === "undo-merge") await api.undoMerge(id);
        else await api.deleteReview(id, target);
        toast(
          kind === "approve" ? "Approved" :
          kind === "reject" ? "Rejected — won't be flagged again" :
          kind === "undo-merge" ? "Merge undone — record restored" :
          "Deleted"
        );
        await loadCounts();
        await loadList();
      } catch (err) {
        toast(`${kind === "approve" ? "Approve" : kind === "reject" ? "Reject" : kind === "undo-merge" ? "Undo" : "Delete"} failed: ${err.message}`, "error");
      } finally {
        state.busy = false;
      }
    }

    /* ── Keyboard shortcuts: j/k navigate, a approve, r reject ──────────── */
    function onKeydown(e) {
      if (["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName)) return;
      if (!state.reviews.length) return;
      const idx = state.reviews.findIndex((r) => r.entryId === state.selectedId);

      if (e.key === "j" || e.key === "ArrowDown") {
        e.preventDefault();
        state.selectedId = state.reviews[Math.min(idx + 1, state.reviews.length - 1)].entryId;
        renderList(); renderDetail();
      } else if (e.key === "k" || e.key === "ArrowUp") {
        e.preventDefault();
        state.selectedId = state.reviews[Math.max(idx - 1, 0)].entryId;
        renderList(); renderDetail();
      } else if (e.key === "a" || e.key === "r") {
        // Grouped (field_update) entries don't have a single approve/reject
        // action — the per-field picker is the only way to resolve them.
        const entry = state.reviews[idx];
        if (entry && entry.kind === "single") act(e.key === "a" ? "approve" : "reject", entry.id);
      }
    }
    document.addEventListener("keydown", onKeydown);

    mountBatchRow();
    loadResolverCard();
    loadCounts();
    loadList();

    // Poll the list (not the detail — avoid yanking focus/scroll from an open
    // detail panel) so new reviews appear without a manual refresh.
    const stopPoll = poll(() => { loadCounts(); loadList(true); }, 10000);

    return () => {
      document.removeEventListener("keydown", onKeydown);
      stopPoll();
    };
  },
};
