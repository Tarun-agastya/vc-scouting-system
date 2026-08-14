/* ══════════════════════════════════════════════════════════════════════════
   SCOUT — Regionale Unternehmen (Phase RC)

   Established employers within 50 km of Memmingen, tracked as GreenTech Hub
   membership prospects. A SEPARATE page on purpose: this is a different
   dataset from the startup funnel (~1,800 records), and burying ~1,300
   regional companies inside it would make both unusable.

   The table keeps the team's own Excel column order so the page reads as the
   familiar artefact. Two halves with different owners:
     · Standort / Mitarbeiter / Umsatz / Branche / Kurzbeschreibung are
       machine-gathered and each carry a source link — hover the ⓘ to see
       where a value came from. Umsatz is a FALLBACK qualifying signal
       (11 Aug 2026) — many established firms don't publish a headcount, so a
       recent, sourced EUR 25m+ revenue figure counts just as much.
     · Prio / Status / Phase / Kontakt / Wer hat Kontakt are HUMAN-ONLY and
       editable here; no automated step ever writes them.
   ══════════════════════════════════════════════════════════════════════════ */

import { api, fmt, esc } from "../api.js";
import { toast, poll } from "../router.js";

const TIER_CHIP = {
  1: { label: "bereit", cls: "chip--ok" },
  2: { label: "anreichern", cls: "chip--warning" },
  3: { label: "niedrige Prio", cls: "" },
  9: { label: "außerhalb", cls: "chip--danger" },
};

function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

export default {
  title: "Regionale Unternehmen",

  async mount(el) {
    const state = {
      q: "", tier: "", branche: "", city: "", status: "",
      maxDistance: "", minEmployees: "", maxEmployees: "", minRevenue: "",
      missingEmployees: false, sort: "distance",
      limit: 100, offset: 0, total: 0, rows: [], selected: null,
    };

    el.innerHTML = `
      <div class="stack">
        <div class="kpis" id="rc-kpis"></div>
        <div id="rc-progress"></div>
        <div class="card">
          <div class="row wrap" style="gap:8px">
            <input class="input" id="rc-q" placeholder="Name, Branche, Ort…" style="max-width:220px">
            <select class="select" id="rc-tier" style="max-width:190px">
              <option value="">Alle Stufen</option>
              <option value="1">1 · bereit (Mitarbeiterzahl bekannt)</option>
              <option value="2">2 · anreichern</option>
              <option value="3">3 · niedrige Priorität</option>
            </select>
            <select class="select" id="rc-branche" style="max-width:190px"><option value="">Alle Branchen</option></select>
            <select class="select" id="rc-city" style="max-width:170px"><option value="">Alle Orte</option></select>
            <select class="select" id="rc-status" style="max-width:160px"><option value="">Alle Status</option></select>
            <select class="select" id="rc-dist" style="max-width:150px">
              <option value="">Alle Entfernungen</option>
              <option value="10">≤ 10 km</option><option value="25">≤ 25 km</option>
              <option value="50">≤ 50 km</option>
            </select>
            <input class="input" id="rc-minemp" type="number" placeholder="MA ab" style="max-width:100px">
            <input class="input" id="rc-maxemp" type="number" placeholder="MA bis" style="max-width:100px">
            <input class="input" id="rc-minrev" type="number" placeholder="Umsatz ab (Mio. €)" style="max-width:150px">
            <select class="select" id="rc-sort" style="max-width:180px">
              <option value="distance">Nach Entfernung</option>
              <option value="employees">Nach Mitarbeitern</option>
              <option value="tier">Nach Stufe</option>
              <option value="name_asc">Nach Name (A→Z)</option>
              <option value="name_desc">Nach Name (Z→A)</option>
            </select>
            <label class="row" style="gap:6px;font-size:12px">
              <input type="checkbox" id="rc-missing"> ohne Mitarbeiterzahl
            </label>
            <span class="grow"></span>
            <button class="btn btn--ghost btn--sm" id="rc-export">⬇ Export für Excel</button>
          </div>
        </div>
        <div class="card" style="padding:0">
          <div class="table-wrap" style="max-height:66vh;overflow:auto">
            <table class="table" id="rc-table"></table>
          </div>
          <div class="row" style="padding:10px 12px;gap:10px;border-top:1px solid var(--border)">
            <span class="dim" style="font-size:12px" id="rc-count"></span>
            <span class="grow"></span>
            <button class="btn btn--ghost btn--sm" id="rc-prev">← Zurück</button>
            <button class="btn btn--ghost btn--sm" id="rc-next">Weiter →</button>
          </div>
        </div>
        <div id="rc-detail"></div>
      </div>`;

    const $ = (id) => el.querySelector(id);
    const tableEl = $("#rc-table"), detailEl = $("#rc-detail");

    function filters() {
      return {
        q: state.q || undefined,
        tier: state.tier || undefined,
        branche: state.branche || undefined,
        city: state.city || undefined,
        status: state.status || undefined,
        max_distance: state.maxDistance || undefined,
        min_employees: state.minEmployees || undefined,
        max_employees: state.maxEmployees || undefined,
        min_revenue: state.minRevenue || undefined,
        missing_employees: state.missingEmployees || undefined,
      };
    }

    /** Background enrichment progress. Hidden entirely when there is nothing
     *  to report, so the page is not cluttered by an idle empty bar. */
    async function loadProgress() {
      const box = $("#rc-progress");
      let s;
      try { s = await api.regionalEnrichmentStatus(); }
      catch { box.innerHTML = ""; return; }

      if (!s.queue_remaining && !s.active) { box.innerHTML = ""; return; }

      const total = s.attempted + s.queue_remaining;
      const donePct = total ? Math.round((s.attempted / total) * 100) : 0;
      const mins = s.seconds_since_last == null ? null : Math.floor(s.seconds_since_last / 60);

      box.innerHTML = `
        <div class="card">
          <div class="row" style="gap:10px;align-items:center">
            <span style="font-size:16px">${s.active ? "⏳" : "⏸"}</span>
            <strong style="font-size:13px">
              ${s.active ? "Anreicherung läuft" : "Anreicherung pausiert"}
            </strong>
            <span class="dim" style="font-size:12px">
              ${fmt.num(s.attempted)} von ${fmt.num(total)} bearbeitet ·
              ${fmt.num(s.queue_remaining)} offen
              ${s.queue_remaining ? ` · noch ca. ${s.eta_hours} h` : ""}
            </span>
            <span class="grow"></span>
            <span class="dim" style="font-size:11px">
              ${mins == null ? "" : `letzte Aktivität vor ${mins} min`}
            </span>
          </div>
          <div style="background:var(--surface-2);border-radius:4px;height:8px;
                      overflow:hidden;margin-top:8px">
            <span style="display:block;height:100%;width:${donePct}%;
                         background:var(--brand-lime)"></span>
          </div>
          ${s.active ? "" : `<div class="dim" style="font-size:11px;margin-top:6px">
            Kein Fortschritt seit ${mins} min — der Lauf ist beendet oder gestoppt.
            Neu starten: <code>python3 -u scripts/rc_enrich.py --limit 400 --apply</code>
          </div>`}
        </div>`;
    }

    async function loadKpis() {
      try {
        const s = await api.regionalStats();
        const tier = (n) => (s.by_tier.find((t) => t.tier === n) || {}).count || 0;
        $("#rc-kpis").innerHTML = `
          <div class="kpi kpi--accent"><div class="kpi__label">Im Umkreis 50 km</div><div class="kpi__value">${fmt.num(s.in_radius)}</div></div>
          <div class="kpi"><div class="kpi__label">Bereit (Stufe 1)</div><div class="kpi__value">${fmt.num(tier(1))}</div></div>
          <div class="kpi"><div class="kpi__label">Anzureichern (Stufe 2)</div><div class="kpi__value">${fmt.num(tier(2))}</div></div>
          <div class="kpi"><div class="kpi__label">Mit Mitarbeiterzahl</div><div class="kpi__value">${fmt.num(s.with_employees)}</div></div>
          <div class="kpi"><div class="kpi__label">Mit Umsatz</div><div class="kpi__value">${fmt.num(s.with_revenue)}</div></div>
          <div class="kpi"><div class="kpi__label">Kontaktiert</div><div class="kpi__value">${fmt.num(s.contacted)}</div></div>`;
      } catch { /* KPIs are a convenience */ }
    }

    async function loadFacets() {
      try {
        const f = await api.regionalFacets();
        const fill = (sel, vals, label) => {
          sel.innerHTML = `<option value="">${label}</option>` +
            vals.map((v) => `<option value="${esc(v)}">${esc(v)}</option>`).join("");
        };
        fill($("#rc-branche"), f.branchen, "Alle Branchen");
        fill($("#rc-city"), f.cities, "Alle Orte");
        fill($("#rc-status"), ["(leer)", ...f.statuses], "Alle Status");
      } catch { /* filters still work without menus */ }
    }

    async function load() {
      let res;
      // state.sort carries the dropdown's raw value ("name_asc"/"name_desc"
      // for the alphabetical modes, a bare mode name otherwise) — split it
      // here rather than in state, so the backend's plain sort/order params
      // stay the single source of truth (see api/routes/regional.py).
      const [sortKey, sortDir] = state.sort.startsWith("name_")
        ? ["name", state.sort.slice("name_".length)]
        : [state.sort, undefined];
      try {
        res = await api.listRegional({ ...filters(), sort: sortKey, order: sortDir,
                                       limit: state.limit, offset: state.offset });
      } catch (err) {
        tableEl.innerHTML = `<tbody><tr><td class="empty">${esc(err.message)}</td></tr></tbody>`;
        return;
      }
      state.rows = res.companies;
      state.total = res.total;
      render();
    }

    /** "45,2 Mio. EUR (2025)" — mirrors the exact format the enrichment
     *  prompt asks the model for, so the dashboard reads like the source. */
    function formatRevenue(row) {
      if (row.revenue_eur_millions == null) return null;
      const num = row.revenue_eur_millions.toLocaleString("de-DE", { maximumFractionDigits: 1 });
      const year = row.revenue_year ? ` (${row.revenue_year})` : "";
      return `${num} Mio. €${year}`;
    }

    /** Machine-gathered values show a ⓘ linking to their source. This is the
     *  register's core promise — every number can be traced, nothing has to
     *  be taken on trust. */
    function cite(row, field, value) {
      const shown = esc(value === null || value === undefined || value === "" ? "—" : value);
      const src = (row.field_sources || {})[field];
      if (!src || typeof src !== "string") return shown;
      return `${shown} <a href="${esc(src)}" target="_blank" rel="noopener"
                 class="dim" title="Quelle: ${esc(src)}" style="text-decoration:none">ⓘ</a>`;
    }

    function render() {
      if (!state.rows.length) {
        tableEl.innerHTML = `<tbody><tr><td class="empty" style="padding:30px">
          Keine Unternehmen für diese Filter.</td></tr></tbody>`;
        $("#rc-count").textContent = "0 Unternehmen";
        return;
      }
      tableEl.innerHTML = `
        <thead><tr>
          <th>Name</th><th>Standort</th><th style="text-align:right">km</th>
          <th style="text-align:right">Mitarbeiter</th><th style="text-align:right">Umsatz</th>
          <th>Branche</th>
          <th>Kurzbeschreibung</th><th>Status</th><th>Prio</th><th>Stufe</th>
        </tr></thead>
        <tbody>${state.rows.map((r) => {
          const t = TIER_CHIP[r.triage_tier] || { label: "?", cls: "" };
          return `
          <tr data-id="${esc(r.id)}" style="cursor:pointer">
            <td><strong>${esc(r.name)}</strong>${r.website
                ? ` <a href="${esc(r.website)}" target="_blank" rel="noopener" class="dim"
                       style="text-decoration:none" title="${esc(r.website)}">↗</a>` : ""}</td>
            <td>${cite(r, "city", r.city)}</td>
            <td style="text-align:right" class="mono">${r.distance_km != null ? r.distance_km.toFixed(0) : "—"}</td>
            <td style="text-align:right" class="mono">${cite(r, "employees", r.employees)}</td>
            <td style="text-align:right" class="mono">${cite(r, "revenue_eur_millions", formatRevenue(r))}</td>
            <td>${cite(r, "branche", r.branche)}</td>
            <td class="truncate" style="max-width:320px">${esc(r.kurzbeschreibung, "—")}</td>
            <td>${r.status ? `<span class="chip">${esc(r.status)}</span>` : '<span class="dim">—</span>'}</td>
            <td class="mono">${r.prio ?? "—"}</td>
            <td><span class="chip ${t.cls}" style="font-size:11px">${t.label}</span></td>
          </tr>`; }).join("")}</tbody>`;

      const from = state.offset + 1, to = Math.min(state.offset + state.limit, state.total);
      $("#rc-count").textContent = `${from}–${to} von ${fmt.num(state.total)} Unternehmen`;
      $("#rc-prev").disabled = state.offset === 0;
      $("#rc-next").disabled = to >= state.total;

      tableEl.querySelectorAll("tr[data-id]").forEach((tr) =>
        tr.addEventListener("click", () => openDetail(tr.dataset.id)));
    }

    async function openDetail(id) {
      let c;
      try { c = await api.getRegional(id); }
      catch (err) { toast(err.message, "error"); return; }
      state.selected = c;

      const srcRows = Object.entries(c.field_sources || {})
        .filter(([k]) => !k.startsWith("proposed_"))
        .map(([k, v]) => `<div style="font-size:12px"><span class="dim">${esc(k)}:</span>
            <a href="${esc(v)}" target="_blank" rel="noopener">${esc(String(v).slice(0, 70))}</a></div>`)
        .join("") || '<div class="dim" style="font-size:12px">Noch keine Quellen erfasst</div>';

      // Values the enrichment found that DISAGREE with what is stored. Shown,
      // never auto-applied — a human decides which is right.
      const disputed = Object.entries(c.field_sources || {})
        .filter(([k]) => k.startsWith("proposed_"))
        .map(([k, v]) => `<div style="font-size:12px">
            <span class="chip chip--warning" style="font-size:10px">abweichend</span>
            <strong>${esc(k.replace("proposed_", ""))}</strong>:
            gespeichert <em>${esc(v.current)}</em> · gefunden <em>${esc(v.value)}</em>
            <a href="${esc(v.source_url)}" target="_blank" rel="noopener">Quelle</a></div>`)
        .join("");

      detailEl.innerHTML = `
        <div class="card">
          <div class="row">
            <span class="card__title">${esc(c.name)}</span>
            <span class="dim" style="font-size:12px">${esc(c.city, "?")} ·
              ${c.distance_km != null ? c.distance_km.toFixed(1) + " km" : "Entfernung unbekannt"} ·
              Quelle: ${esc(c.source, "?")}</span>
            <span class="grow"></span>
            <button class="btn btn--ghost btn--sm" id="rc-close">✕</button>
          </div>
          ${c.kurzbeschreibung ? `<p style="font-size:13px;line-height:1.5">${esc(c.kurzbeschreibung)}</p>` : ""}
          ${disputed ? `<div class="stack" style="gap:4px;margin:8px 0">${disputed}</div>` : ""}

          <div class="grid-2" style="margin-top:12px">
            <div>
              <div class="dim" style="font-size:11px;text-transform:uppercase;letter-spacing:.06em;margin-bottom:6px">
                Kontaktpflege — nur von Hand</div>
              <div class="stack" style="gap:8px">
                <label style="font-size:12px">Status
                  <input class="input" id="ed-status" value="${esc(c.status, "")}" placeholder="z.B. Absage, aktiv"></label>
                <label style="font-size:12px">Prio
                  <input class="input" id="ed-prio" type="number" value="${c.prio ?? ""}"></label>
                <label style="font-size:12px">Phase
                  <input class="input" id="ed-phase" value="${esc(c.phase, "")}"></label>
                <label style="font-size:12px">Wer hat Kontakt
                  <input class="input" id="ed-wer" value="${esc(c.wer_hat_kontakt, "")}"></label>
                <label style="font-size:12px">Kontakt allgemein
                  <textarea class="input" id="ed-kontakt" rows="3">${esc(c.kontakt, "")}</textarea></label>
                <button class="btn btn--primary btn--sm" id="rc-save">Speichern</button>
              </div>
            </div>
            <div>
              <div class="dim" style="font-size:11px;text-transform:uppercase;letter-spacing:.06em;margin-bottom:6px">
                Quellen je Feld</div>
              <div class="stack" style="gap:4px">${srcRows}</div>
              ${c.notes ? `<div class="dim" style="font-size:12px;margin-top:10px">⚠ ${esc(c.notes)}</div>` : ""}
            </div>
          </div>
        </div>`;

      detailEl.querySelector("#rc-close").addEventListener("click", () => {
        detailEl.innerHTML = ""; state.selected = null;
      });
      detailEl.querySelector("#rc-save").addEventListener("click", async () => {
        const prio = detailEl.querySelector("#ed-prio").value;
        try {
          await api.editRegional(c.id, {
            status: detailEl.querySelector("#ed-status").value || null,
            prio: prio === "" ? null : Number(prio),
            phase: detailEl.querySelector("#ed-phase").value || null,
            wer_hat_kontakt: detailEl.querySelector("#ed-wer").value || null,
            kontakt: detailEl.querySelector("#ed-kontakt").value || null,
          });
          toast("Gespeichert");
          await Promise.all([load(), loadKpis(), loadFacets()]);
        } catch (err) { toast(`Speichern fehlgeschlagen: ${err.message}`, "error"); }
      });
      detailEl.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }

    // ── wiring ────────────────────────────────────────────────────────────
    const reload = () => { state.offset = 0; load(); };
    $("#rc-q").addEventListener("input", debounce((e) => { state.q = e.target.value; reload(); }, 300));
    $("#rc-tier").addEventListener("change", (e) => { state.tier = e.target.value; reload(); });
    $("#rc-branche").addEventListener("change", (e) => { state.branche = e.target.value; reload(); });
    $("#rc-city").addEventListener("change", (e) => { state.city = e.target.value; reload(); });
    $("#rc-status").addEventListener("change", (e) => { state.status = e.target.value; reload(); });
    $("#rc-dist").addEventListener("change", (e) => { state.maxDistance = e.target.value; reload(); });
    $("#rc-minemp").addEventListener("input", debounce((e) => { state.minEmployees = e.target.value; reload(); }, 400));
    $("#rc-maxemp").addEventListener("input", debounce((e) => { state.maxEmployees = e.target.value; reload(); }, 400));
    $("#rc-minrev").addEventListener("input", debounce((e) => { state.minRevenue = e.target.value; reload(); }, 400));
    $("#rc-sort").addEventListener("change", (e) => { state.sort = e.target.value; reload(); });
    $("#rc-missing").addEventListener("change", (e) => { state.missingEmployees = e.target.checked; reload(); });
    $("#rc-prev").addEventListener("click", () => {
      state.offset = Math.max(0, state.offset - state.limit); load();
    });
    $("#rc-next").addEventListener("click", () => {
      if (state.offset + state.limit < state.total) { state.offset += state.limit; load(); }
    });
    // Export goes through the server so it covers the WHOLE filtered set, not
    // just the page in the browser.
    $("#rc-export").addEventListener("click", () => {
      window.location.href = api.regionalExportUrl(filters());
    });

    await Promise.all([loadKpis(), loadFacets(), loadProgress()]);
    await load();

    // The TABLE is not polled — it would fight with whatever the user is
    // typing into a filter. Only the progress strip and the KPI counts
    // refresh, every 30s, since an enrichment run takes hours and a static
    // page gave no sign it was working at all.
    const stop = poll(() => { loadProgress(); loadKpis(); }, 30000);
    return () => stop();
  },
};
