/* ══════════════════════════════════════════════════════════════════════════
   ONE-PAGER — generate a GT Hub one-pager draft from a pitch deck.

   Upload a .pdf or .pptx, get a populated draft, preview it, download the
   editable PowerPoint. The heavy lifting runs in a SEPARATE PROCESS on the
   server (see api/routes/onepager.py) — deliberately, so a generator failure
   can never take the dashboard or the scouting pipeline with it.

   Nothing here auto-approves anything: a draft always lands with an
   open-questions list, and that list is the most important thing on screen.
   ══════════════════════════════════════════════════════════════════════════ */

import { api, fmt, esc } from "../api.js";
import { toast, confirmAction } from "../router.js";

export default {
  title: "One-Pager",

  async mount(el) {
    const state = { drafts: [], selected: null, busy: false };

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
          <span class="card__title">Neuen One-Pager erzeugen</span>
          <span class="card__hint" style="margin-left:auto">Pitch Deck als .pdf oder .pptx</span>
        </div>
        <div class="stack" style="gap:10px">
          <div class="row wrap" style="gap:10px">
            <input class="input" type="file" id="op-file" accept=".pdf,.pptx" style="max-width:280px">
            <input class="input" id="op-name" placeholder="Startup-Name *" style="max-width:220px">
            <input class="input" id="op-url" placeholder="Website (optional)" style="max-width:240px">
          </div>
          <div class="row wrap" style="gap:14px;align-items:center">
            <label class="row" style="gap:6px;font-size:12px">
              <input type="checkbox" id="op-nollm"> ohne KI-Entwurf (nur Deck + Bilder)
            </label>
            <label class="row" style="gap:6px;font-size:12px">
              <input type="checkbox" id="op-force"> vorhandenen Entwurf überschreiben
            </label>
            <span class="grow"></span>
            <button class="btn btn--primary" id="op-go">Entwurf erzeugen</button>
          </div>
          <div class="dim" style="font-size:12px">
            Der Entwurf wird lokal erzeugt (qwen2.5:7b) und dauert je nach Deck ca. 20–60&nbsp;Sekunden.
            Läuft gerade eine Ingestion, kann es länger dauern — beide teilen sich die GPU.
            Ein <code>.ppt</code> muss vorher als <code>.pptx</code> gespeichert werden.
          </div>
        </div>`;

      formCard.querySelector("#op-go").addEventListener("click", generate);
      formCard.querySelector("#op-name").addEventListener("keydown", (e) => {
        if (e.key === "Enter") generate();
      });
    }

    function setBusy(on, label) {
      state.busy = on;
      const btn = formCard.querySelector("#op-go");
      if (btn) {
        btn.disabled = on;
        btn.textContent = on ? (label || "Wird erzeugt…") : "Entwurf erzeugen";
      }
    }

    async function generate() {
      if (state.busy) return;
      const file = formCard.querySelector("#op-file").files[0];
      const name = formCard.querySelector("#op-name").value.trim();
      const url = formCard.querySelector("#op-url").value.trim();
      const noLlm = formCard.querySelector("#op-nollm").checked;
      const force = formCard.querySelector("#op-force").checked;

      if (!file) { toast("Bitte ein Pitch Deck auswählen", "error"); return; }
      if (!name) { toast("Bitte den Startup-Namen eingeben", "error"); return; }

      setBusy(true);
      logCard.style.display = "";
      logCard.innerHTML = `<div class="row" style="gap:10px;padding:4px 0">
          <span class="spinner"></span>
          <span class="dim">Deck wird gelesen, Bilder extrahiert${noLlm ? "" : ", Text wird lokal entworfen"}…</span>
        </div>`;

      try {
        const res = await api.generateOnePager({ file, name, url, noLlm, force });
        renderLog(res);
        toast(`Entwurf für „${res.draft.name}" erzeugt`);
        await loadList();
        select(res.draft.slug);
      } catch (err) {
        logCard.innerHTML = `<div class="empty" style="padding:16px">
            <div class="empty__title">Erzeugung fehlgeschlagen</div>
            <div>${esc(err.message)}</div>
          </div>`;
      } finally {
        setBusy(false);
      }
    }

    function renderLog(res) {
      const q = res.draft.open_questions || [];
      logCard.innerHTML = `
        <div class="card__head">
          <span class="card__title">${esc(res.draft.name)}</span>
          <span class="chip">Entwurf</span>
          <span class="grow"></span>
          <button class="btn btn--ghost btn--sm" id="op-log-close">schliessen</button>
        </div>
        ${res.draft.claim ? `<div style="font-size:15px;margin-bottom:10px">${esc(res.draft.claim)}</div>` : ""}
        <div class="dim" style="font-size:12px;margin-bottom:6px">
          ${q.length} Punkt${q.length === 1 ? "" : "e"}, die ein Mensch prüfen muss:
        </div>
        <ul style="margin:0;padding-left:18px;font-size:12.5px;line-height:1.55">
          ${q.map((x) => `<li>${esc(x)}</li>`).join("") || `<li class="dim">keine</li>`}
        </ul>`;
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
        listCard.innerHTML = `<div class="empty"><div class="empty__title">Konnte Entwürfe nicht laden</div>
                               <div>${esc(err.message)}</div></div>`;
        return;
      }
      renderList();
    }

    function renderList() {
      if (!state.drafts.length) {
        listCard.innerHTML = `<div class="card__head"><span class="card__title">Vorhandene One-Pager</span></div>
          <div class="empty" style="padding:20px"><div>Noch keine Entwürfe. Oben ein Pitch Deck hochladen.</div></div>`;
        return;
      }
      listCard.innerHTML = `
        <div class="card__head">
          <span class="card__title">Vorhandene One-Pager</span>
          <span class="chip">${state.drafts.length}</span>
        </div>
        <div class="table-wrap">
          <table class="table">
            <thead><tr><th>Startup</th><th>Claim</th><th>Status</th><th>Offene Punkte</th><th>Geändert</th><th></th></tr></thead>
            <tbody>
              ${state.drafts.map((d) => `
                <tr data-slug="${esc(d.slug)}" style="cursor:pointer">
                  <td><strong>${esc(d.name)}</strong></td>
                  <td class="truncate dim" style="max-width:280px">${esc(d.claim, "—")}</td>
                  <td><span class="chip ${d.status === "approved" ? "chip--brand" : ""}">${esc(d.status)}</span></td>
                  <td>${d.open_questions.length
                        ? `<span class="chip chip--warning">${d.open_questions.length}</span>`
                        : `<span class="dim">—</span>`}</td>
                  <td class="dim">${fmt.dateTime(new Date(d.updated_at * 1000).toISOString())}</td>
                  <td style="white-space:nowrap">
                    <button class="btn btn--ghost btn--sm" data-act="preview" data-slug="${esc(d.slug)}">Vorschau</button>
                    <a class="btn btn--ghost btn--sm" href="${api.onePagerPptxUrl(d.slug)}" download>PPTX</a>
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
    function select(slug) {
      state.selected = slug;
      const d = state.drafts.find((x) => x.slug === slug);
      previewEl.innerHTML = `
        <div class="card" style="margin-top:var(--gap)">
          <div class="card__head">
            <span class="card__title">Vorschau — ${esc(d ? d.name : slug)}</span>
            <span class="grow"></span>
            <a class="btn btn--ghost btn--sm" href="${api.onePagerPreviewUrl(slug)}" target="_blank" rel="noopener">in neuem Tab</a>
            <a class="btn btn--ghost btn--sm" href="${api.onePagerPptxUrl(slug)}" download>PowerPoint laden</a>
          </div>
          <div class="dim" style="font-size:12px;margin-bottom:8px">
            Bearbeitet wird die YAML-Datei unter
            <code>templates/one_pager/data/${esc(slug)}.yaml</code> — dort auch die zwei Bilder eintragen.
          </div>
          <iframe src="${api.onePagerPreviewUrl(slug)}"
                  style="width:100%;height:660px;border:1px solid var(--border);border-radius:8px;background:#fff"
                  title="One-Pager Vorschau"></iframe>
        </div>`;
      previewEl.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }

    buildForm();
    await loadList();
  },
};
