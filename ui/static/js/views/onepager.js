/* ══════════════════════════════════════════════════════════════════════════
   ONE-PAGER — generate a GT Hub one-pager draft from a pitch deck.

   Upload a .pdf or .pptx, get a populated draft in BOTH languages, preview
   either one, download the editable PowerPoint. German is the final version
   that gets exported, so it is drafted straight from the deck and is the
   default everywhere; English is translated from it. The heavy lifting runs
   in a SEPARATE PROCESS on the server (see api/routes/onepager.py) —
   deliberately, so a generator failure can never take the dashboard or the
   scouting pipeline with it.

   Nothing here auto-approves anything: a draft always lands with an
   open-questions list, and that list is the most important thing on screen.
   ══════════════════════════════════════════════════════════════════════════ */

import { api, fmt, esc } from "../api.js";
import { toast, confirmAction, poll } from "../router.js";

const LANGS = ["de", "en"];
const FINAL = "de";
const LANG_NAME = { de: "Deutsch", en: "English" };
const LANG_SHORT = { de: "DE", en: "EN" };
const FRAME_W = 1328, FRAME_H = 768;   // render.py's 1280x720 slide + 24px margin

/* "Your input": exact values + a free-text box for extra facts and
   instructions. Shared by the create form and the regenerate panel. Whatever
   is entered here beats the deck, the website and the web search, and the
   model is told to follow the instructions (without inventing facts). */
const NOTES_PLACEHOLDER =
  "Facts the deck doesn't have, and instructions for the text. For example:\n" +
  "• Team: 12 people, 8 of them engineers.\n" +
  "• Pilot with Siemens Energy signed in May 2026 — mention it under customers.\n" +
  "• Emphasise the licensing model; don't mention the planned funding round.";

function inputFieldsHTML(prefix, v = {}) {
  return `
    <div class="row wrap" style="gap:10px">
      <input class="input" id="${prefix}-location" placeholder="Location" value="${esc(v.location || "")}" style="max-width:180px">
      <input class="input" id="${prefix}-founded" placeholder="Founded (year)" value="${esc(v.founded || "")}" style="max-width:140px">
      <input class="input" id="${prefix}-team" placeholder="Team size (e.g. 12 or ca. 10)" value="${esc(v.team_size || "")}" style="max-width:200px">
      <label class="row" style="gap:6px;font-size:12px" title="Used instead of the logo found on the website">
        Logo <input class="input" type="file" id="${prefix}-logo" accept=".png,.jpg,.jpeg,.svg,.webp" style="max-width:230px">
      </label>
    </div>
    <textarea class="input" id="${prefix}-notes" rows="4" placeholder="${esc(NOTES_PLACEHOLDER)}"
              style="width:100%;resize:vertical;font:inherit;font-size:13px;line-height:1.45">${esc(v.notes || "")}</textarea>`;
}

function readInputFields(root, prefix) {
  const val = (id) => (root.querySelector(`#${prefix}-${id}`)?.value || "").trim();
  return {
    location: val("location"), founded: val("founded"), teamSize: val("team"),
    notes: val("notes"), logo: root.querySelector(`#${prefix}-logo`)?.files?.[0] || null,
  };
}

export default {
  title: "One-Pager",

  async mount(el) {
    const state = { drafts: [], selected: null, lang: FINAL, busy: false,
                    picked: new Map(), maxBatch: 5, batchRunning: false, lastBatchId: null };

    el.innerHTML = `
      <div class="stack">
        <div class="card" id="op-db"></div>
        <div class="card" id="op-form"></div>
        <div class="card" id="op-log" style="display:none"></div>
        <div class="card" id="op-list"></div>
        <div id="op-preview"></div>
      </div>`;

    const dbCard = el.querySelector("#op-db");
    const formCard = el.querySelector("#op-form");
    const logCard = el.querySelector("#op-log");
    const listCard = el.querySelector("#op-list");
    const previewEl = el.querySelector("#op-preview");

    /* ── From the HubDrive database ───────────────────────────────────── */
    /* No deck: the record is a lead, and the tool researches what a one-pager
       needs (website + Impressum, the source article, targeted searches for
       whatever is missing). One startup at a time, holding the same GPU lock
       as ingestion; at most state.maxBatch per batch. */
    function buildDbCard() {
      dbCard.innerHTML = `
        <div class="card__head">
          <span class="card__title">Create from the HubDrive database</span>
          <span class="card__hint" style="margin-left:auto">no pitch deck needed · up to <span id="db-max">${state.maxBatch}</span> startups per batch</span>
        </div>
        <div class="stack" style="gap:10px">
          <input class="input" id="db-q" placeholder="Search startups by name…" autocomplete="off" style="max-width:360px">
          <div id="db-results"></div>
          <div id="db-picked"></div>
          <div class="row wrap" style="gap:14px;align-items:center">
            <label class="row" style="gap:6px;font-size:12px">
              Write first in
              <select class="input" id="db-lang" style="width:auto;padding:2px 6px">
                <option value="de" selected>Deutsch (final version)</option>
                <option value="en">English</option>
              </select>
            </label>
            <label class="row" style="gap:6px;font-size:12px"><input type="checkbox" id="db-web" checked> Search the web</label>
            <label class="row" style="gap:6px;font-size:12px"><input type="checkbox" id="db-paid" checked> allow paid searches (Tavily)</label>
            <label class="row" style="gap:6px;font-size:12px"><input type="checkbox" id="db-force"> overwrite existing one-pagers</label>
            <span class="grow"></span>
            <button class="btn btn--primary" id="db-go" disabled>Create one-pagers</button>
          </div>
          <div class="dim" style="font-size:12px">
            The database record is only a starting point — it can be thin or wrong. For each startup the
            tool finds the company's own website (from the record, the source article or a search),
            reads its about, product, team and Impressum pages, then searches only for what a one-pager
            still lacks (figures, customers, competitors, business model, founding year, team size) —
            up to 4 searches each, free sources and cache first. Anything still missing is listed for you.
            About 2 minutes per startup, one at a time; a batch waits its turn if ingestion is using the GPU.
          </div>
          <div id="db-progress"></div>
        </div>`;

      const q = dbCard.querySelector("#db-q");
      let timer = null;
      q.addEventListener("input", () => {
        clearTimeout(timer);
        timer = setTimeout(() => searchDb(q.value.trim()), 250);
      });
      dbCard.querySelector("#db-web").addEventListener("change", (e) => {
        dbCard.querySelector("#db-paid").disabled = !e.target.checked;
      });
      dbCard.querySelector("#db-go").addEventListener("click", startBatch);
      renderPicked();
    }

    async function searchDb(term) {
      const box = dbCard.querySelector("#db-results");
      if (term.length < 2) { box.innerHTML = ""; return; }
      let res;
      try { res = await api.listStartups({ q: term, limit: 8, sort: "name" }); }
      catch (err) { box.innerHTML = `<div class="dim" style="font-size:12px">${esc(err.message)}</div>`; return; }
      const rows = res.startups || [];
      if (!rows.length) { box.innerHTML = `<div class="dim" style="font-size:12px">No startup matches “${esc(term)}”.</div>`; return; }
      box.innerHTML = `<div class="stack" style="gap:2px;max-width:640px">${rows.map((r) => `
          <label class="row" style="gap:8px;font-size:13px;padding:3px 6px;border-radius:6px;cursor:pointer">
            <input type="checkbox" data-pick="${esc(r.id)}" data-name="${esc(r.name)}" ${state.picked.has(r.id) ? "checked" : ""}>
            <strong>${esc(r.name)}</strong>
            <span class="dim">${esc([r.city, r.country].filter(Boolean).join(", ") || "—")} · ${esc(r.industry || "no industry")}</span>
          </label>`).join("")}</div>`;
      box.querySelectorAll("[data-pick]").forEach((cb) => cb.addEventListener("change", () => {
        if (cb.checked) {
          if (state.picked.size >= state.maxBatch) {
            cb.checked = false;
            toast(`At most ${state.maxBatch} startups per batch — each takes about 2 minutes of the machine's GPU`, "error");
            return;
          }
          state.picked.set(cb.dataset.pick, cb.dataset.name);
        } else {
          state.picked.delete(cb.dataset.pick);
        }
        renderPicked();
      }));
    }

    function renderPicked() {
      const box = dbCard.querySelector("#db-picked");
      const n = state.picked.size;
      box.innerHTML = n ? `<div class="row wrap" style="gap:6px;align-items:center">
          <span style="font-size:12px;font-weight:600">Selected ${n} of ${state.maxBatch}:</span>
          ${[...state.picked].map(([id, name]) => `<span class="chip">${esc(name)}
             <button class="btn btn--ghost btn--sm" data-unpick="${esc(id)}" title="Remove" style="padding:0 4px;margin-left:2px">×</button></span>`).join("")}
        </div>` : "";
      box.querySelectorAll("[data-unpick]").forEach((b) => b.addEventListener("click", () => {
        state.picked.delete(b.dataset.unpick);
        renderPicked();
        dbCard.querySelectorAll(`[data-pick="${b.dataset.unpick}"]`).forEach((cb) => { cb.checked = false; });
      }));
      const go = dbCard.querySelector("#db-go");
      go.disabled = !n || state.batchRunning;
      go.textContent = state.batchRunning ? "Batch running…" : `Create ${n || ""} one-pager${n === 1 ? "" : "s"}`.replace("  ", " ");
    }

    async function startBatch() {
      if (!state.picked.size || state.batchRunning) return;
      const body = {
        startup_ids: [...state.picked.keys()],
        draft_lang: dbCard.querySelector("#db-lang").value,
        web_search: dbCard.querySelector("#db-web").checked,
        paid_search: dbCard.querySelector("#db-paid").checked,
        force: dbCard.querySelector("#db-force").checked,
      };
      try {
        const res = await api.createOnePagersFromDb(body);
        state.picked.clear();
        dbCard.querySelector("#db-results").innerHTML = "";
        dbCard.querySelector("#db-q").value = "";
        showBatch(res);
        toast(`Started: ${res.batch.items.length} one-pager${res.batch.items.length === 1 ? "" : "s"}`);
      } catch (err) {
        toast(err.message, "error");
      }
    }

    const STATUS = {
      queued: ["queued", ""], waiting: ["waiting for GPU", "chip--warning"], running: ["working…", "chip--brand"],
      done: ["done", "chip--brand"], failed: ["failed", "chip--danger"], skipped: ["skipped", "chip--warning"],
    };

    function showBatch(res) {
      if (res && res.max_batch) {
        state.maxBatch = res.max_batch;
        const m = dbCard.querySelector("#db-max");
        if (m) m.textContent = res.max_batch;
      }
      const b = res && res.batch;
      const box = dbCard.querySelector("#db-progress");
      const wasRunning = state.batchRunning;
      state.batchRunning = !!(b && !b.finished_at);
      renderPicked();
      if (!b) { box.innerHTML = ""; return; }
      const done = b.items.filter((i) => ["done", "failed", "skipped"].includes(i.status)).length;
      box.innerHTML = `
        <div style="border:1px solid var(--border);border-radius:8px;padding:10px 12px">
          <div class="row" style="gap:8px;align-items:center;margin-bottom:6px">
            <strong style="font-size:13px">${b.finished_at ? "Last batch" : "Batch running"}</strong>
            <span class="dim" style="font-size:12px">${done} of ${b.items.length} finished</span>
            ${b.finished_at ? "" : `<span class="spinner"></span>`}
          </div>
          <table class="table"><tbody>
            ${b.items.map((i) => {
              const [label, cls] = STATUS[i.status] || [i.status, ""];
              const missing = (i.missing || []).length;
              return `<tr>
                <td><strong>${esc(i.name)}</strong></td>
                <td><span class="chip ${cls}">${esc(label)}</span></td>
                <td class="dim" style="font-size:12px">${esc(i.message || "")}
                  ${i.status === "done" ? `${i.open_items} open item${i.open_items === 1 ? "" : "s"}${missing ? ` · ${missing} thing${missing === 1 ? "" : "s"} not found online` : " · everything found"}` : ""}</td>
                <td class="dim" style="font-size:12px">${i.seconds != null ? `${i.seconds}s` : ""}</td>
                <td>${i.slug ? `<button class="btn btn--ghost btn--sm" data-open="${esc(i.slug)}">Preview</button>` : ""}</td>
              </tr>`;
            }).join("")}
          </tbody></table>
        </div>`;
      box.querySelectorAll("[data-open]").forEach((btn) => btn.addEventListener("click", () => select(btn.dataset.open, FINAL)));
      // Refresh the list as one-pagers land, and once more when the batch ends.
      if (done && (state.lastDone !== done || (wasRunning && b.finished_at))) {
        state.lastDone = done;
        loadList();
      }
    }

    async function pollBatch() {
      try { showBatch(await api.onePagerBatch()); } catch { /* transient */ }
    }

    /* ── Upload form ──────────────────────────────────────────────────── */
    function buildForm() {
      formCard.innerHTML = `
        <div class="card__head">
          <span class="card__title">Create a new one-pager</span>
          <span class="card__hint" style="margin-left:auto">Pitch deck as .pdf or .pptx</span>
        </div>
        <div class="stack" style="gap:10px">
          <div class="row wrap" style="gap:10px">
            <input class="input" type="file" id="op-file" accept=".pdf,.pptx" style="max-width:280px">
            <input class="input" id="op-name" placeholder="Startup name *" style="max-width:220px">
            <input class="input" id="op-url" placeholder="Company website" title="Shown on the page as a link, read alongside the deck, and used to find the startup's logo" style="max-width:240px">
          </div>
          <div class="stack" style="gap:6px">
            <div style="font-size:12px;font-weight:600">Your input <span class="dim" style="font-weight:400">— optional; beats every other source and is kept when the draft is regenerated</span></div>
            ${inputFieldsHTML("op")}
          </div>
          <div class="row wrap" style="gap:14px;align-items:center">
            <label class="row" style="gap:6px;font-size:12px">
              Draft from the deck in
              <select class="input" id="op-draftlang" style="width:auto;padding:2px 6px">
                <option value="de" selected>Deutsch (final version)</option>
                <option value="en">English</option>
              </select>
            </label>
            <label class="row" style="gap:6px;font-size:12px"
                   title="Looks up the company on the web to fill gaps the deck leaves (city, founding year, customers, press)">
              <input type="checkbox" id="op-web" checked> Search the web
            </label>
            <label class="row" style="gap:6px;font-size:12px"
                   title="Free sources and cached results are always tried first; Tavily is only used when they find nothing">
              <input type="checkbox" id="op-paid" checked> allow paid searches (Tavily)
            </label>
            <label class="row" style="gap:6px;font-size:12px">
              <input type="checkbox" id="op-nollm"> without AI draft (deck + images only)
            </label>
            <label class="row" style="gap:6px;font-size:12px">
              <input type="checkbox" id="op-force"> overwrite existing draft
            </label>
            <span class="grow"></span>
            <button class="btn btn--primary" id="op-go">Create draft</button>
          </div>
          <div class="dim" style="font-size:12px" id="op-budget"></div>
          <div class="dim" style="font-size:12px">
            Every one-pager is created in <strong>both Deutsch and English</strong>. The language
            chosen above is drafted from the deck; the other one is translated from it, so both
            state the same facts. Deutsch is the version that gets exported.
            The whole deck and the website (if given) are read. Location, founding year and team
            size are taken from the deck, the website or the web search unless you enter them.
            The startup's logo is taken from its website unless you upload one. Drafting runs locally
            (Gemma&nbsp;4) and takes about 2–3&nbsp;minutes for both languages — longer while an
            ingestion run is using the GPU.
            Save a <code>.ppt</code> as <code>.pptx</code> first.
          </div>
        </div>`;

      const web = formCard.querySelector("#op-web");
      const paid = formCard.querySelector("#op-paid");
      web.addEventListener("change", () => { paid.disabled = !web.checked; });
      loadBudget();

      formCard.querySelector("#op-go").addEventListener("click", generate);
      formCard.querySelector("#op-name").addEventListener("keydown", (e) => {
        if (e.key === "Enter") generate();
      });
    }

    /* Web-search credits: shown before anyone spends them. */
    async function loadBudget() {
      const el = formCard.querySelector("#op-budget");
      if (!el) return;
      let b;
      try { b = await api.onePagerSearchBudget(); } catch { b = null; }
      if (!b || !b.tavily) {
        el.innerHTML = `Web search: Tavily balance unavailable — paid searches will be skipped, free and cached ones still run.`;
        return;
      }
      const t = b.tavily;
      const avail = b.paid_searches_available ?? 0;
      el.innerHTML = `
        Web search: up to ${b.per_draft} searches per one-pager, free sources and cache first.
        Tavily this month: <strong>${t.left}</strong> of ${t.limit} credits left
        (the last ${b.reserve} are reserved for the scouting pipeline) ·
        one-pager used ${b.onepager_used_this_month} of its ${b.onepager_monthly_cap}/month ·
        ${avail > 0 ? `${avail} paid searches available` : `<strong>no paid searches available</strong> — free and cached only`}
        ${b.searxng_ok ? "" : " · free search engine is down"}`;
    }

    function setBusy(on, label) {
      state.busy = on;
      const btn = formCard.querySelector("#op-go");
      if (btn) {
        btn.disabled = on;
        btn.textContent = on ? (label || "Creating…") : "Create draft";
      }
    }

    async function generate() {
      if (state.busy) return;
      const file = formCard.querySelector("#op-file").files[0];
      const name = formCard.querySelector("#op-name").value.trim();
      const url = formCard.querySelector("#op-url").value.trim();
      const draftLang = formCard.querySelector("#op-draftlang").value;
      const noLlm = formCard.querySelector("#op-nollm").checked;
      const webSearch = formCard.querySelector("#op-web").checked;
      const paidSearch = formCard.querySelector("#op-paid").checked;
      const force = formCard.querySelector("#op-force").checked;
      const input = readInputFields(formCard, "op");

      if (!file) { toast("Choose a pitch deck first", "error"); return; }
      if (!name) { toast("Enter the startup name", "error"); return; }

      setBusy(true);
      logCard.style.display = "";
      logCard.innerHTML = `<div class="row" style="gap:10px;padding:4px 0">
          <span class="spinner"></span>
          <span class="dim">Reading the deck, extracting images${noLlm ? "" :
            `, drafting in ${LANG_NAME[draftLang]} and translating`}…</span>
        </div>`;

      try {
        const res = await api.generateOnePager({ file, name, url, noLlm, force, draftLang, webSearch, paidSearch, ...input });
        await loadList();
        renderLog(res.draft);
        toast(`Draft for "${res.draft.name}" created in Deutsch and English`);
        select(res.draft.slug, FINAL);
      } catch (err) {
        logCard.innerHTML = `<div class="empty" style="padding:16px">
            <div class="empty__title">Creation failed</div>
            <div>${esc(err.message)}</div>
          </div>`;
      } finally {
        setBusy(false);
        loadBudget();
      }
    }

    function renderLog(draft) {
      const blocks = LANGS.filter((l) => draft.versions[l]).map((l) => {
        const q = draft.versions[l].open_questions || [];
        return `
          <div style="margin-top:8px">
            <div class="dim" style="font-size:12px;margin-bottom:4px">
              <strong>${LANG_NAME[l]}</strong>${l === FINAL ? " (final)" : ""} —
              ${q.length} item${q.length === 1 ? "" : "s"} a person needs to check:
            </div>
            <ul style="margin:0;padding-left:18px;font-size:12.5px;line-height:1.55">
              ${q.map((x) => `<li>${esc(x)}</li>`).join("") || `<li class="dim">none</li>`}
            </ul>
          </div>`;
      }).join("");
      logCard.innerHTML = `
        <div class="card__head">
          <span class="card__title">${esc(draft.name)}</span>
          <span class="chip">Draft</span>
          <span class="grow"></span>
          <button class="btn btn--ghost btn--sm" id="op-log-close">Close</button>
        </div>
        ${draft.claim ? `<div style="font-size:15px;margin-bottom:4px">${esc(draft.claim)}</div>` : ""}
        ${blocks}`;
      logCard.querySelector("#op-log-close").addEventListener("click", () => {
        logCard.style.display = "none";
      });
    }

    /* ── Existing drafts ──────────────────────────────────────────────── */
    async function loadList() {
      try {
        const res = await api.listOnePagers();
        state.drafts = res.one_pagers || [];
      } catch (err) {
        listCard.innerHTML = `<div class="empty"><div class="empty__title">Couldn't load drafts</div>
                               <div>${esc(err.message)}</div></div>`;
        return;
      }
      renderList();
    }

    function versionChip(d, lang) {
      const v = d.versions[lang];
      if (!v) return `<span class="chip" style="opacity:.55" title="No ${LANG_NAME[lang]} version yet">${LANG_SHORT[lang]} missing</span>`;
      const n = v.open_questions.length;
      const cls = v.status === "approved" ? "chip--brand" : n ? "chip--warning" : "";
      const title = `${LANG_NAME[lang]}: ${v.status}, ${n} open item${n === 1 ? "" : "s"}`;
      return `<span class="chip ${cls}" title="${esc(title)}">${LANG_SHORT[lang]} · ${esc(v.status)}${n ? ` · ${n}` : ""}</span>`;
    }

    function renderList() {
      if (!state.drafts.length) {
        listCard.innerHTML = `<div class="card__head"><span class="card__title">One-pagers</span></div>
          <div class="empty" style="padding:20px"><div>No drafts yet. Upload a pitch deck above.</div></div>`;
        return;
      }
      listCard.innerHTML = `
        <div class="card__head">
          <span class="card__title">One-pagers</span>
          <span class="chip">${state.drafts.length}</span>
        </div>
        <div class="table-wrap">
          <table class="table">
            <thead><tr><th>Startup</th><th>Claim (Deutsch)</th><th>Versions</th><th>Updated</th><th></th></tr></thead>
            <tbody>
              ${state.drafts.map((d) => `
                <tr data-slug="${esc(d.slug)}" style="cursor:pointer">
                  <td><strong>${esc(d.name)}</strong></td>
                  <td class="truncate dim" style="max-width:280px">${esc(d.claim, "—")}</td>
                  <td style="white-space:nowrap">${LANGS.map((l) => versionChip(d, l)).join(" ")}</td>
                  <td class="dim">${fmt.dateTime(new Date(d.updated_at * 1000).toISOString())}</td>
                  <td style="white-space:nowrap">
                    <button class="btn btn--ghost btn--sm" data-act="preview" data-slug="${esc(d.slug)}">Preview</button>
                    ${d.versions[FINAL]
                      ? `<a class="btn btn--ghost btn--sm" href="${api.onePagerPptxUrl(d.slug, FINAL)}" download
                            title="Editable PowerPoint, Deutsch (final version)">PPTX (DE)</a>`
                      : ""}
                  </td>
                </tr>`).join("")}
            </tbody>
          </table>
        </div>`;

      listCard.querySelectorAll('[data-act="preview"]').forEach((b) =>
        b.addEventListener("click", (e) => { e.stopPropagation(); select(b.dataset.slug); }));
      listCard.querySelectorAll("tr[data-slug]").forEach((tr) =>
        tr.addEventListener("click", () => select(tr.dataset.slug)));
    }

    /* ── Preview ──────────────────────────────────────────────────────── */
    function select(slug, lang) {
      state.selected = slug;
      const d = state.drafts.find((x) => x.slug === slug);
      const has = (l) => !!(d && d.versions[l]);
      // Keep the language the person last looked at, unless this startup lacks it.
      let cur = lang || state.lang;
      if (!has(cur)) cur = LANGS.find(has) || FINAL;
      state.lang = cur;
      const other = cur === "de" ? "en" : "de";

      const toggle = LANGS.map((l) => `
        <button class="btn btn--sm ${l === cur ? "btn--primary" : "btn--ghost"}" data-lang="${l}"
                ${has(l) ? "" : "disabled"} title="${has(l) ? "" : `No ${LANG_NAME[l]} version yet`}">
          ${LANG_NAME[l]}${l === FINAL ? " (final)" : ""}
        </button>`).join("");

      const translateBtn = has(cur)
        ? `<button class="btn btn--ghost btn--sm" id="op-translate"
                   title="Translate the ${LANG_NAME[cur]} version into ${LANG_NAME[other]}">
             ${has(other) ? `Update ${LANG_NAME[other]} from ${LANG_NAME[cur]}` : `Create ${LANG_NAME[other]} version`}
           </button>`
        : "";

      previewEl.innerHTML = `
        <div class="card" style="margin-top:var(--gap)">
          <div class="card__head" style="flex-wrap:wrap;gap:8px">
            <span class="card__title">Preview — ${esc(d ? d.name : slug)}</span>
            <span class="row" style="gap:4px">${toggle}</span>
            <span class="grow"></span>
            ${translateBtn}
            ${has(cur) ? `
              <a class="btn btn--ghost btn--sm" href="${api.onePagerPreviewUrl(slug, cur)}" target="_blank" rel="noopener">Open in new tab</a>
              <a class="btn btn--ghost btn--sm" href="${api.onePagerPptxUrl(slug, cur)}" download>Download PowerPoint (${LANG_SHORT[cur]})</a>` : ""}
          </div>
          <div class="dim" style="font-size:12px;margin-bottom:8px">
            Edit the YAML file <code>templates/one_pager/data/${esc(slug)}.${cur}.yaml</code> —
            enter the two images there too, in both language files.
            ${cur === FINAL ? "" : "This is the English version, for reading and sharing; the Deutsch version is the one that gets exported."}
          </div>
          ${d ? inputPanelHTML(d) : ""}
          ${has(cur)
            ? `<div class="op-frame" style="width:100%;overflow:hidden;border:1px solid var(--border);border-radius:8px;background:#E6E6E6">
                 <iframe src="${api.onePagerPreviewUrl(slug, cur)}" scrolling="no"
                   style="width:${FRAME_W}px;height:${FRAME_H}px;border:0;display:block;transform-origin:0 0"
                   title="One-pager preview (${LANG_NAME[cur]})"></iframe>
               </div>`
            : `<div class="empty" style="padding:20px"><div>No version to preview yet.</div></div>`}
        </div>`;

      fitFrame();
      previewEl.querySelectorAll("[data-lang]").forEach((b) =>
        b.addEventListener("click", () => select(slug, b.dataset.lang)));
      const tb = previewEl.querySelector("#op-translate");
      if (tb) tb.addEventListener("click", () => translate(slug, cur, other, has(other), tb));
      const rb = previewEl.querySelector("#rg-go");
      if (rb) rb.addEventListener("click", () => regenerate(slug, rb));
      previewEl.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }

    /* The rendered page is a fixed 1280x720 slide (+ its 24px margin); scale
       the whole iframe to the card's width so the page is seen entire. */
    function fitFrame() {
      const box = previewEl.querySelector(".op-frame");
      const frame = box && box.querySelector("iframe");
      if (!frame) return;
      const k = Math.min(1, box.clientWidth / FRAME_W);
      frame.style.transform = `scale(${k})`;
      box.style.height = `${Math.ceil(FRAME_H * k)}px`;
    }
    window.addEventListener("resize", fitFrame);

    /* The panel is prefilled with the input given last time, so what's shown is
       exactly what will be used: clearing a field clears that input. */
    function inputPanelHTML(d) {
      const v = d.versions[FINAL] || d.versions[LANGS.find((l) => d.versions[l])] || {};
      const m = v.manual || {};
      const given = ["location", "founded", "team_size", "notes", "logo"].filter((k) => m[k]);
      return `
        <details class="op-input" style="margin-bottom:10px;border:1px solid var(--border);border-radius:8px;padding:8px 12px">
          <summary style="cursor:pointer;font-size:13px;font-weight:600">
            Your input${given.length ? ` <span class="chip">${given.length} given</span>` : ""}
            <span class="dim" style="font-weight:400"> — change it and regenerate (both languages)</span>
          </summary>
          <div class="stack" style="gap:8px;margin-top:10px">
            <input class="input" id="rg-url" placeholder="Company website" value="${esc(v.website || "")}" style="max-width:320px">
            ${inputFieldsHTML("rg", m)}
            ${m.logo ? `<div class="dim" style="font-size:12px">Your uploaded logo is kept unless you choose a new one.</div>` : ""}
            <div class="row" style="gap:10px;align-items:center">
              <button class="btn btn--primary btn--sm" id="rg-go" ${d.has_deck ? "" : "disabled"}>Regenerate with this input</button>
              <span class="dim" style="font-size:12px">${d.has_deck
                ? "Redrafts from the kept deck — about 2–3 minutes. Searches are usually cached (free). Edits made directly in the YAML are replaced."
                : "This draft's deck wasn't kept (it predates regeneration). Upload the deck again above with “overwrite existing draft” — your input there is used."}</span>
            </div>
          </div>
        </details>`;
    }

    async function regenerate(slug, btn) {
      const panel = previewEl.querySelector(".op-input");
      const input = readInputFields(panel, "rg");
      const url = (panel.querySelector("#rg-url")?.value || "").trim();
      if (!confirmAction("Redraft this one-pager in both languages with this input? " +
                         "Edits made directly in its YAML files will be replaced.")) return;
      btn.disabled = true;
      btn.textContent = "Regenerating…";
      try {
        const res = await api.regenerateOnePager(slug, { url, ...input });
        await loadList();
        renderLog(res.draft);
        logCard.style.display = "";
        toast(`"${res.draft.name}" regenerated`);
        select(slug, state.lang);
      } catch (err) {
        toast(err.message, "error");
        btn.disabled = false;
        btn.textContent = "Regenerate with this input";
      } finally {
        loadBudget();
      }
    }

    async function translate(slug, from, to, exists, btn) {
      if (exists && !confirmAction(
        `Replace the ${LANG_NAME[to]} version with a fresh translation of the ${LANG_NAME[from]} one? ` +
        `Any edits made directly in the ${LANG_NAME[to]} file will be lost.`)) return;
      btn.disabled = true;
      btn.textContent = "Translating…";
      try {
        await api.translateOnePager(slug, from, exists);
        toast(`${LANG_NAME[to]} version ${exists ? "updated" : "created"}`);
        await loadList();
        select(slug, to);
      } catch (err) {
        toast(err.message, "error");
        btn.disabled = false;
        btn.textContent = exists ? `Update ${LANG_NAME[to]} from ${LANG_NAME[from]}` : `Create ${LANG_NAME[to]} version`;
      }
    }

    buildDbCard();
    buildForm();
    await loadList();
    const stopBatchPoll = poll(pollBatch, () => (state.batchRunning ? 4000 : 30000));
    return () => { stopBatchPoll(); window.removeEventListener("resize", fitFrame); };
  },
};
