/* ══════════════════════════════════════════════════════════════════════════
   SCOUT — Review Inbox
   The pipeline never auto-merges or auto-overwrites; every staged change
   waits here. Two-pane triage (list + detail) with keyboard shortcuts:
   j/k navigate, a approve, r reject — fast review of a growing queue.
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

function rowLabel(rv) {
  if (rv.review_type === "field_update") {
    const fields = (rv.changed_fields || []).join(", ") || "—";
    return `${esc(rv.master_name)} <span class="dim">· ${esc(fields)}</span>`;
  }
  return `${esc(rv.incoming_name)} <span class="dim">~ ${esc(rv.master_name)}</span>`;
}

export default {
  title: "Review Inbox",

  mount(el, params = {}) {
    const state = {
      status: "pending", type: "", risk: "", q: "", evidenceLevel: "",
      runId: params.run_id || "", // Phase Q2: batch filter — either deep-linked from Browse or picked below
      reviews: [], selectedId: null, busy: false,
      selectedIds: new Set(), // Phase Q4: bulk-select for approve/reject, cleared on any filter change
    };

    el.innerHTML = `
      <div class="stack">
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
        <div id="bulk-toolbar"></div>
        <div class="inbox-grid" id="inbox-grid" style="align-items:start">
          <div class="card" id="review-list" style="padding:0;max-height:70vh;overflow-y:auto"></div>
          <div class="card" id="review-detail"></div>
        </div>
      </div>`;

    const countsEl = el.querySelector("#counts");
    const bulkToolbar = el.querySelector("#bulk-toolbar");
    const listEl = el.querySelector("#review-list");
    const detailEl = el.querySelector("#review-detail");

    el.querySelector("#f-status").addEventListener("change", (e) => { state.status = e.target.value; loadList(); });
    el.querySelector("#f-type").addEventListener("change", (e) => { state.type = e.target.value; loadList(); });
    el.querySelector("#f-risk").addEventListener("change", (e) => { state.risk = e.target.value; loadList(); });
    el.querySelector("#f-evidence").addEventListener("change", (e) => { state.evidenceLevel = e.target.value; loadList(); });
    el.querySelector("#f-q").addEventListener("input", debounce((e) => { state.q = e.target.value; loadList(); }, 300));

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
      row.querySelector("#f-batch-pick").addEventListener("change", (e) => { state.runId = e.target.value; mountBatchRow(); loadList(); });
      row.querySelector("#f-batch-id").addEventListener("input", debounce((e) => { state.runId = e.target.value.trim(); loadList(); }, 300));
      row.querySelector("#f-batch-clear")?.addEventListener("click", () => { state.runId = ""; mountBatchRow(); loadList(); });
    }

    async function loadCounts() {
      try {
        const all = await api.listReviews({ status: "pending", limit: 500 });
        const c = { high: 0, low: 0, anomaly: 0 };
        for (const r of all.reviews) c[r.risk_level] = (c[r.risk_level] || 0) + 1;
        countsEl.innerHTML = `
          <div class="kpi kpi--accent"><div class="kpi__label">Pending</div><div class="kpi__value">${all.total}</div></div>
          <div class="kpi"><div class="kpi__label">🔴 Conflicts</div><div class="kpi__value">${c.high}</div></div>
          <div class="kpi"><div class="kpi__label">🟡 New info</div><div class="kpi__value">${c.low}</div></div>
          <div class="kpi"><div class="kpi__label">⚠️ Anomalies</div><div class="kpi__value">${c.anomaly}</div></div>`;
      } catch { /* non-fatal — counts are a convenience */ }
    }

    async function loadList(preserveSelection = false) {
      try {
        const res = await api.listReviews({
          status: state.status || undefined,
          review_type: state.type || undefined,
          risk_level: state.risk || undefined,
          evidence_level: state.evidenceLevel || undefined,
          q: state.q || undefined,
          run_id: state.runId || undefined,
          limit: 200,
        });
        state.reviews = res.reviews || [];
      } catch (err) {
        listEl.innerHTML = `<div class="empty">${esc(err.message)}</div>`;
        return;
      }

      if (!preserveSelection || !state.reviews.some((r) => r.id === state.selectedId)) {
        state.selectedId = state.reviews[0]?.id || null;
      }
      // Bulk selection: a filter change starts fresh; a background poll
      // refresh keeps it but drops any id that fell out of the loaded list
      // (resolved by someone else, or no longer matches the filter).
      if (!preserveSelection) {
        state.selectedIds.clear();
      } else {
        const loadedIds = new Set(state.reviews.map((r) => r.id));
        for (const id of state.selectedIds) if (!loadedIds.has(id)) state.selectedIds.delete(id);
      }
      renderList();
      renderDetail();
    }

    function renderList() {
      // Phase Q4 (29 Jul, after the queue hit 1,010 pending): bulk-select is
      // only meaningful for pending reviews — approve/reject both require
      // status=="pending". Recomputed every render (not a module-level
      // const) since state.status changes live via the filter dropdown.
      // Scoped to what's currently loaded (up to the 200 fetch limit);
      // filter narrower + repeat for a bigger backlog.
      const bulkEnabled = state.status === "pending";

      if (!state.reviews.length) {
        listEl.innerHTML = `<div class="empty" style="padding:24px"><div class="empty__title">Nothing here</div>
                             <div>${state.status === "pending" ? "All clear 🎉" : "No items match these filters"}</div></div>`;
        renderBulkToolbar();
        return;
      }
      const allLoadedSelected = state.reviews.length > 0 && state.reviews.every((rv) => state.selectedIds.has(rv.id));
      const header = bulkEnabled ? `
        <div class="row" style="padding:8px 12px;border-bottom:1px solid var(--border);gap:8px">
          <input type="checkbox" id="select-all-loaded" ${allLoadedSelected ? "checked" : ""}>
          <span class="dim" style="font-size:11px">Select all ${state.reviews.length} loaded</span>
        </div>` : "";

      listEl.innerHTML = header + state.reviews.map((rv) => {
        const risk = RISK[rv.risk_level] || RISK.none;
        const active = rv.id === state.selectedId;
        return `
          <div class="row" data-review-id="${esc(rv.id)}"
               style="padding:10px 12px;cursor:pointer;border-bottom:1px solid var(--border);gap:8px;
                      ${active ? "background:var(--brand-lime-glow);border-left:3px solid var(--brand-lime)" : "border-left:3px solid transparent"}">
            ${bulkEnabled ? `<input type="checkbox" class="row-select" data-id="${esc(rv.id)}" ${state.selectedIds.has(rv.id) ? "checked" : ""} style="flex:none">` : ""}
            <span style="flex:none">${risk.mark}</span>
            <div class="grow" style="min-width:0">
              <div class="truncate" style="font-size:13px;font-weight:550">${rowLabel(rv)}</div>
              <div class="dim truncate" style="font-size:11px">${TYPE_LABEL[rv.review_type]} · ${fmt.dateTime(rv.created_at)}</div>
            </div>
          </div>`;
      }).join("");

      listEl.querySelectorAll("[data-review-id]").forEach((row) =>
        row.addEventListener("click", () => {
          state.selectedId = row.dataset.reviewId;
          renderList();
          renderDetail();
        }));

      if (bulkEnabled) {
        listEl.querySelector("#select-all-loaded")?.addEventListener("click", (e) => {
          e.stopPropagation();
          if (e.target.checked) state.reviews.forEach((rv) => state.selectedIds.add(rv.id));
          else state.reviews.forEach((rv) => state.selectedIds.delete(rv.id));
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

    async function renderDetail() {
      if (!state.selectedId) {
        detailEl.innerHTML = `<div class="empty" style="padding:40px">Select an item from the list</div>`;
        return;
      }
      detailEl.innerHTML = `<div class="row" style="padding:40px;justify-content:center"><span class="spinner"></span></div>`;

      let rv;
      try { rv = await api.getReview(state.selectedId); }
      catch (err) { detailEl.innerHTML = `<div class="empty">${esc(err.message)}</div>`; return; }

      const risk = RISK[rv.risk_level] || RISK.none;
      const isPending = rv.status === "pending";

      let bodyHtml;
      if (rv.review_type === "field_update") {
        const changes = rv.proposed_changes || {};
        bodyHtml = `
          <div class="table-wrap">
            <table class="table">
              <thead><tr><th>Field</th><th>Current</th><th>Proposed</th><th>From</th></tr></thead>
              <tbody>
                ${Object.entries(changes).map(([field, c]) => `
                  <tr>
                    <td><strong>${esc(field)}</strong></td>
                    <td class="dim">${esc(c.old, "—")}</td>
                    <td>${esc(c.new, "—")}</td>
                    <td class="dim" style="font-size:12px">${esc(c.incoming_source, "—")}<br>${fmt.dateTime(c.incoming_extracted_at)}</td>
                  </tr>`).join("")}
              </tbody>
            </table>
          </div>`;
      } else {
        const m = rv.master || {}, inc = rv.incoming || {};
        bodyHtml = `
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
      }

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
        evidenceRows = Object.entries(rawEvidence)
          .filter(([k]) => k !== "aggregate_score")
          .map(([k, v]) => `
            <div>
              <div class="row" style="font-size:12px"><span class="dim">${esc(k.replace(/_/g, " "))}</span>
                <span class="mono" style="margin-left:auto">${(v * 100).toFixed(0)}%</span></div>
              <div style="background:var(--surface-2);border-radius:4px;height:6px;overflow:hidden;margin-top:3px">
                <span style="display:block;height:100%;width:${v * 100}%;background:var(--brand-lime)"></span>
              </div>
            </div>`).join("");
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
                ✅ ${rv.review_type === "field_update" ? "Apply changes" : "Merge — same company"}
              </button>
              <button class="btn btn--danger" id="reject-btn">
                ✋ ${rv.review_type === "field_update" ? "Keep current (reject)" : "Keep separate — different"}
              </button>
              ${rv.review_type === "field_update" ? `
                <button class="btn btn--ghost" id="delete-master-btn" style="margin-left:auto">
                  🗑 Delete this record
                </button>` : ""}
            </div>` : `
            <div class="row wrap" style="gap:10px;align-items:center">
              <span class="chip">Already ${esc(rv.status)}</span>
              ${rv.status === "approved" && rv.review_type !== "field_update" ? `
                <button class="btn btn--ghost btn--sm" id="undo-merge-btn" title="Reinsert the deleted record from this review's saved snapshot; the record it was merged into is left as-is">
                  ↩️ Undo merge
                </button>` : ""}
            </div>`}
        </div>`;

      detailEl.querySelector("#approve-btn")?.addEventListener("click", () => act("approve"));
      detailEl.querySelector("#reject-btn")?.addEventListener("click", () => act("reject"));
      detailEl.querySelector("#delete-master-btn")?.addEventListener("click", () =>
        act("delete", "master", rv.master_name || rv.master?.name));
      detailEl.querySelector("#delete-incoming-btn")?.addEventListener("click", () =>
        act("delete", "incoming", rv.incoming_name || rv.incoming?.name));
      detailEl.querySelector("#undo-merge-btn")?.addEventListener("click", () =>
        act("undo-merge", null, rv.incoming_name));
    }

    async function act(kind, target, recordName) {
      if (state.busy || !state.selectedId) return;
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
        if (kind === "approve") await api.approveReview(state.selectedId);
        else if (kind === "reject") await api.rejectReview(state.selectedId);
        else if (kind === "undo-merge") await api.undoMerge(state.selectedId);
        else await api.deleteReview(state.selectedId, target);
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
      const idx = state.reviews.findIndex((r) => r.id === state.selectedId);

      if (e.key === "j" || e.key === "ArrowDown") {
        e.preventDefault();
        state.selectedId = state.reviews[Math.min(idx + 1, state.reviews.length - 1)].id;
        renderList(); renderDetail();
      } else if (e.key === "k" || e.key === "ArrowUp") {
        e.preventDefault();
        state.selectedId = state.reviews[Math.max(idx - 1, 0)].id;
        renderList(); renderDetail();
      } else if (e.key === "a") {
        act("approve");
      } else if (e.key === "r") {
        act("reject");
      }
    }
    document.addEventListener("keydown", onKeydown);

    mountBatchRow();
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
