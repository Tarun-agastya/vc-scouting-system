/* ══════════════════════════════════════════════════════════════════════════
   SCOUT — Hash router + shared UI primitives (toasts, confirm)
   Hash routing (#/browse) needs no server rewrite rules — StaticFiles just
   serves index.html and the browser handles the rest.
   ══════════════════════════════════════════════════════════════════════════ */

const routes = new Map();
let currentCleanup = null;

/** register("browse", { title, mount(el, params) -> optional cleanup fn }) */
export function register(name, view) {
  routes.set(name, view);
}

export function navigate(hash) {
  if (location.hash === hash) resolve();
  else location.hash = hash;
}

/** "#/browse?q=ai" -> { name: "browse", params: {q: "ai"} } */
function parse() {
  const raw = location.hash.replace(/^#\/?/, "") || "overview";
  const [name, query = ""] = raw.split("?");
  return { name: name || "overview", params: Object.fromEntries(new URLSearchParams(query)) };
}

async function resolve() {
  const { name, params } = parse();
  const view = routes.get(name) || routes.get("overview");

  // Tear down the previous view (stops its polling timers etc.)
  if (typeof currentCleanup === "function") {
    try { currentCleanup(); } catch { /* never let cleanup break navigation */ }
  }
  currentCleanup = null;

  document.querySelectorAll(".nav__item").forEach((el) =>
    el.classList.toggle("is-active", el.dataset.route === name));
  document.getElementById("topbar-title").textContent = view.title || "";
  document.getElementById("sidebar")?.classList.remove("is-open");

  const el = document.getElementById("view");
  el.innerHTML = `<div class="row" style="padding:40px;justify-content:center">
                    <span class="spinner"></span></div>`;
  try {
    currentCleanup = (await view.mount(el, params)) || null;
  } catch (err) {
    el.innerHTML = `<div class="empty">
        <div class="empty__title">Something went wrong</div>
        <div>${err.message}</div>
      </div>`;
  }
}

export function startRouter() {
  window.addEventListener("hashchange", resolve);
  resolve();
}

/* ── Toasts ─────────────────────────────────────────────────────────────── */

export function toast(message, kind = "ok") {
  const wrap = document.getElementById("toasts");
  const el = document.createElement("div");
  el.className = `toast ${kind === "error" ? "toast--error" : ""}`;
  el.textContent = message;
  wrap.appendChild(el);
  setTimeout(() => el.remove(), kind === "error" ? 6000 : 3200);
}

/* ── Confirm dialog (native, but centralised so it's swappable) ─────────── */

export function confirmAction(message) {
  return window.confirm(message);
}

/* ── Poller: interval that pauses when the tab is hidden ────────────────── */

export function poll(fn, ms) {
  let stopped = false;
  let timer = null;

  const tick = async () => {
    if (stopped) return;
    if (!document.hidden) {
      try { await fn(); } catch { /* transient errors shouldn't kill the poller */ }
    }
    if (!stopped) timer = setTimeout(tick, ms);
  };
  tick();

  return () => { stopped = true; if (timer) clearTimeout(timer); };
}
