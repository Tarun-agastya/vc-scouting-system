/* ══════════════════════════════════════════════════════════════════════════
   SCOUT — Bootstrap
   Wires the shell (theme, health, nav badges) and registers the views.
   ══════════════════════════════════════════════════════════════════════════ */

import { api } from "./api.js";
import { register, startRouter, poll } from "./router.js";

import overview from "./views/overview.js";
import browse from "./views/browse.js";
import reviews from "./views/reviews.js";
import ingestion from "./views/ingestion.js";
import sources from "./views/sources.js";

/* ── Theme (persisted; falls back to the OS preference) ─────────────────── */
const THEME_KEY = "scout.theme";
function applyTheme(t) {
  if (t) document.documentElement.setAttribute("data-theme", t);
  else document.documentElement.removeAttribute("data-theme");
}
applyTheme(localStorage.getItem(THEME_KEY));

document.getElementById("theme-btn").addEventListener("click", () => {
  const current = document.documentElement.getAttribute("data-theme")
    || (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark");
  const next = current === "dark" ? "light" : "dark";
  localStorage.setItem(THEME_KEY, next);
  applyTheme(next);
});

/* ── Mobile sidebar ─────────────────────────────────────────────────────── */
const menuBtn = document.getElementById("menu-btn");
const sidebar = document.getElementById("sidebar");
const syncMenu = () => { menuBtn.style.display = innerWidth <= 900 ? "" : "none"; };
menuBtn.addEventListener("click", () => sidebar.classList.toggle("is-open"));
addEventListener("resize", syncMenu);
syncMenu();

/* ── Routes ─────────────────────────────────────────────────────────────── */
register("overview", overview);
register("browse", browse);
register("reviews", reviews);
register("ingestion", ingestion);
register("sources", sources);

startRouter();

/* ── Shell-wide status: health, pending-review badge, ingestion dot ──────
   One poll drives the whole chrome, so every page shows a live pending count
   and whether a run is in flight — without each view re-polling. */
const healthDot = document.getElementById("health-dot");
const healthText = document.getElementById("health-text");
const reviewBadge = document.getElementById("nav-review-count");
const ingestDot = document.getElementById("nav-ingest-dot");

poll(async () => {
  // Health + startup count
  try {
    const h = await api.health();
    healthDot.className = "dot dot--live";
    healthText.textContent = `${h.startups_in_db} startups`;
  } catch {
    healthDot.className = "dot dot--error";
    healthText.textContent = "backend offline";
    return; // if the API is down, the rest will fail too
  }

  // Pending reviews badge
  try {
    const r = await api.listReviews({ status: "pending", limit: 200 });
    const n = r.total ?? 0;
    reviewBadge.textContent = n;
    reviewBadge.classList.toggle("hidden", n === 0);
  } catch { /* non-fatal */ }

  // Ingestion running indicator
  try {
    const s = await api.ingestionStatus();
    const running = !!s.current_run;
    ingestDot.className = `dot ${running ? "dot--live" : "dot--idle"}`;
    ingestDot.classList.toggle("hidden", !running);
    ingestDot.title = running ? `Running: ${s.current_run.source}` : "";
  } catch { /* non-fatal */ }
}, 5000);
