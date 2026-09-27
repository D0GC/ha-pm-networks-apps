# Changelog

## 1.2.0 – 2026-09-27

- Neue Option `betriebsart`: `pm_networks` (Standard) mit der Integration PM Klima, `generisch` für beliebige Thermostate ohne PM Klima. Im Modus `pm_networks` bleibt das Verhalten von 1.1.1 unverändert. Klima Studio erkennt, ob PM Klima geladen ist, und zeigt einen Hinweis, wenn die Betriebsart nicht passt; umgeschaltet wird nie automatisch.
- Generisch, Räume: aus den Bereichen von Home Assistant (Bereich der Entität vor dem des Geräts), Thermostate ohne Bereich als eigener Raum. Sensoren des Bereichs nach Geräteklasse. Mehrere Thermostate im Bereich: gesteuert wird eines, die übrigen werden genannt. Better Thermostat hat Vorrang vor seinen Quellgeräten. Entitäten von PM Klima werden nie als Thermostat verwendet.
- Generisch, Steuerung: Modi Plan, Hand und Aus über die Standard-Dienste `climate.*`. Zeitweise Temperatur (nur Plan), Handwert (nur Hand), Boost über das Preset `boost` oder höchstens 25 °C mit Rück-Timer, „Zurück zum Plan“. Timer und Modi liegen in `/data/coach.db` und überstehen einen Neustart. Thermostate mit Temperaturbereich werden nur angezeigt.
- Generisch, Plananwendung: Neue Option `plan_anwenden` (Standard an). Klima Studio stellt Räume im Modus Plan minütlich nach dem Heizplan ein, außerhalb der Blöcke auf die Absenktemperatur (neue Option `absenktemperatur`, Standard 17 °C, je Raum Override `absenk`).
- Generisch, Heizperiode: gleiche Automatik wie bisher, Zustand intern. Neue Option `sommer_aktion`: `plan_pausieren_und_aus` (Standard) pausiert die Heizpläne und schaltet Räume im Modus Plan aus, `plan_pausieren` pausiert nur. Zu Beginn der Heizperiode werden die Räume wieder eingeschaltet. `heizperiode_entitaet` wird generisch nur als optionaler Spiegel geschaltet.
- Sicherheitsmechanismen im generischen Modus: neue Räume starten im Modus Hand; geschrieben wird nur auf Thermostate im Zustand `heat` und nur bei geändertem Soll; Toleranz von halber Schrittweite, mindestens 0,25 °C; Handänderungen am Gerät gelten bis zum nächsten Planwechsel; kein Erhöhen bei offenem Fenster; Pause bei nicht verfügbarem Thermostat und Backoff nach Fehlern (bis 60 Minuten); Hinweis bei nicht übernommenem Sollwert.
- Klima-Coach nach Betriebsart: Regeln gekennzeichnet (70 mit PM Networks, 61 generisch), generische Gegenstücke für Plan, Hand und Aus, z. B. „Heizplan noch nicht aktiv“. Eigene Anweisungen an die KI je Betriebsart.
- Oberfläche: blendet Freigabe, Integrationsstatus, Hinweise der Integration und Schimmelrisiko nach Betriebsart ein. Übersicht generisch mit Karte „Betrieb“ und Status je Raum. Raumkarten generisch mit Plan-Soll, nächstem Wechsel und Hinweisen.
- Fußzeile mit „© 2026 PM Networks“.

## 1.1.1 – 2026-09-27

- Behoben: Die Reiter „Steuerung“, „Coach“ und die übrigen Reiter wurden nach einer langsam ladenden Übersicht von dieser überschrieben, man landete immer wieder auf der Übersicht. Ein veralteter Ladevorgang schreibt nicht mehr in die sichtbare Ansicht.
- Oberfläche wird über versionierte Pfade (`static/<version>/…`) und mit `Cache-Control: no-cache` ausgeliefert, damit nach einem Update keine zwischengespeicherte ältere Oberfläche läuft.

## 1.1.0 – 2026-09-26

- Neuer Reiter „Steuerung“: je Raum Betriebsart Auto, Hand oder Aus, zeitweise Temperatur (bis Planwechsel, 1 h, 3 h, dauerhaft; nur in Auto), Handwert, Boost (30, 60, 120 min) und „Zurück zum Plan“. Alle Befehle laufen über die Integration PM Klima; Klima Studio schaltet keine Thermostate selbst.
- Heizperiode und Sommerbetrieb: Klima Studio schaltet eine Freigabe-Entität (Standard `input_boolean.pm_heizperiode`), die in der Integration als Freigabe eingetragen wird. Modi Automatik, Heizperiode, Sommer. Automatik nach Tagesmittel der Außentemperatur (Heizgrenze 13 °C, Hysterese 2 K, 2 Tage bis Beginn, 3 Tage bis Ende, einstellbar). Der Helfer lässt sich per „Einrichten“ anlegen; er steht danach auf an, außer die Tagesmittel zeigen eindeutig das Ende der Heizperiode. Feste Modi werden bei jeder Prüfung durchgesetzt, das Umschalten auf Sommer erfordert eine Bestätigung. Winterschutz: Steht die Freigabe in der Automatik länger als 6 Stunden auf aus, obwohl Heizperiode ermittelt ist, wird sie wieder eingeschaltet.
- Neuer Reiter „Coach“: lokale Hinweise aus einer Wissensdatenbank mit 70 Regeln, Rückmeldung „Erledigt“ und „7 Tage ausblenden“, dazu die Empfehlungen der Integration.
- KI-Analyse auf Knopfdruck über eine `ai_task`-Entität (Standard `ai_task.claude_ai_task`), mit Vorschau des übermittelten Lageberichts und Verlauf. Vorgeschlagene Maßnahmen werden nie automatisch ausgeführt.
- Neue Optionen `heizperiode_entitaet`, `coach_ki_entitaet`, `coach_anwesenheit`, `wetter_entitaet`.
- Neue SQLite-Datenbank `/data/coach.db` für Einstellungen, Rückmeldungen, KI-Analysen (höchstens 50) und ein Ereignisprotokoll (höchstens 2000 Einträge).

## 1.0.1 – 2026-09-25

- Heizplan: Beim Speichern müssen alle sieben Tage übermittelt werden. Ein vollständig leerer Plan wird nur nach ausdrücklicher Bestätigung (Rückfrage in der Oberfläche) geschrieben.
- Heizplan: Sekunden in Zeitangaben (z. B. 06:00:30) bleiben erhalten.
- Heizplan: Speicher-ID wird ausschließlich aus der Entitätsregistrierung gelesen. Ist sie nicht lesbar, wird der Vorgang abgebrochen statt eine ID anzunehmen. Zuordnungs-Cache begrenzt und bei Raumaktualisierung geleert.
- Raum-Overrides: je Feld nur passende Domänen (Heizplan nur `schedule.*`).
- WebSocket: offene Anfragen je Verbindung verwaltet; ein Neuaufbau bricht keine neuen Anfragen mehr ab.
- Zeitzone wird beim Start im Hintergrund erneut geladen, falls Home Assistant noch nicht bereit ist; der Berichtstermin wird danach neu berechnet.
- Historien-Cache auf 800 Einträge erweitert.
- Oberfläche: alle dynamischen Texte konsequent maskiert, auch in Diagramm-Tooltips und Legenden.
- `build.yaml` entfernt, das Base-Image steht als Multi-Arch-Tag im Dockerfile. Watchdog auf `/api/health`.

## 1.0.0 – 2026-09-25

- Erste Version.
- Heizplan-Editor für die `schedule`-Helfer der Integration PM Klima: Wochenansicht, Blöcke ziehen, verschieben, verlängern, Temperatur in 0,5-°C-Schritten, Tag kopieren (Werktage, Wochenende), Änderungsübersicht vor dem Speichern, Warnung bei Fremdänderung.
- Auswertungen für 24 Stunden, 7 und 30 Tage: Heizstunden, Soll und Ist, Luftfeuchte und Schimmelrisiko (70/80 %), CO2 (1000/1400 ppm), Fensterzeiten, Lüftungserfolg.
- Wochenbericht mit Archiv (12 Berichte) und optionalem Push.
