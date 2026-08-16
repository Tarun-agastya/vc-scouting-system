/* ══════════════════════════════════════════════════════════════════════════
   SCOUT — Bootstrap
   Wires the shell (theme, health, nav badges) and registers the views.
   ══════════════════════════════════════════════════════════════════════════ */

import { api } from "./api.js";
import { register, startRouter, poll, navigate } from "./router.js";
import { initPalette } from "./palette.js";

import overview from "./views/overview.js";
import browse from "./views/browse.js";
import reviews from "./views/reviews.js";
import ingestion from "./views/ingestion.js";
import sources from "./views/sources.js";
import regional from "./views/regional.js";

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
register("regional", regional);

/* ── Command palette + manual refresh ─────────────────────────────────────
   Refresh re-navigates to the current hash; router.navigate() force-resolves
   even when the hash is unchanged, so this re-mounts the active view. */
initPalette(api);
document.getElementById("refresh-btn").addEventListener("click", () => {
  navigate(location.hash || "#/overview");
});

startRouter();

/* ── Shell-wide status: health, pending-review badge, ingestion dot ──────
   One poll drives the whole chrome, so every page shows a live pending count
   and whether a run is in flight — without each view re-polling. */
const healthDot = document.getElementById("health-dot");
const healthText = document.getElementById("health-text");
const reviewBadge = document.getElementById("nav-review-count");
const ingestDot = document.getElementById("nav-ingest-dot");

poll(async () => {
  // All three in PARALLEL, not one-after-another. These are independent reads;
  // awaiting them in sequence made the shell's own refresh cost three serial
  // round trips every 5 seconds on every page. A backend that's down fails all
  // three effectively instantly (connection refused), so nothing is gained by
  // gating the other two behind health.
  const [health, counts, ingest] = await Promise.allSettled([
    api.health(),
    // reviewCounts, NOT listReviews({limit:200}). Measured 16 Aug: the old
    // call pulled up to 200 FULL review objects — 84.7 KB, ~440 bytes/row —
    // every 5 seconds on every page, and then read exactly one integer off
    // it (`total`) for the nav badge. /reviews/counts answers the same
    // question from a SQL aggregate in 0.1 KB. Over an 8-hour day with the
    // dashboard open that is ~490 MB of transfer and 5,760 pointless
    // 200-row serialisations saved.
    api.reviewCounts("pending"),
    api.ingestionStatus(),
  ]);

  if (health.status === "fulfilled") {
    healthDot.className = "dot dot--live";
    healthText.textContent = `${health.value.startups_in_db} startups`;
  } else {
    healthDot.className = "dot dot--error";
    healthText.textContent = "backend offline";
    return; // everything below would only render stale chrome
  }

  if (counts.status === "fulfilled") {
    const n = counts.value.total ?? 0;
    reviewBadge.textContent = n;
    reviewBadge.classList.toggle("hidden", n === 0);
  }

  if (ingest.status === "fulfilled") {
    const running = !!ingest.value.current_run;
    ingestDot.className = `dot ${running ? "dot--live" : "dot--idle"}`;
    ingestDot.classList.toggle("hidden", !running);
    ingestDot.title = running ? `Running: ${ingest.value.current_run.source}` : "";
  }
}, 5000);
