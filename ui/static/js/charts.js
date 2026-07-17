/* ══════════════════════════════════════════════════════════════════════════
   SCOUT — Minimal dependency-free SVG charts.

   Deliberately NOT a vendored third-party library: this system must run
   unattended on a Mac mini for a month with no network access and no build
   step. A hand-rolled ~100-line renderer is fewer moving parts and zero risk
   of a corrupted/truncated vendor file than copying in a bundled Chart.js.
   Colors are set via CSS var() so charts re-theme instantly on light/dark
   toggle with no re-render needed.
   ══════════════════════════════════════════════════════════════════════════ */

import { esc } from "./api.js";

const NS = "http://www.w3.org/2000/svg";
const el = (tag, attrs = {}) => {
  const n = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  return n;
};

/** Horizontal ranked bar chart — good for categorical breakdowns (industry, country). */
export function hBarChart(container, { labels, values, colors, max } = {}) {
  container.innerHTML = "";
  if (!labels?.length) {
    container.innerHTML = `<div class="empty" style="padding:20px">No data yet</div>`;
    return;
  }
  const m = max ?? Math.max(...values, 1);
  const rowH = 28;
  const wrap = document.createElement("div");
  wrap.style.cssText = "display:flex;flex-direction:column;gap:6px";

  labels.forEach((label, i) => {
    const v = values[i];
    const pct = m ? Math.max((v / m) * 100, 2) : 2;
    const row = document.createElement("div");
    row.style.cssText = `display:grid;grid-template-columns:120px 1fr 34px;align-items:center;gap:8px;height:${rowH}px`;
    row.innerHTML = `
      <span class="truncate dim" style="font-size:12px" title="${esc(label)}">${esc(label)}</span>
      <span style="background:var(--surface-2);border-radius:4px;height:14px;overflow:hidden">
        <span style="display:block;height:100%;width:${pct}%;background:${colors?.[i] ?? "var(--brand-lime)"};border-radius:4px"></span>
      </span>
      <span class="mono" style="font-size:12px;text-align:right">${v}</span>`;
    wrap.appendChild(row);
  });
  container.appendChild(wrap);
}

/** Donut chart — good for a small fixed set of categories (score tiers). */
export function donutChart(container, { labels, values, colors }) {
  container.innerHTML = "";
  const total = values.reduce((a, b) => a + b, 0);
  if (!total) {
    container.innerHTML = `<div class="empty" style="padding:20px">No data yet</div>`;
    return;
  }
  const size = 150, r = 56, cx = size / 2, cy = size / 2, stroke = 20;
  const circumference = 2 * Math.PI * r;
  const svg = el("svg", { viewBox: `0 0 ${size} ${size}`, width: size, height: size });

  let offset = 0;
  values.forEach((v, i) => {
    if (!v) return;
    const frac = v / total;
    const dash = frac * circumference;
    const circle = el("circle", {
      cx, cy, r, fill: "none",
      stroke: colors[i], "stroke-width": stroke,
      "stroke-dasharray": `${dash} ${circumference - dash}`,
      "stroke-dashoffset": -offset,
      transform: `rotate(-90 ${cx} ${cy})`,
    });
    circle.style.transition = "stroke-dashoffset .3s";
    svg.appendChild(circle);
    offset += dash;
  });

  const centerText = el("text", {
    x: cx, y: cy - 3, "text-anchor": "middle", class: "mono",
    style: "fill:var(--ink);font-size:20px;font-weight:700",
  });
  centerText.textContent = total;
  const centerLabel = el("text", {
    x: cx, y: cy + 14, "text-anchor": "middle",
    style: "fill:var(--ink-3);font-size:10px",
  });
  centerLabel.textContent = "total";
  svg.append(centerText, centerLabel);

  const row = document.createElement("div");
  row.style.cssText = "display:flex;align-items:center;gap:18px;flex-wrap:wrap";
  const legend = document.createElement("div");
  legend.style.cssText = "display:flex;flex-direction:column;gap:6px";
  labels.forEach((label, i) => {
    const item = document.createElement("div");
    item.className = "row";
    item.style.fontSize = "12px";
    item.innerHTML = `<span style="width:9px;height:9px;border-radius:2px;background:${colors[i]};flex:none"></span>
                       <span class="dim">${esc(label)}</span>
                       <span class="mono" style="margin-left:auto;padding-left:12px">${values[i]}</span>`;
    legend.appendChild(item);
  });
  row.append(svg, legend);
  container.appendChild(row);
}

/** Simple bar-per-bucket time series — good for "startups added over the last N days". */
export function areaChart(container, { labels, values, color }) {
  container.innerHTML = "";
  if (!labels?.length) {
    container.innerHTML = `<div class="empty" style="padding:20px">No data yet</div>`;
    return;
  }
  const w = 560, h = 130, pad = 4;
  const max = Math.max(...values, 1);
  const bw = (w - pad * 2) / labels.length;

  const svg = el("svg", { viewBox: `0 0 ${w} ${h + 20}`, style: "width:100%;height:auto;display:block" });
  labels.forEach((label, i) => {
    const v = values[i];
    const barH = Math.max((v / max) * h, v > 0 ? 3 : 0);
    const x = pad + i * bw;
    const rect = el("rect", {
      x: x + 1.5, y: h - barH, width: Math.max(bw - 3, 1), height: barH,
      rx: 2, style: `fill:${color ?? "var(--brand-lime)"};opacity:${v ? 1 : .18}`,
    });
    const title = el("title");
    title.textContent = `${label}: ${v}`;
    rect.appendChild(title);
    svg.appendChild(rect);

    // Sparse x-axis labels so long ranges stay legible
    if (labels.length <= 10 || i % Math.ceil(labels.length / 8) === 0) {
      const t = el("text", {
        x: x + bw / 2, y: h + 14, "text-anchor": "middle",
        style: "fill:var(--ink-3);font-size:9px",
      });
      t.textContent = label;
      svg.appendChild(t);
    }
  });
  container.appendChild(svg);
}
