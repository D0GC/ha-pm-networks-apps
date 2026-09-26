# PM Klima Studio

Klima Studio ergänzt die Integration **PM Klima**. Es plant, wertet aus und gibt Steuerbefehle an die Integration weiter.
Klima Studio schaltet nie selbst ein Thermostat: Thermostate und Zeitplanlogik bleiben vollständig in der Integration.

## Voraussetzungen

- Home Assistant 2026.9 oder neuer mit der Integration PM Klima.
- Heizpläne als über die Oberfläche angelegte `schedule`-Helfer (`schedule.heizplan_<raum>`) mit Blockdaten `temperatur`.
- Recorder aktiv. Für 30-Tage-Auswertungen der Feuchte-, Schimmel- und CO2-Werte werden Langzeitstatistiken
  (`state_class: measurement`) verwendet.
- Für die Sommerautomatik: ein Außentemperatursensor mit Langzeitstatistik (Standard `sensor.aussentemperatur`).
- Optional für den Klima-Coach: eine KI-Aufgabe (`ai_task`-Entität, z. B. über die Integration Anthropic oder OpenAI)
  und eine Wettervorhersage (`weather`-Entität).

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

## Steuerung

Der Reiter **Steuerung** zeigt je Raum eine Karte mit Ist- und Solltemperatur, Luftfeuchte, dem Anzeigetext der
Integration, der Plantemperatur und dem nächsten Planwechsel. Die Karten werden alle 30 Sekunden aktualisiert,
solange der Reiter geöffnet ist.

Jeder Befehl läuft ausschließlich über die Integration PM Klima: Klima Studio ruft die Dienste `pm_heizung.*` und
`climate.*` auf den Entitäten `climate.pm_<raum>` auf, nie ein Thermostat direkt. Räume ohne eine solche Entität
werden angezeigt, lassen sich aber nicht steuern. Jeder Befehl wird im Ereignisprotokoll der App vermerkt.

**Betriebsart** (je Raum):

| Betriebsart | Wirkung |
|---|---|
| Auto | Der Raum folgt seinem Heizplan. Nur in dieser Betriebsart ist eine zeitweise Abweichung möglich. |
| Hand | Der Raum hält dauerhaft einen festen Handwert, unabhängig vom Heizplan. Der Handwert lässt sich in 0,5-°C-Schritten ändern. |
| Aus | Der Raum bleibt ausgeschaltet, bis Sie ihn wieder auf Auto oder Hand stellen. Vor dem Ausschalten erscheint eine Rückfrage. |

Jeder Wechsel der Betriebsart beendet eine laufende Abweichung und einen laufenden Boost.

**Vorübergehend ändern** (nur in Auto): Temperatur in 0,5-°C-Schritten wählen, Dauer wählen und **Anwenden**.

| Dauer | Wirkung |
|---|---|
| Bis Planwechsel | Gilt bis zum nächsten Wechsel im Heizplan. |
| 1 h, 3 h | Gilt für die gewählte Zeit. |
| Dauerhaft | Gilt, bis Sie **Zurück zum Plan** wählen oder die Betriebsart wechseln. |

Warum nur in Auto: Die Integration führt eine zeitweise Abweichung nur im Modus Auto als Abweichung vom Plan.
In Hand würde sie stattdessen den Handwert dauerhaft ändern, in Aus den Raum dauerhaft auf Hand stellen.
Klima Studio lehnt den Befehl deshalb außerhalb von Auto ab und bittet Sie, den Raum zuerst auf Auto zu stellen.

**Boost:** heizt den Raum für 30, 60 oder 120 Minuten auf die Höchsttemperatur des Thermostats. Die Betriebsart
bleibt unverändert. Ein Boost wirkt auch in der Betriebsart Aus.

**Zurück zum Plan** erscheint, solange eine Abweichung oder ein Boost läuft, und beendet beides.

Ist der Hauptschalter der Integration (`switch.pm_heizung_aktiv`) aus, zeigt der Reiter eine Warnung: Die Integration
ist dann pausiert und gibt keine Befehle an die Thermostate weiter. Sperrt die Integration das Heizen, wird der Grund
ebenfalls angezeigt.

## Heizperiode und Sommerbetrieb

Oben im Reiter **Steuerung** liegt die Karte **Heizperiode**. Sie schaltet zwischen Heizperiode und Sommerbetrieb um.

### Funktionsweise

Klima Studio schaltet dafür nur eine Freigabe-Entität, standardmäßig den Helfer `input_boolean.pm_heizperiode`
(Option **heizperiode_entitaet**). Diese Entität ist in der Integration PM Klima als Freigabe eingetragen:

- **an** = Heizperiode. Die Integration darf heizen.
- **aus** = Sommer. Die Integration sperrt das Heizen. Räume in Auto ohne laufende Abweichung werden ausgeschaltet.

Die Sperre selbst setzt die Integration um. Klima Studio spricht dabei kein Thermostat an.

### Einmalige Einrichtung

1. In Klima Studio: Reiter **Steuerung** → Karte **Heizperiode** → **Einrichten**. Klima Studio legt den Helfer
   `input_boolean.pm_heizperiode` („PM Heizperiode“) an. In der Automatik steht er danach auf **an**, außer die
   Tagesmittel zeigen eindeutig das Ende der Heizperiode. Der Knopf erscheint nur, solange die Entität fehlt.
2. In Home Assistant: **Einstellungen → Geräte & Dienste → PM Klima → Konfigurieren**, bis zum Schritt **Sperre**.
3. Dort als **Freigabe-Entität** `input_boolean.pm_heizperiode` auswählen und als **Sperrwirkung** „aus“ wählen.
4. Speichern.

Achtung: Steht die Freigabe beim Verknüpfen auf aus, schaltet die Integration Räume im Modus Auto sofort ab.

Solange die Verknüpfung fehlt, hat das Umschalten keine Wirkung auf die Heizung. Klima Studio erkennt das im
Sommerbetrieb am Sperrgrund der Integration und zeigt dann einen Hinweis mit dieser Anleitung.

Als Freigabe-Entität sind auch `binary_sensor.*` und `schedule.*` zulässig. Diese kann Klima Studio nur lesen,
nicht schalten. Die Modi Heizperiode und Sommer stehen dann nicht zur Verfügung.

### Modi

| Modus | Wirkung |
|---|---|
| Automatik | Klima Studio entscheidet anhand der Außentemperatur (siehe unten). Standard. |
| Heizperiode | Die Freigabe wird sofort eingeschaltet und bei jeder Prüfung wieder eingeschaltet, falls sie aus steht. |
| Sommer | Die Freigabe wird sofort ausgeschaltet und bei jeder Prüfung wieder ausgeschaltet, falls sie an steht. Vor dem Umschalten fragt die Karte nach. |

### Regel der Automatik

Grundlage ist das Tagesmittel der Außentemperatur (Option **aussentemperatur**) der letzten sieben abgeschlossenen
Kalendertage. Es stammt aus der Langzeitstatistik, ersatzweise aus der Historie. Ein Tag zählt nur, wenn Werte aus
mindestens 12 Stunden vorliegen.

- Die Heizperiode beginnt, wenn das Tagesmittel an den letzten **2 Tagen** unter der **Heizgrenze von 13 °C** liegt.
- Sie endet, wenn das Tagesmittel an den letzten **3 Tagen** mindestens Heizgrenze plus **Hysterese von 2 K** erreicht, also 15 °C.
- Dazwischen bleibt der bisherige Zustand.
- Fehlen Tagesmittel, ändert Klima Studio nichts und nennt den Grund.

Heizgrenze (5–20 °C), Hysterese (0–5 K) und die Anzahl der Tage (je 1–7) lassen sich in der Karte einstellen.
Die Karte zeigt die Tagesmittel als Balken, die Entscheidung mit Begründung und den letzten Wechsel.

Klima Studio prüft beim Start und danach stündlich. In der Automatik wird die Freigabe geschaltet, wenn sich die
Entscheidung ändert oder Sie den Modus wechseln. Ein Schaltbefehl gilt erst als erledigt, wenn die Freigabe danach
tatsächlich den neuen Zustand hat; sonst wiederholt Klima Studio ihn bei der nächsten Prüfung. Schalten Sie den
Helfer von Hand in Home Assistant um, bleibt das bestehen, bis die Automatik zu einer neuen Entscheidung kommt.
Die Karte zeigt die Abweichung als Hinweis an.

**Winterschutz:** Steht die Freigabe länger als 6 Stunden auf aus, obwohl die Automatik Heizperiode ermittelt,
schaltet Klima Studio sie wieder ein. Jede Umschaltung wird im Ereignisprotokoll vermerkt.

### Was im Sommer weiterheizt

Die Sperre der Integration betrifft nur Räume in Auto ohne laufende Abweichung. Weiter heizen:

- Räume in der Betriebsart Hand (die Karte nennt sie im Sommerbetrieb),
- Räume mit einer laufenden Abweichung,
- ein Boost.

### Hinweise

- Die Integration sperrt auch, wenn die eingetragene Freigabe-Entität fehlt oder nicht verfügbar ist.
  Löschen Sie den Helfer deshalb nicht, solange er in der Integration eingetragen ist.
- Der Hauptschalter `switch.pm_heizung_aktiv` ist kein Sommerschalter. Ist er aus, pausiert die Integration
  vollständig und sendet keine Befehle mehr. Klima Studio zeigt ihn nur an und
  schaltet ihn nicht.
- Die Integration hat zusätzlich eine eigene Außentemperatur-Sperre, die auf den aktuellen Messwert reagiert.
  Sie bleibt unverändert und wirkt unabhängig von der Freigabe. Die Heizperiode von Klima Studio beruht dagegen
  auf Tagesmitteln über mehrere Tage und wechselt deshalb seltener.

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

## Klima-Coach

Der Reiter **Coach** gibt Hinweise zum Heizen, Lüften und Energiesparen.

**Aktuelle Hinweise** entstehen lokal aus einer Wissensdatenbank, die Teil der App ist (70 Regeln zu den Themen
Heizen, Lüften, Luftfeuchte, CO2, Heizplan, Energie, Wartung und Sommer). Die Regeln prüfen die aktuellen Werte,
die Wochenwerte und die Heizpläne jedes Raums sowie Außentemperatur, Wettervorhersage, Heizperiode und Jahreszeit.
Die Auswertung läuft vollständig in der App und verursacht keine Kosten.

- Hinweise sind nach Priorität sortiert (dringend, empfehlenswert, Hinweis). Die Begründung lässt sich aufklappen.
- **Übernehmen** öffnet je nach Hinweis den Heizplan oder die Steuerung des Raums, bei Vorschlägen zur Steuerung mit Temperatur und Dauer vorausgefüllt.
  Gespeichert oder geschaltet wird erst, wenn Sie dort selbst bestätigen.
- **Erledigt** blendet den Hinweis bis zum nächsten Tag aus, **7 Tage ausblenden** für eine Woche.

Darunter stehen die **Empfehlungen der Integration** aus `sensor.pm_klima_empfehlung`.

**KI-Coach:** **Analyse anfordern** sendet einen Lagebericht an die KI-Aufgabe aus der Option
**coach_ki_entitaet** (Standard `ai_task.claude_ai_task`) und zeigt eine Zusammenfassung und bis zu acht Tipps.

- Die Analyse läuft nur auf Knopfdruck, nie automatisch. Sie kann bis zu drei Minuten dauern.
- Es läuft höchstens eine Analyse gleichzeitig.
- **Was wird übermittelt?** zeigt vorab genau den Lagebericht, der gesendet würde.
- Vorgeschlagene Maßnahmen werden nie automatisch ausgeführt. Sie lassen sich wie lokale Hinweise übernehmen.
- Die letzten Analysen stehen im Verlauf. Kann die Antwort der KI nicht gelesen werden, wird der Rohtext angezeigt.
- Fehlt die KI-Entität, meldet Klima Studio das. Die lokalen Hinweise funktionieren auch ohne KI.

## Wochenbericht

Standardmäßig sonntags um 18:00 Uhr. Die letzten 12 Berichte liegen in `/data/berichte.json`.
Push nur, wenn unter **bericht_notify** Dienste eingetragen sind. Der Knopf „Bericht jetzt erstellen“
sendet nur dann einen Push, wenn das Häkchen gesetzt ist.

## Optionen

| Option | Standard | Bedeutung |
|---|---|---|
| bericht_tag | sonntag | Wochentag des Wochenberichts. |
| bericht_uhrzeit | 18:00 | Uhrzeit des Wochenberichts (Zeitzone von Home Assistant). |
| bericht_notify | leer | notify-Dienste für den Push des Wochenberichts. |
| raeume_override | leer | Abweichende Zuordnung je Raum, siehe „Räume“. |
| praefix_klima | `climate.pm_` | Präfix der Thermostate für die Raumerkennung. |
| praefix_heizplan | `schedule.heizplan_` | Präfix der Heizpläne. |
| empfehlung | `sensor.pm_klima_empfehlung` | Empfehlungssensor der Integration. |
| aussentemperatur | `sensor.aussentemperatur` | Außentemperatur, auch Grundlage der Heizperiode. |
| aussenfeuchte | `sensor.aussenluftfeuchte` | Außenluftfeuchte. |
| heizperiode_entitaet | `input_boolean.pm_heizperiode` | Freigabe-Entität für Heizperiode (an) und Sommer (aus). Zulässig: `input_boolean.*` (schaltbar), `binary_sensor.*`, `schedule.*` (nur lesen). |
| coach_ki_entitaet | `ai_task.claude_ai_task` | KI-Aufgabe für die Analyse im Klima-Coach. Zulässig: `ai_task.*`. |
| coach_anwesenheit | true | Personen und Anwesenheit in den Lagebericht für die KI aufnehmen. |
| wetter_entitaet | `weather.dwd_zuhause` | Wettervorhersage für den Klima-Coach. Zulässig: `weather.*`. |
| log_level | info | Protokollstufe. |

Ungültige Werte werden durch den Standard ersetzt; im Protokoll der App erscheint eine Warnung.

## Datenschutz

- Die lokalen Hinweise des Klima-Coachs entstehen ausschließlich in der App.
- Daten verlassen Home Assistant nur, wenn Sie **Analyse anfordern** wählen. Erst dann wird der Lagebericht erstellt
  und über die KI-Aufgabe von Home Assistant an den dahinterliegenden KI-Dienst übermittelt, bei Claude an Anthropic.
- Der Lagebericht enthält Raumnamen, aktuelle Messwerte und Wochenwerte (Temperatur, Feuchte, Schimmelrisiko, CO2,
  Fensterzeiten), die Heizpläne, Außenwerte und Wettervorhersage, den Stand der Heizperiode und der Integration sowie
  die Titel der lokalen Hinweise. Mit **coach_anwesenheit** (Standard: an) kommen die Namen der Personen und ihre
  Anwesenheit hinzu. Schalten Sie die Option aus, wenn Sie das nicht möchten.
- **Was wird übermittelt?** zeigt den vollständigen Lagebericht vorab.
- Der Lagebericht enthält keine Zugangsdaten, Tokens, IP-Adressen oder Geräte-IDs.
- Klima Studio speichert Einstellungen, Rückmeldungen zu Hinweisen, ein Ereignisprotokoll (höchstens 2000 Einträge)
  und die KI-Analysen lokal in der SQLite-Datenbank `/data/coach.db`. Je Analyse werden Lagebericht und Antwort
  gespeichert, höchstens die letzten 50 Analysen.

## Sicherheit

- Erreichbar ausschließlich über Home-Assistant-Ingress (Anfragen nur von 172.30.32.2).
- Der Supervisor-Watchdog prüft `/api/health`.
- Das Supervisor-Token wird nur aus der Umgebung gelesen und nicht gespeichert.
- Keine externen Server für die Oberfläche: Schrift, Symbole und Diagramme liegen im Image.
  Einzige Ausnahme ist die KI-Analyse auf Knopfdruck (siehe „Datenschutz“).

## Lizenzen

Montserrat: SIL Open Font License 1.1 (`static/fonts/OFL.txt`). Diagramme sind eine eigene SVG-Implementierung.
