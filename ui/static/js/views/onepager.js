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
import { toast, confirmAction } from "../router.js";

const LANGS = ["de", "en"];
const FINAL = "de";
const LANG_NAME = { de: "Deutsch", en: "English" };
const LANG_SHORT = { de: "DE", en: "EN" };

export default {
  title: "One-Pager",

  async mount(el) {
    const state = { drafts: [], selected: null, lang: FINAL, busy: false };

    el.innerHTML = `
      <div class="stack">
        <div class="card" id="op-form"></div>
        <div class="card" id="op-log" style="display:none"></div>
        <div class="card" id="op-list"></div>
        <div id="op-preview"></div>
      </div>`;

    const formCard = el.querySelector("#op-form");
    const logCard = el.querySelector("#op-log");
    const listCard = el.querySelector("#op-list");
    const previewEl = el.querySelector("#op-preview");

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
            <input class="input" id="op-url" placeholder="Company website (optional)" title="Its text is read alongside the deck — useful when the deck is thin or image-heavy" style="max-width:240px">
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
            The whole deck and the website (if given) are read. Drafting runs locally
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
        const res = await api.generateOnePager({ file, name, url, noLlm, force, draftLang, webSearch, paidSearch });
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
          ${has(cur)
            ? `<iframe src="${api.onePagerPreviewUrl(slug, cur)}"
                  style="width:100%;height:660px;border:1px solid var(--border);border-radius:8px;background:#fff"
                  title="One-pager preview (${LANG_NAME[cur]})"></iframe>`
            : `<div class="empty" style="padding:20px"><div>No version to preview yet.</div></div>`}
        </div>`;

      previewEl.querySelectorAll("[data-lang]").forEach((b) =>
        b.addEventListener("click", () => select(slug, b.dataset.lang)));
      const tb = previewEl.querySelector("#op-translate");
      if (tb) tb.addEventListener("click", () => translate(slug, cur, other, has(other), tb));
      previewEl.scrollIntoView({ behavior: "smooth", block: "nearest" });
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

    buildForm();
    await loadList();
  },
};
