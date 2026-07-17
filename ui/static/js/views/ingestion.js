/* ══════════════════════════════════════════════════════════════════════════
   SCOUT — Ingestion Control Center (the headline feature)

   Trigger any ingestion job and watch it run live: current source, elapsed
   time, ticking counters, batch progress ("source 3 of 19"), GPU-lock state,
   run history, and the next scheduled sweep. Polls /ingestion/status every
   2s — RunRecord now carries live_metrics + batch fields (backend change in
   this same phase) so counters move mid-run, not just after it finishes.
   ══════════════════════════════════════════════════════════════════════════ */

import { api, fmt, esc } from "../api.js";
import { poll, toast } from "../router.js";

const METRIC_LABELS = [
  ["pages_crawled", "Pages crawled"],
  ["chunks_created", "Chunks created"],
  ["chunks_filtered", "Chunks filtered"],
  ["qwen_calls", "LLM calls"],
  ["startups_extracted", "Startups found"],
  ["startups_inserted", "New masters"],
  ["updates_staged", "Updates staged"],
  ["duplicates_staged", "Duplicates staged"],
];

const STATUS_DOT = { running: "dot--live", failed: "dot--error", skipped: "dot--error" };

// Fixed cron schedule (api/main.py) — mirrored here for the "next run" countdown.
// Server + browser are on the same office LAN, so local time is a safe match.
const SCHEDULE = [
  { label: "Full sweep", days: [1, 4], hour: 5, minute: 0 },   // Mon(1) + Thu(4) 05:00
  { label: "Gmail top-up", days: null, hour: 13, minute: 0 },   // daily
  { label: "AI review explanations", days: null, hour: 2, minute: 0 }, // daily
];

function nextOccurrence({ days, hour, minute }) {
  const now = new Date();
  for (let add = 0; add < 8; add++) {
    const d = new Date(now);
    d.setDate(d.getDate() + add);
    d.setHours(hour, minute, 0, 0);
    if (d <= now) continue;
    if (days && !days.includes(d.getDay())) continue;
    return d;
  }
  return null;
}

function untilText(date) {
  if (!date) return "—";
  const ms = date - Date.now();
  const h = Math.floor(ms / 3600000);
  const m = Math.floor((ms % 3600000) / 60000);
  return h > 24 ? `in ${Math.floor(h / 24)}d ${h % 24}h` : h > 0 ? `in ${h}h ${m}m` : `in ${m}m`;
}

export default {
  title: "Ingestion Control",

  async mount(el) {
    el.innerHTML = `<div class="stack">
      <div class="card"><div class="skeleton" style="height:120px"></div></div>
      <div class="skeleton" style="height:180px"></div>
    </div>`;

    let sourcesCache = null;
    let busy = false; // prevents double-submits while a trigger request is in flight

    const triggerRun = async (label, fn) => {
      if (busy) return;
      busy = true;
      try {
        await fn();
        toast(`${label} started`);
      } catch (err) {
        toast(`${label} failed to start: ${err.message}`, "error");
      } finally {
        busy = false;
        render(); // immediate refresh so the panel reflects the new run right away
      }
    };

    async function render() {
      let status, sources;
      try {
        [status, sources] = await Promise.all([api.ingestionStatus(), api.listSources()]);
        sourcesCache = sources;
      } catch (err) {
        el.innerHTML = `<div class="empty"><div class="empty__title">Couldn't load ingestion status</div>
                         <div>${esc(err.message)}</div></div>`;
        return;
      }

      const running = status.current_run;
      const disabled = !!running || busy;

      el.innerHTML = "";

      /* ── Trigger bar ─────────────────────────────────────────────────── */
      const triggerCard = document.createElement("div");
      triggerCard.className = "card";
      triggerCard.innerHTML = `
        <div class="card__head">
          <span class="card__title">Run ingestion</span>
          <span class="card__hint" style="margin-left:auto">
            ${running ? "A run is already in progress" : "GPU mutex serializes everything — safe to trigger anytime"}
          </span>
        </div>
        <div class="row wrap" style="gap:8px">
          <button class="btn btn--primary" data-act="all" ${disabled ? "disabled" : ""}>
            ▶ Run full sweep
          </button>
          <button class="btn" data-act="rss" ${disabled ? "disabled" : ""}>RSS feeds</button>
          <button class="btn" data-act="newsletters" ${disabled ? "disabled" : ""}>Newsletters</button>
          <button class="btn" data-act="accelerators" ${disabled ? "disabled" : ""}>Accelerators</button>
          <button class="btn" data-act="universities" ${disabled ? "disabled" : ""}>Universities</button>
          <span class="grow"></span>
          <select class="select" id="targeted-select" style="max-width:240px" ${disabled ? "disabled" : ""}>
            <option value="">Run a specific source…</option>
            ${(sources.web_sources || []).map((s) =>
              `<option value="${esc(s.source_id)}">${esc(s.source_name)}</option>`).join("")}
          </select>
          <button class="btn" data-act="targeted" ${disabled ? "disabled" : ""}>Run</button>
        </div>`;
      el.appendChild(triggerCard);

      triggerCard.querySelector('[data-act="all"]')?.addEventListener("click", () =>
        triggerRun("Full sweep", api.runAll));
      triggerCard.querySelector('[data-act="rss"]')?.addEventListener("click", () =>
        triggerRun("RSS ingestion", api.runRss));
      triggerCard.querySelector('[data-act="newsletters"]')?.addEventListener("click", () =>
        triggerRun("Newsletter ingestion", api.runNewsletters));
      triggerCard.querySelector('[data-act="accelerators"]')?.addEventListener("click", () =>
        triggerRun("Accelerator sweep", api.runAccelerators));
      triggerCard.querySelector('[data-act="universities"]')?.addEventListener("click", () =>
        triggerRun("University sweep", api.runUniversities));
      triggerCard.querySelector('[data-act="targeted"]')?.addEventListener("click", () => {
        const sel = triggerCard.querySelector("#targeted-select");
        if (!sel.value) { toast("Pick a source first", "error"); return; }
        const label = sel.options[sel.selectedIndex].text;
        triggerRun(label, () => api.runTargeted({ source_id: sel.value }));
      });

      /* ── Live run panel ──────────────────────────────────────────────── */
      const liveCard = document.createElement("div");
      liveCard.className = "card";
      liveCard.style.marginTop = "var(--gap)";

      if (running) {
        const pct = running.batch_total ? Math.round((running.batch_index / running.batch_total) * 100) : null;
        const m = running.metrics || {};
        liveCard.innerHTML = `
          <div class="card__head">
            <span class="dot dot--live"></span>
            <span class="card__title">${esc(running.source)}</span>
            <span class="chip chip--brand">${esc(running.kind)}</span>
            <span class="dim mono" id="live-elapsed" style="margin-left:auto"></span>
          </div>
          ${pct !== null ? `
            <div class="row" style="gap:10px;margin-bottom:12px">
              <span class="dim" style="font-size:12px;white-space:nowrap">Source ${running.batch_index} of ${running.batch_total}</span>
              <span style="flex:1;background:var(--surface-2);border-radius:4px;height:8px;overflow:hidden">
                <span style="display:block;height:100%;width:${pct}%;background:var(--brand-lime);transition:width .4s"></span>
              </span>
              <span class="dim mono" style="font-size:12px">${pct}%</span>
            </div>` : ""}
          <div class="kpis" style="grid-template-columns:repeat(auto-fit,minmax(120px,1fr))">
            ${METRIC_LABELS.map(([k, label]) => `
              <div style="padding:8px 0">
                <div class="kpi__label">${label}</div>
                <div class="kpi__value" style="font-size:20px">${fmt.num(m[k] ?? 0)}</div>
              </div>`).join("")}
          </div>`;

        // Fast local ticking clock (separate from the 2s data poll) so elapsed
        // time feels alive without hammering the backend.
        const elSpan = liveCard.querySelector("#live-elapsed");
        const tickId = setInterval(() => {
          if (elSpan.isConnected) elSpan.textContent = fmt.elapsed(running.started_at);
          else clearInterval(tickId);
        }, 1000);
        elSpan.textContent = fmt.elapsed(running.started_at);
      } else {
        const lr = status.last_run;
        liveCard.innerHTML = `
          <div class="card__head">
            <span class="dot dot--idle"></span>
            <span class="card__title">Idle</span>
            <span class="dim" style="margin-left:auto">GPU: ${status.gpu_locked ? "busy" : "available"}</span>
          </div>
          ${lr ? `
            <div class="row" style="font-size:13px">
              <span class="dot ${lr.status === "failed" ? "dot--error" : "dot--idle"}"></span>
              <span>Last run: <strong>${esc(lr.source)}</strong></span>
              <span class="chip">${esc(lr.status)}</span>
              <span class="dim">${fmt.dateTime(lr.ended_at)}</span>
              ${lr.error ? `<span class="chip chip--danger" title="${esc(lr.error)}">error</span>` : ""}
            </div>`
            : `<div class="empty" style="padding:16px">No runs yet — trigger one above</div>`}`;
      }
      el.appendChild(liveCard);

      /* ── Next scheduled run ──────────────────────────────────────────── */
      const schedCard = document.createElement("div");
      schedCard.className = "card";
      schedCard.style.marginTop = "var(--gap)";
      schedCard.innerHTML = `
        <div class="card__head"><span class="card__title">Scheduled jobs</span></div>
        <div class="stack" style="gap:8px">
          ${SCHEDULE.map((s) => {
            const next = nextOccurrence(s);
            return `<div class="row" style="font-size:13px">
              <span class="truncate grow">${esc(s.label)}</span>
              <span class="dim">${next ? next.toLocaleString(undefined, { weekday: "short", hour: "2-digit", minute: "2-digit" }) : "—"}</span>
              <span class="chip">${untilText(next)}</span>
            </div>`;
          }).join("")}
        </div>`;
      el.appendChild(schedCard);

      /* ── Run history ─────────────────────────────────────────────────── */
      const histCard = document.createElement("div");
      histCard.className = "card";
      histCard.style.marginTop = "var(--gap)";
      const rows = status.history || [];
      histCard.innerHTML = `
        <div class="card__head"><span class="card__title">Recent runs</span></div>
        ${rows.length ? `
          <div class="table-wrap">
            <table class="table">
              <thead><tr>
                <th>Source</th><th>Kind</th><th>Status</th><th>Started</th><th>Result</th>
              </tr></thead>
              <tbody>
                ${rows.map((r) => {
                  const m = r.metrics || {};
                  const summary = r.kind === "web"
                    ? `${fmt.num(m.startups_extracted ?? 0)} found · ${fmt.num(m.startups_inserted ?? 0)} new`
                    : r.kind === "newsletter" ? `${fmt.num(m.startups_stored ?? 0)} stored`
                    : "—";
                  return `<tr>
                    <td class="truncate" style="max-width:220px">${esc(r.source)}</td>
                    <td><span class="chip">${esc(r.kind)}</span></td>
                    <td><span class="row" style="gap:6px">
                          <span class="dot ${STATUS_DOT[r.status] || "dot--idle"}"></span>${esc(r.status)}</span></td>
                    <td class="dim">${fmt.dateTime(r.started_at)}</td>
                    <td class="${r.error ? "" : "dim"}" title="${esc(r.error || "")}">
                      ${r.error ? `⚠ ${esc(r.error.slice(0, 60))}` : summary}</td>
                  </tr>`;
                }).join("")}
              </tbody>
            </table>
          </div>` : `<div class="empty" style="padding:16px">No runs yet</div>`}`;
      el.appendChild(histCard);
    }

    return poll(render, 2000);
  },
};
