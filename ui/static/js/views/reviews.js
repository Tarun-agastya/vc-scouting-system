/* ══════════════════════════════════════════════════════════════════════════
   SCOUT — Review Inbox
   The pipeline never auto-merges or auto-overwrites; every staged change
   waits here. Two-pane triage (list + detail) with keyboard shortcuts:
   j/k navigate, a approve, r reject — fast review of a growing queue.
   ══════════════════════════════════════════════════════════════════════════ */

import { api, fmt, esc } from "../api.js";
import { toast, confirmAction, poll } from "../router.js";

const RISK = {
  high:    { mark: "🔴", label: "Conflict", chip: "chip--danger" },
  low:     { mark: "🟡", label: "New info", chip: "chip--warning" },
  anomaly: { mark: "⚠️", label: "Anomaly", chip: "chip--warning" },
  none:    { mark: "⚪", label: "—", chip: "" },
};
const TYPE_LABEL = { field_update: "Field change", possible_duplicate: "Possible duplicate", anomaly: "Anomaly" };
const PROFILE_FIELDS = ["name", "description", "website", "city", "country", "funding_stage", "founded_year", "industry"];

function rowLabel(rv) {
  if (rv.review_type === "field_update") {
    const fields = (rv.changed_fields || []).join(", ") || "—";
    return `${esc(rv.master_name)} <span class="dim">· ${esc(fields)}</span>`;
  }
  return `${esc(rv.incoming_name)} <span class="dim">~ ${esc(rv.master_name)}</span>`;
}

export default {
  title: "Review Inbox",

  mount(el) {
    const state = {
      status: "pending", type: "", risk: "",
      reviews: [], selectedId: null, busy: false,
    };

    el.innerHTML = `
      <div class="stack">
        <div class="kpis" id="counts"></div>
        <div class="card">
          <div class="row wrap" style="gap:8px">
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
            <span class="dim" style="margin-left:auto;font-size:12px">j/k navigate · a approve · r reject</span>
          </div>
        </div>
        <div class="inbox-grid" id="inbox-grid" style="align-items:start">
          <div class="card" id="review-list" style="padding:0;max-height:70vh;overflow-y:auto"></div>
          <div class="card" id="review-detail"></div>
        </div>
      </div>`;

    const countsEl = el.querySelector("#counts");
    const listEl = el.querySelector("#review-list");
    const detailEl = el.querySelector("#review-detail");

    el.querySelector("#f-status").addEventListener("change", (e) => { state.status = e.target.value; loadList(); });
    el.querySelector("#f-type").addEventListener("change", (e) => { state.type = e.target.value; loadList(); });
    el.querySelector("#f-risk").addEventListener("change", (e) => { state.risk = e.target.value; loadList(); });

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
      renderList();
      renderDetail();
    }

    function renderList() {
      if (!state.reviews.length) {
        listEl.innerHTML = `<div class="empty" style="padding:24px"><div class="empty__title">Nothing here</div>
                             <div>${state.status === "pending" ? "All clear 🎉" : "No items match these filters"}</div></div>`;
        return;
      }
      listEl.innerHTML = state.reviews.map((rv) => {
        const risk = RISK[rv.risk_level] || RISK.none;
        const active = rv.id === state.selectedId;
        return `
          <div class="row" data-review-id="${esc(rv.id)}"
               style="padding:10px 12px;cursor:pointer;border-bottom:1px solid var(--border);gap:8px;
                      ${active ? "background:var(--brand-lime-glow);border-left:3px solid var(--brand-lime)" : "border-left:3px solid transparent"}">
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

      const evidenceRows = Object.entries(rv.evidence || {})
        .filter(([k]) => k !== "aggregate_score")
        .map(([k, v]) => `
          <div>
            <div class="row" style="font-size:12px"><span class="dim">${esc(k.replace(/_/g, " "))}</span>
              <span class="mono" style="margin-left:auto">${(v * 100).toFixed(0)}%</span></div>
            <div style="background:var(--surface-2);border-radius:4px;height:6px;overflow:hidden;margin-top:3px">
              <span style="display:block;height:100%;width:${v * 100}%;background:var(--brand-lime)"></span>
            </div>
          </div>`).join("");

      detailEl.innerHTML = `
        <div class="stack" style="gap:16px">
          <div class="row">
            <span style="font-size:18px">${risk.mark}</span>
            <span class="card__title" style="font-size:15px">${TYPE_LABEL[rv.review_type]}</span>
            <span class="chip ${risk.chip}">${risk.label}</span>
            <span class="dim" style="margin-left:auto;font-size:12px">via ${esc(rv.source, "unknown source")}</span>
          </div>

          ${rv.llm_explanation ? `
            <div class="card" style="background:var(--surface-2)">
              <div class="dim" style="font-size:11px;text-transform:uppercase;letter-spacing:.06em;margin-bottom:6px">🤖 AI explanation (not a decision)</div>
              <div style="font-size:13px;line-height:1.5">${esc(rv.llm_explanation)}</div>
            </div>` : `<div class="dim" style="font-size:12px">No AI explanation yet — added nightly at 02:00</div>`}

          ${bodyHtml}

          <div class="card">
            <div class="card__head"><span class="card__title">Match evidence</span></div>
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
            </div>` : `<div class="chip">Already ${rv.status}</div>`}
        </div>`;

      detailEl.querySelector("#approve-btn")?.addEventListener("click", () => act("approve"));
      detailEl.querySelector("#reject-btn")?.addEventListener("click", () => act("reject"));
      detailEl.querySelector("#delete-master-btn")?.addEventListener("click", () =>
        act("delete", "master", rv.master_name || rv.master?.name));
      detailEl.querySelector("#delete-incoming-btn")?.addEventListener("click", () =>
        act("delete", "incoming", rv.incoming_name || rv.incoming?.name));
    }

    async function act(kind, target, recordName) {
      if (state.busy || !state.selectedId) return;
      if (kind === "delete") {
        const label = recordName ? `"${recordName}"` : "this record";
        if (!confirmAction(`Permanently delete ${label}? This removes it from the database entirely — not a merge, not a reject. This cannot be undone.`)) return;
      }
      state.busy = true;
      try {
        if (kind === "approve") await api.approveReview(state.selectedId);
        else if (kind === "reject") await api.rejectReview(state.selectedId);
        else await api.deleteReview(state.selectedId, target);
        toast(
          kind === "approve" ? "Approved" :
          kind === "reject" ? "Rejected — won't be flagged again" :
          "Deleted"
        );
        await loadCounts();
        await loadList();
      } catch (err) {
        toast(`${kind === "approve" ? "Approve" : kind === "reject" ? "Reject" : "Delete"} failed: ${err.message}`, "error");
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
