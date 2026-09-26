/* PM Klima Studio – schlanke SVG-Diagramme (eigene Implementierung, keine Fremdbibliothek). */
(function () {
  "use strict";
  const NS = "http://www.w3.org/2000/svg";

  const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  function el(name, attrs, parent) {
    const node = document.createElementNS(NS, name);
    for (const [k, v] of Object.entries(attrs || {})) node.setAttribute(k, v);
    if (parent) parent.appendChild(node);
    return node;
  }

  function niceStep(span, target) {
    const raw = span / Math.max(1, target);
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const norm = raw / mag;
    const step = norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10;
    return step * mag;
  }

  function valueAt(data, t, step) {
    let lo = 0, hi = data.length;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (data[mid][0] <= t) lo = mid + 1; else hi = mid; }
    const i = lo - 1;
    if (i < 0) return null;
    if (step || i + 1 >= data.length) return data[i][1];
    const [t0, v0] = data[i], [t1, v1] = data[i + 1];
    if (v0 == null || v1 == null) return v0;
    return v0 + (v1 - v0) * ((t - t0) / (t1 - t0 || 1));
  }

  function fmtTime(ms, tz, span) {
    const d = new Date(ms);
    const opts = span > 2 * 86400e3
      ? { weekday: "short", day: "2-digit", month: "2-digit", timeZone: tz }
      : { hour: "2-digit", minute: "2-digit", timeZone: tz };
    return new Intl.DateTimeFormat("de-DE", opts).format(d);
  }

  function fmtFull(ms, tz) {
    return new Intl.DateTimeFormat("de-DE", { weekday: "short", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit", timeZone: tz }).format(new Date(ms));
  }

  function xTicks(start, end, tz) {
    const span = end - start;
    const hour = 3600e3;
    let stepH = span <= 26 * hour ? 3 : span <= 8 * 86400e3 ? 24 : 24 * 5;
    const ticks = [];
    // an lokalen Stunden ausrichten
    const fmt = new Intl.DateTimeFormat("en-GB", { hour: "2-digit", hour12: false, timeZone: tz });
    let t = Math.ceil(start / hour) * hour;
    let guard = 0;
    while (t <= end && guard++ < 2000) {
      const h = parseInt(fmt.format(new Date(t)), 10) % 24;
      if (stepH < 24 ? h % stepH === 0 : h === 0) {
        if (stepH <= 24 || ticks.length === 0 || t - ticks[ticks.length - 1] >= stepH * hour - 2 * hour) ticks.push(t);
      }
      t += hour;
    }
    return ticks;
  }

  /**
   * opts: { start, end, tz, unit, yMin, yMax, series:[{name,color,data,step,width,dash,fill}],
   *         bands:[{name,color,items:[[from,to],...],opacity}], thresholds:[{y,color,label}], digits }
   */
  function render(container, opts) {
    container.innerHTML = "";
    container.classList.add("chart");
    const W = Math.max(280, container.clientWidth || 600);
    const H = Math.max(160, container.clientHeight || 240);
    const m = { l: 42, r: 12, t: 10, b: 24 };
    const iw = W - m.l - m.r, ih = H - m.t - m.b;
    const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": opts.label || "Diagramm" }, container);
    const start = opts.start, end = opts.end;
    const series = (opts.series || []).filter(s => s.data && s.data.length);

    let lo = Infinity, hi = -Infinity;
    for (const s of series) for (const p of s.data) if (p[1] != null) { lo = Math.min(lo, p[1]); hi = Math.max(hi, p[1]); }
    for (const th of opts.thresholds || []) { if (opts.includeThresholds !== false) { lo = Math.min(lo, th.y); hi = Math.max(hi, th.y); } }
    if (!isFinite(lo)) { lo = opts.yMin ?? 0; hi = opts.yMax ?? 1; }
    if (opts.yMin != null) lo = Math.min(lo, opts.yMin);
    if (opts.yMax != null) hi = Math.max(hi, opts.yMax);
    if (hi - lo < 1e-6) { hi += 1; lo -= 1; }
    const pad = (hi - lo) * 0.08;
    lo -= pad; hi += pad;
    const ystep = niceStep(hi - lo, H < 200 ? 4 : 5);
    lo = Math.floor(lo / ystep) * ystep; hi = Math.ceil(hi / ystep) * ystep;

    const X = t => m.l + ((t - start) / (end - start)) * iw;
    const Y = v => m.t + ih - ((v - lo) / (hi - lo)) * ih;

    // Gitter
    const grid = el("g", { class: "grid" }, svg);
    for (let v = lo; v <= hi + 1e-9; v += ystep) {
      el("line", { x1: m.l, x2: W - m.r, y1: Y(v), y2: Y(v), stroke: "rgba(197,192,211,.12)", "stroke-width": 1 }, grid);
      const txt = el("text", { x: m.l - 6, y: Y(v) + 3.5, "text-anchor": "end", fill: "rgba(236,235,242,.6)", "font-size": 10 }, grid);
      txt.textContent = (Math.round(v * 10) / 10).toLocaleString("de-DE");
    }
    for (const t of xTicks(start, end, opts.tz)) {
      el("line", { x1: X(t), x2: X(t), y1: m.t, y2: m.t + ih, stroke: "rgba(197,192,211,.07)" }, grid);
      const txt = el("text", { x: X(t), y: H - 6, "text-anchor": "middle", fill: "rgba(236,235,242,.6)", "font-size": 10 }, grid);
      txt.textContent = fmtTime(t, opts.tz, end - start);
    }

    // Bänder (Heizen, Fenster offen)
    for (const b of opts.bands || []) {
      const g = el("g", {}, svg);
      for (const [a, z] of b.items || []) {
        const x0 = X(Math.max(a, start)), x1 = X(Math.min(z, end));
        if (x1 - x0 <= 0) continue;
        el("rect", { x: x0, y: b.lane != null ? m.t + ih - 6 - b.lane * 7 : m.t, width: Math.max(1, x1 - x0),
          height: b.lane != null ? 5 : ih, fill: b.color, opacity: b.opacity ?? 0.18, rx: b.lane != null ? 2 : 0 }, g);
      }
    }

    // Schwellen
    for (const th of opts.thresholds || []) {
      if (th.y < lo || th.y > hi) continue;
      el("line", { x1: m.l, x2: W - m.r, y1: Y(th.y), y2: Y(th.y), stroke: th.color, "stroke-width": 1.2, "stroke-dasharray": "5 4", opacity: .9 }, svg);
      const t = el("text", { x: W - m.r - 4, y: Y(th.y) - 4, "text-anchor": "end", fill: th.color, "font-size": 10, "font-weight": 600 }, svg);
      t.textContent = th.label || th.y;
    }

    // Linien
    for (const s of series) {
      let d = "", open = false, prev = null;
      for (const [t, v] of s.data) {
        if (t < start - 1 || t > end + 1) { if (t > end) break; }
        if (v == null) { open = false; prev = null; continue; }
        const x = X(Math.min(Math.max(t, start), end)), y = Y(v);
        if (!open) { d += `M${x.toFixed(1)},${y.toFixed(1)}`; open = true; }
        else if (s.step && prev) d += `H${x.toFixed(1)}V${y.toFixed(1)}`;
        else d += `L${x.toFixed(1)},${y.toFixed(1)}`;
        prev = [x, y];
      }
      if (s.step && prev && open) d += `H${X(end).toFixed(1)}`;
      if (!d) continue;
      el("path", { d, fill: "none", stroke: s.color, "stroke-width": s.width || 2, "stroke-linejoin": "round",
        "stroke-linecap": "round", "stroke-dasharray": s.dash || "" }, svg);
    }

    // Markierungen (z. B. CO2-Spitzen)
    for (const mk of opts.markers || []) {
      if (mk.t < start || mk.t > end || mk.v == null) continue;
      el("circle", { cx: X(mk.t), cy: Y(mk.v), r: 4, fill: mk.color, stroke: "#0F0C2A", "stroke-width": 1.5 }, svg);
    }

    // Tooltip
    const cross = el("line", { y1: m.t, y2: m.t + ih, stroke: "rgba(236,235,242,.5)", "stroke-width": 1, visibility: "hidden" }, svg);
    const hit = el("rect", { x: m.l, y: m.t, width: iw, height: ih, fill: "transparent" }, svg);
    const tip = document.createElement("div");
    tip.className = "tip"; tip.hidden = true; container.appendChild(tip);
    const digits = opts.digits ?? 1;
    function move(ev) {
      const rect = svg.getBoundingClientRect();
      const px = ((ev.clientX - rect.left) / rect.width) * W;
      if (px < m.l || px > W - m.r) { hide(); return; }
      const t = start + ((px - m.l) / iw) * (end - start);
      cross.setAttribute("x1", px); cross.setAttribute("x2", px); cross.setAttribute("visibility", "visible");
      const rows = [`<b>${esc(fmtFull(t, opts.tz))}</b>`];
      for (const s of series) {
        const v = valueAt(s.data, t, s.step);
        if (v != null) rows.push(`<span style="color:${esc(s.color)}">●</span> ${esc(s.name)}: ${esc(Number(v).toLocaleString("de-DE", { maximumFractionDigits: digits }))}${opts.unit ? " " + esc(opts.unit) : ""}`);
      }
      for (const b of opts.bands || []) {
        if ((b.items || []).some(([a, z]) => t >= a && t <= z)) rows.push(`<span style="color:${esc(b.color)}">■</span> ${esc(b.name)}`);
      }
      tip.innerHTML = rows.join("<br>");
      tip.hidden = false;
      const left = (px / W) * rect.width;
      tip.style.left = Math.min(Math.max(left, 80), rect.width - 80) + "px";
      tip.style.top = (m.t + 8) + "px";
    }
    function hide() { cross.setAttribute("visibility", "hidden"); tip.hidden = true; }
    hit.addEventListener("pointermove", move);
    hit.addEventListener("pointerdown", move);
    hit.addEventListener("pointerleave", hide);
    return svg;
  }

  function legend(items) {
    return `<div class="chart-legend">${items.map(i =>
      `<span><i class="${i.band ? "band" : ""}" style="background:${esc(i.color)}"></i>${esc(i.name)}</span>`).join("")}</div>`;
  }

  window.KSCharts = { render, legend, valueAt };
})();
