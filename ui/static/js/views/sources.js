/* ══════════════════════════════════════════════════════════════════════════
   SCOUT — Sources manager
   The "Add source" form Fabian/Stefan were promised: add/remove RSS feeds
   and web sources without touching config/sources.yaml directly. Per-source
   health is read from the ingestion run history (bounded to the last 10
   runs kept in memory — an honest limitation, noted in the UI).
   ══════════════════════════════════════════════════════════════════════════ */

import { api, fmt, esc } from "../api.js";
import { toast, confirmAction } from "../router.js";

const SOURCE_TYPES = ["university_hub", "incubator", "accelerator", "startup_network", "intelligence_platform"];
const PRIORITIES = ["HIGH", "MEDIUM", "LOW"];

function slugify(name) {
  return name.toLowerCase().trim().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
}

export default {
  title: "Sources",

  mount(el) {
    let data = null;
    let historyBySource = {};

    el.innerHTML = `<div class="stack">
      <div class="skeleton" style="height:200px"></div>
      <div class="skeleton" style="height:200px"></div>
    </div>`;

    async function load() {
      try {
        const [sources, status] = await Promise.all([api.listSources(), api.ingestionStatus()]);
        data = sources;
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
              <span class="card__title">Web sources</span>
              <span class="dim" style="font-size:12px">${data.web_sources.length} sources · health reflects the last 10 runs</span>
              <button class="btn btn--primary btn--sm" id="add-web-btn" style="margin-left:auto">+ Add web source</button>
            </div>
            <div id="add-web-form"></div>
            <div class="table-wrap">
              <table class="table">
                <thead><tr><th>Name</th><th>Type</th><th>Location</th><th>Priority</th><th>Last run</th><th></th></tr></thead>
                <tbody>
                  ${data.web_sources.map((s) => `
                    <tr>
                      <td><strong>${esc(s.source_name)}</strong><br>
                        <a href="${esc(s.primary_url)}" target="_blank" rel="noopener" class="dim truncate" style="font-size:11px">${esc(s.primary_url)}</a></td>
                      <td class="dim">${esc(s.source_type.replace(/_/g, " "))}</td>
                      <td class="dim">${esc(s.location)}</td>
                      <td><span class="chip ${s.priority === "HIGH" ? "chip--brand" : ""}">${esc(s.priority)}</span></td>
                      <td>${healthChip(s.source_name)}</td>
                      <td class="row" style="gap:6px;justify-content:flex-end">
                        <button class="btn btn--sm" data-run="${esc(s.source_id)}">▶ Run now</button>
                        <button class="btn btn--sm btn--danger" data-del="${esc(s.source_id)}">Delete</button>
                      </td>
                    </tr>`).join("")}
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
    }

    // Not auto-polled: this page's own actions (add/delete/run-now) already
    // trigger a reload where needed, and re-rendering on a timer would wipe
    // out an "Add source" form mid-typing (unlike Ingestion/Overview, list
    // changes here aren't time-sensitive enough to justify that tradeoff).
    load();
  },
};
