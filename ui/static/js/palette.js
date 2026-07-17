/* ══════════════════════════════════════════════════════════════════════════
   SCOUT — ⌘K command palette
   Jump to any page or run a common action without touching the mouse.
   ══════════════════════════════════════════════════════════════════════════ */

import { navigate, toast } from "./router.js";

function buildCommands(api) {
  return [
    { label: "Go to Overview", hint: "page", run: () => navigate("#/overview") },
    { label: "Go to Browse & Search", hint: "page", run: () => navigate("#/browse") },
    { label: "Go to Review Inbox", hint: "page", run: () => navigate("#/reviews") },
    { label: "Go to Ingestion Control", hint: "page", run: () => navigate("#/ingestion") },
    { label: "Go to Sources", hint: "page", run: () => navigate("#/sources") },
    {
      label: "Run full ingestion sweep", hint: "action",
      run: async () => { await api.runAll(); toast("Full sweep started"); navigate("#/ingestion"); },
    },
    {
      label: "Toggle light / dark theme", hint: "action",
      run: () => document.getElementById("theme-btn").click(),
    },
  ];
}

export function initPalette(api) {
  const overlay = document.getElementById("palette-overlay");
  const input = document.getElementById("palette-input");
  const list = document.getElementById("palette-list");
  const commands = buildCommands(api);
  let filtered = commands;
  let activeIdx = 0;

  function render() {
    if (!filtered.length) {
      list.innerHTML = `<div class="palette__empty">No matching command</div>`;
      return;
    }
    list.innerHTML = filtered.map((c, i) => `
      <div class="palette__item ${i === activeIdx ? "is-active" : ""}" data-idx="${i}">
        <span class="grow">${c.label}</span>
        <span class="dim">${c.hint}</span>
      </div>`).join("");
    list.querySelectorAll("[data-idx]").forEach((el) =>
      el.addEventListener("mousemove", () => { activeIdx = Number(el.dataset.idx); render(); }));
    list.querySelectorAll("[data-idx]").forEach((el) =>
      el.addEventListener("click", () => execute(filtered[Number(el.dataset.idx)])));
  }

  function execute(cmd) {
    close();
    cmd?.run();
  }

  function open() {
    overlay.classList.remove("hidden");
    input.value = "";
    filtered = commands;
    activeIdx = 0;
    render();
    setTimeout(() => input.focus(), 0);
  }

  function close() {
    overlay.classList.add("hidden");
  }

  input.addEventListener("input", () => {
    const q = input.value.toLowerCase();
    filtered = commands.filter((c) => c.label.toLowerCase().includes(q));
    activeIdx = 0;
    render();
  });

  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") { e.preventDefault(); activeIdx = Math.min(activeIdx + 1, filtered.length - 1); render(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); activeIdx = Math.max(activeIdx - 1, 0); render(); }
    else if (e.key === "Enter") { e.preventDefault(); execute(filtered[activeIdx]); }
    else if (e.key === "Escape") { close(); }
  });

  overlay.addEventListener("click", (e) => { if (e.target === overlay) close(); });
  document.getElementById("palette-btn").addEventListener("click", open);

  document.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
      e.preventDefault();
      overlay.classList.contains("hidden") ? open() : close();
    }
  });
}
