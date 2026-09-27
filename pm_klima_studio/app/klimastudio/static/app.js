/* PM Klima Studio – Oberfläche (ohne Build-Kette, ohne externe Hosts). */
(function () {
  "use strict";

  const DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"];
  const DAY_LONG = { monday: "Montag", tuesday: "Dienstag", wednesday: "Mittwoch", thursday: "Donnerstag", friday: "Freitag", saturday: "Samstag", sunday: "Sonntag" };
  const DAY_SHORT = { monday: "Mo", tuesday: "Di", wednesday: "Mi", thursday: "Do", friday: "Fr", saturday: "Sa", sunday: "So" };
  const WEEKDAYS = DAYS.slice(0, 5), WEEKEND = DAYS.slice(5);
  const SNAP = 15, DAY_MIN = 1440, T_MIN = 5, T_MAX = 25;
  const C = { auto: "#B885D6", heat: "#E8875A", off: "#827E96", heizt: "#F2B35C", cloud: "#ECEBF2", lavender: "#C5C0D3", purple: "#784295", ok: "#7FC8A9", warn: "#F2B35C", bad: "#E8875A", sky: "#8FB3E8" };
  const RANGE_LABEL = { "24h": "24 Stunden", "7d": "7 Tage", "30d": "30 Tage" };

  const S = {
    info: null, tab: "uebersicht", raum: null, bereich: "24h",
    plan: null, // {raum, id, name, revision, tage, orig}
    sel: null,  // {day, idx} oder {neu:true, day, start, end, temp}
    gen: 0,     // Render-Generation: nur der jüngste render()-Lauf darf schreiben
    view: null, // Container des aktuellen Reiters in #main
  };

  const $ = (sel, root) => (root || document).querySelector(sel);
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const num = (v, d = 1) => v == null || isNaN(v) ? "–" : Number(v).toLocaleString("de-DE", { maximumFractionDigits: d, minimumFractionDigits: 0 });
  const hm = m => `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;
  const parseHm = s => {
    const m = /^\s*(\d{1,2})[:.]?(\d{2})\s*$/.exec(String(s));
    if (!m) return NaN;
    const h = +m[1], mi = +m[2];
    if (h === 24 && mi === 0) return DAY_MIN;
    return h < 24 && mi < 60 ? h * 60 + mi : NaN;
  };
  const tz = () => (S.info && S.info.zeitzone) || "Europe/Berlin";
  const fmtDate = (ms, withTime = true) => new Intl.DateTimeFormat("de-DE", Object.assign({ weekday: "short", day: "2-digit", month: "2-digit", timeZone: tz() }, withTime ? { hour: "2-digit", minute: "2-digit" } : {})).format(new Date(ms));
  const clone = o => JSON.parse(JSON.stringify(o));
  // Betriebsart: generisch (ohne PM Klima) oder pm_networks (Standard, auch bei älteren Servern ohne Feld)
  const generisch = () => !!(S.info && S.info.betriebsart === "generisch");
  // Fähigkeit laut /api/info; fehlt die Angabe (älterer Server), gilt das bisherige Verhalten
  const kann = k => { const f = S.info && S.info.faehigkeiten; return !f || typeof f[k] !== "boolean" ? true : f[k]; };

  // ------------------------------------------------------------------ API
  async function api(path, opts = {}) {
    const init = { method: opts.method || "GET", headers: {} };
    if (opts.body !== undefined) { init.body = JSON.stringify(opts.body); init.headers["Content-Type"] = "application/json"; }
    const res = await fetch("api/" + path, init);
    const text = await res.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch (_) { data = { fehler: text }; }
    if (!res.ok && !(opts.accept || []).includes(res.status)) {
      const err = new Error((data && (data.fehler || data.message)) || `HTTP ${res.status}`);
      err.status = res.status; err.data = data; throw err;
    }
    return { status: res.status, data };
  }

  function toast(msg, err) {
    const t = $("#toast");
    t.textContent = msg; t.className = "toast" + (err ? " err" : ""); t.hidden = false;
    clearTimeout(toast._t); toast._t = setTimeout(() => { t.hidden = true; }, err ? 6000 : 3200);
  }

  function modal(title, bodyHtml, actions, opts) {
    const m = $("#modal");
    m.querySelector(".modal-card").classList.toggle("wide", !!(opts && opts.wide));
    $("#modal-title").textContent = title;
    $("#modal-body").innerHTML = bodyHtml;
    const box = $("#modal-actions"); box.innerHTML = "";
    return new Promise(resolve => {
      for (const a of actions) {
        const b = document.createElement("button");
        b.className = "btn " + (a.cls || ""); b.textContent = a.label;
        b.addEventListener("click", () => { m.hidden = true; resolve(a.value); });
        box.appendChild(b);
      }
      m.hidden = false;
      const first = box.querySelector(".primary") || box.querySelector("button");
      if (first) first.focus();
    });
  }

  // ------------------------------------------------------------------ Navigation
  function route() {
    const parts = location.hash.replace(/^#\/?/, "").split("/");
    // Gültige Reiter ergeben sich aus der Navigation in index.html (keine zweite, feste Liste)
    const tabs = [...document.querySelectorAll("#tabs a[data-tab]")].map(a => a.dataset.tab);
    const tab = tabs.includes(parts[0]) ? parts[0] : "uebersicht";
    if (S.plan && S.tab === "heizplan" && tab !== "heizplan" && isDirty()) {
      if (!confirm("Ungespeicherte Änderungen am Heizplan verwerfen?")) { location.hash = "#/heizplan/" + S.plan.raum; return; }
      S.plan = null;
    }
    if (S.tab !== tab && EXT[S.tab] && EXT[S.tab].leave) EXT[S.tab].leave();
    S.tab = tab;
    S.parts = parts;
    $("#toast").hidden = true;
    if (parts[1] && S.info && S.info.raeume.some(r => r.raum === parts[1])) S.raum = parts[1];
    document.querySelectorAll("#tabs a").forEach(a => a.classList.toggle("active", a.dataset.tab === tab));
    render();
  }

  function rooms() { return (S.info && S.info.raeume) || []; }
  function roomName(slug) { const r = rooms().find(x => x.raum === slug); return r ? r.name : slug; }

  function roomChips(filter) {
    const list = rooms().filter(filter || (() => true));
    if (!S.raum || !list.some(r => r.raum === S.raum)) S.raum = list.length ? list[0].raum : null;
    return `<div class="chips" id="roomchips">${list.map(r =>
      `<button class="chip ${r.raum === S.raum ? "active" : ""}" data-raum="${esc(r.raum)}">${esc(r.name)}</button>`).join("")}</div>`;
  }
  function bindRoomChips(main, onChange) {
    main.querySelectorAll("#roomchips .chip").forEach(b => b.addEventListener("click", () => {
      if (S.tab === "heizplan" && isDirty() && !confirm("Ungespeicherte Änderungen verwerfen?")) return;
      S.raum = b.dataset.raum;
      // Ändert sich der Hash, rendert route(); ein zusätzlicher Lauf würde nur verworfen.
      const ziel = `#/${S.tab}/${S.raum}`;
      if (location.hash !== ziel) location.hash = ziel; else if (onChange) onChange();
    }));
  }
  function rangeChips() {
    return `<div class="chips" id="rangechips">${Object.keys(RANGE_LABEL).map(k =>
      `<button class="chip ${k === S.bereich ? "active" : ""}" data-b="${k}">${RANGE_LABEL[k]}</button>`).join("")}</div>`;
  }
  function bindRangeChips(main) {
    main.querySelectorAll("#rangechips .chip").forEach(b => b.addEventListener("click", () => { S.bereich = b.dataset.b; render(); }));
  }

  // Jeder Lauf schreibt in einen eigenen, frischen Container. Ein veralteter Lauf (Reiter inzwischen
  // gewechselt) schreibt nur noch in einen abgehängten Knoten und bricht nach jedem await ab.
  const stale = view => !view.isConnected;
  async function render() {
    const gen = ++S.gen;
    const main = document.createElement("div");
    main.className = "view";
    S.view = main;
    $("#main").replaceChildren(main);
    try {
      if (S.tab === "uebersicht") await renderOverview(main);
      else if (S.tab === "heizplan") await renderPlan(main);
      else if (S.tab === "auswertung") await renderAnalysis(main);
      else if (S.tab === "berichte") await renderReports(main);
      else if (EXT[S.tab]) await EXT[S.tab].render(main, S.parts || []);
      else main.innerHTML = `<div class="errbox">Die Oberfläche ist veraltet. Bitte die Seite neu laden.</div>`;
    } catch (e) {
      if (gen !== S.gen || stale(main)) return;
      main.innerHTML = `<div class="errbox">${esc(e.message)}</div>`;
    }
  }

  // ------------------------------------------------------------------ Übersicht
  function modeInfo(cur) {
    if (!cur || !cur.modus) return { cls: "off", text: "unbekannt" };
    if (cur.hvac_action === "heating") return { cls: "heizt", text: "heizt" };
    if (cur.modus === "auto") return { cls: "auto", text: "Auto" };
    if (cur.modus === "heat") return { cls: "heat", text: "Heizen" };
    if (cur.modus === "off") return { cls: "off", text: "Aus" };
    return { cls: "off", text: cur.modus };
  }
  function statusLine(r) {
    const parts = [];
    for (const p of [r.anzeige, r.grund, r.preset !== "none" ? r.preset : null]) {
      if (p && !parts.some(x => x.toLowerCase() === String(p).toLowerCase())) parts.push(String(p));
    }
    return parts.join(" · ");
  }
  const lvl = (v, warn, bad) => v == null ? "" : v >= bad ? "bad" : v >= warn ? "warn" : "ok";

  // Betriebsart generisch: App-Modus (plan | hand | aus), Plan-Soll und Status aus /api/aktuell (app_modus, plan_soll, app_status), Thermostat-Modus klein
  const APP_MODUS = { plan: { cls: "auto", text: "Plan" }, hand: { cls: "heat", text: "Hand" }, aus: { cls: "off", text: "Aus" } };
  const HVAC_TEXT = { heat: "Heizen", off: "Aus", auto: "Auto", heat_cool: "Heizen/Kühlen", cool: "Kühlen", dry: "Entfeuchten", fan_only: "Lüfter", unavailable: "nicht verfügbar", unknown: "unbekannt" };
  const hvacText = m => m == null ? "–" : HVAC_TEXT[m] || String(m);
  // Nur Anzeige: Thermostat mit Temperaturbereich oder nicht verfügbar
  const nurAnzeige = cur => !!cur.soll_bereich || cur.modus == null || cur.modus === "unavailable" || cur.modus === "unknown";
  function modeInfoGen(cur) {
    if (cur.hvac_action === "heating") return { cls: "heizt", text: "heizt" };
    if (!cur.app_modus) return { cls: "off", text: cur.modus ? hvacText(cur.modus) : "unbekannt" };
    if (nurAnzeige(cur)) return { cls: "off", text: "nur Anzeige" };
    return APP_MODUS[cur.app_modus] || { cls: "off", text: String(cur.app_modus) };
  }
  // Ende eines Timers (Boost, Überbrückung): heute nur Uhrzeit, sonst mit Tag
  function bisText(iso) {
    const ms = iso ? Date.parse(iso) : NaN;
    if (isNaN(ms)) return "";
    const tag = d => new Intl.DateTimeFormat("de-DE", { day: "2-digit", month: "2-digit", year: "numeric", timeZone: tz() }).format(d);
    const uhr = new Intl.DateTimeFormat("de-DE", { hour: "2-digit", minute: "2-digit", timeZone: tz() }).format(new Date(ms));
    return tag(new Date(ms)) === tag(new Date()) ? `bis ${uhr} Uhr` : `bis ${fmtDate(ms)} Uhr`;
  }
  // Status aus /api/aktuell (app_status, status_bis); ältere Server ohne app_status: aus app_modus ableiten
  function statusGen(cur) {
    if (cur.app_status) return cur.app_status;
    if (cur.app_modus !== "plan") return cur.app_modus;
    return kann("plan_anwendung") ? "plan" : "plan_inaktiv";
  }
  function statusLineGen(cur, slug) {
    if (!cur.app_modus) return cur.modus ? `Thermostat ${hvacText(cur.modus)}` : "";
    if (nurAnzeige(cur)) return `nicht steuerbar · Thermostat ${hvacText(cur.modus)}`;
    const p = [];
    const info = rooms().find(x => x.raum === slug);
    const st = statusGen(cur);
    const soll = typeof cur.soll === "number" ? cur.soll : null;
    if (st === "boost") p.push(["Boost", bisText(cur.status_bis)].filter(Boolean).join(" "));
    else if (st === "ueberbrueckung") p.push(cur.status_bis ? `Überbrückung ${bisText(cur.status_bis)}` : "Überbrückung dauerhaft");
    else if (st === "hand") p.push("Handbetrieb");
    else if (st === "aus") p.push("ausgeschaltet");
    else if (st === "sommerpause") p.push("Sommerpause, Heizplan pausiert");
    else if (st === "plan_inaktiv") {
      if (!kann("plan_anwendung")) p.push("Plananwendung abgeschaltet");
      else if (info && !info.schedule && cur.plan_soll == null) p.push("kein Heizplan");
      else p.push("Heizplan wird nicht angewendet");
    } else if (st === "plan") {
      if (cur.modus !== "off" && soll != null && cur.plan_soll != null && Math.abs(soll - cur.plan_soll) >= 0.25) p.push("weicht vom Plan ab");
      else p.push("folgt dem Heizplan");
    }
    if (cur.app_modus === "plan" && cur.plan_soll != null && st !== "sommerpause") p.push(`Plan-Soll ${num(cur.plan_soll)} °C`);
    p.push(`Thermostat ${hvacText(cur.modus)}`);
    return p.join(" · ");
  }
  const sollText = r => r.soll != null ? num(r.soll) + " °C" : r.soll_bereich ? `${num(r.soll_bereich.min)}–${num(r.soll_bereich.max)} °C` : "–";

  async function renderOverview(main) {
    main.innerHTML = `<div class="bar"><h2 class="grow">Übersicht</h2>${rangeChips()}</div><div class="loading">Auswertung läuft …</div>`;
    bindRangeChips(main);
    const gen = generisch();
    const [cur, ov] = await Promise.all([api("aktuell"), api("uebersicht?bereich=" + S.bereich)]);
    if (stale(main)) return;
    const c = cur.data, o = ov.data;
    const recos = (c.empfehlungen || []).map(e => `<li>${esc(e.text)}</li>`).join("") ||
      `<li class="muted">Derzeit liegen keine Empfehlungen vor.</li>`;
    const tiles = o.raeume.map(a => {
      const r = c.raeume[a.raum] || {};
      const m = gen ? modeInfoGen(r) : modeInfo(r);
      const f = a.feuchte || {}, s = a.schimmel || {}, co = a.co2 || {}, w = a.fenster || {};
      return `<article class="card glass">
        <div class="room-head"><h3>${esc(a.name)}</h3><span class="mode ${esc(m.cls)}">${esc(m.text)}</span></div>
        <div class="temps"><span class="ist">${num(r.ist)} °C</span><span class="soll">Soll ${gen ? sollText(r) : r.soll == null ? "–" : num(r.soll) + " °C"}</span></div>
        <div class="grund">${esc(gen ? statusLineGen(r, a.raum) : statusLine(r))}</div>
        <div class="metrics">
          <div class="metric"><div class="lbl">Heizzeit</div><div class="val">${a.heizstunden == null ? "–" : num(a.heizstunden) + " h"}</div><div class="sub">${RANGE_LABEL[S.bereich]}</div></div>
          <div class="metric ${lvl(r.feuchte, 60, 70)}"><div class="lbl">Feuchte</div><div class="val">${r.feuchte == null ? "–" : num(r.feuchte, 0) + " %"}</div><div class="sub">${f.stunden_70 ? num(f.stunden_70) + " h über 70 %" : "max " + num(f.max, 0) + " %"}</div></div>
          ${a.schimmel || r.schimmel != null ? `<div class="metric ${lvl(r.schimmel, 70, 80)}"><div class="lbl">Schimmel</div><div class="val">${num(r.schimmel, 0)} %</div><div class="sub">max ${num(s.max, 0)} %</div></div>` : ""}
          ${a.co2 ? `<div class="metric ${lvl(r.co2, 1000, 1400)}"><div class="lbl">CO2</div><div class="val">${num(r.co2, 0)}</div><div class="sub">${co.spitzen_1400 || 0}× über 1400</div></div>` : ""}
          ${a.fenster ? `<div class="metric ${r.fenster === "on" ? "warn" : ""}"><div class="lbl">Fenster</div><div class="val">${r.fenster === "on" ? "offen" : "zu"}</div><div class="sub">${num(w.offen_stunden)} h offen</div></div>` : ""}
        </div>
      </article>`;
    }).join("");
    main.innerHTML = `<div class="bar"><h2 class="grow">Übersicht</h2>${rangeChips()}</div>
      <div class="hero">
        ${kann("empfehlungen_integration") ? `<section class="card glass"><div class="kicker">Empfehlungen der Integration</div><ul class="reco">${recos}</ul></section>` : betriebHtml(c.raeume || {}, !!c.sommerpause)}
        <section class="card glass"><div class="kicker">Außen</div>
          <div class="temps"><span class="ist">${num(c.aussen.temperatur)} °C</span><span class="soll">${num(c.aussen.feuchte, 0)} % rF</span></div>
          <div class="grund">Nächster Wochenbericht: ${S.info.bericht.naechster ? fmtDate(Date.parse(S.info.bericht.naechster)) : "–"}</div>
        </section>
      </div>
      <div class="grid-tiles">${tiles || '<div class="empty">Keine Räume erkannt.</div>'}</div>`;
    bindRangeChips(main);
  }

  // Generisch: statt der Empfehlungen der Integration eine Zusammenfassung des Betriebs
  function betriebHtml(aktuell, sommerpause) {
    // nur steuerbare Räume zählen (Bereichs-Thermostate und nicht verfügbare sind nur Anzeige)
    const liste = Object.entries(aktuell).filter(([, r]) => r.app_modus && !nurAnzeige(r))
      .map(([slug, r]) => ({ modus: r.app_modus, status: statusGen(r), name: roomName(slug) }));
    const anzahl = k => liste.filter(z => z.modus === k).length;
    const namen = f => liste.filter(f).map(z => z.name);
    const hand = namen(z => z.modus === "hand");
    const timer = namen(z => z.status === "boost" || z.status === "ueberbrueckung");
    const plan = !kann("plan_anwendung") ? "Die Plananwendung ist abgeschaltet (App-Konfiguration). Klima Studio schreibt keine Plantemperaturen."
      : sommerpause ? "Sommerpause: Die Plananwendung ist pausiert, bis die Heizperiode wieder beginnt. Räume im Modus Hand bleiben unberührt."
      : "Klima Studio wendet die Heizpläne der Räume im Modus Plan an.";
    const extra = [hand.length ? `im Modus Hand: ${hand.join(", ")}` : "", timer.length ? `Boost/Überbrückung: ${timer.join(", ")}` : ""].filter(Boolean);
    return `<section class="card glass"><div class="kicker">Betrieb</div>
      <p class="m0">${esc(plan)}</p>
      ${liste.length ? `<p class="small muted">Plan ${anzahl("plan")} · Hand ${anzahl("hand")} · Aus ${anzahl("aus")}${extra.map(t => " · " + esc(t)).join("")}</p>` : ""}
    </section>`;
  }

  // ------------------------------------------------------------------ Heizplan
  function isDirty() {
    return !!(S.plan && JSON.stringify(norm(S.plan.tage)) !== JSON.stringify(norm(S.plan.orig)));
  }
  function norm(tage) {
    const o = {};
    for (const d of DAYS) o[d] = (tage[d] || []).map(b => [b.start, b.end, b.temp, JSON.stringify(b.extra || {})]).sort((a, b) => a[0] - b[0]);
    return o;
  }
  function tempColor(t) {
    if (t == null) return "linear-gradient(180deg,#9a96b0,#827E96)";
    const k = Math.min(1, Math.max(0, (t - 15) / 8)); // 15 °C lavendel … 23 °C orange
    const a = [184, 133, 214], b = [232, 135, 90];
    const c = a.map((v, i) => Math.round(v + (b[i] - v) * k));
    const c2 = c.map(v => Math.min(255, v + 22));
    return `linear-gradient(180deg,rgb(${c2}),rgb(${c}))`;
  }

  // false: der Lauf ist veraltet (Container abgehängt), S.plan bleibt unverändert.
  async function loadPlan(force, main) {
    if (!force && S.plan && S.plan.raum === S.raum) return true;
    const res = await api("heizplan/" + encodeURIComponent(S.raum));
    if (stale(main)) return false;
    const d = res.data;
    S.plan = { raum: S.raum, id: d.id, name: d.name, entity: d.entity_id, revision: d.revision, tage: clone(d.tage), orig: clone(d.tage) };
    S.sel = null;
    return true;
  }

  async function renderPlan(main) {
    const withPlan = r => !!r.schedule;
    main.innerHTML = `<div class="bar">${roomChips(withPlan)}</div><div class="loading">Heizplan wird geladen …</div>`;
    bindRoomChips(main, () => render());
    if (!S.raum) { main.innerHTML = '<div class="empty">Kein Raum mit Heizplan gefunden.</div>'; return; }
    if (!(await loadPlan(false, main))) return;
    drawPlan(main);
  }

  function drawPlan(main) {
    const p = S.plan;
    const dirty = isDirty();
    main.innerHTML = `
      <div class="bar">${roomChips(r => !!r.schedule)}<span class="grow"></span>
        <span class="small muted">${dirty ? '<span class="dirty-dot"></span>Ungespeichert' : esc(p.entity)}</span>
        <button class="btn ghost" id="btn-reload">Neu laden</button>
        <button class="btn" id="btn-discard" ${dirty ? "" : "disabled"}>Verwerfen</button>
        <button class="btn primary" id="btn-save" ${dirty ? "" : "disabled"}>Speichern …</button>
      </div>
      <div class="editor">
        <section class="week glass" id="week">
          <div class="week-head"><div></div>${DAYS.map(d => `<div class="dayhead ${S.sel && S.sel.day === d ? "sel" : ""}" data-day="${d}"><span class="long">${DAY_LONG[d]}</span><span class="short">${DAY_SHORT[d]}</span></div>`).join("")}</div>
          <div class="week-body">
            <div class="gutter">${Array.from({ length: 9 }, (_, i) => `<span style="top:${(i * 3 / 24) * 100}%">${String(i * 3).padStart(2, "0")}</span>`).join("")}</div>
            ${DAYS.map(d => `<div class="daycol" data-day="${d}"></div>`).join("")}
          </div>
        </section>
        <aside class="side glass ${S.sel ? "" : "empty"}" id="side"></aside>
      </div>`;
    bindRoomChips(main, () => render());
    $("#btn-reload", main).addEventListener("click", async () => {
      if (isDirty() && !confirm("Änderungen verwerfen und neu laden?")) return;
      if (!(await loadPlan(true, main))) return;
      drawPlan(main); toast("Heizplan neu geladen.");
    });
    $("#btn-discard", main).addEventListener("click", () => { p.tage = clone(p.orig); S.sel = null; drawPlan(main); });
    $("#btn-save", main).addEventListener("click", () => save(main));
    main.querySelectorAll(".dayhead").forEach(h => h.addEventListener("click", () => dayMenu(h.dataset.day, main)));
    for (const d of DAYS) drawDay(d, main);
    drawNow(main);
    drawSide(main);
    for (const col of main.querySelectorAll(".daycol")) bindColumn(col, main);
  }

  function drawNow(main) {
    const now = new Date();
    const parts = new Intl.DateTimeFormat("en-GB", { weekday: "long", hour: "2-digit", minute: "2-digit", hour12: false, timeZone: tz() }).formatToParts(now);
    const wd = parts.find(x => x.type === "weekday").value.toLowerCase();
    const mins = (parseInt(parts.find(x => x.type === "hour").value, 10) % 24) * 60 + parseInt(parts.find(x => x.type === "minute").value, 10);
    const col = main.querySelector(`.daycol[data-day="${wd}"]`);
    if (!col) return;
    const line = document.createElement("div");
    line.className = "nowline"; line.style.top = (mins / DAY_MIN * 100) + "%";
    col.appendChild(line);
  }

  function drawDay(day, main) {
    const col = main.querySelector(`.daycol[data-day="${day}"]`);
    col.querySelectorAll(".block").forEach(b => b.remove());
    const blocks = S.plan.tage[day] || [];
    blocks.sort((a, b) => a.start - b.start);
    blocks.forEach((b, idx) => col.appendChild(blockEl(day, idx, b)));
  }

  function blockEl(day, idx, b) {
    const div = document.createElement("div");
    div.className = "block" + (S.sel && !S.sel.neu && S.sel.day === day && S.sel.idx === idx ? " sel" : "");
    div.dataset.day = day; div.dataset.idx = idx;
    placeBlock(div, b);
    div.style.background = tempColor(b.temp);
    div.innerHTML = `<div class="handle h-top" data-h="top"></div><span class="t">${b.temp == null ? "–" : num(b.temp) + "°"}</span><span class="z">${hm(b.start)}–${hm(b.end)}</span><div class="handle h-bottom" data-h="bottom"></div>`;
    div.title = `${DAY_LONG[day]} ${hm(b.start)}–${hm(b.end)}, ${b.temp == null ? "ohne Temperatur" : num(b.temp) + " °C"}`;
    div.addEventListener("pointerdown", ev => startDrag(ev, div, day, idx));
    return div;
  }
  function placeBlock(div, b) {
    div.style.top = (b.start / DAY_MIN * 100) + "%";
    div.style.height = `calc(${((b.end - b.start) / DAY_MIN * 100)}% - 1px)`;
  }

  function colFromX(root, x) {
    for (const col of root.querySelectorAll(".daycol")) {
      const r = col.getBoundingClientRect();
      if (x >= r.left - 2 && x <= r.right + 2) return col;
    }
    return null;
  }
  function minuteAt(col, y) {
    const r = col.getBoundingClientRect();
    return Math.min(DAY_MIN, Math.max(0, ((y - r.top) / r.height) * DAY_MIN));
  }
  const snap = m => Math.round(m / SNAP) * SNAP;
  function fits(day, start, end, ignoreIdx) {
    if (start < 0 || end > DAY_MIN || start >= end) return false;
    return !(S.plan.tage[day] || []).some((b, i) => i !== ignoreIdx && start < b.end && b.start < end);
  }

  // Ziehen: verschieben (auch tageübergreifend) oder Anfang/Ende ändern
  function startDrag(ev, div, day, idx) {
    if (ev.button > 0) return;
    ev.preventDefault(); ev.stopPropagation();
    const mode = ev.target.dataset.h || "move";
    const b = S.plan.tage[day][idx];
    const col0 = div.parentElement;
    const m0 = minuteAt(col0, ev.clientY);
    const x0 = ev.clientX, y0 = ev.clientY;
    const orig = { start: b.start, end: b.end };
    let moved = false, target = { day, start: b.start, end: b.end }, ok = true;
    div.setPointerCapture(ev.pointerId);

    function onMove(e) {
      if (!moved && Math.abs(e.clientX - x0) + Math.abs(e.clientY - y0) < 5) return;
      moved = true;
      const col = mode === "move" ? (colFromX(col0.parentElement, e.clientX) || col0) : col0;
      const dm = snap(minuteAt(col0, e.clientY) - m0);
      if (mode === "move") {
        const len = orig.end - orig.start;
        let s = Math.min(Math.max(0, orig.start + dm), DAY_MIN - len);
        target = { day: col.dataset.day, start: s, end: s + len };
      } else if (mode === "top") {
        const s = Math.min(Math.max(0, snap(orig.start + dm)), orig.end - SNAP);
        target = { day, start: s, end: orig.end };
      } else {
        const e2 = Math.max(Math.min(DAY_MIN, snap(orig.end + dm)), orig.start + SNAP);
        target = { day, start: orig.start, end: e2 };
      }
      ok = fits(target.day, target.start, target.end, target.day === day ? idx : -1);
      if (col !== div.parentElement) col.appendChild(div);
      placeBlock(div, target);
      div.classList.toggle("invalid", !ok);
      div.querySelector(".z").textContent = `${hm(target.start)}–${hm(target.end)}`;
    }
    function onUp() {
      div.removeEventListener("pointermove", onMove);
      div.removeEventListener("pointerup", onUp);
      div.removeEventListener("pointercancel", onUp);
      if (!moved) { S.sel = { day, idx }; redraw(); return; }
      if (!ok) { toast("Blöcke dürfen sich nicht überlappen.", true); redraw(); return; }
      const blk = S.plan.tage[day].splice(idx, 1)[0];
      if (blk.start !== target.start) delete blk.start_s;
      if (blk.end !== target.end) delete blk.end_s;
      blk.start = target.start; blk.end = target.end;
      (S.plan.tage[target.day] = S.plan.tage[target.day] || []).push(blk);
      S.plan.tage[target.day].sort((a, c) => a.start - c.start);
      S.sel = { day: target.day, idx: S.plan.tage[target.day].indexOf(blk) };
      redraw();
    }
    div.addEventListener("pointermove", onMove);
    div.addEventListener("pointerup", onUp);
    div.addEventListener("pointercancel", onUp);
  }

  // Leere Fläche: Maus zieht einen neuen Block auf, Touch-Tipp öffnet "Neuer Block"
  function bindColumn(col, main) {
    col.addEventListener("pointerdown", ev => {
      if (ev.target !== col || ev.button > 0) return;
      const day = col.dataset.day;
      const startMin = Math.floor(minuteAt(col, ev.clientY) / 60) * 60;
      if (ev.pointerType !== "mouse") {
        const y0 = ev.clientY, t0 = Date.now();
        const up = e => {
          col.removeEventListener("pointerup", up);
          if (Math.abs(e.clientY - y0) < 8 && Date.now() - t0 < 600) newBlockAt(day, startMin);
        };
        col.addEventListener("pointerup", up);
        return;
      }
      ev.preventDefault();
      const s0 = snap(minuteAt(col, ev.clientY));
      const ghost = document.createElement("div");
      ghost.className = "block ghost"; ghost.style.background = tempColor(defaultTemp());
      ghost.innerHTML = '<span class="t"></span><span class="z"></span>';
      col.appendChild(ghost);
      let range = [s0, s0 + 60], dragged = false;
      col.setPointerCapture(ev.pointerId);
      const move = e => {
        const m = snap(minuteAt(col, e.clientY));
        if (Math.abs(m - s0) >= SNAP) dragged = true;
        range = m >= s0 ? [s0, Math.max(m, s0 + SNAP)] : [m, s0];
        placeBlock(ghost, { start: range[0], end: range[1] });
        ghost.querySelector(".z").textContent = `${hm(range[0])}–${hm(range[1])}`;
        ghost.classList.toggle("invalid", !fits(day, range[0], range[1], -1));
      };
      const up = () => {
        col.removeEventListener("pointermove", move); col.removeEventListener("pointerup", up);
        ghost.remove();
        if (!dragged) { newBlockAt(day, startMin); return; }
        addBlock(day, range[0], range[1], defaultTemp());
      };
      col.addEventListener("pointermove", move); col.addEventListener("pointerup", up);
    });
  }

  function defaultTemp() {
    const all = DAYS.flatMap(d => S.plan.tage[d] || []).map(b => b.temp).filter(t => t != null);
    return all.length ? all[all.length - 1] : 20;
  }
  function freeSlot(day, start, len) {
    for (let s = start; s + SNAP <= DAY_MIN; s += SNAP) {
      let e = Math.min(DAY_MIN, s + len);
      while (e > s + SNAP && !fits(day, s, e, -1)) e -= SNAP;
      if (fits(day, s, e, -1)) return [s, e];
    }
    return null;
  }
  function newBlockAt(day, start) {
    const slot = freeSlot(day, start, 120);
    if (!slot) { toast("An diesem Tag ist kein freier Zeitraum mehr.", true); return; }
    S.sel = { neu: true, day, start: slot[0], end: slot[1], temp: defaultTemp() };
    redraw();
  }
  function addBlock(day, start, end, temp) {
    if (!fits(day, start, end, -1)) { toast("Der Zeitraum überlappt einen bestehenden Block.", true); redraw(); return; }
    const blk = { start, end, temp, extra: {} };
    (S.plan.tage[day] = S.plan.tage[day] || []).push(blk);
    S.plan.tage[day].sort((a, b) => a.start - b.start);
    S.sel = { day, idx: S.plan.tage[day].indexOf(blk) };
    redraw();
  }
  function redraw() { drawPlan(S.view); }

  function drawSide(main) {
    const side = $("#side", main);
    const legend = `<div class="legend"><span>15 °C</span><span class="scale" style="background:linear-gradient(90deg,#B885D6,#E8875A)"></span><span>23 °C</span></div>`;
    const help = `<div class="help"><b>Maus:</b> auf freier Fläche ziehen legt einen Block an. <b>Touch:</b> freie Fläche antippen. Blöcke lassen sich verschieben, an den Kanten verlängern und antippen zum Bearbeiten. <b>Tagesname</b> öffnet Kopieren und Leeren.</div>`;
    if (!S.sel) {
      side.innerHTML = `<div class="kicker">${esc(S.plan.name || "")}</div><h3>Block wählen</h3><p class="small muted">${planHinweis()}</p>${legend}${help}`;
      return;
    }
    const isNew = !!S.sel.neu;
    const b = isNew ? S.sel : S.plan.tage[S.sel.day][S.sel.idx];
    if (!b) { S.sel = null; drawSide(main); return; }
    side.innerHTML = `<div class="side-body">
      <div class="kicker">${DAY_LONG[S.sel.day]}</div>
      <h3>${isNew ? "Neuer Block" : "Block bearbeiten"}</h3>
      <div class="row"><label for="f-from">Von</label><input type="text" id="f-from" class="hm" inputmode="numeric" maxlength="5" placeholder="hh:mm" value="${hm(b.start)}"></div>
      <div class="row"><label for="f-to">Bis</label><input type="text" id="f-to" class="hm" inputmode="numeric" maxlength="5" placeholder="hh:mm" value="${hm(b.end)}"></div>
      <p class="small muted">24-Stunden-Format, Ende 24:00 für Mitternacht.</p>
      <div class="row"><label>Temperatur</label>
        <div class="stepper"><button type="button" id="t-minus" aria-label="0,5 Grad weniger">−</button><span class="v" id="t-val">${b.temp == null ? "–" : num(b.temp) + " °C"}</span><button type="button" id="t-plus" aria-label="0,5 Grad mehr">+</button></div></div>
      <div id="f-err"></div>
      <div class="actions">
        ${isNew ? `<button class="btn ghost" id="b-cancel">Abbrechen</button><button class="btn primary" id="b-add">Anlegen</button>`
                : `<button class="btn danger" id="b-del">Löschen</button><button class="btn ghost" id="b-close">Fertig</button>`}
      </div></div>${legend}`;
    const from = $("#f-from", side), to = $("#f-to", side);
    const apply = () => {
      const s = parseHm(from.value), e = parseHm(to.value);
      const errBox = $("#f-err", side);
      if (isNaN(s) || isNaN(e)) { errBox.innerHTML = '<div class="errbox small">Uhrzeit bitte als hh:mm angeben (00:00 bis 24:00).</div>'; return false; }
      if (s >= e) { errBox.innerHTML = '<div class="errbox small">Beginn muss vor dem Ende liegen.</div>'; return false; }
      if (!fits(S.sel.day, s, e, isNew ? -1 : S.sel.idx)) { errBox.innerHTML = '<div class="errbox small">Überlappt einen anderen Block.</div>'; return false; }
      errBox.innerHTML = "";
      if (b.start !== s) delete b.start_s;
      if (b.end !== e) delete b.end_s;
      b.start = s; b.end = e;
      from.value = hm(s); to.value = hm(e);
      if (!isNew) { drawDay(S.sel.day, main); refreshBar(main); }
      return true;
    };
    from.addEventListener("change", apply); to.addEventListener("change", apply);
    const step = d => {
      const t = Math.min(T_MAX, Math.max(T_MIN, Math.round(((b.temp ?? 20) + d) * 2) / 2));
      b.temp = t; $("#t-val", side).textContent = num(t) + " °C";
      if (!isNew) { drawDay(S.sel.day, main); refreshBar(main); }
    };
    $("#t-minus", side).addEventListener("click", () => step(-0.5));
    $("#t-plus", side).addEventListener("click", () => step(0.5));
    if (isNew) {
      $("#b-cancel", side).addEventListener("click", () => { S.sel = null; redraw(); });
      $("#b-add", side).addEventListener("click", () => { if (apply()) addBlock(S.sel.day, b.start, b.end, b.temp ?? 20); });
    } else {
      $("#b-del", side).addEventListener("click", () => { S.plan.tage[S.sel.day].splice(S.sel.idx, 1); S.sel = null; redraw(); });
      $("#b-close", side).addEventListener("click", () => { S.sel = null; redraw(); });
    }
  }

  // Generisch: Absenktemperatur des Raums (Override absenk) bzw. der App (/api/info absenktemperatur)
  function absenkText(slug) {
    const r = rooms().find(x => x.raum === slug);
    const t = r && typeof r.absenk === "number" ? r.absenk : S.info && typeof S.info.absenktemperatur === "number" ? S.info.absenktemperatur : null;
    return t == null ? "die Absenktemperatur" : `die Absenktemperatur von ${esc(num(t))} °C`;
  }

  function planHinweis() {
    if (!generisch()) return "Außerhalb der Blöcke gilt die Grundtemperatur der Integration.";
    if (!kann("plan_anwendung")) return "Die Plananwendung ist abgeschaltet (App-Konfiguration). Der Plan wird gespeichert, aber nicht an die Thermostate übertragen.";
    return `Die App wendet diesen Plan an, solange der Raum im Modus Plan ist. Außerhalb der Blöcke gilt ${absenkText(S.plan && S.plan.raum)}.`;
  }

  function refreshBar(main) {
    const dirty = isDirty();
    $("#btn-save", main).disabled = !dirty; $("#btn-discard", main).disabled = !dirty;
  }

  async function dayMenu(day, main) {
    const html = `<p class="small muted">Blöcke von ${DAY_LONG[day]} übernehmen nach:</p>
      <div class="copy-days">${DAYS.filter(d => d !== day).map(d => `<label class="check"><input type="checkbox" value="${d}"> ${DAY_SHORT[d]}</label>`).join("")}</div>
      <div class="chips"><button class="chip" data-set="wd">Werktage</button><button class="chip" data-set="we">Wochenende</button><button class="chip" data-set="all">Alle</button><button class="chip" data-set="none">Keine</button></div>`;
    const p = modal(`${DAY_LONG[day]}`, html, [
      { label: "Tag leeren", value: "clear", cls: "danger" },
      { label: "Abbrechen", value: null, cls: "ghost" },
      { label: "Kopieren", value: "copy", cls: "primary" },
    ]);
    document.querySelectorAll("#modal-body [data-set]").forEach(b => b.addEventListener("click", () => {
      const set = b.dataset.set === "wd" ? WEEKDAYS : b.dataset.set === "we" ? WEEKEND : b.dataset.set === "all" ? DAYS : [];
      document.querySelectorAll("#modal-body input[type=checkbox]").forEach(i => { i.checked = set.includes(i.value); });
    }));
    const getTargets = () => [...document.querySelectorAll("#modal-body input:checked")].map(i => i.value);
    let targets = [];
    document.querySelector("#modal-actions").addEventListener("click", () => { targets = getTargets(); }, { capture: true, once: true });
    const res = await p;
    if (res === "clear") { S.plan.tage[day] = []; S.sel = null; redraw(); return; }
    if (res !== "copy") return;
    if (!targets.length) { toast("Keine Zieltage gewählt.", true); return; }
    for (const t of targets) S.plan.tage[t] = clone(S.plan.tage[day] || []);
    S.sel = null; redraw();
    toast(`${DAY_LONG[day]} auf ${targets.length} Tag(e) übertragen.`);
  }

  function diffHtml(diff) {
    if (!diff.length) return '<p class="muted">Keine inhaltlichen Unterschiede.</p>';
    return diff.map(d => `<div class="diff-day"><h3>${esc(d.label)}</h3><ul>${d.changes.map(c =>
      `<li class="op-${c.op}">${esc(c.text)}</li>`).join("")}</ul></div>`).join("");
  }

  function fullDays(tage) {
    const out = {};
    for (const d of DAYS) out[d] = (tage[d] || []).slice().sort((a, b) => a.start - b.start);
    return out;
  }

  async function save(main) {
    const p = S.plan;
    const tage = fullDays(p.tage);
    let leer = false;
    if (DAYS.every(d => !tage[d].length)) {
      leer = await modal("Heizplan vollständig leeren?", `<div class="warnbox">An keinem Tag ist ein Block eingetragen. ${generisch() ? `Nach dem Speichern wendet die App für ${esc(roomName(p.raum))} keinen Plan an. Der zuletzt gesetzte Sollwert bleibt bestehen.` : `Nach dem Speichern gilt in ${esc(roomName(p.raum))} dauerhaft die Grundtemperatur der Integration.`}</div>`,
        [{ label: "Abbrechen", value: false, cls: "ghost" }, { label: "Leeren bestätigen", value: true, cls: "danger" }]);
      if (!leer) return;
    }
    const extra = leer ? { leer_bestaetigt: true } : {};
    let prev;
    try {
      prev = await api(`heizplan/${encodeURIComponent(p.raum)}/vorschau`, { method: "POST", body: Object.assign({ tage, revision: p.revision }, extra) });
    } catch (e) {
      const det = e.data && e.data.details ? `<ul>${e.data.details.map(x => `<li>${esc(x)}</li>`).join("")}</ul>` : "";
      await modal("Heizplan ungültig", `<div class="errbox">${esc(e.message)}${det}</div>`, [{ label: "Schließen", value: null, cls: "primary" }]);
      return;
    }
    const d = prev.data;
    const warn = d.konflikt ? `<div class="warnbox">Der Heizplan wurde seit dem Laden außerhalb von Klima Studio geändert. Die Übersicht zeigt die Unterschiede zum aktuellen Stand in Home Assistant.</div>` : "";
    const ok = await modal(`Änderungen an „${esc(p.name)}“`, warn + diffHtml(d.diff) +
      `<p class="small muted">Name und Symbol des Helfers bleiben unverändert.</p>`,
      [{ label: "Abbrechen", value: false, cls: "ghost" }, { label: d.konflikt ? "Trotzdem speichern" : "Speichern", value: true, cls: "primary" }]);
    if (!ok) return;
    const res = await api(`heizplan/${encodeURIComponent(p.raum)}`, {
      method: "POST", body: Object.assign({ tage, revision: d.konflikt ? d.revision_aktuell : p.revision }, extra), accept: [409],
    });
    if (res.status === 409) {
      const again = await modal("Fremdänderung erkannt", `<div class="warnbox">${esc(res.data.fehler)}</div>${diffHtml(res.data.diff)}`,
        [{ label: "Neu laden", value: "reload", cls: "ghost" }, { label: "Überschreiben", value: "force", cls: "danger" }]);
      if (again === "reload") { if (await loadPlan(true, main)) drawPlan(main); return; }
      const r2 = await api(`heizplan/${encodeURIComponent(p.raum)}`, { method: "POST", body: Object.assign({ tage, revision: res.data.revision_aktuell }, extra) });
      afterSave(r2.data, main); return;
    }
    afterSave(res.data, main);
  }
  function afterSave(data, main) {
    S.plan.tage = clone(data.tage); S.plan.orig = clone(data.tage); S.plan.revision = data.revision; S.sel = null;
    drawPlan(main);
    toast("Heizplan gespeichert.");
  }

  // ------------------------------------------------------------------ Auswertung
  async function renderAnalysis(main) {
    main.innerHTML = `<div class="bar">${roomChips()}<span class="grow"></span>${rangeChips()}</div><div class="loading">Auswertung läuft …</div>`;
    bindRoomChips(main, () => render()); bindRangeChips(main);
    if (!S.raum) { main.innerHTML = '<div class="empty">Keine Räume erkannt.</div>'; return; }
    const { data: a } = await api(`auswertung/${encodeURIComponent(S.raum)}?bereich=${S.bereich}`);
    if (stale(main) || a.raum !== S.raum) return;
    const start = a.zeitraum.start, end = a.zeitraum.ende;
    const f = a.feuchte, s = a.schimmel, co = a.co2, w = a.fenster, l = a.lueftung;
    const stats = [
      ["Heizstunden", a.heizstunden == null ? "–" : num(a.heizstunden) + " h"],
      ["Fenster offen", w ? num(w.offen_stunden) + " h" : "–", w ? `${w.anzahl} Öffnungen` : "kein Fenstersensor"],
      ["Längste Öffnung", w && w.laengste ? num(w.laengste.dauer_min, 0) + " min" : "–", w && w.laengste ? fmtDate(w.laengste.start) : ""],
      ["Feuchte > 70 %", f ? num(f.stunden_70) + " h" : "–", f ? `max ${num(f.max, 0)} %` : ""],
      ["CO2-Spitzen", co ? `${co.spitzen_1000} / ${co.spitzen_1400}` : "–", co ? "über 1000 / 1400 ppm" : "kein CO2-Sensor"],
      ["Lüftungserfolg", l ? l.bewertung : "–", l ? `Ø −${num(l.feuchte_abfall_mittel)} % · −${num(l.co2_abfall_mittel, 0)} ppm` : ""],
    ];
    const winBand = w ? [{ name: "Fenster offen", color: C.sky, items: w.intervalle, opacity: .16 }] : [];
    main.innerHTML = `<div class="bar">${roomChips()}<span class="grow"></span>${rangeChips()}</div>
      ${a.hinweise && a.hinweise.length ? `<ul class="notes">${a.hinweise.map(h => `<li>${esc(h)}</li>`).join("")}</ul>` : ""}
      <div class="stats-row">${stats.map(([k, v, sub]) => `<div class="stat glass"><div class="lbl">${esc(k)}</div><div class="val">${esc(v)}</div><div class="small muted">${esc(sub || "")}</div></div>`).join("")}</div>
      <div class="charts">
        <section class="chart-card glass wide"><h3>Soll und Ist</h3>${KSCharts.legend([{ name: "Ist", color: C.cloud }, { name: "Soll", color: C.auto }, { name: "heizt", color: C.heizt, band: true }, ...(w ? [{ name: "Fenster offen", color: C.sky, band: true }] : [])])}<div id="c-temp" class="chart"></div></section>
        <section class="chart-card glass"><h3>Luftfeuchte</h3>${KSCharts.legend([{ name: "Feuchte", color: C.lavender }, { name: "70 %", color: C.warn }, { name: "80 %", color: C.bad }])}<div id="c-hum" class="chart"></div></section>
        ${!kann("schimmel") && !(s && s.verlauf) ? "" : `<section class="chart-card glass"><h3>Schimmelrisiko</h3>${KSCharts.legend([{ name: "Risiko", color: C.auto }, { name: "70 %", color: C.warn }, { name: "80 %", color: C.bad }])}<div id="c-mold" class="chart"></div></section>`}
        <section class="chart-card glass wide"><h3>CO2</h3>${KSCharts.legend([{ name: "CO2", color: C.lavender }, { name: "1000 ppm", color: C.warn }, { name: "1400 ppm", color: C.bad }])}<div id="c-co2" class="chart"></div></section>
        <section class="chart-card glass wide"><h3>Lüftungen</h3><div id="t-vent"></div></section>
      </div>`;
    bindRoomChips(main, () => render()); bindRangeChips(main);
    const base = { start, end, tz: tz() };
    const emptyNote = (id, text) => { const n = $(id, main); n.innerHTML = `<div class="empty">${text}</div>`; };
    const draw = () => {
      if (a.soll_ist) KSCharts.render($("#c-temp", main), Object.assign({}, base, { unit: "°C", label: "Soll und Ist", series: [
        { name: "Soll", color: C.auto, data: a.soll_ist.soll, step: true, width: 2, dash: "6 3" },
        { name: "Ist", color: C.cloud, data: a.soll_ist.ist, width: 2 }],
        bands: [{ name: "heizt", color: C.heizt, items: a.soll_ist.heizt, opacity: .2 }, ...winBand] }));
      else emptyNote("#c-temp", "Keine Thermostatdaten.");
      if (f && f.verlauf) KSCharts.render($("#c-hum", main), Object.assign({}, base, { unit: "%", yMin: 30, label: "Luftfeuchte", series: [{ name: "Feuchte", color: C.lavender, data: f.verlauf }],
        thresholds: [{ y: 70, color: C.warn, label: "70 %" }, { y: 80, color: C.bad, label: "80 %" }], bands: winBand }));
      else emptyNote("#c-hum", "Kein Feuchtesensor zugeordnet.");
      if (!$("#c-mold", main)) { /* generisch ohne Schimmeldaten: keine Karte */ }
      else if (s && s.verlauf) KSCharts.render($("#c-mold", main), Object.assign({}, base, { unit: "%", label: "Schimmelrisiko", series: [{ name: "Risiko", color: C.auto, data: s.verlauf }],
        thresholds: [{ y: 70, color: C.warn, label: "70 %" }, { y: 80, color: C.bad, label: "80 %" }] }));
      else emptyNote("#c-mold", "Kein Schimmelrisiko-Sensor.");
      if (co && co.verlauf) KSCharts.render($("#c-co2", main), Object.assign({}, base, { unit: "ppm", digits: 0, label: "CO2", yMin: 400, series: [{ name: "CO2", color: C.lavender, data: co.verlauf }],
        thresholds: [{ y: 1000, color: C.warn, label: "1000 ppm" }, { y: 1400, color: C.bad, label: "1400 ppm" }], bands: winBand,
        markers: (co.episoden || []).map(e => ({ t: e.start, v: e.max, color: e.max > 1400 ? C.bad : C.warn })) }));
      else emptyNote("#c-co2", "Kein CO2-Sensor zugeordnet.");
    };
    draw();
    clearTimeout(renderAnalysis._rz);
    window.onresize = () => { clearTimeout(renderAnalysis._rz); renderAnalysis._rz = setTimeout(() => { if (S.tab === "auswertung" && !stale(main)) draw(); }, 200); };
    const ev = (l && l.ereignisse) || [];
    $("#t-vent", main).innerHTML = ev.length ? `<table class="plain"><thead><tr><th>Beginn</th><th>Dauer</th><th>Feuchte</th><th>CO2</th></tr></thead><tbody>${
      ev.slice().reverse().slice(0, 15).map(e => `<tr><td>${fmtDate(e.start)}</td><td>${num(e.dauer_min, 0)} min</td>
        <td>${e.feuchte_abfall == null ? "–" : `${num(e.feuchte_start, 0)} % → −${num(e.feuchte_abfall)}`}</td>
        <td>${e.co2_abfall == null ? "–" : `${num(e.co2_start, 0)} ppm → −${num(e.co2_abfall, 0)}`}</td></tr>`).join("")}</tbody></table>
      <p class="small muted">Abfall = Wert beim Öffnen minus Tiefstwert in den folgenden 30 Minuten.</p>`
      : `<div class="empty">${w ? "Im Zeitraum keine Fensteröffnungen." : "Für diesen Raum ist kein Fenstersensor zugeordnet."}</div>`;
  }

  // ------------------------------------------------------------------ Berichte
  async function renderReports(main, selectId) {
    const { data: list } = await api("berichte");
    if (stale(main)) return;
    const b = S.info.bericht;
    const cur = selectId || (list[0] && list[0].id);
    main.innerHTML = `<div class="bar"><h2 class="grow">Wochenberichte</h2>
        <label class="check"><input type="checkbox" id="r-push" ${b.notify.length ? "" : "disabled"}> zusätzlich Push senden</label>
        <button class="btn primary" id="r-new">Bericht jetzt erstellen</button></div>
      <p class="small muted">Automatisch jeden ${esc(b.tag.charAt(0).toUpperCase() + b.tag.slice(1))} um ${esc(b.uhrzeit)} Uhr. Push: ${b.notify.length ? esc(b.notify.join(", ")) : "nicht eingerichtet"}. Archiviert werden die letzten 12 Berichte.</p>
      <div class="reports">
        <nav class="rep-list glass">${list.length ? list.map(r => `<button class="rep-item ${r.id === cur ? "active" : ""}" data-id="${esc(r.id)}">
          <div class="d">${fmtDate(Date.parse(r.erstellt))}</div>
          <div class="small muted">${r.anlass === "manuell" ? "manuell" : "planmäßig"}${r.auffaellig ? ` · ${esc(r.auffaellig)} Auffälligkeit(en)` : ""}${(r.push || []).length ? " · Push" : ""}</div></button>`).join("")
          : '<div class="empty small">Noch kein Bericht vorhanden.</div>'}</nav>
        <article class="rep-view glass" id="rep-view"><div class="muted">Bericht wählen.</div></article>
      </div>`;
    $("#r-new", main).addEventListener("click", async () => {
      const btn = $("#r-new", main); btn.disabled = true; btn.textContent = "Wird erstellt …";
      try {
        const push = $("#r-push", main).checked;
        const { data } = await api("berichte", { method: "POST", body: { push } });
        toast(push ? "Bericht erstellt und versendet." : "Bericht erstellt.");
        await renderReports(main, data.id);
      } catch (e) { toast(e.message, true); btn.disabled = false; btn.textContent = "Bericht jetzt erstellen"; }
    });
    main.querySelectorAll(".rep-item").forEach(x => x.addEventListener("click", () => {
      main.querySelectorAll(".rep-item").forEach(y => y.classList.toggle("active", y === x));
      showReport(x.dataset.id, main);
    }));
    if (cur) await showReport(cur, main);
  }

  async function showReport(id, main) {
    const { data: r } = await api("berichte/" + encodeURIComponent(id));
    if (stale(main)) return;
    const view = $("#rep-view", main);
    const mold = kann("schimmel") || r.raeume.some(x => x.schimmel_max != null);
    view.innerHTML = `<div class="kicker">Zeitraum ${esc(new Date(r.zeitraum.start).toLocaleDateString("de-DE", { timeZone: tz() }))} – ${esc(new Date(r.zeitraum.ende).toLocaleDateString("de-DE", { timeZone: tz() }))}</div>
      <p class="intro">${esc(r.einleitung)}</p>
      <section><h3>Heizstunden</h3><table class="plain"><thead><tr><th>Raum</th><th>Heizzeit</th><th>Feuchte max</th>${mold ? "<th>Schimmel max</th>" : ""}<th>CO2 max</th><th>Fenster offen</th></tr></thead><tbody>
        ${r.raeume.map(x => `<tr><td>${esc(x.name)}</td><td>${x.heizstunden == null ? "–" : num(x.heizstunden) + " h"}</td><td>${num(x.feuchte_max, 0)} %</td>${mold ? `<td>${x.schimmel_max == null ? "–" : num(x.schimmel_max, 0) + " %"}</td>` : ""}<td>${x.co2_max == null ? "–" : num(x.co2_max, 0) + " ppm"}</td><td>${x.fenster_offen_stunden == null ? "–" : num(x.fenster_offen_stunden) + " h"}</td></tr>`).join("")}
      </tbody></table></section>
      <section><h3>Auffällige Räume</h3>${r.auffaellig.length ? `<ul>${r.auffaellig.map(x => `<li>${esc(x.text)}</li>`).join("")}</ul>` : '<p class="muted">Keine. ${mold ? "Feuchte, Schimmelrisiko und CO2" : "Feuchte und CO2"} blieben im Rahmen.</p>'}</section>
      <section><h3>Längste offene Fenster</h3>${r.fenster_laengste.length ? `<ul>${r.fenster_laengste.map(x => `<li>${esc(x.name)}: ${num(x.dauer_min, 0)} min ab ${fmtDate(x.start)}</li>`).join("")}</ul>` : '<p class="muted">Keine Öffnungen erfasst.</p>'}</section>
      <section><h3>Empfehlungen</h3><ul>${r.empfehlungen.map(x => `<li>${esc(x)}</li>`).join("")}</ul></section>
      <p class="muted">${esc(r.schluss)}</p>
      ${(r.push || []).length ? `<p class="small muted">Push: ${r.push.map(p => `${esc(p.dienst)} ${p.ok ? "gesendet" : "fehlgeschlagen"}`).join(", ")}</p>` : ""}`;
  }

  // ------------------------------------------------------------------ Erweiterungen (steuerung.js, coach.js)
  // Zusätzliche Reiter registrieren sich in window.KSTabs = {name: {render(main, parts), leave()}}.
  const EXT = window.KSTabs = window.KSTabs || {};
  window.KSApp = { S, $, esc, num, fmtDate, tz, api, toast, modal, rooms, roomName, generisch, kann, hvacText, prefill: null };

  // ------------------------------------------------------------------ Start
  // Hinweis zur Betriebsart (z. B. PM Klima fehlt); ausblendbar bis zum Neuladen, nie automatisch umschalten
  function hinweisBetriebsart() {
    const box = $("#hinweis-betriebsart");
    const text = S.info && S.info.hinweis_betriebsart;
    if (!box || !text) return;
    box.innerHTML = `<div class="infobox"><p>${esc(text)}</p><button type="button" class="linkbtn" id="hinweis-zu">Ausblenden</button></div>`;
    box.hidden = false;
    $("#hinweis-zu", box).addEventListener("click", () => { box.hidden = true; });
  }

  async function boot() {
    try {
      S.info = (await api("info")).data;
    } catch (e) {
      $("#main").innerHTML = `<div class="errbox">Verbindung zu Home Assistant fehlgeschlagen: ${esc(e.message)}</div>`;
      return;
    }
    hinweisBetriebsart();
    window.addEventListener("hashchange", route);
    window.addEventListener("beforeunload", e => { if (isDirty()) { e.preventDefault(); e.returnValue = ""; } });
    route();
  }
  boot();
})();
