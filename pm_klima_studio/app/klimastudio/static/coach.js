/* PM Klima Studio – Reiter „Coach“: lokale Hinweise, Empfehlungen der Integration und KI-Auswertung. */
(function () {
  "use strict";

  const THEMA = { heizen: "Heizen", lueften: "Lüften", feuchte: "Feuchte", co2: "CO2", heizplan: "Heizplan", energie: "Energie", wartung: "Wartung", sommer: "Sommer", ki: "KI" };
  const PRIO = { 1: "dringend", 2: "wichtig", 3: "Hinweis" };

  const st = {
    data: null,       // GET /api/coach
    verlauf: [],      // GET /api/coach/ki
    lauf: null,       // laufende Anfrage (Promise)
    ergebnis: null,   // angezeigter KI-Lauf
    ergebnisFehler: null,
    hinweis: null,    // z. B. „Analyse läuft bereits“ (409 laeuft)
    view: null,       // Container dieses Reiters (von app.js je render()-Lauf neu erzeugt)
  };

  const A = () => window.KSApp;
  const esc = s => A().esc(s);
  const aktiv = () => A().S.tab === "coach";
  // Elemente nur im Container des jüngsten Laufs suchen (ein abgehängter Container liefert nichts Sichtbares).
  const $v = sel => st.view ? st.view.querySelector(sel) : null;
  const fmtIso = iso => { const t = Date.parse(iso); return isNaN(t) ? esc(iso || "–") : esc(A().fmtDate(t)); };
  const raumName = slug => slug ? A().roomName(slug) : null;
  const kiEntitaet = () => { const i = A().S.info; return (i && i.coach && i.coach.ki_entitaet) || null; };

  async function render(main) {
    st.view = main;
    if (!st.data || !main.hasChildNodes()) main.innerHTML = `<div class="bar"><h2 class="grow">Klima-Coach</h2></div><div class="loading">Hinweise werden ermittelt …</div>`;
    const [c, v] = await Promise.all([A().api("coach"), A().api("coach/ki").catch(() => ({ data: [] }))]);
    if (!aktiv() || st.view !== main || !main.isConnected) return;
    st.data = c.data;
    st.verlauf = Array.isArray(v.data) ? v.data : [];
    zeichnen(main);
  }

  function zeichnen(main) {
    const d = st.data;
    const tipps = (d.tipps || []).slice().sort((a, b) => (a.prioritaet || 3) - (b.prioritaet || 3));
    const integ = d.integration || [];
    main.innerHTML = `<div class="bar"><h2 class="grow">Klima-Coach</h2>
        <span class="small muted">${d.saison === "sommer" ? "Sommer" : "Heizperiode"} · Wissensstand ${esc(d.wissen_version || "–")}</span>
        <button class="btn ghost" id="co-reload">Aktualisieren</button></div>
      <section class="co-sec"><h3 class="sec-title">Aktuelle Hinweise</h3>
        <p class="small muted">Lokal ausgewertet aus den Daten Ihres Zuhauses. Es werden keine Daten übertragen.</p>
        <div class="tip-list" id="co-tipps">${tipps.length ? tipps.map(t => tippHtml(t, true)).join("")
          : '<div class="empty glass card">Derzeit liegen keine Hinweise vor.</div>'}</div></section>
      <section class="co-sec"><h3 class="sec-title">Hinweise der Integration</h3>
        <div class="card glass">${integ.length ? `<ul class="reco">${integ.map(integHtml).join("")}</ul>`
          : '<p class="muted m0">Die Integration meldet derzeit keine Empfehlungen.</p>'}</div></section>
      <section class="co-sec"><h3 class="sec-title">KI-Coach</h3>
        <div class="card glass ki-card">
          <p class="m0">Auf Wunsch wertet ein KI-Dienst den aktuellen Lagebericht aus und ergänzt die lokalen Hinweise. Vorschläge werden nie automatisch ausgeführt.</p>
          <p class="small muted m0">KI-Dienst: ${esc(kiEntitaet() || "nicht festgelegt")}</p>
          <div class="ki-actions"><button class="linkbtn" id="ki-was">Was wird übermittelt?</button><span class="grow"></span>
            <button class="btn primary" id="ki-start">Analyse anfordern</button></div>
          <div id="ki-zustand"></div>
        </div>
        <div id="ki-ergebnis"></div>
        <div id="ki-verlauf"></div>
      </section>`;
    main.querySelector("#co-reload").addEventListener("click", async ev => {
      const btn = ev.currentTarget;
      btn.disabled = true;
      try { await render(main); } catch (e) { if (main.isConnected) { A().toast(e.message, true); btn.disabled = false; } }
    });
    bindTipps(main.querySelector("#co-tipps"), tipps);
    main.querySelector("#ki-was").addEventListener("click", lagebericht);
    main.querySelector("#ki-start").addEventListener("click", analyse);
    zeichneKi();
  }

  function tippHtml(t, lokal) {
    const p = [1, 2, 3].includes(t.prioritaet) ? t.prioritaet : 3;
    const raum = t.raum_name || raumName(t.raum);
    const mt = t.massnahme && t.massnahme.typ;
    const uebernehmen = mt === "heizplan" || mt === "steuerung";
    const meta = [PRIO[p], THEMA[t.thema] || t.thema, raum].filter(Boolean);
    return `<article class="tip glass p${p}" data-id="${esc(t.id || "")}">
      <div class="tip-meta">${meta.map(x => `<span>${esc(x)}</span>`).join("")}</div>
      <h4>${esc(t.titel)}</h4>
      <p class="m0">${esc(t.text)}</p>
      ${t.begruendung ? `<details><summary>Begründung</summary><p class="small muted m0">${esc(t.begruendung)}</p></details>` : ""}
      ${massnahmeText(t.massnahme)}
      <div class="tip-actions">
        ${uebernehmen ? `<button class="btn primary" data-act="uebernehmen">Übernehmen</button>` : ""}
        ${lokal ? `<button class="btn" data-act="erledigt">Erledigt</button><button class="btn ghost" data-act="ausblenden">7 Tage ausblenden</button>` : ""}
      </div></article>`;
  }

  const MODUS = { auto: "Auto", heat: "Hand", off: "Aus" };

  function massnahmeText(m) {
    if (!m || m.typ !== "steuerung") return "";
    if (m.aktion === "modus") return MODUS[m.modus] ? `<div class="small muted">Vorschlag: Betriebsart ${esc(MODUS[m.modus])}</div>` : "";
    if (m.aktion === "zurueck") return `<div class="small muted">Vorschlag: zurück zum Plan</div>`;
    const teile = m.aktion === "boost" ? ["Boost"] : [];
    if (typeof m.temperatur === "number") teile.push(`${A().num(m.temperatur)} °C`);
    if (typeof m.dauer === "number") teile.push(m.dauer === 0 ? "dauerhaft" : m.dauer % 60 === 0 ? `${m.dauer / 60} h` : `${m.dauer} min`);
    return teile.length ? `<div class="small muted">Vorschlag: ${esc(teile.join(", "))}</div>` : "";
  }

  function integHtml(e) {
    const p = [1, 2, 3].includes(e.prioritaet) ? e.prioritaet : null;
    const meta = [p ? PRIO[p] : null, THEMA[e.thema] || e.thema, raumName(e.raum)].filter(Boolean);
    return `<li><div>${esc(e.text)}</div><div class="small muted">${esc(meta.join(" · "))}${e.seit ? ` · seit ${fmtIso(e.seit)}` : ""}</div></li>`;
  }

  function bindTipps(box, tipps) {
    if (!box) return;
    box.querySelectorAll(".tip").forEach((el, i) => {
      const t = tipps[i];
      el.querySelectorAll("[data-act]").forEach(b => b.addEventListener("click", () => {
        const act = b.dataset.act;
        if (act === "uebernehmen") uebernehmen(t);
        else rueckmeldung(t, act, el);
      }));
    });
  }

  function uebernehmen(t) {
    const m = t.massnahme || {};
    const raum = t.raum;
    if (m.typ === "heizplan") {
      const info = A().rooms().find(r => r.raum === raum);
      if (raum && info && !info.schedule) { A().toast("Für diesen Raum ist kein Heizplan hinterlegt.", true); return; }
      location.hash = raum ? `#/heizplan/${encodeURIComponent(raum)}` : "#/heizplan";
    } else if (m.typ === "steuerung") {
      A().prefill = raum ? { raum, temperatur: m.temperatur, dauer: m.dauer, aktion: m.aktion, modus: m.modus } : null;
      location.hash = raum ? `#/steuerung/${encodeURIComponent(raum)}` : "#/steuerung";
    }
  }

  async function rueckmeldung(t, aktion, el) {
    const body = aktion === "ausblenden" ? { id: t.id, aktion, tage: 7 } : { id: t.id, aktion };
    el.querySelectorAll("button").forEach(b => { b.disabled = true; });
    try {
      await A().api("coach/rueckmeldung", { method: "POST", body });
      st.data.tipps = (st.data.tipps || []).filter(x => x.id !== t.id);
      el.remove();
      const box = $v("#co-tipps");
      if (box && !box.querySelector(".tip")) box.innerHTML = '<div class="empty glass card">Derzeit liegen keine Hinweise vor.</div>';
      A().toast(aktion === "ausblenden" ? "Der Hinweis wird 7 Tage lang ausgeblendet." : "Als erledigt vermerkt.");
    } catch (e) {
      A().toast(e.message, true);
      el.querySelectorAll("button").forEach(b => { b.disabled = false; });
    }
  }

  // ------------------------------------------------------------------ KI
  async function lagebericht() {
    let d;
    try { d = (await A().api("coach/lagebericht")).data; } catch (e) { A().toast(e.message, true); return; }
    const json = JSON.stringify(d.lage, null, 2);
    await A().modal("Was wird übermittelt?",
      `<p class="small">Der Lagebericht wird zur Auswertung an den KI-Dienst <b>${esc(d.entitaet || "–")}</b> übermittelt.
        Er enthält keine Zugangsdaten, IP-Adressen oder Geräte-IDs.</p>
      <p class="small muted">Umfang: ${esc(A().num(d.zeichen, 0))} Zeichen · Anwesenheit ${d.anwesenheit ? "enthalten" : "nicht enthalten"}</p>
      <pre class="json">${esc(json)}</pre>`,
      [{ label: "Schließen", value: null, cls: "primary" }], { wide: true });
  }

  async function analyse() {
    if (st.lauf) return;
    st.ergebnisFehler = null;
    st.hinweis = null;
    // Kein clientseitiges Zeitlimit: der Server wartet bis zu 180 s auf den KI-Dienst.
    st.lauf = A().api("coach/ki", { method: "POST", body: {} });
    zeichneKi();
    try {
      const { data } = await st.lauf;
      st.ergebnis = data;
      if (data && data.fehler) A().toast("Die Auswertung konnte nicht vollständig verarbeitet werden.", true);
      else A().toast("Die Analyse liegt vor.");
    } catch (e) {
      if (e.status === 409 && e.data && e.data.code === "laeuft") {
        // Ein anderer Lauf (z. B. aus einem zweiten Fenster) ist noch aktiv
        st.ergebnisFehler = null;
        st.hinweis = e.message;
        A().toast(e.message);
      } else {
        st.ergebnisFehler = e.message;
        A().toast(e.message, true);
      }
    } finally {
      st.lauf = null;
    }
    // Auch fehlgeschlagene Läufe werden serverseitig gespeichert
    try { st.verlauf = (await A().api("coach/ki")).data || []; } catch (_) { /* Verlauf bleibt */ }
    if (aktiv()) zeichneKi();
  }

  function zeichneKi() {
    const z = $v("#ki-zustand");
    if (!z) return;
    const btn = $v("#ki-start");
    btn.disabled = !!st.lauf;
    btn.textContent = st.lauf ? "Analyse läuft …" : "Analyse anfordern";
    z.innerHTML = st.lauf ? `<div class="infobox small"><span class="spinner" aria-hidden="true"></span>Die Auswertung kann bis zu drei Minuten dauern. Sie können den Reiter währenddessen verlassen.</div>`
      : st.ergebnisFehler ? `<div class="errbox small">${esc(st.ergebnisFehler)}</div>`
      : st.hinweis ? `<div class="infobox small">${esc(st.hinweis)}</div>` : "";
    zeichneErgebnis();
    zeichneVerlauf();
  }

  function zeichneErgebnis() {
    const box = $v("#ki-ergebnis");
    const r = st.ergebnis;
    if (!box) return;
    if (!r) { box.innerHTML = ""; return; }
    const tipps = (r.tipps || []).slice().sort((a, b) => (a.prioritaet || 3) - (b.prioritaet || 3));
    const kopf = `<div class="small muted">${fmtIso(r.erstellt)} · ${esc(r.entitaet || "")}${r.dauer_s != null ? ` · ${esc(A().num(r.dauer_s, 0))} s` : ""}</div>`;
    box.innerHTML = `<div class="card glass ki-result">
        <div class="kicker">Ergebnis der KI-Auswertung</div>${kopf}
        ${r.fehler ? `<div class="errbox small">${esc(r.fehler)}</div>` : ""}
        ${r.zusammenfassung ? `<p class="ki-sum">${esc(r.zusammenfassung)}</p>` : ""}
        ${r.roh && !tipps.length ? `<p class="small muted m0">Antwort des KI-Dienstes im Rohformat:</p><pre class="json">${esc(typeof r.roh === "string" ? r.roh : JSON.stringify(r.roh, null, 2))}</pre>` : ""}
      </div>
      ${tipps.length ? `<div class="tip-list" id="ki-tipps">${tipps.map(t => tippHtml(Object.assign({}, t, { raum_name: raumName(t.raum) }), false)).join("")}</div>` : ""}`;
    bindTipps($v("#ki-tipps"), tipps);
  }

  function zeichneVerlauf() {
    const box = $v("#ki-verlauf");
    if (!box) return;
    const v = st.verlauf || [];
    if (!v.length) { box.innerHTML = ""; return; }
    box.innerHTML = `<h3 class="sec-title">Frühere Auswertungen</h3><nav class="rep-list glass">${v.map(x =>
      `<button class="rep-item ${st.ergebnis && st.ergebnis.id === x.id ? "active" : ""}" data-id="${esc(x.id)}">
        <div class="d">${fmtIso(x.erstellt)}</div>
        <div class="small muted ellip">${x.fehler ? "Fehler: " + esc(x.fehler) : `${esc(A().num(x.anzahl_tipps || 0, 0))} Tipps · ${esc(x.zusammenfassung || "")}`}</div></button>`).join("")}</nav>`;
    box.querySelectorAll(".rep-item").forEach(b => b.addEventListener("click", async () => {
      try {
        const { data } = await A().api("coach/ki/" + encodeURIComponent(b.dataset.id));
        st.ergebnis = data; st.ergebnisFehler = null; st.hinweis = null;
        if (!aktiv()) return;
        zeichneKi();
        const e = $v("#ki-ergebnis");
        if (e) e.scrollIntoView({ block: "start", behavior: "smooth" });
      } catch (e) { A().toast(e.message, true); }
    }));
  }

  window.KSTabs = window.KSTabs || {};
  window.KSTabs.coach = { render };
})();
