# Changelog

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
