/* ══════════════════════════════════════════════════════════════════════════
   SCOUT — Ingestion Control Center (the headline feature)

   Trigger any ingestion job and watch it run live: current source, elapsed
   time, ticking counters, batch progress ("source 3 of 19"), GPU-lock state,
   run history, and the next scheduled sweep. Polls /ingestion/status every
   2s — RunRecord now carries live_metrics + batch fields so counters move
   mid-run, not just after it finishes.

   The trigger bar (buttons + the "run a specific source" dropdown) is built
   ONCE and only rebuilt if the actual source list changes — NOT on every
   2s poll. Earlier this whole page (including the <select>) was torn down
   and recreated every tick, which force-closed the dropdown if a user had
   it open browsing sources (a native <select>'s open list is destroyed the
   instant its underlying DOM node is replaced). Only the disabled state is
   toggled live; only the status region below it (live panel, schedule,
   history) rebuilds every tick.
   ══════════════════════════════════════════════════════════════════════════ */

import { api, fmt, esc } from "../api.js";
import { poll, toast, confirmAction } from "../router.js";

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

const STATUS_DOT = { running: "dot--live", failed: "dot--error", skipped: "dot--error", cancelled: "dot--idle" };

// Fixed cron schedule (api/main.py) — mirrored here for the "next run" countdown.
// Server + browser are on the same office LAN, so local time is a safe match.
const SCHEDULE = [
  { label: "Full sweep", days: [1, 4], hour: 5, minute: 0 },   // Mon(1) + Thu(4) 05:00
  { label: "Gmail top-up", days: null, hour: 13, minute: 0 },   // daily
  { label: "AI review explanations", days: null, hour: 2, minute: 0 }, // daily
  { label: "Verification recheck", days: null, hour: 3, minute: 0 },  // daily
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
      <div class="card" id="trigger-card"><div class="skeleton" style="height:60px"></div></div>
      <div id="status-region"><div class="skeleton" style="height:180px"></div></div>
    </div>`;

    const triggerCard = el.querySelector("#trigger-card");
    const statusRegion = el.querySelector("#status-region");

    let busy = false;              // prevents double-submits while a trigger request is in flight
    let triggerBuilt = false;
    let lastSourceKey = null;      // rebuild the <select> only when the source list actually changes

    const triggerRun = async (label, fn) => {
      if (busy) return;
      busy = true;
      setTriggerDisabled(true);
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

    function setTriggerDisabled(disabled) {
      triggerCard.querySelectorAll("button[data-act]").forEach((b) => { b.disabled = disabled; });
      const sel = triggerCard.querySelector("#targeted-select");
      if (sel) sel.disabled = disabled;
    }

    function buildTriggerCard(sources, disabled, runningNote) {
      triggerCard.innerHTML = `
        <div class="card__head">
          <span class="card__title">Run ingestion</span>
          <span class="card__hint" style="margin-left:auto">${runningNote}</span>
        </div>
        <div class="row wrap" style="gap:8px">
          <button class="btn btn--primary" data-act="all" ${disabled ? "disabled" : ""}>
            ▶ Run full sweep
          </button>
          <button class="btn" data-act="rss" ${disabled ? "disabled" : ""}>RSS feeds</button>
          <button class="btn" data-act="newsletters" ${disabled ? "disabled" : ""}>Newsletters</button>
          <button class="btn" data-act="newsletters-backfill" title="One-time deep sweep — reaches older mail the routine 14-day window never touches, including anything sitting in Promotions" ${disabled ? "disabled" : ""}>Newsletters (full backfill)</button>
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

      triggerCard.querySelector('[data-act="all"]')?.addEventListener("click", () =>
        triggerRun("Full sweep", api.runAll));
      triggerCard.querySelector('[data-act="rss"]')?.addEventListener("click", () =>
        triggerRun("RSS ingestion", api.runRss));
      triggerCard.querySelector('[data-act="newsletters"]')?.addEventListener("click", () =>
        triggerRun("Newsletter ingestion", api.runNewsletters));
      triggerCard.querySelector('[data-act="newsletters-backfill"]')?.addEventListener("click", () => {
        if (!confirmAction(
          "Run a full mailbox backfill? This processes every unread-by-the-pipeline email " +
          "going back years, not just the last 14 days — including anything in Promotions. " +
          "Already-processed emails are always skipped, so this is safe to run more than once."
        )) return;
        triggerRun("Newsletter backfill", () => api.runNewsletters(3650));
      });
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
    }

    async function render() {
      let status, sources, verification;
      try {
        [status, sources] = await Promise.all([api.ingestionStatus(), api.listSources()]);
      } catch (err) {
        // Only replace the page on a hard failure (e.g. first load) — if the
        // trigger bar already rendered once, leave it alone so a transient
        // poll error doesn't yank away an open dropdown either.
        if (!triggerBuilt) {
          el.innerHTML = `<div class="empty"><div class="empty__title">Couldn't load ingestion status</div>
                           <div>${esc(err.message)}</div></div>`;
        }
        return;
      }

      try {
        verification = await api.verificationStatus();
      } catch {
        verification = null; // Data quality card degrades to a fallback state
      }

      const running = status.current_run;
      const disabled = !!running || busy;
      const runningNote = running ? "A run is already in progress" : "GPU mutex serializes everything — safe to trigger anytime";

      // Rebuild the trigger bar (and its <select>) only on first load or when
      // the actual set of sources changes — never on a routine 2s tick, so an
      // open dropdown is never force-closed mid-browse.
      const sourceKey = (sources.web_sources || []).map((s) => s.source_id).join("|");
      if (!triggerBuilt || sourceKey !== lastSourceKey) {
        buildTriggerCard(sources, disabled, runningNote);
        triggerBuilt = true;
        lastSourceKey = sourceKey;
      } else {
        setTriggerDisabled(disabled);
        const hint = triggerCard.querySelector(".card__hint");
        if (hint) hint.textContent = runningNote;
      }

      /* ── Status region: live panel + schedule + history — rebuilt every tick ── */
      statusRegion.innerHTML = "";

      const liveCard = document.createElement("div");
      liveCard.className = "card";

      if (running) {
        const pct = running.batch_total ? Math.round((running.batch_index / running.batch_total) * 100) : null;
        const m = running.metrics || {};
        liveCard.innerHTML = `
          <div class="card__head">
            <span class="dot dot--live"></span>
            <span class="card__title">${esc(running.source)}</span>
            <span class="chip chip--brand">${esc(running.kind)}</span>
            <span class="dim mono" id="live-elapsed" style="margin-left:auto"></span>
            <button class="btn btn--sm btn--danger" id="stop-btn">⏹ Stop</button>
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

        liveCard.querySelector("#stop-btn").addEventListener("click", async (e) => {
          if (!confirmAction(`Stop "${running.source}"? Anything already found stays saved — only what's left is skipped.`)) return;
          const btn = e.currentTarget;
          btn.disabled = true;
          btn.textContent = "Stopping…";
          try {
            await api.stopIngestion(running.run_id);
            toast("Stop requested");
          } catch (err) {
            toast(err.message, "error");
            btn.disabled = false;
            btn.textContent = "⏹ Stop";
          }
        });
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
              <span class="dot ${STATUS_DOT[lr.status] || "dot--idle"}"></span>
              <span>Last run: <strong>${esc(lr.source)}</strong></span>
              <span class="chip">${esc(lr.status)}</span>
              <span class="dim">${fmt.dateTime(lr.ended_at)}</span>
              ${lr.error && lr.status === "failed" ? `<span class="chip chip--danger" title="${esc(lr.error)}">error</span>` : ""}
            </div>`
            : `<div class="empty" style="padding:16px">No runs yet — trigger one above</div>`}`;
      }
      statusRegion.appendChild(liveCard);

      /* ── Data quality (Phase H-3) ──────────────────────────────────────── */
      const dqCard = document.createElement("div");
      dqCard.className = "card";
      dqCard.style.marginTop = "var(--gap)";
      const v = verification?.overall || { unverified: 0, verified: 0, flagged: 0 };
      const noExcerpt = verification?.no_source_excerpt ?? 0;
      dqCard.innerHTML = `
        <div class="card__head">
          <span class="card__title">Data quality</span>
          <span class="dim" style="margin-left:auto;font-size:12px">Source-grounding + AI recheck</span>
        </div>
        <div class="row wrap" style="gap:24px;align-items:flex-end">
          <div><div class="dim" style="font-size:12px">Unverified</div>
               <strong style="font-size:22px">${fmt.num(v.unverified)}</strong></div>
          <div><div class="dim" style="font-size:12px">Verified</div>
               <strong style="font-size:22px;color:var(--brand-lime)">${fmt.num(v.verified)}</strong></div>
          <div><div class="dim" style="font-size:12px">Flagged</div>
               <strong style="font-size:22px;color:var(--danger)">${fmt.num(v.flagged)}</strong></div>
          <span class="grow"></span>
          <button class="btn btn--primary" id="recheck-btn" ${disabled ? "disabled" : ""}>🔍 Recheck now</button>
        </div>
        <div class="row wrap" style="gap:12px;align-items:center;margin-top:14px;padding-top:14px;border-top:1px solid var(--border)">
          <div>
            <div class="dim" style="font-size:12px">No source excerpt — needs a web check</div>
            <strong style="font-size:18px">${fmt.num(noExcerpt)}</strong>
            <span class="dim" style="font-size:12px">(pre-21 Jul backlog, can't be rechecked locally)</span>
          </div>
          <span class="grow"></span>
          <button class="btn" id="webverify-btn" ${disabled ? "disabled" : ""}>🌐 Web-verify backlog</button>
        </div>
        ${verification ? "" : `<div class="dim" style="font-size:12px;margin-top:8px">Couldn't load verification counts</div>`}`;
      dqCard.querySelector("#recheck-btn")?.addEventListener("click", () =>
        triggerRun("Verification recheck", () => api.runRecheck(20)));
      dqCard.querySelector("#webverify-btn")?.addEventListener("click", () =>
        triggerRun("Web verification", () => api.runWebVerify(15)));
      statusRegion.appendChild(dqCard);

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
      statusRegion.appendChild(schedCard);

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
                    : r.kind === "recheck" ? `${fmt.num(m.verified ?? 0)} verified · ${fmt.num(m.flagged ?? 0)} flagged`
                    : r.kind === "web_verify" ? `${fmt.num(m.verified ?? 0)} verified · ${fmt.num(m.staged ?? 0)} staged · ${fmt.num(m.unchanged ?? 0)} unchanged`
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
      statusRegion.appendChild(histCard);
    }

    return poll(render, 2000);
  },
};
