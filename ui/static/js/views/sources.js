/* ══════════════════════════════════════════════════════════════════════════
   SCOUT — Sources manager
   The "Add source" form Fabian/Stefan were promised: add/remove RSS feeds
   and web sources without touching config/sources.yaml directly. Per-source
   health is read from the ingestion run history (bounded to the last 10
   runs kept in memory — an honest limitation, noted in the UI).
   ══════════════════════════════════════════════════════════════════════════ */

import { api, fmt, esc } from "../api.js";
import { toast, confirmAction, poll } from "../router.js";

const SOURCE_TYPES = ["university_hub", "incubator", "accelerator", "startup_network", "intelligence_platform"];
const PRIORITIES = ["HIGH", "MEDIUM", "LOW"];

function slugify(name) {
  return name.toLowerCase().trim().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
}

/** Lowercased, www-stripped netloc — must match processing/site_profile_store.normalize_domain(). */
function domainOf(url) {
  try {
    const host = new URL(url).hostname.toLowerCase();
    return host.startsWith("www.") ? host.slice(4) : host;
  } catch { return ""; }
}

const SHAPE_LABEL = {
  logo_grid: "🖼 Logo grid", card_directory: "🗂 Card directory",
  detail_page: "📄 Detail page", prose_listing: "📝 Prose listing",
  article_feed: "📰 Article feed", mixed: "🔀 Mixed",
  non_content: "— Non-content", unknown: "? Unknown",
};

/** Phase R-2: the source's own entry-point profile — domain-default (url_pattern "").
 *  A source can also have per-page profiles (e.g. a portfolio page BFS discovers
 *  mid-crawl) that don't show here; those become visible once Phase R-4/R-6 wire
 *  per-page profiling into real crawls. This column is "what does the pipeline
 *  think happens if it visits this source's registered URL right now". */
function entryProfileFor(profiles, primaryUrl) {
  const domain = domainOf(primaryUrl);
  return profiles.find((p) => p.domain === domain && p.url_pattern === "") || null;
}

function shapeChip(profile) {
  if (!profile) return `<span class="dim" style="font-size:12px">not profiled yet</span>`;
  const label = SHAPE_LABEL[profile.page_shape] || profile.page_shape;
  const expects = profile.expected_entity_count > 0;
  return `<span class="row" style="gap:5px;font-size:12px" title="${esc(profile.reason)}">
    <span>${esc(label)}</span>
    ${expects ? `<span class="dim">· ${profile.expected_entity_count} expected</span>` : ""}
  </span>`;
}

function recallChip(profile) {
  if (!profile) return `<span class="dim" style="font-size:12px">—</span>`;
  if (profile.status === "pinned") {
    return `<span class="chip" style="font-size:11px" title="Manually pinned — never auto-re-probed">📌 pinned</span>`;
  }
  if (profile.last_expected == null) {
    // Phase R-5 (recall audit) hasn't run yet, so nothing has been measured
    // against a real extraction — showing "0%" here would be a lie, not a
    // finding. Distinct from "not profiled yet" (no shape known at all).
    return `<span class="dim" style="font-size:12px">not measured yet</span>`;
  }
  const pct = Math.round((profile.recall_ratio ?? 0) * 100);
  const cls = profile.status === "flagged" ? "chip--danger" : pct >= 80 ? "chip--brand" : "";
  const title = profile.status === "flagged" ? esc(profile.flag_reason || "") : "";
  return `<span class="chip ${cls}" style="font-size:11px" title="${title}">
    ${profile.status === "flagged" ? "🚩 " : ""}${profile.last_extracted}/${profile.last_expected} · ${pct}%
  </span>`;
}

export default {
  title: "Sources",

  mount(el) {
    let data = null;
    let historyBySource = {};
    let theses = null;      // [{id, name, kind, summary}]
    let taxonomy = null;    // {industries: [...], tech_clusters: {industry: [...]}}
    let profiles = [];      // Phase R-2: what the structural inspector learned per source
    let stopBatchPoll = null;

    el.innerHTML = `<div class="stack">
      <div class="skeleton" style="height:200px"></div>
      <div class="skeleton" style="height:200px"></div>
    </div>`;

    async function load() {
      try {
        const [sources, status, thesesRes, taxRes, profilesRes] = await Promise.all([
          api.listSources(), api.ingestionStatus(), api.listTheses(), api.thesesTaxonomy(),
          api.listSiteProfiles().catch(() => ({ profiles: [] })), // never block the page on this
        ]);
        data = sources;
        theses = thesesRes.theses || [];
        taxonomy = taxRes;
        profiles = profilesRes.profiles || [];
        historyBySource = {};
        for (const h of status.history || []) {
          if (!(h.source in historyBySource)) historyBySource[h.source] = h; // most recent first
        }
      } catch (err) {
        el.innerHTML = `<div class="empty"><div class="empty__title">Couldn't load sources</div><div>${esc(err.message)}</div></div>`;
        return;
      }
      render();
    }

    function healthChip(sourceName) {
      const h = historyBySource[sourceName];
      if (!h) return `<span class="dim" style="font-size:12px">no recent runs</span>`;
      const dot = h.status === "completed" ? "dot--live" : h.status === "running" ? "dot--live" : "dot--error";
      const found = h.metrics?.startups_extracted;
      return `<span class="row" style="gap:5px;font-size:12px">
        <span class="dot ${dot}"></span>${esc(h.status)}
        ${found !== undefined ? `· ${found} found` : ""} · ${fmt.dateTime(h.ended_at || h.started_at)}
      </span>`;
    }

    function render() {
      el.innerHTML = `
        <div class="stack">
          <div class="card">
            <div class="card__head">
              <span class="card__title">Scouting themes</span>
              <span class="dim" style="font-size:12px">${theses.length} active · stakeholder theses + ad-hoc themes, ranks &amp; filters Browse's "Relevant to" dropdown</span>
              <button class="btn btn--primary btn--sm" id="add-theme-btn" style="margin-left:auto">+ Add theme</button>
            </div>
            <div id="add-theme-form"></div>
            <div class="table-wrap">
              <table class="table">
                <thead><tr><th>Name</th><th>Kind</th><th>Summary</th><th></th></tr></thead>
                <tbody>
                  ${theses.map((t) => `
                    <tr>
                      <td><strong>${esc(t.name)}</strong></td>
                      <td><span class="chip ${t.kind === "stakeholder" ? "chip--brand" : ""}">${esc(t.kind)}</span></td>
                      <td class="dim truncate" style="max-width:420px" title="${esc(t.summary)}">${esc(t.summary)}</td>
                      <td class="row" style="gap:6px;justify-content:flex-end">
                        ${t.kind === "adhoc" ? `<button class="btn btn--sm btn--danger" data-del-theme="${esc(t.id)}">Delete</button>` : `<span class="dim" style="font-size:12px">protected</span>`}
                      </td>
                    </tr>`).join("")}
                </tbody>
              </table>
            </div>
          </div>

          <div class="card">
            <div class="card__head">
              <span class="card__title">Web sources</span>
              <span class="dim" style="font-size:12px">${data.web_sources.length} sources · health reflects the last 10 runs</span>
              <button class="btn btn--sm" id="profile-all-btn" style="margin-left:auto" title="Structurally probes every source's entry page and learns its extraction strategy — no LLM, no crawl, just an inspection pass">🔍 Profile all sources</button>
              <button class="btn btn--primary btn--sm" id="add-web-btn">+ Add web source</button>
            </div>
            <div id="batch-profile-status"></div>
            <div id="add-web-form"></div>
            <div class="table-wrap">
              <table class="table">
                <thead><tr><th>Name</th><th>Type</th><th>Location</th><th>Priority</th><th>Last run</th>
                  <th title="Phase R-2: what the structural inspector detected on this source's entry page">Shape</th>
                  <th title="Extracted vs. structurally-expected, once Phase R-5's recall audit has run">Recall</th>
                  <th></th></tr></thead>
                <tbody>
                  ${data.web_sources.map((s) => {
                    const profile = entryProfileFor(profiles, s.primary_url);
                    return `
                    <tr>
                      <td><strong>${esc(s.source_name)}</strong><br>
                        <a href="${esc(s.primary_url)}" target="_blank" rel="noopener" class="dim truncate" style="font-size:11px">${esc(s.primary_url)}</a></td>
                      <td class="dim">${esc(s.source_type.replace(/_/g, " "))}</td>
                      <td class="dim">${esc(s.location)}</td>
                      <td><span class="chip ${s.priority === "HIGH" ? "chip--brand" : ""}">${esc(s.priority)}</span></td>
                      <td>${healthChip(s.source_name)}</td>
                      <td>${shapeChip(profile)}</td>
                      <td>${recallChip(profile)}</td>
                      <td class="row" style="gap:6px;justify-content:flex-end">
                        <button class="btn btn--sm" data-reinspect="${esc(s.source_id)}" title="Force a fresh probe now, bypassing the cache">🔍</button>
                        <button class="btn btn--sm" data-run="${esc(s.source_id)}">▶ Run now</button>
                        <button class="btn btn--sm btn--danger" data-del="${esc(s.source_id)}">Delete</button>
                      </td>
                    </tr>`;
                  }).join("")}
                </tbody>
              </table>
            </div>
          </div>

          <div class="card">
            <div class="card__head">
              <span class="card__title">RSS feeds</span>
              <span class="dim" style="font-size:12px">${data.rss_feeds.length} feeds</span>
              <button class="btn btn--primary btn--sm" id="add-rss-btn" style="margin-left:auto">+ Add RSS feed</button>
            </div>
            <div id="add-rss-form"></div>
            <div class="table-wrap">
              <table class="table">
                <thead><tr><th>Name</th><th>URL</th><th>Region</th><th>Type</th></tr></thead>
                <tbody>
                  ${data.rss_feeds.map((f) => `
                    <tr>
                      <td><strong>${esc(f.name)}</strong></td>
                      <td class="dim truncate" style="max-width:280px">${esc(f.url)}</td>
                      <td class="dim">${esc(f.region)}</td>
                      <td class="dim">${esc(f.type)}</td>
                    </tr>`).join("")}
                </tbody>
              </table>
            </div>
          </div>

          <div class="card">
            <div class="card__head"><span class="card__title">Newsletter intake</span></div>
            <div class="stack" style="gap:10px;font-size:13px">
              <div>
                <div class="dim" style="margin-bottom:6px">Trusted senders</div>
                ${data.newsletter_senders.length
                  ? data.newsletter_senders.map((s) => `<span class="chip" style="margin-right:6px">${esc(s)}</span>`).join("")
                  : `<span class="dim">None set — every sender in the inbox is accepted; relevance is filtered by content</span>`}
              </div>
              <div>
                <div class="dim" style="margin-bottom:6px">Gmail search terms</div>
                ${data.newsletter_search_terms.map((t) => `<span class="chip" style="margin-right:6px">${esc(t)}</span>`).join("")}
              </div>
            </div>
          </div>
        </div>`;

      /* ── Add web source form ─────────────────────────────────────────── */
      const addWebForm = el.querySelector("#add-web-form");
      el.querySelector("#add-web-btn").addEventListener("click", () => {
        addWebForm.innerHTML = `
          <form id="web-form" class="card" style="background:var(--surface-2);margin:12px 0">
            <div class="grid-2">
              <div class="field"><label class="field__label">Source name</label>
                <input class="input" name="source_name" required placeholder="e.g. Berlin Startup Hub"></div>
              <div class="field"><label class="field__label">Source ID (auto-filled)</label>
                <input class="input" name="source_id" required placeholder="berlin_startup_hub"></div>
              <div class="field" style="grid-column:1/-1"><label class="field__label">Website URL</label>
                <input class="input" type="url" name="primary_url" required placeholder="https://…"></div>
              <div class="field"><label class="field__label">Location</label>
                <input class="input" name="location" placeholder="Berlin, Germany"></div>
              <div class="field"><label class="field__label">Type</label>
                <select class="select" name="source_type">
                  ${SOURCE_TYPES.map((t) => `<option value="${t}">${t.replace(/_/g, " ")}</option>`).join("")}
                </select></div>
              <div class="field"><label class="field__label">Priority</label>
                <select class="select" name="priority">
                  ${PRIORITIES.map((p) => `<option value="${p}" ${p === "MEDIUM" ? "selected" : ""}>${p}</option>`).join("")}
                </select></div>
            </div>
            <div class="row" style="gap:8px;margin-top:12px">
              <button type="submit" class="btn btn--primary">Add source</button>
              <button type="button" class="btn btn--ghost" id="cancel-web">Cancel</button>
            </div>
          </form>`;
        const nameInput = addWebForm.querySelector('[name="source_name"]');
        const idInput = addWebForm.querySelector('[name="source_id"]');
        nameInput.addEventListener("input", () => { idInput.value = slugify(nameInput.value); });
        addWebForm.querySelector("#cancel-web").addEventListener("click", () => { addWebForm.innerHTML = ""; });
        addWebForm.querySelector("#web-form").addEventListener("submit", async (e) => {
          e.preventDefault();
          const fd = new FormData(e.target);
          const payload = Object.fromEntries(fd.entries());
          try {
            await api.addWebSource(payload);
            toast(`Added "${payload.source_name}"`);
            addWebForm.innerHTML = "";
            load();
          } catch (err) { toast(`Couldn't add source: ${err.message}`, "error"); }
        });
      });

      /* ── Add RSS feed form ────────────────────────────────────────────── */
      const addRssForm = el.querySelector("#add-rss-form");
      el.querySelector("#add-rss-btn").addEventListener("click", () => {
        addRssForm.innerHTML = `
          <form id="rss-form" class="card" style="background:var(--surface-2);margin:12px 0">
            <div class="grid-2">
              <div class="field"><label class="field__label">Feed name</label>
                <input class="input" name="name" required placeholder="e.g. TechCrunch Europe"></div>
              <div class="field"><label class="field__label">Feed URL</label>
                <input class="input" type="url" name="url" required placeholder="https://…/feed"></div>
              <div class="field"><label class="field__label">Region</label>
                <input class="input" name="region" value="europe"></div>
              <div class="field"><label class="field__label">Type</label>
                <input class="input" name="type" value="news"></div>
            </div>
            <div class="row" style="gap:8px;margin-top:12px">
              <button type="submit" class="btn btn--primary">Add feed</button>
              <button type="button" class="btn btn--ghost" id="cancel-rss">Cancel</button>
            </div>
          </form>`;
        addRssForm.querySelector("#cancel-rss").addEventListener("click", () => { addRssForm.innerHTML = ""; });
        addRssForm.querySelector("#rss-form").addEventListener("submit", async (e) => {
          e.preventDefault();
          const payload = Object.fromEntries(new FormData(e.target).entries());
          try {
            await api.addRssFeed(payload);
            toast(`Added "${payload.name}"`);
            addRssForm.innerHTML = "";
            load();
          } catch (err) { toast(`Couldn't add feed: ${err.message}`, "error"); }
        });
      });

      /* ── Add theme form (Phase V-4) ──────────────────────────────────── */
      const addThemeForm = el.querySelector("#add-theme-form");
      el.querySelector("#add-theme-btn").addEventListener("click", () => {
        const industryOptions = taxonomy.industries.map((i) => `<option value="${esc(i)}">${esc(i)}</option>`).join("");
        const clusterOptions = Object.entries(taxonomy.tech_clusters).map(([industry, clusters]) => `
          <optgroup label="${esc(industry)}">
            ${clusters.map((c) => `<option value="${esc(c)}">${esc(c)}</option>`).join("")}
          </optgroup>`).join("");
        addThemeForm.innerHTML = `
          <form id="theme-form" class="card" style="background:var(--surface-2);margin:12px 0">
            <div class="stack" style="gap:10px">
              <div class="grid-2">
                <div class="field"><label class="field__label">Theme name</label>
                  <input class="input" name="name" required placeholder="e.g. Construction Tech"></div>
                <div class="field"><label class="field__label">Theme ID (auto-filled)</label>
                  <input class="input" name="id" required placeholder="construction_tech"></div>
              </div>
              <div class="field"><label class="field__label">Summary — what makes a startup relevant (2-3 sentences, this is what gets semantically matched)</label>
                <textarea class="textarea" name="summary" required rows="3" placeholder="e.g. Startups building software, robotics, or materials for the construction and civil-engineering industry — BIM, site digitalization, construction robotics, sustainable building materials…"></textarea></div>
              <div class="grid-2">
                <div class="field"><label class="field__label">Industries (ctrl/cmd-click to select several)</label>
                  <select class="select" name="industries" multiple size="6">${industryOptions}</select></div>
                <div class="field"><label class="field__label">Tech clusters</label>
                  <select class="select" name="tech_clusters" multiple size="6">${clusterOptions}</select></div>
              </div>
              <div class="field"><label class="field__label">Keywords (comma-separated)</label>
                <input class="input" name="keywords" placeholder="e.g. BIM, site digitalization, construction robotics"></div>
            </div>
            <div class="row" style="gap:8px;margin-top:12px">
              <button type="submit" class="btn btn--primary">Add theme</button>
              <button type="button" class="btn btn--ghost" id="cancel-theme">Cancel</button>
            </div>
          </form>`;
        const nameInput = addThemeForm.querySelector('[name="name"]');
        const idInput = addThemeForm.querySelector('[name="id"]');
        nameInput.addEventListener("input", () => { idInput.value = slugify(nameInput.value); });
        addThemeForm.querySelector("#cancel-theme").addEventListener("click", () => { addThemeForm.innerHTML = ""; });
        addThemeForm.querySelector("#theme-form").addEventListener("submit", async (e) => {
          e.preventDefault();
          const form = e.target;
          const payload = {
            id: form.elements.id.value,
            name: form.elements.name.value,
            summary: form.elements.summary.value,
            industries: Array.from(form.elements.industries.selectedOptions).map((o) => o.value),
            tech_clusters: Array.from(form.elements.tech_clusters.selectedOptions).map((o) => o.value),
            keywords: form.elements.keywords.value.split(",").map((k) => k.trim()).filter(Boolean),
          };
          try {
            await api.addThesis(payload);
            toast(`Added theme "${payload.name}"`);
            addThemeForm.innerHTML = "";
            load();
          } catch (err) { toast(`Couldn't add theme: ${err.message}`, "error"); }
        });
      });

      el.querySelectorAll("[data-del-theme]").forEach((btn) => btn.addEventListener("click", async () => {
        const name = btn.closest("tr").querySelector("strong")?.textContent || btn.dataset.delTheme;
        if (!confirmAction(`Remove theme "${name}"? It will stop appearing in Browse's "Relevant to" filter.`)) return;
        try {
          await api.deleteThesis(btn.dataset.delTheme);
          toast(`Removed "${name}"`);
          load();
        } catch (err) { toast(`Couldn't remove theme: ${err.message}`, "error"); }
      }));

      /* ── Run now / delete ────────────────────────────────────────────── */
      el.querySelectorAll("[data-run]").forEach((btn) => btn.addEventListener("click", async () => {
        btn.disabled = true;
        btn.textContent = "Starting…";
        try {
          await api.runTargeted({ source_id: btn.dataset.run });
          toast("Run started — see the Ingestion page for progress");
        } catch (err) {
          toast(`Couldn't start run: ${err.message}`, "error");
          btn.disabled = false;
          btn.textContent = "▶ Run now";
        }
      }));

      el.querySelectorAll("[data-del]").forEach((btn) => btn.addEventListener("click", async () => {
        const row = btn.closest("tr");
        const name = row.querySelector("strong")?.textContent || btn.dataset.del;
        if (!confirmAction(`Remove "${name}" from the source registry?`)) return;
        try {
          await api.deleteWebSource(btn.dataset.del);
          toast(`Removed "${name}"`);
          load();
        } catch (err) { toast(`Couldn't remove source: ${err.message}`, "error"); }
      }));

      /* ── Site profiles: re-inspect one source / profile all (Phase R-2) ── */
      el.querySelectorAll("[data-reinspect]").forEach((btn) => btn.addEventListener("click", async () => {
        btn.disabled = true;
        const original = btn.textContent;
        btn.textContent = "…";
        try {
          await api.reinspectSource({ source_id: btn.dataset.reinspect });
          toast("Re-inspected — shape/recall updated below");
          await load();
        } catch (err) {
          toast(`Re-inspect failed: ${err.message}`, "error");
          btn.disabled = false;
          btn.textContent = original;
        }
      }));

      el.querySelector("#profile-all-btn")?.addEventListener("click", async () => {
        try {
          await api.batchProfileSources();
          toast("Profiling every source — this page will update as results land");
          watchBatch();
        } catch (err) {
          toast(`Couldn't start profiling: ${err.message}`, "error");
        }
      });

      renderBatchStatus();
    }

    /* Polls /sources/profiles/batch-status while a "Profile all sources" run
       is in flight, re-loading the table as it goes so Shape/Recall fill in
       live rather than only after the whole batch finishes. */
    function watchBatch() {
      if (stopBatchPoll) return; // already watching
      stopBatchPoll = poll(async () => {
        const st = await api.batchProfileStatus();
        const box = el.querySelector("#batch-profile-status");
        if (box) box.innerHTML = batchStatusHtml(st);
        if (!st.running) {
          stopBatchPoll?.();
          stopBatchPoll = null;
          await load(); // final refresh with the completed results
        }
      }, 2000);
    }

    function batchStatusHtml(st) {
      if (!st.running && !st.finished_at) return "";
      if (st.running) {
        return `<div class="card row" style="gap:10px;align-items:center;background:var(--surface-2);margin-bottom:10px">
          <span class="spinner"></span>
          <span style="font-size:13px">Profiling sources… ${st.done}/${st.total}${st.current_url ? ` — <span class="dim">${esc(st.current_url)}</span>` : ""}</span>
        </div>`;
      }
      return `<div class="card row" style="gap:10px;align-items:center;background:var(--surface-2);margin-bottom:10px">
        <span style="font-size:13px">✅ Profiled ${st.done}/${st.total}${st.failed ? ` (${st.failed} failed)` : ""}</span>
      </div>`;
    }

    async function renderBatchStatus() {
      try {
        const st = await api.batchProfileStatus();
        const box = el.querySelector("#batch-profile-status");
        if (box) box.innerHTML = batchStatusHtml(st);
        if (st.running) watchBatch();
      } catch { /* non-fatal — the button still works without this */ }
    }

    // Not auto-polled: this page's own actions (add/delete/run-now) already
    // trigger a reload where needed, and re-rendering on a timer would wipe
    // out an "Add source" form mid-typing (unlike Ingestion/Overview, list
    // changes here aren't time-sensitive enough to justify that tradeoff).
    // The one exception is the "Profile all sources" batch (Phase R-2),
    // which polls its own status while running — stopped here so navigating
    // away mid-batch doesn't leak a timer (the batch itself keeps running
    // server-side; only the UI's poll stops).
    load();
    return () => { stopBatchPoll?.(); };
  },
};
