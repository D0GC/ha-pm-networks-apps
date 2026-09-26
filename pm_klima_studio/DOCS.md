# PM Klima Studio

Klima Studio ergänzt die Integration **PM Klima**. Es plant und wertet aus, steuert aber nichts selbst:
Thermostate und Zeitplanlogik bleiben vollständig in der Integration.

## Voraussetzungen

- Home Assistant 2026.9 oder neuer mit der Integration PM Klima.
- Heizpläne als über die Oberfläche angelegte `schedule`-Helfer (`schedule.heizplan_<raum>`) mit Blockdaten `temperatur`.
- Recorder aktiv. Für 30-Tage-Auswertungen der Feuchte-, Schimmel- und CO2-Werte werden Langzeitstatistiken
  (`state_class: measurement`) verwendet.

## Räume

Räume werden automatisch erkannt:

| Rolle | Erkennung |
|---|---|
| Thermostat | `climate.pm_<raum>` (Präfix einstellbar) |
| Heizplan | `schedule.heizplan_<raum>` (Präfix einstellbar) |
| Schimmelrisiko, Luftqualität, Lüften | `sensor.pm_<raum>_schimmelrisiko`, `sensor.pm_<raum>_luftqualitaet`, `binary_sensor.pm_<raum>_lueften_empfohlen` |
| Fenster | `binary_sensor.pm_<raum>_fenster_offen`, sonst Fenstersensor mit Raumnamen |
| Luftfeuchte | Feuchtesensor mit Raumnamen; bei mehreren der Wert, den das Thermostat meldet |
| CO2 | CO2-Sensor mit Raumnamen, sonst Abgleich mit dem Attribut `co2` der Luftqualität |

Abweichungen lassen sich unter **raeume_override** festlegen, zum Beispiel:

```yaml
raeume_override:
  - raum: wohnzimmer
    co2: sensor.wetterstation_kohlendioxid
  - raum: badezimmer
    name: Bad
  - raum: schlafzimmer
    ausblenden: true
```

## Heizplan-Editor

- **Maus:** auf freier Fläche ziehen legt einen Block an. **Touch:** freie Fläche antippen.
- Blöcke verschieben (auch auf andere Tage), an Ober- oder Unterkante verlängern, antippen zum Bearbeiten.
- Temperatur 5–25 °C in 0,5-Schritten. Außerhalb der Blöcke gilt die Grundtemperatur der Integration.
- Tagesname antippen: Tag auf Werktage, Wochenende oder einzelne Tage kopieren, Tag leeren.
- **Speichern** zeigt zuerst alle Änderungen. Unmittelbar vor dem Schreiben wird der Helfer neu gelesen;
  wurde er inzwischen anderweitig geändert, erscheint eine Warnung mit den Unterschieden.
- Name und Symbol des Helfers bleiben unverändert. Weitere Blockdaten neben `temperatur` bleiben erhalten.
- In YAML angelegte Zeitpläne sind nicht bearbeitbar.
- Ein vollständig leerer Plan (kein Block an keinem Tag) wird nur nach einer ausdrücklichen Rückfrage gespeichert.
- Sekunden in vorhandenen Zeitangaben (z. B. 06:00:30) bleiben erhalten, solange der Block nicht verändert wird.
- Die Zuordnung Entität → Helfer wird aus der Entitätsregistrierung gelesen. Ist sie nicht lesbar, bricht Klima Studio ab.

Geschrieben wird über die WebSocket-API `schedule/update`. Home Assistant ersetzt dabei den vollständigen
Eintrag; Klima Studio sendet deshalb stets Name, Symbol und alle sieben Tage mit.

## Auswertungen

Zeiträume 24 Stunden, 7 und 30 Tage. Heizstunden (Zeit mit `hvac_action: heating`), Soll/Ist und Fensterzeiten
stammen aus der Recorder-Historie (Standard 10 Tage), längere Zeiträume werden entsprechend gekennzeichnet.
Lüftungserfolg: Abfall von Feuchte und CO2 in den 30 Minuten nach dem Öffnen eines Fensters.

## Wochenbericht

Standardmäßig sonntags um 18:00 Uhr. Die letzten 12 Berichte liegen in `/data/berichte.json`.
Push nur, wenn unter **bericht_notify** Dienste eingetragen sind. Der Knopf „Bericht jetzt erstellen“
sendet nur dann einen Push, wenn das Häkchen gesetzt ist.

## Sicherheit

- Erreichbar ausschließlich über Home-Assistant-Ingress (Anfragen nur von 172.30.32.2).
- Der Supervisor-Watchdog prüft `/api/health`.
- Das Supervisor-Token wird nur aus der Umgebung gelesen und nicht gespeichert.
- Keine externen Server: Schrift, Symbole und Diagramme liegen im Image.

## Lizenzen

Montserrat: SIL Open Font License 1.1 (`static/fonts/OFL.txt`). Diagramme sind eine eigene SVG-Implementierung.
