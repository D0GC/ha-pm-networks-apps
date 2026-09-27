/* PM Klima Studio – Reiter „Steuerung“: Raumsteuerung und Heizperiode über die Integration PM Klima. */
(function () {
  "use strict";

  const REFRESH_MS = 30000;
  const DAUER = [
    { key: "plan", wert: null, label: "Bis Planwechsel" },
    { key: "60", wert: 60, label: "1 h" },
    { key: "180", wert: 180, label: "3 h" },
    { key: "0", wert: 0, label: "Dauerhaft" },
  ];
  const BOOST = [30, 60, 120];
  const MODI_PM = [{ m: "auto", label: "Auto" }, { m: "heat", label: "Hand" }, { m: "off", label: "Aus" }];
  // Betriebsart generisch: App-Modus je Raum (Plan = App wendet den Heizplan an, Hand = nicht, Aus = Thermostat aus)
  const MODI_GEN = [{ m: "plan", label: "Plan" }, { m: "hand", label: "Hand" }, { m: "aus", label: "Aus" }];
  const GRUND_GEN = { zeitplan: "Folgt dem Heizplan", sommer: "Sommer: Plananwendung pausiert", overlay: "Abweichung vom Plan", boost: "Boost", manuell: "Handbetrieb", aus: "Ausgeschaltet" };
  const HP_MODI = [{ m: "automatik", label: "Automatik" }, { m: "heizperiode", label: "Heizperiode" }, { m: "sommer", label: "Sommer" }];
  const HP_GRENZEN = {
    heizgrenze: { min: 5, max: 20, step: 0.5, label: "Heizgrenze", unit: "°C" },
    hysterese: { min: 0, max: 5, step: 0.5, label: "Hysterese", unit: "K" },
    tage_start: { min: 1, max: 7, step: 1, label: "Tage bis Beginn", unit: "Tage" },
    tage_ende: { min: 1, max: 7, step: 1, label: "Tage bis Ende", unit: "Tage" },
  };

  const st = {
    data: null,     // letzte Antwort von GET /api/steuerung
    ctl: {},        // je Raum: {temp, dauer, hand, fokus}
    timer: null,
    stand: null,
    hpOffen: false, // Einstellungen aufgeklappt
    hpEntwurf: null,
    busy: false,
    view: null,     // Container dieses Reiters (von app.js je render()-Lauf neu erzeugt)
  };

  const A = () => window.KSApp;
  const esc = s => A().esc(s);
  const num = (v, d) => A().num(v, d);
  const aktiv = () => A().S.tab === "steuerung";
  const gen = () => A().generisch();
  const MODI = () => gen() ? MODI_GEN : MODI_PM;
  // Modusnamen der Betriebsart: Plan-/Auto-Modus, Handmodus, Aus
  const M = () => gen() ? { plan: "plan", hand: "hand", aus: "aus" } : { plan: "auto", hand: "heat", aus: "off" };
  // Nur der sichtbare Container des jüngsten Laufs darf beschrieben werden.
  const aktuell = view => aktiv() && st.view === view && view.isConnected;
  const $v = sel => st.view.querySelector(sel);

  // Rückgabe ist HTML-sicher (landet in innerHTML).
  function fmtZeit(iso) {
    if (!iso) return "–";
    const t = Date.parse(iso);
    if (isNaN(t)) return esc(String(iso));
    const heute = new Intl.DateTimeFormat("de-DE", { dateStyle: "short", timeZone: A().tz() }).format(new Date());
    const tag = new Intl.DateTimeFormat("de-DE", { dateStyle: "short", timeZone: A().tz() }).format(new Date(t));
    const uhr = new Intl.DateTimeFormat("de-DE", { hour: "2-digit", minute: "2-digit", timeZone: A().tz() }).format(new Date(t));
    return esc(tag === heute ? `${uhr} Uhr` : `${A().fmtDate(t, false)}, ${uhr} Uhr`);
  }
  const grad = v => v == null ? "–" : `${esc(num(v))} °C`;
  const WARN_VERKNUEPFEN = "Steht die Freigabe beim Verknüpfen auf aus, schaltet die Integration Räume im Modus Auto sofort ab.";

  // ------------------------------------------------------------------ Laden
  function stopTimer() { clearTimeout(st.timer); st.timer = null; }
  function planTimer() {
    stopTimer();
    const view = st.view;
    st.timer = setTimeout(async () => {
      if (!aktuell(view)) return;
      if (st.busy || !document.getElementById("modal").hidden) { planTimer(); return; }
      try { await laden(); if (aktuell(view)) zeichnen(); } catch (e) { /* stiller Fehler, nächster Versuch folgt */ }
      if (aktuell(view)) planTimer();
    }, REFRESH_MS);
  }

  async function laden() {
    const { data } = await A().api("steuerung");
    st.data = data;
    st.stand = Date.now();
  }

  async function render(main, parts) {
    stopTimer();
    st.view = main;
    if (!st.data || !main.hasChildNodes()) main.innerHTML = `<div class="bar"><h2 class="grow">Steuerung</h2></div><div class="loading">Zustand wird geladen …</div>`;
    await laden();
    if (!aktuell(main)) return;
    const pf = A().prefill;
    A().prefill = null;
    const ziel = (pf && pf.raum) || parts[1] || null;
    if (pf && pf.raum && st.data.raeume[pf.raum]) uebernehmeVorgabe(pf);
    zeichnen();
    if (ziel && st.data.raeume[ziel]) {
      const card = cardEl(ziel);
      if (card) {
        card.classList.add("focus");
        card.scrollIntoView({ block: "center", behavior: "smooth" });
        setTimeout(() => card.classList.remove("focus"), 4000);
      }
    }
    planTimer();
  }

  // Vorschlag des Coachs vorbelegen bzw. markieren – nie selbst ausführen.
  // aktion: overlay (Standard) | boost | zurueck | modus (mit Feld modus)
  function uebernehmeVorgabe(pf) {
    const r = st.data.raeume[pf.raum];
    const c = ctlFor(pf.raum, r, true);
    const aktion = ["overlay", "boost", "zurueck", "modus"].includes(pf.aktion) ? pf.aktion : "overlay";
    c.vorschlagModus = null;
    if (aktion === "modus") {
      c.fokus = "modus";
      if (MODI().some(x => x.m === pf.modus)) c.vorschlagModus = pf.modus;
    } else if (aktion === "zurueck") {
      c.fokus = "zurueck";
    } else if (aktion === "boost") {
      c.fokus = "boost";
    } else {
      if (typeof pf.temperatur === "number") {
        const t = klemme(r, pf.temperatur);
        if (r.modus === M().hand) c.hand = t; else c.temp = t;
      }
      if (pf.dauer === null || typeof pf.dauer === "number") c.dauer = pf.dauer;
      c.geaendert = true;
      c.fokus = "overlay";
    }
    let text = "Die Werte des Coachs sind vorbelegt. Bitte prüfen und bestätigen.";
    if (aktion === "modus" && c.vorschlagModus) {
      const ziel = (MODI().find(x => x.m === c.vorschlagModus) || {}).label;
      text = c.vorschlagModus === r.modus ? `Der Raum ist bereits im Modus ${ziel}.` : `Vorschlag des Coachs: Betriebsart ${ziel}. Bitte prüfen und selbst umschalten.`;
    } else if (aktion === "zurueck") {
      text = r.overlay_bis || r.boost_bis ? "Vorschlag des Coachs: Zurück zum Plan. Bitte prüfen und bestätigen." : "Der Raum folgt bereits dem Plan.";
    } else if (aktion === "boost") {
      text = "Vorschlag des Coachs: Boost. Bitte Dauer wählen.";
    } else if (r.modus === M().aus) {
      text = `Der Raum ist ausgeschaltet. Eine vorübergehende Änderung ist nur im Modus ${gen() ? "Plan" : "Auto"} möglich.`;
    } else if (gen() && !r.steuerbar) {
      text = r.steuerbar_grund || "Dieser Raum kann nicht gesteuert werden.";
    }
    A().toast(text);
  }

  function klemme(r, t) {
    const lo = r.min_temp ?? 5, hi = r.max_temp ?? 25, sch = r.schritt || 0.5;
    return Math.min(hi, Math.max(lo, Math.round(t / sch) * sch));
  }

  function ctlFor(slug, r, neu) {
    let c = st.ctl[slug];
    if (!c || neu) {
      const basis = r.soll ?? r.zeitplan_temperatur ?? 20;
      c = st.ctl[slug] = Object.assign({ temp: klemme(r, basis), hand: klemme(r, basis), dauer: null, geaendert: false }, c ? { dauer: c.dauer } : {});
    }
    // Automatische Aktualisierung übernimmt den Sollwert, solange der Nutzer nichts verändert hat
    if (!c.geaendert && r.soll != null) { c.temp = klemme(r, r.soll); c.hand = klemme(r, r.soll); }
    return c;
  }

  // ------------------------------------------------------------------ Zeichnen
  function zeichnen() {
    const main = st.view;
    const d = st.data;
    const integ = d.integration || {};
    const raeume = Object.entries(d.raeume || {});
    const stand = st.stand ? new Intl.DateTimeFormat("de-DE", { hour: "2-digit", minute: "2-digit", second: "2-digit", timeZone: A().tz() }).format(new Date(st.stand)) : "";
    main.innerHTML = `<div class="bar"><h2 class="grow">Steuerung</h2>
        <span class="small muted">Stand ${esc(stand)} Uhr</span>
        <button class="btn ghost" id="ctl-reload">Aktualisieren</button></div>
      ${integ.aktiv === false ? `<div class="errbox">Die Integration ist pausiert. Steuerbefehle werden nicht an die Thermostate weitergegeben.</div>` : ""}
      ${integ.gesperrt ? `<div class="warnbox small">Die Integration sperrt derzeit das Heizen${integ.beschreibung ? ": " + esc(integ.beschreibung) : "."}</div>` : ""}
      <section class="card glass hp-card" id="hp-card">${hpHtml(d.heizperiode, raeume)}</section>
      <div class="ctl-grid">${raeume.map(([slug, r]) => `<article class="card glass ctl-card" data-raum="${esc(slug)}">${cardHtml(slug, r)}</article>`).join("")
        || '<div class="empty">Keine Räume erkannt.</div>'}</div>
      <p class="small muted">${gen() ? "Klima Studio schaltet die Thermostate direkt über Home Assistant. Der Modus Plan wendet den Heizplan des Raums an, im Modus Hand bleibt die Temperatur, bis Sie sie ändern."
        : "Alle Befehle laufen über die Integration PM Klima. Klima Studio schaltet die Thermostate nicht selbst."}</p>`;
    main.querySelector("#ctl-reload").addEventListener("click", async ev => {
      const btn = ev.currentTarget;
      btn.disabled = true;
      try { await laden(); if (aktuell(main)) { zeichnen(); planTimer(); } } catch (e) { if (aktuell(main)) { A().toast(e.message, true); btn.disabled = false; } }
    });
    bindHp();
    for (const [slug] of raeume) bindCard(slug);
  }

  // ------------------------------------------------------------------ Heizperiode
  function hpHtml(hp, raeume) {
    if (!hp) return `<div class="kicker">Heizperiode</div><p class="muted">Der Status der Heizperiode ist derzeit nicht verfügbar. Die Raumsteuerung ist davon nicht betroffen.</p>`;
    const zustand = hp.aktiv === true ? "Heizperiode aktiv" : hp.aktiv === false ? "Sommerbetrieb" : "Zustand unbekannt";
    const zcls = hp.aktiv === true ? "heat" : hp.aktiv === false ? "auto" : "off";
    const e = hp.entscheidung || {};
    if (gen() || hp.intern) return hpHtmlGen(hp, raeume, zustand, zcls, e);
    const anleitung = `Einstellungen → Geräte &amp; Dienste → PM Klima → Konfigurieren → Schritt „Sperre“ → Freigabe-Entität: <b>${esc(hp.entitaet)}</b>. ${esc(WARN_VERKNUEPFEN)}`;
    let hinweise = "";
    if (hp.vorhanden === false) {
      hinweise += `<div class="warnbox"><p class="m0">Die Freigabe-Entität <b>${esc(hp.entitaet)}</b> ist in Home Assistant nicht vorhanden. Klima Studio kann sie als Helfer anlegen.</p>
        <div class="actions left"><button class="btn primary" id="hp-einrichten">Einrichten</button></div></div>`;
    } else if (hp.verknuepft === false) {
      hinweise += `<div class="warnbox"><p class="m0">Die Freigabe ist in der Integration offenbar nicht eingetragen. Die Umschaltung auf Sommer hat daher keine Wirkung auf die Heizung.
        Tragen Sie die Entität einmalig ein:</p><p class="m0 small">${anleitung}</p></div>`;
    } else if (hp.verknuepft == null) {
      hinweise += `<details class="hp-hint small muted"><summary>Die Verknüpfung mit der Integration wird im Sommerbetrieb geprüft.</summary>
        <p class="m0">Falls noch nicht geschehen, tragen Sie die Freigabe einmalig in der Integration ein: ${anleitung}</p></details>`;
    }
    const abw = hp.abweichung;
    if (abw && hp.vorhanden !== false) {
      const wort = z => z === "on" ? "an" : z === "off" ? "aus" : `nicht verfügbar (${z})`;
      let text = `Die Freigabe steht auf ${esc(wort(abw.ist))}, ermittelt ist ${abw.soll === "on" ? "Heizperiode" : "Sommer"}.`;
      if (abw.ausstehend) text += " Klima Studio schaltet sie bei der nächsten Prüfung um.";
      else if (abw.ist === "on" || abw.ist === "off") {
        text += ` Die manuelle Umschaltung in Home Assistant${abw.seit ? ` (seit ${fmtZeit(abw.seit)})` : ""} bleibt bestehen.`;
        if (abw.soll === "on" && abw.ist === "off") {
          text += abw.winterschutz_ab
            ? ` Winterschutz: Steht die Freigabe weiter auf aus, schaltet Klima Studio die Heizperiode um ${fmtZeit(abw.winterschutz_ab)} wieder ein.`
            : " Winterschutz: Steht die Freigabe länger als 6 Stunden auf aus, schaltet Klima Studio die Heizperiode wieder ein.";
        }
      }
      hinweise += `<div class="warnbox small" id="hp-abweichung">${text}</div>`;
    }
    if (hp.aktiv === false) {
      const heizend = raeume.filter(([, r]) => r.modus === "heat").map(([, r]) => r.name);
      if (heizend.length) hinweise += `<div class="infobox small">Im Sommerbetrieb heizen Räume im Modus Hand trotz Sperre weiter: ${esc(heizend.join(", "))}.</div>`;
    }
    const f = st.hpEntwurf || hp;
    const feld = k => {
      const g = HP_GRENZEN[k];
      return `<label class="field"><span>${g.label} (${g.unit})</span><input type="text" id="hp-${k}" value="${esc(typeof f[k] === "number" ? num(f[k]) : f[k] ?? "")}" inputmode="${g.step < 1 ? "decimal" : "numeric"}" maxlength="4" autocomplete="off"></label>`;
    };
    return `<div class="hp-head">
        <div><div class="kicker">Heizperiode</div><h3 class="hp-title"><span class="mode ${zcls}">${esc(zustand)}</span></h3></div>
        ${seg("hp-modus", HP_MODI.map(x => ({ v: x.m, label: x.label })), hp.modus, hp.vorhanden === false)}
      </div>
      ${hinweise}
      <div class="hp-body">
        <dl class="kv">
          <dt>Entscheidung</dt><dd>${esc(e.grund || "–")}</dd>
          <dt>Außen aktuell</dt><dd>${grad(hp.aussen_aktuell)}</dd>
          <dt>Freigabe</dt><dd>${esc(hp.entitaet || "–")}${hp.zustand ? ` · ${hp.zustand === "on" ? "an" : "aus"}` : ""}</dd>
          <dt>Letzte Änderung</dt><dd>${fmtZeit(hp.letzte_aenderung)}</dd>
          <dt>Nächste Prüfung</dt><dd>${fmtZeit(hp.naechste_pruefung)}</dd>
        </dl>
        <figure class="hp-chart">${balken(hp)}<figcaption class="chart-legend">
          <span><i style="background:#F2B35C"></i>Heizgrenze ${grad(hp.heizgrenze)}</span>
          <span><i class="dash"></i>Ende ab ${grad(hp.heizgrenze != null ? hp.heizgrenze + (hp.hysterese || 0) : null)}</span></figcaption></figure>
      </div>
      <details class="hp-set" id="hp-set" ${st.hpOffen ? "open" : ""}><summary>Einstellungen der Automatik</summary>
        <div class="fields">${Object.keys(HP_GRENZEN).map(feld).join("")}</div>
        <p class="small muted">Die Heizperiode beginnt, wenn die Tagesmittel der letzten Tage (Tage bis Beginn) alle unter der Heizgrenze liegen.
          Sie endet, wenn die Tagesmittel der letzten Tage (Tage bis Ende) alle mindestens Heizgrenze plus Hysterese erreichen.</p>
        <div class="actions left"><button class="btn primary" id="hp-save">Speichern</button><button class="btn ghost" id="hp-reset">Zurücksetzen</button></div>
      </details>`;
  }

  // Generisch: Zustand intern, keine Freigabe-Entität; statt der Freigabe-Hinweise Sommer-Aktion und Plananwendung
  const SOMMER_AKTION = {
    plan_pausieren_und_aus: "Plananwendung pausieren und Räume im Modus Plan ausschalten",
    plan_pausieren: "Plananwendung pausieren, Thermostate bleiben eingeschaltet",
  };
  function planAnwendung() { return A().kann("plan_anwendung"); }
  function hpHtmlGen(hp, raeume, zustand, zcls, e) {
    let hinweise = "";
    const abw = hp.abweichung;
    if (abw) {
      hinweise += `<div class="warnbox small" id="hp-abweichung">Ermittelt ist ${abw.soll === "on" ? "Heizperiode" : "Sommer"}, eingestellt ist ${abw.ist === "on" ? "Heizperiode" : "Sommer"}. Klima Studio stellt bei der nächsten Prüfung um.</div>`;
    }
    if (hp.aktiv === false) {
      const hand = raeume.filter(([, r]) => r.modus === "hand" && r.hvac_modus !== "off").map(([, r]) => r.name);
      if (hand.length) hinweise += `<div class="infobox small">Räume im Modus Hand bleiben im Sommerbetrieb unberührt und heizen weiter: ${esc(hand.join(", "))}.</div>`;
    }
    if (!planAnwendung()) {
      hinweise += `<div class="infobox small">Die Plananwendung ist abgeschaltet. Die Heizperiode wirkt sich daher nur auf das Ein- und Ausschalten aus. Die Plananwendung wird in der App-Konfiguration (Option plan_anwenden) geschaltet.</div>`;
    }
    const aktion = SOMMER_AKTION[hp.sommer_aktion] || (hp.sommer_aktion ? String(hp.sommer_aktion) : "–");
    const spiegel = hp.vorhanden ? `${esc(hp.entitaet)}${hp.spiegel ? ` · ${hp.spiegel === "on" ? "an" : "aus"}` : ""}` : "keine (optional)";
    const spiegelHilfe = !hp.vorhanden && String(hp.entitaet || "").startsWith("input_boolean.")
      ? `<details class="hp-hint small muted"><summary>Optional: Spiegel-Entität für eigene Automationen</summary>
          <p class="m0">Klima Studio kann <b>${esc(hp.entitaet)}</b> als Helfer anlegen und als Spiegel der Heizperiode schalten (an = Heizperiode, aus = Sommer). Für die Heizperiode selbst ist sie nicht nötig.</p>
          <div class="actions left"><button class="btn" id="hp-einrichten">Anlegen</button></div></details>` : "";
    return `<div class="hp-head">
        <div><div class="kicker">Heizperiode</div><h3 class="hp-title"><span class="mode ${zcls}">${esc(zustand)}</span></h3></div>
        ${seg("hp-modus", HP_MODI.map(x => ({ v: x.m, label: x.label })), hp.modus, false)}
      </div>
      ${hinweise}
      <div class="hp-body">
        <dl class="kv">
          <dt>Entscheidung</dt><dd>${esc(e.grund || "–")}</dd>
          <dt>Außen aktuell</dt><dd>${grad(hp.aussen_aktuell)}</dd>
          <dt>Sommer-Aktion</dt><dd>${esc(aktion)}</dd>
          <dt>Plananwendung</dt><dd>${planAnwendung() ? "an" : "aus"} <span class="small muted">· geschaltet in der App-Konfiguration</span></dd>
          <dt>Spiegel</dt><dd>${spiegel}</dd>
          <dt>Letzte Änderung</dt><dd>${fmtZeit(hp.letzte_aenderung)}</dd>
          <dt>Nächste Prüfung</dt><dd>${fmtZeit(hp.naechste_pruefung)}</dd>
        </dl>
        <figure class="hp-chart">${balken(hp)}<figcaption class="chart-legend">
          <span><i style="background:#F2B35C"></i>Heizgrenze ${grad(hp.heizgrenze)}</span>
          <span><i class="dash"></i>Ende ab ${grad(hp.heizgrenze != null ? hp.heizgrenze + (hp.hysterese || 0) : null)}</span></figcaption></figure>
      </div>
      ${spiegelHilfe}
      ${hpEinstellungen(hp)}`;
  }

  function hpEinstellungen(hp) {
    const f = st.hpEntwurf || hp;
    const feld = k => {
      const g = HP_GRENZEN[k];
      return `<label class="field"><span>${g.label} (${g.unit})</span><input type="text" id="hp-${k}" value="${esc(typeof f[k] === "number" ? num(f[k]) : f[k] ?? "")}" inputmode="${g.step < 1 ? "decimal" : "numeric"}" maxlength="4" autocomplete="off"></label>`;
    };
    return `<details class="hp-set" id="hp-set" ${st.hpOffen ? "open" : ""}><summary>Einstellungen der Automatik</summary>
        <div class="fields">${Object.keys(HP_GRENZEN).map(feld).join("")}</div>
        <p class="small muted">Die Heizperiode beginnt, wenn die Tagesmittel der letzten Tage (Tage bis Beginn) alle unter der Heizgrenze liegen.
          Sie endet, wenn die Tagesmittel der letzten Tage (Tage bis Ende) alle mindestens Heizgrenze plus Hysterese erreichen.</p>
        <div class="actions left"><button class="btn primary" id="hp-save">Speichern</button><button class="btn ghost" id="hp-reset">Zurücksetzen</button></div>
      </details>`;
  }

  function balken(hp) {
    const werte = (hp.tagesmittel || []).slice(-7);
    if (!werte.length) return `<div class="empty small">Keine Tagesmittel verfügbar.</div>`;
    const W = 320, H = 150, m = { l: 30, r: 8, t: 14, b: 22 };
    const iw = W - m.l - m.r, ih = H - m.t - m.b;
    const gr = hp.heizgrenze, ende = gr != null ? gr + (hp.hysterese || 0) : null;
    const vals = werte.map(w => w.mittel).filter(v => v != null).concat([gr, ende].filter(v => v != null));
    let lo = Math.min(0, ...vals), hi = Math.max(...vals, 1);
    lo = Math.floor((lo - 1) / 5) * 5; hi = Math.ceil((hi + 1) / 5) * 5;
    const Y = v => m.t + ih - ((v - lo) / (hi - lo)) * ih;
    const bw = iw / werte.length;
    let s = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Tagesmittel der Außentemperatur, letzte ${werte.length} Tage">`;
    for (let v = lo; v <= hi; v += 5) {
      s += `<line x1="${m.l}" x2="${W - m.r}" y1="${Y(v).toFixed(1)}" y2="${Y(v).toFixed(1)}" stroke="rgba(197,192,211,.12)"/>`;
      s += `<text x="${m.l - 5}" y="${(Y(v) + 3.5).toFixed(1)}" text-anchor="end" fill="rgba(236,235,242,.6)" font-size="10">${v}</text>`;
    }
    werte.forEach((w, i) => {
      const x = m.l + i * bw + bw * 0.18, bwi = bw * 0.64;
      const tag = new Intl.DateTimeFormat("de-DE", { weekday: "short", timeZone: "UTC" }).format(new Date(w.datum + "T12:00:00Z")).replace(".", "");
      s += `<text x="${(x + bwi / 2).toFixed(1)}" y="${H - 6}" text-anchor="middle" fill="rgba(236,235,242,.6)" font-size="10">${esc(tag)}</text>`;
      if (w.mittel == null) {
        s += `<text x="${(x + bwi / 2).toFixed(1)}" y="${(Y(lo) - 4).toFixed(1)}" text-anchor="middle" fill="rgba(236,235,242,.45)" font-size="10">–</text>`;
        return;
      }
      const farbe = gr != null && w.mittel < gr ? "#E8875A" : ende != null && w.mittel >= ende ? "#7FC8A9" : "#B885D6";
      const y0 = Y(Math.max(0, lo)), y1 = Y(w.mittel);
      const top = Math.min(y0, y1), h = Math.max(1.5, Math.abs(y0 - y1));
      s += `<rect x="${x.toFixed(1)}" y="${top.toFixed(1)}" width="${bwi.toFixed(1)}" height="${h.toFixed(1)}" rx="3" fill="${farbe}" opacity=".85"><title>${esc(w.datum)}: ${esc(num(w.mittel))} °C</title></rect>`;
      s += `<text x="${(x + bwi / 2).toFixed(1)}" y="${(Math.min(y1, y0) - 4).toFixed(1)}" text-anchor="middle" fill="#ECEBF2" font-size="9.5" font-weight="600">${esc(num(w.mittel))}</text>`;
    });
    if (gr != null) s += `<line x1="${m.l}" x2="${W - m.r}" y1="${Y(gr).toFixed(1)}" y2="${Y(gr).toFixed(1)}" stroke="#F2B35C" stroke-width="1.5"/>`;
    if (ende != null && ende !== gr) s += `<line x1="${m.l}" x2="${W - m.r}" y1="${Y(ende).toFixed(1)}" y2="${Y(ende).toFixed(1)}" stroke="#F2B35C" stroke-width="1.2" stroke-dasharray="5 4" opacity=".8"/>`;
    return s + "</svg>";
  }

  function seg(id, items, aktuell, gesperrt, vorschlag) {
    return `<div class="seg" id="${esc(id)}" role="group">${items.map(x =>
      `<button type="button" data-v="${esc(x.v)}" class="${x.v === aktuell ? "active" : ""}${x.v === vorschlag && x.v !== aktuell ? " vorschlag" : ""}" aria-pressed="${x.v === aktuell}" ${x.v === vorschlag && x.v !== aktuell ? 'title="Vorschlag des Coachs"' : ""} ${gesperrt ? "disabled" : ""}>${esc(x.label)}</button>`).join("")}</div>`;
  }

  function bindHp() {
    const card = $v("#hp-card");
    const hp = st.data.heizperiode;
    if (!hp) return;
    card.querySelectorAll("#hp-modus button").forEach(b => b.addEventListener("click", async () => {
      if (b.dataset.v === hp.modus) return;
      if (b.dataset.v === "sommer") {
        const ok = await A().modal("Auf Sommer umstellen?",
          gen() || hp.intern ? `<p>Im Sommerbetrieb pausiert Klima Studio die Plananwendung.${hp.sommer_aktion === "plan_pausieren" ? " Die Thermostate bleiben eingeschaltet." : " Räume im Modus Plan werden ausgeschaltet und zu Beginn der Heizperiode wieder eingeschaltet."} Räume im Modus Hand bleiben unberührt.</p>`
            : `<p>Im Sommerbetrieb schaltet die Integration alle Räume im Modus Auto ab. Räume im Modus Hand, Abweichungen und Boost heizen weiter.</p>`,
          [{ label: "Abbrechen", value: false, cls: "ghost" }, { label: "Sommer einschalten", value: true, cls: "danger" }]);
        if (!ok) return;
      }
      hpSenden({ modus: b.dataset.v }, "Modus der Heizperiode geändert.");
    }));
    const det = $v("#hp-set");
    det.addEventListener("toggle", () => { st.hpOffen = det.open; });
    for (const k of Object.keys(HP_GRENZEN)) {
      const inp = $v("#hp-" + k);
      inp.addEventListener("input", () => {
        st.hpEntwurf = st.hpEntwurf || Object.assign({}, hp);
        st.hpEntwurf[k] = inp.value;
      });
    }
    $v("#hp-reset").addEventListener("click", () => { st.hpEntwurf = null; zeichneHp(); });
    $v("#hp-save").addEventListener("click", () => {
      const body = {};
      for (const [k, g] of Object.entries(HP_GRENZEN)) {
        const raw = String($v("#hp-" + k).value).replace(",", ".").trim();
        const v = Number(raw);
        if (raw === "" || isNaN(v) || v < g.min || v > g.max || Math.abs(v / g.step - Math.round(v / g.step)) > 1e-9) {
          A().toast(`${g.label}: bitte einen Wert von ${num(g.min)} bis ${num(g.max)} in Schritten von ${num(g.step)} angeben.`, true);
          return;
        }
        body[k] = v;
      }
      hpSenden(body, "Einstellungen gespeichert.", true);
    });
    const ein = $v("#hp-einrichten");
    if (ein) ein.addEventListener("click", einrichten);
  }

  function zeichneHp() {
    const card = $v("#hp-card");
    if (!card) return; // neuer Lauf lädt noch
    card.classList.remove("busy");
    card.innerHTML = hpHtml(st.data.heizperiode, Object.entries(st.data.raeume || {}));
    bindHp();
  }

  function sperre(el, an) { el.querySelectorAll("button, input").forEach(b => { b.disabled = an; }); el.classList.toggle("busy", an); }

  async function hpSenden(body, meldung, entwurfVerwerfen) {
    const card = $v("#hp-card");
    st.busy = true; sperre(card, true);
    try {
      const { data } = await A().api("heizperiode", { method: "POST", body });
      st.data.heizperiode = data;
      if (entwurfVerwerfen) st.hpEntwurf = null;
      A().toast(meldung);
    } catch (e) {
      A().toast(e.message, true);
    } finally {
      st.busy = false;
      if (aktiv()) zeichneHp();
    }
  }

  async function einrichten() {
    const card = $v("#hp-card");
    st.busy = true; sperre(card, true);
    try {
      const { data } = await A().api("heizperiode/einrichten", { method: "POST", body: {} });
      st.data.heizperiode = data;
      if (aktiv()) zeichneHp();
      if (gen() || data.intern) {
        await A().modal("Spiegel angelegt", `<p>${esc(data.hinweis || "Die Entität wurde angelegt.")}</p>`, [{ label: "Verstanden", value: true, cls: "primary" }]);
        return;
      }
      await A().modal("Freigabe angelegt", `<p>${esc(data.hinweis || "Die Freigabe-Entität wurde angelegt.")}</p>
        <div class="infobox small">Tragen Sie die Entität einmalig in der Integration ein:<br>
        Einstellungen → Geräte &amp; Dienste → PM Klima → Konfigurieren → Schritt „Sperre“ → Freigabe-Entität: <b>${esc(data.entitaet)}</b></div>`,
        [{ label: "Verstanden", value: true, cls: "primary" }]);
    } catch (e) {
      A().toast(e.message, true);
      if (aktiv()) zeichneHp();
    } finally {
      st.busy = false;
    }
  }

  // ------------------------------------------------------------------ Raumkarten
  function modeInfo(r) {
    if (r.hvac_action === "heating") return { cls: "heizt", text: "heizt" };
    if (gen()) return { plan: { cls: "auto", text: "Plan" }, hand: { cls: "heat", text: "Hand" }, aus: { cls: "off", text: "Aus" } }[r.modus] || { cls: "off", text: r.modus || "unbekannt" };
    return { auto: { cls: "auto", text: "Auto" }, heat: { cls: "heat", text: "Hand" }, off: { cls: "off", text: "Aus" } }[r.modus] || { cls: "off", text: r.modus || "unbekannt" };
  }

  function statusZeilen(r) {
    const z = [];
    if (r.boost_bis) z.push(`Boost bis ${fmtZeit(r.boost_bis)}`);
    if (r.overlay_bis === "dauerhaft") z.push("Abweichung vom Plan: dauerhaft");
    else if (r.overlay_bis) z.push(`Abweichung vom Plan bis ${fmtZeit(r.overlay_bis)}`);
    if (r.fenster_offen) z.push("Fenster offen");
    return z;
  }

  function cardHtml(slug, r) {
    if (gen()) return cardHtmlGen(slug, r);
    const m = modeInfo(r);
    const kopf = `<div class="room-head"><h3>${esc(r.name || slug)}</h3><span class="mode ${esc(m.cls)}">${esc(m.text)}</span></div>
      <div class="temps"><span class="ist">${grad(r.ist)}</span><span class="soll">Soll ${grad(r.soll)}</span>${r.feuchte != null ? `<span class="soll">${num(r.feuchte, 0)} %</span>` : ""}</div>
      <div class="grund">${esc(r.anzeige || r.grund || "")}</div>`;
    if (!r.steuerbar) return kopf + `<p class="small muted">Dieser Raum wird nicht von PM Klima gesteuert.</p>`;
    const c = ctlFor(slug, r);
    const z = statusZeilen(r);
    const plan = r.modus === "auto" ? `<div class="small muted">Plan ${grad(r.zeitplan_temperatur)}${r.naechster_wechsel ? ` · nächster Wechsel ${fmtZeit(r.naechster_wechsel)}${r.naechster_wechsel_grund ? " (" + esc(r.naechster_wechsel_grund) + ")" : ""}` : ""}</div>` : "";
    let teil = "";
    if (r.modus === "auto") {
      const dauern = DAUER.slice();
      if (c.dauer != null && !dauern.some(x => x.wert === c.dauer)) dauern.splice(3, 0, { key: String(c.dauer), wert: c.dauer, label: dauerText(c.dauer) });
      teil = `<div class="ctl-block ${c.fokus === "overlay" ? "hl" : ""}"><div class="lbl">Vorübergehend ändern</div>
        <div class="ctl-row">${stepper("ov", c.temp)}
        <button class="btn primary" data-a="overlay">Anwenden</button></div>
        <div class="chips small-chips" data-dauer>${dauern.map(x => `<button type="button" class="chip ${x.wert === c.dauer ? "active" : ""}" data-d="${x.wert == null ? "" : x.wert}">${esc(x.label)}</button>`).join("")}</div></div>`;
    } else if (r.modus === "heat") {
      teil = `<div class="ctl-block ${c.fokus === "overlay" ? "hl" : ""}"><div class="lbl">Handwert</div>
        <div class="ctl-row">${stepper("hand", c.hand)}<button class="btn primary" data-a="temperatur">Übernehmen</button></div></div>`;
    } else if (r.modus === "off") {
      teil = `<p class="small muted m0">Der Raum ist ausgeschaltet. Ein Boost heizt trotzdem für die gewählte Dauer.</p>`;
    }
    return kopf + (z.length ? `<div class="ctl-status">${z.map(x => `<span class="pill">${esc(x)}</span>`).join("")}</div>` : "") + plan +
      `<div class="ctl-block ${c.fokus === "modus" ? "hl" : ""}"><div class="lbl">Betriebsart</div>${seg("seg-" + slug, MODI_PM.map(x => ({ v: x.m, label: x.label })), r.modus, false, c.fokus === "modus" ? c.vorschlagModus : null)}</div>
      ${teil}
      <div class="ctl-block ${c.fokus === "boost" || c.fokus === "zurueck" ? "hl" : ""}"><div class="lbl">Boost auf ${grad(r.max_temp)}</div>
        <div class="ctl-row wrap">${BOOST.map(b => `<button class="btn" data-a="boost" data-d="${b}">${b} min</button>`).join("")}
        ${r.overlay_bis || r.boost_bis ? `<span class="grow"></span><button class="btn ${c.fokus === "zurueck" ? "vorschlag" : "ghost"}" data-a="zurueck">Zurück zum Plan</button>` : ""}</div></div>`;
  }

  function sollGen(r) {
    if (r.soll != null) return grad(r.soll);
    if (r.soll_bereich) return `${esc(num(r.soll_bereich.min))}–${esc(num(r.soll_bereich.max))} °C`;
    return "–";
  }

  function overlayBlock(c) {
    const dauern = DAUER.slice();
    if (c.dauer != null && !dauern.some(x => x.wert === c.dauer)) dauern.splice(3, 0, { key: String(c.dauer), wert: c.dauer, label: dauerText(c.dauer) });
    return `<div class="ctl-block ${c.fokus === "overlay" ? "hl" : ""}"><div class="lbl">Vorübergehend ändern</div>
        <div class="ctl-row">${stepper("ov", c.temp)}
        <button class="btn primary" data-a="overlay">Anwenden</button></div>
        <div class="chips small-chips" data-dauer>${dauern.map(x => `<button type="button" class="chip ${x.wert === c.dauer ? "active" : ""}" data-d="${x.wert == null ? "" : x.wert}">${esc(x.label)}</button>`).join("")}</div></div>`;
  }

  // Betriebsart generisch: App-Modus Plan/Hand/Aus, Thermostat-Modus klein, Plan-Soll
  function cardHtmlGen(slug, r) {
    const info = A().rooms().find(x => x.raum === slug);
    const ohnePlan = !!info && !info.schedule;
    const m = r.steuerbar ? modeInfo(r) : { cls: "off", text: "nur Anzeige" };
    const grund = !r.steuerbar || (ohnePlan && r.grund === "zeitplan") ? "" : GRUND_GEN[r.grund] || r.grund || "";
    const kopf = `<div class="room-head"><h3>${esc(r.name || slug)}</h3><span class="mode ${esc(m.cls)}">${esc(m.text)}</span></div>
      <div class="temps"><span class="ist">${grad(r.ist)}</span><span class="soll">Soll ${sollGen(r)}</span>${r.feuchte != null ? `<span class="soll">${num(r.feuchte, 0)} %</span>` : ""}</div>
      <div class="grund">${esc(grund)}${grund ? " · " : ""}<span class="hvac">Thermostat ${esc(A().hvacText(r.hvac_modus))}${r.preset && r.preset !== "none" ? ` · Preset ${esc(r.preset)}` : ""}</span></div>`;
    if (!r.steuerbar) return kopf + `<p class="small muted">${esc(r.steuerbar_grund || "Dieser Raum kann nicht gesteuert werden.")}</p>`;
    const c = ctlFor(slug, r);
    const z = statusZeilen(r);
    let plan = "";
    if (r.modus === "plan") {
      const teile = [];
      if (ohnePlan && r.plan_soll == null) teile.push("Kein Heizplan zugeordnet");
      else {
        teile.push(`Plan-Soll ${grad(r.plan_soll)}`);
        if (r.naechster_wechsel) teile.push(`nächster Wechsel ${fmtZeit(r.naechster_wechsel)}`);
      }
      if (r.plan_anwendung === false || !A().kann("plan_anwendung")) teile.push("Plananwendung abgeschaltet (App-Konfiguration)");
      else if (r.sommer_pause) teile.push("Sommer: Plananwendung pausiert");
      plan = `<div class="small muted">${teile.join(" · ")}</div>`;
    }
    let teil = "";
    if (r.modus === "plan") teil = overlayBlock(c);
    else if (r.modus === "hand") {
      teil = `<div class="ctl-block ${c.fokus === "overlay" ? "hl" : ""}"><div class="lbl">Handwert</div>
        <div class="ctl-row">${stepper("hand", c.hand)}<button class="btn primary" data-a="temperatur">Übernehmen</button></div></div>`;
    } else if (r.modus === "aus") {
      teil = `<p class="small muted m0">Der Raum ist ausgeschaltet. Stellen Sie ihn auf Plan oder Hand, um wieder zu heizen.</p>`;
    }
    const modi = MODI_GEN.filter(x => !Array.isArray(r.modi) || r.modi.includes(x.m) || x.m === r.modus);
    const ausAn = r.modus === "aus" || r.hvac_modus === "off";
    const boostLbl = r.boost_art === "preset" ? "Boost (Preset des Thermostats)" : `Boost auf ${grad(r.max_temp)}`;
    const zurueck = r.overlay_bis || r.boost_bis
      ? `<span class="grow"></span><button class="btn ${c.fokus === "zurueck" ? "vorschlag" : "ghost"}" data-a="zurueck">${r.modus === "plan" ? "Zurück zum Plan" : "Boost beenden"}</button>` : "";
    const boost = ausAn
      ? (zurueck ? `<div class="ctl-block"><div class="ctl-row wrap">${zurueck}</div></div>` : "")
      : `<div class="ctl-block ${c.fokus === "boost" || c.fokus === "zurueck" ? "hl" : ""}"><div class="lbl">${boostLbl}</div>
        <div class="ctl-row wrap">${BOOST.map(b => `<button class="btn" data-a="boost" data-d="${b}">${b} min</button>`).join("")}${zurueck}</div></div>`;
    return kopf + (z.length ? `<div class="ctl-status">${z.map(x => `<span class="pill">${esc(x)}</span>`).join("")}</div>` : "") + plan +
      `<div class="ctl-block ${c.fokus === "modus" ? "hl" : ""}"><div class="lbl">Betriebsart</div>${seg("seg-" + slug, modi.map(x => ({ v: x.m, label: x.label })), r.modus, false, c.fokus === "modus" ? c.vorschlagModus : null)}</div>
      ${teil}${boost}`;
  }

  function dauerText(min) {
    if (min === 0) return "Dauerhaft";
    if (min % 60 === 0) return `${min / 60} h`;
    return `${min} min`;
  }

  function stepper(id, wert) {
    return `<div class="stepper"><button type="button" data-step="-1" data-s="${id}" aria-label="0,5 Grad weniger">−</button><span class="v">${grad(wert)}</span><button type="button" data-step="1" data-s="${id}" aria-label="0,5 Grad mehr">+</button></div>`;
  }

  function cardEl(slug) { return st.view.querySelector(`.ctl-card[data-raum="${CSS.escape(slug)}"]`); }

  function bindCard(slug) {
    const card = cardEl(slug);
    const r = st.data.raeume[slug];
    if (!card || !r.steuerbar) return;
    const c = st.ctl[slug];
    card.querySelectorAll("[data-step]").forEach(b => b.addEventListener("click", () => {
      const feld = b.dataset.s === "hand" ? "hand" : "temp";
      c[feld] = klemme(r, c[feld] + Number(b.dataset.step) * (r.schritt || 0.5));
      c.geaendert = true;
      b.parentElement.querySelector(".v").textContent = grad(c[feld]);
    }));
    card.querySelectorAll("[data-dauer] .chip").forEach(b => b.addEventListener("click", () => {
      c.dauer = b.dataset.d === "" ? null : Number(b.dataset.d);
      c.geaendert = true;
      card.querySelectorAll("[data-dauer] .chip").forEach(x => x.classList.toggle("active", x === b));
    }));
    card.querySelectorAll(".seg button").forEach(b => b.addEventListener("click", async () => {
      const modus = b.dataset.v;
      if (modus === r.modus) return;
      if (modus === M().aus) {
        const ok = await A().modal(`${r.name || slug} ausschalten?`,
          gen() ? `<p>Klima Studio schaltet das Thermostat aus. Der Raum bleibt ausgeschaltet, bis Sie ihn wieder auf Plan oder Hand stellen. Der Heizplan wird bis dahin nicht angewendet.</p>`
            : `<p>Der Raum bleibt ausgeschaltet, bis Sie ihn wieder auf Auto oder Hand stellen. Der Heizplan wird bis dahin nicht ausgeführt.</p>`,
          [{ label: "Abbrechen", value: false, cls: "ghost" }, { label: "Ausschalten", value: true, cls: "danger" }]);
        if (!ok) return;
      }
      senden(slug, { aktion: "modus", modus }, { auto: "Auf Zeitplan umgestellt.", heat: "Auf Handbetrieb umgestellt.", off: "Raum ausgeschaltet.",
        plan: "Der Raum folgt dem Heizplan.", hand: "Auf Handbetrieb umgestellt.", aus: "Raum ausgeschaltet." }[modus]);
    }));
    card.querySelectorAll("[data-a]").forEach(b => b.addEventListener("click", () => {
      const a = b.dataset.a;
      if (a === "overlay") {
        const body = { aktion: "overlay", temperatur: c.temp, dauer: c.dauer };
        const bis = c.dauer == null ? "bis zum nächsten Planwechsel" : c.dauer === 0 ? "dauerhaft" : `für ${dauerText(c.dauer)}`;
        senden(slug, body, `${grad(c.temp)} ${bis} gesetzt.`);
      } else if (a === "temperatur") senden(slug, { aktion: "temperatur", temperatur: c.hand }, `Handwert ${grad(c.hand)} gesetzt.`);
      else if (a === "boost") senden(slug, { aktion: "boost", dauer: Number(b.dataset.d) }, `Boost für ${b.dataset.d} min gestartet.`);
      else if (a === "zurueck") senden(slug, { aktion: "zurueck" }, gen() && r.modus !== "plan" ? "Boost beendet." : "Der Raum folgt wieder dem Plan.");
    }));
  }

  async function senden(slug, body, meldung) {
    const card = cardEl(slug);
    st.busy = true; sperre(card, true);
    try {
      const { data } = await A().api("steuerung/" + encodeURIComponent(slug), { method: "POST", body });
      if (data && data.raum) st.data.raeume[slug] = data.raum;
      const c = st.ctl[slug];
      if (c) { c.geaendert = false; c.fokus = null; c.vorschlagModus = null; }
      A().toast(meldung);
    } catch (e) {
      A().toast(e.message, true);
    } finally {
      st.busy = false;
    }
    if (!aktiv()) return;
    const neu = cardEl(slug);
    if (neu) { neu.classList.remove("busy"); neu.innerHTML = cardHtml(slug, st.data.raeume[slug]); bindCard(slug); }
    if (body.aktion === "modus" && st.data.heizperiode && st.data.heizperiode.aktiv === false) zeichneHp();
  }

  window.KSTabs = window.KSTabs || {};
  window.KSTabs.steuerung = { render, leave: stopTimer };
})();
