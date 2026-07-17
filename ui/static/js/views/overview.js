/* ══════════════════════════════════════════════════════════════════════════
   SCOUT — Overview (home)
   KPI tiles + charts + recent activity, aggregated client-side from the
   existing /scout/list, /reviews, /ingestion/status, /sources endpoints.
   91 records today — pulling up to 1000 and aggregating in the browser is
   cheap and avoids adding a stats endpoint the backend doesn't need yet.
   ══════════════════════════════════════════════════════════════════════════ */

import { api, fmt, esc } from "../api.js";
import { navigate, poll } from "../router.js";
import { hBarChart, donutChart, areaChart } from "../charts.js";

const TIERS = [
  ["PRIORITY", "Priority", "var(--brand-lime)"],
  ["HIGH_QUALITY_LEAD", "High-quality lead", "var(--brand-lime-dim)"],
  ["INTERESTING", "Interesting", "var(--info)"],
  ["EARLY_DISCOVERY", "Early discovery", "var(--warning)"],
  ["WEAK_SIGNAL", "Weak signal", "var(--ink-3)"],
];

function toDateKey(iso) {
  if (!iso) return null;
  const d = new Date(iso.endsWith?.("Z") ? iso : iso + "Z");
  if (isNaN(d)) return null;
  return d.toISOString().slice(0, 10);
}

function topN(counts, n = 6) {
  return Object.entries(counts)
    .sort((a, b) => b[1] - a[1])
    .slice(0, n);
}

function kpiTile({ label, value, meta, accent, route }) {
  const div = document.createElement("div");
  div.className = `kpi${accent ? " kpi--accent" : ""}`;
  div.innerHTML = `
    <div class="kpi__label">${esc(label)}</div>
    <div class="kpi__value">${esc(value)}</div>
    ${meta ? `<div class="kpi__meta">${meta}</div>` : ""}`;
  if (route) div.addEventListener("click", () => navigate(route));
  return div;
}

function skeletonGrid(n, cls) {
  return `<div class="${cls}">${Array.from({ length: n })
    .map(() => `<div class="skeleton" style="height:96px"></div>`).join("")}</div>`;
}

export default {
  title: "Overview",

  async mount(el) {
    el.innerHTML = `
      <div class="stack">
        ${skeletonGrid(6, "kpis")}
        <div class="grid-2">
          <div class="skeleton" style="height:220px"></div>
          <div class="skeleton" style="height:220px"></div>
        </div>
      </div>`;

    const render = async () => {
      let startups, reviews, status, sources;
      try {
        [startups, reviews, status, sources] = await Promise.all([
          api.listStartups({ limit: 1000, sort: "created_at", order: "desc" }),
          api.listReviews({ status: "pending", limit: 1 }),
          api.ingestionStatus(),
          api.listSources(),
        ]);
      } catch (err) {
        el.innerHTML = `<div class="empty"><div class="empty__title">Couldn't load the overview</div>
                         <div>${esc(err.message)}</div></div>`;
        return;
      }

      const rows = startups.startups || [];
      const now = Date.now();
      const sevenDaysAgo = now - 7 * 86400 * 1000;
      const new7d = rows.filter((s) => {
        const d = s.created_at ? new Date(s.created_at.endsWith?.("Z") ? s.created_at : s.created_at + "Z") : null;
        return d && !isNaN(d) && d.getTime() >= sevenDaysAgo;
      }).length;

      // ── Tier distribution ──────────────────────────────────────────────
      const tierCounts = Object.fromEntries(TIERS.map(([k]) => [k, 0]));
      let unscored = 0;
      for (const s of rows) {
        if (s.score_tier && tierCounts[s.score_tier] !== undefined) tierCounts[s.score_tier]++;
        else unscored++;
      }

      // ── Growth over the last 14 days ───────────────────────────────────
      const days = [];
      for (let i = 13; i >= 0; i--) {
        const d = new Date(now - i * 86400 * 1000);
        days.push(d.toISOString().slice(0, 10));
      }
      const byDay = Object.fromEntries(days.map((d) => [d, 0]));
      for (const s of rows) {
        const key = toDateKey(s.created_at);
        if (key && key in byDay) byDay[key]++;
      }

      // ── Top industries / countries ─────────────────────────────────────
      const industryCounts = {}, countryCounts = {};
      for (const s of rows) {
        if (s.industry) industryCounts[s.industry] = (industryCounts[s.industry] || 0) + 1;
        if (s.country) countryCounts[s.country] = (countryCounts[s.country] || 0) + 1;
      }
      const topIndustries = topN(industryCounts);
      const topCountries = topN(countryCounts);

      // ── Ingestion / sources KPIs ────────────────────────────────────────
      const running = status.current_run;
      const lastRun = status.last_run;
      const sourceCount = (sources?.rss_feeds?.length || 0) + (sources?.web_sources?.length || 0);

      el.innerHTML = "";

      // KPI row
      const kpis = document.createElement("div");
      kpis.className = "kpis";
      kpis.append(
        kpiTile({ label: "Total startups", value: fmt.num(startups.total), accent: true, route: "#/browse" }),
        kpiTile({ label: "New (7 days)", value: fmt.num(new7d), meta: "since last week", route: "#/browse" }),
        kpiTile({
          label: "Pending reviews", value: fmt.num(reviews.total),
          meta: reviews.total ? "needs attention" : "all clear", route: "#/reviews",
        }),
        kpiTile({
          label: "Ingestion",
          value: running ? "Running" : "Idle",
          meta: running
            ? `<span class="row" style="gap:5px"><span class="dot dot--live"></span>${esc(running.source)}</span>`
            : lastRun ? `last: ${esc(lastRun.source)} (${lastRun.status})` : "no runs yet",
          route: "#/ingestion",
        }),
        kpiTile({ label: "Sources", value: fmt.num(sourceCount), meta: "RSS + web", route: "#/sources" }),
        kpiTile({
          label: "GPU", value: status.gpu_locked ? "Busy" : "Idle",
          meta: status.gpu_locked ? "extraction/reasoning in progress" : "available", route: "#/ingestion",
        }),
      );
      el.appendChild(kpis);

      // Charts row 1: tier donut + growth
      const chartsRow1 = document.createElement("div");
      chartsRow1.className = "grid-2";
      chartsRow1.style.marginTop = "var(--gap)";

      const tierCard = document.createElement("div");
      tierCard.className = "card";
      tierCard.innerHTML = `<div class="card__head"><span class="card__title">Score tier distribution</span></div>
                             <div id="chart-tiers"></div>`;
      const growthCard = document.createElement("div");
      growthCard.className = "card";
      growthCard.innerHTML = `<div class="card__head"><span class="card__title">New startups — last 14 days</span></div>
                               <div id="chart-growth"></div>`;
      chartsRow1.append(tierCard, growthCard);
      el.appendChild(chartsRow1);

      const tierLabels = [...TIERS.map(([, label]) => label), ...(unscored ? ["Unscored"] : [])];
      const tierValues = [...TIERS.map(([k]) => tierCounts[k]), ...(unscored ? [unscored] : [])];
      const tierColors = [...TIERS.map(([, , c]) => c), ...(unscored ? ["var(--border-2)"] : [])];
      donutChart(tierCard.querySelector("#chart-tiers"), { labels: tierLabels, values: tierValues, colors: tierColors });
      areaChart(growthCard.querySelector("#chart-growth"), {
        labels: days.map((d) => d.slice(5)), // MM-DD
        values: days.map((d) => byDay[d]),
      });

      // Charts row 2: industries + countries
      const chartsRow2 = document.createElement("div");
      chartsRow2.className = "grid-2";
      chartsRow2.style.marginTop = "var(--gap)";

      const indCard = document.createElement("div");
      indCard.className = "card";
      indCard.innerHTML = `<div class="card__head"><span class="card__title">Top industries</span></div>
                            <div id="chart-industries"></div>`;
      const geoCard = document.createElement("div");
      geoCard.className = "card";
      geoCard.innerHTML = `<div class="card__head"><span class="card__title">Top countries</span></div>
                            <div id="chart-geo"></div>`;
      chartsRow2.append(indCard, geoCard);
      el.appendChild(chartsRow2);

      hBarChart(indCard.querySelector("#chart-industries"), {
        labels: topIndustries.map((x) => x[0]), values: topIndustries.map((x) => x[1]),
      });
      hBarChart(geoCard.querySelector("#chart-geo"), {
        labels: topCountries.map((x) => x[0]), values: topCountries.map((x) => x[1]),
      });

      // Activity row: recent runs + newest startups
      const actRow = document.createElement("div");
      actRow.className = "grid-2";
      actRow.style.marginTop = "var(--gap)";

      const runsCard = document.createElement("div");
      runsCard.className = "card";
      const recentRuns = (status.history || []).slice(0, 6);
      runsCard.innerHTML = `
        <div class="card__head">
          <span class="card__title">Recent ingestion runs</span>
          <span class="card__hint" style="margin-left:auto">
            <a href="#/ingestion" class="muted">View all →</a>
          </span>
        </div>
        ${recentRuns.length ? `<div class="stack" style="gap:8px">${recentRuns.map((r) => `
          <div class="row" style="font-size:13px">
            <span class="dot ${r.status === "running" ? "dot--live" : r.status === "failed" ? "dot--error" : "dot--idle"}"></span>
            <span class="truncate grow">${esc(r.source)}</span>
            <span class="chip">${esc(r.status)}</span>
            <span class="dim" style="font-size:11px">${fmt.dateTime(r.ended_at || r.started_at)}</span>
          </div>`).join("")}</div>`
          : `<div class="empty" style="padding:16px">No runs yet</div>`}`;

      const newestCard = document.createElement("div");
      newestCard.className = "card";
      const newest = rows.slice(0, 6);
      newestCard.innerHTML = `
        <div class="card__head">
          <span class="card__title">Newest startups</span>
          <span class="card__hint" style="margin-left:auto">
            <a href="#/browse" class="muted">Browse all →</a>
          </span>
        </div>
        ${newest.length ? `<div class="stack" style="gap:8px">${newest.map((s) => `
          <div class="row" style="font-size:13px">
            <span class="truncate grow">${esc(s.name)}</span>
            <span class="dim truncate" style="max-width:120px">${esc(s.industry || "—")}</span>
            <span class="dim" style="font-size:11px">${fmt.dateTime(s.created_at)}</span>
          </div>`).join("")}</div>`
          : `<div class="empty" style="padding:16px">No startups yet</div>`}`;

      actRow.append(runsCard, newestCard);
      el.appendChild(actRow);
    };

    return poll(render, 15000);
  },
};
