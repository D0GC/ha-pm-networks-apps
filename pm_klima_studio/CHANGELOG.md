# Changelog

## 1.1.0 – 2026-09-26

- Neuer Reiter „Steuerung“: je Raum Betriebsart Auto, Hand oder Aus, zeitweise Temperatur (bis Planwechsel, 1 h, 3 h, dauerhaft; nur in Auto), Handwert, Boost (30, 60, 120 min) und „Zurück zum Plan“. Alle Befehle laufen über die Integration PM Klima; Klima Studio schaltet keine Thermostate selbst.
- Heizperiode und Sommerbetrieb: Klima Studio schaltet eine Freigabe-Entität (Standard `input_boolean.pm_heizperiode`), die in der Integration als Freigabe eingetragen wird. Modi Automatik, Heizperiode, Sommer. Automatik nach Tagesmittel der Außentemperatur (Heizgrenze 13 °C, Hysterese 2 K, 2 Tage bis Beginn, 3 Tage bis Ende, einstellbar). Der Helfer lässt sich per „Einrichten“ anlegen.
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
