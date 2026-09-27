# PM Klima Studio

![PM Klima Studio auf Desktop und Smartphone](https://raw.githubusercontent.com/D0GC/ha-pm-networks-apps/main/docs/images/titelbild.webp)

Klima Studio plant, steuert und wertet das Raumklima in Home Assistant aus: Heizpläne als Wochenansicht,
Steuerung je Raum, Heizperiode und Sommerbetrieb, Auswertungen, ein lokaler Klima-Coach mit optionaler KI-Analyse
und ein Wochenbericht.

Die ausführliche Anleitung mit Screenshots finden Sie auch auf GitHub:
<https://github.com/D0GC/ha-pm-networks-apps/blob/main/docs/ANLEITUNG.md>

## Betriebsarten im Überblick

Klima Studio kennt zwei Betriebsarten. Sie wählen sie einmal in den App-Optionen (Option `betriebsart`).

| | **PM Networks** (`pm_networks`, Standard) | **Generisch** (`generisch`) |
|---|---|---|
| Für wen | Installationen mit der Integration **PM Klima** (`pm_heizung`) | Installationen ohne PM Klima, beliebige Thermostate |
| Räume | aus `climate.pm_<raum>` | aus den Bereichen von Home Assistant |
| Wer schaltet die Thermostate | die Integration PM Klima | Klima Studio selbst über `climate.*`-Dienste |
| Heizpläne ausführen | die Integration | Klima Studio (Option `plan_anwenden`) |
| Raum-Betriebsarten | Auto, Hand, Aus | Plan, Hand, Aus |
| Heizperiode | schaltet eine Freigabe-Entität, die Integration sperrt | interner Zustand, Klima Studio pausiert die Heizpläne |
| Schimmelrisiko, Luftqualität | ja, aus den Sensoren der Integration | nein (geplant für 1.3.0) |
| Hinweise der Integration | ja (`sensor.pm_klima_empfehlung`) | nein |
| Klima-Coach | 70 Regeln | 61 Regeln, eigene Regeln für Plan/Hand/Aus |
| Heizplan-Editor, Auswertungen, KI, Wochenbericht | ja | ja |

Wenn Sie PM Klima nicht verwenden, stellen Sie `betriebsart: generisch` ein. Klima Studio schaltet nie
automatisch zwischen den Betriebsarten um. Es zeigt aber oben in der Oberfläche einen Hinweis, wenn die gewählte
Betriebsart nicht zur Installation passt.

## Voraussetzungen

Für beide Betriebsarten:

- Home Assistant OS oder Home Assistant Supervised (Apps, ehemals Add-ons), Version 2026.9 oder neuer.
- Architektur `aarch64` oder `amd64`.
- Recorder aktiv. Auswertungen über 30 Tage nutzen Langzeitstatistiken (`state_class: measurement`).
- Für die Heizperiode: ein Außentemperatursensor mit Langzeitstatistik (Standard `sensor.aussentemperatur`).
- Optional für den Klima-Coach: eine KI-Aufgabe (`ai_task`-Entität, z. B. über die Integration Anthropic oder
  OpenAI) und eine Wettervorhersage (`weather`-Entität).

Nur **PM Networks**:

- Die Integration PM Klima ist eingerichtet und geladen.
- Thermostate der Integration als `climate.pm_<raum>`.
- Heizpläne als über die Oberfläche angelegte `schedule`-Helfer `schedule.heizplan_<raum>` mit Blockdaten `temperatur`.

Nur **Generisch**:

- Thermostate als `climate`-Entitäten, die eine Solltemperatur unterstützen.
- Die Thermostate sind Bereichen (Räumen) zugeordnet, direkt oder über ihr Gerät. Ohne Bereich bildet jedes
  Thermostat einen eigenen Raum.
- Optional je Raum ein `schedule`-Helfer `schedule.heizplan_<bereich>` (siehe [Heizpläne](#heizpläne)).

## Installation

1. **Einstellungen → Apps → App-Store** öffnen.
2. Menü (drei Punkte) → **Repositories**.
3. `https://github.com/D0GC/ha-pm-networks-apps` eintragen und **Hinzufügen** wählen.
4. **PM Klima Studio** auswählen und **Installieren**. Das Image wird lokal gebaut. Auf einem Raspberry Pi dauert
   das einige Minuten.
5. Im Reiter **Konfiguration** die Betriebsart wählen (siehe unten) und speichern.
6. Im Reiter **Info** die App starten. Optional **In Seitenleiste anzeigen** einschalten.

Klima Studio erscheint danach in der Seitenleiste als **Klima Studio**. Die Oberfläche ist nur für Administratoren
sichtbar. Updates erscheinen wie gewohnt unter **Einstellungen → System → Updates**.

## Betriebsart wählen

Öffnen Sie **Einstellungen → Apps → PM Klima Studio → Konfiguration**.

- Mit der Integration PM Klima: `betriebsart` auf `pm_networks` lassen (Standard).
- Ohne PM Klima: `betriebsart` auf `generisch` stellen.

Speichern Sie und starten Sie die App neu. Optionen, die nicht gesetzt sind, blendet Home Assistant unter
**Nicht verwendete optionale Konfigurationsoptionen anzeigen** ein. `raeume_override` bearbeiten Sie am
einfachsten über das Menü **Als YAML bearbeiten**.

Passt die Betriebsart nicht zur Installation, erscheint oben in Klima Studio ein Hinweis:

- `pm_networks` gewählt, PM Klima aber nicht geladen: Steuerung, Freigabe der Heizperiode und die Hinweise der
  Integration stehen nicht zur Verfügung. Der Hinweis empfiehlt die Betriebsart `generisch`.
- `generisch` gewählt, PM Klima aber geladen: Der Hinweis nennt die Funktionen, die mit `pm_networks` möglich wären.

Der Hinweis lässt sich bis zum nächsten Neuladen ausblenden.

## Erste Schritte

### Mit PM Networks

1. **Übersicht** öffnen und prüfen, ob alle Räume erscheinen. Fehlt ein Raum, siehe [Räume](#räume).
2. **Steuerung → Heizperiode → Einrichten** wählen und die Freigabe einmalig in PM Klima eintragen
   (siehe [Heizperiode mit PM Networks](#heizperiode-mit-pm-networks)).
3. Im Reiter **Heizplan** die Pläne der Räume prüfen und bei Bedarf anpassen.
4. Optional: `bericht_notify` für den Wochenbericht und `coach_ki_entitaet` für die KI-Analyse eintragen.

### Generisch

1. In Home Assistant jedes Thermostat einem Bereich zuordnen (**Einstellungen → Bereiche**).
2. **Übersicht** öffnen und prüfen, ob alle Räume erscheinen. Neue Räume stehen im Modus Hand. Klima Studio
   verändert noch kein Thermostat.
3. Je Raum einen Zeitplan-Helfer `schedule.heizplan_<bereich>` anlegen (siehe [Heizpläne](#heizpläne)).
4. Im Reiter **Heizplan** Blöcke mit Temperaturen eintragen und speichern.
5. Absenktemperatur prüfen: Option `absenktemperatur`, je Raum `absenk` in `raeume_override`.
6. Im Reiter **Steuerung** die Räume auf **Plan** stellen. Klima Studio wendet den Heizplan danach an.
7. Die Karte **Heizperiode** prüfen und bei Bedarf `sommer_aktion` anpassen.

## Optionen

| Option | Standard | Betriebsart | Bedeutung |
|---|---|---|---|
| `betriebsart` | `pm_networks` | beide | `pm_networks` mit der Integration PM Klima, `generisch` für beliebige Thermostate. |
| `plan_anwenden` | `true` | generisch | Klima Studio stellt die Thermostate nach den Heizplänen ein. |
| `sommer_aktion` | `plan_pausieren_und_aus` | generisch | Verhalten im Sommer: `plan_pausieren_und_aus` pausiert die Heizpläne und schaltet Räume im Modus Plan aus. `plan_pausieren` pausiert nur die Heizpläne. |
| `absenktemperatur` | `17` | generisch | Solltemperatur außerhalb der Heizplan-Blöcke in °C, 5 bis 25 in 0,5er-Schritten. Je Raum über `absenk` änderbar. |
| `bericht_tag` | `sonntag` | beide | Wochentag des Wochenberichts (`montag` bis `sonntag`). |
| `bericht_uhrzeit` | `18:00` | beide | Uhrzeit des Wochenberichts im Format HH:MM, Zeitzone von Home Assistant. |
| `bericht_notify` | leer | beide | notify-Dienste für den Push des Wochenberichts, z. B. `notify.mobile_app_telefon`. |
| `raeume_override` | leer | beide | Abweichende Zuordnung je Raum, siehe [Räume](#räume). |
| `praefix_klima` | `climate.pm_` | PM Networks | Präfix der Thermostate für die Raumerkennung. |
| `praefix_heizplan` | `schedule.heizplan_` | beide | Präfix der Heizplan-Helfer. |
| `empfehlung` | `sensor.pm_klima_empfehlung` | PM Networks | Empfehlungssensor der Integration. |
| `aussentemperatur` | `sensor.aussentemperatur` | beide | Außentemperatur, auch Grundlage der Heizperiode. |
| `aussenfeuchte` | `sensor.aussenluftfeuchte` | beide | Außenluftfeuchte. |
| `heizperiode_entitaet` | `input_boolean.pm_heizperiode` | beide | PM Networks: Freigabe-Entität (an = Heizperiode, aus = Sommer). Generisch: optionaler Spiegel. Zulässig: `input_boolean.*`, `binary_sensor.*`, `schedule.*`. |
| `coach_ki_entitaet` | `ai_task.claude_ai_task` | beide | KI-Aufgabe für die Analyse im Klima-Coach. Zulässig: `ai_task.*`. |
| `coach_anwesenheit` | `true` | beide | Personen und Anwesenheit in den Lagebericht für die KI aufnehmen. |
| `wetter_entitaet` | `weather.dwd_zuhause` | beide | Wettervorhersage für den Klima-Coach. Zulässig: `weather.*`. |
| `log_level` | `info` | beide | Protokollstufe: `debug`, `info`, `warning` oder `error`. |

Ungültige Werte ersetzt Klima Studio durch den Standard und schreibt eine Warnung in das Protokoll der App.

**Beispiel PM Networks**

```yaml
betriebsart: pm_networks
bericht_tag: sonntag
bericht_uhrzeit: "18:00"
bericht_notify:
  - notify.mobile_app_telefon
heizperiode_entitaet: input_boolean.pm_heizperiode
coach_ki_entitaet: ai_task.claude_ai_task
wetter_entitaet: weather.zuhause
log_level: info
```

**Beispiel Generisch**

```yaml
betriebsart: generisch
plan_anwenden: true
sommer_aktion: plan_pausieren_und_aus
absenktemperatur: 17
aussentemperatur: sensor.aussentemperatur
bericht_tag: sonntag
bericht_uhrzeit: "18:00"
bericht_notify: []
raeume_override:
  - raum: schlafzimmer
    absenk: 16
coach_ki_entitaet: ai_task.claude_ai_task
wetter_entitaet: weather.zuhause
log_level: info
```

## Räume

Jeder Raum hat ein Kürzel (z. B. `wohnzimmer`). Über dieses Kürzel ordnen Sie Heizpläne und Overrides zu.

### PM Networks

Räume werden aus den Entitäten der Integration erkannt:

| Rolle | Erkennung |
|---|---|
| Thermostat | `climate.pm_<raum>` (Präfix über `praefix_klima`) |
| Heizplan | `schedule.heizplan_<raum>` (Präfix über `praefix_heizplan`) |
| Schimmelrisiko, Luftqualität, Lüften | `sensor.pm_<raum>_schimmelrisiko`, `sensor.pm_<raum>_luftqualitaet`, `binary_sensor.pm_<raum>_lueften_empfohlen` |
| Fenster | `binary_sensor.pm_<raum>_fenster_offen`, sonst Fenster- oder Türsensor mit dem Kürzel im Namen |
| Luftfeuchte | Feuchtesensor mit dem Kürzel im Namen; bei mehreren der Wert, den das Thermostat meldet |
| CO2 | CO2-Sensor mit dem Kürzel im Namen, sonst Abgleich mit dem Attribut `co2` der Luftqualität |

Das Kürzel ist der Teil nach `climate.pm_`.

### Generisch

Räume ergeben sich aus den Bereichen von Home Assistant:

- Jeder Bereich mit mindestens einem Thermostat wird ein Raum. Kürzel ist die Bereichs-ID, Name der Bereichsname.
  Die Bereichs-ID sehen Sie in der Adresszeile, wenn Sie den Bereich unter **Einstellungen → Bereiche** öffnen.
- Maßgeblich ist der Bereich der Entität, sonst der Bereich ihres Geräts.
- Thermostate ohne Bereich bilden einen eigenen Raum. Kürzel ist die Objekt-ID (z. B. `gaestezimmer_heizung`
  für `climate.gaestezimmer_heizung`).
- Mehrere Thermostate in einem Bereich: Gesteuert wird nur das erste (alphabetisch). Die übrigen zeigt die Raumkarte
  als Hinweis. Ein anderes Thermostat wählen Sie über `raeume_override`.
- Better Thermostat: Die Better-Thermostat-Entität hat Vorrang. Thermostate im selben Bereich oder Gerät gelten als
  ihre Quellgeräte und werden nicht direkt gesteuert.
- Entitäten der Integration PM Klima und ausgeblendete Entitäten werden nie als Thermostat verwendet.
- Sensoren: zuerst die im Bereich eingetragenen Temperatur- und Feuchtesensoren, dann Sensoren des Bereichs nach
  Geräteklasse (`humidity`, `carbon_dioxide`, `window`/`door`/`opening`, `temperature`). Ersatzweise Sensoren ohne
  Bereich mit dem Kürzel im Namen.

### Overrides

Unter `raeume_override` legen Sie Abweichungen je Raum fest. Pflichtfeld ist `raum` (das Kürzel).

| Feld | Zulässig | Wirkung |
|---|---|---|
| `name` | Text | Anzeigename |
| `climate` | `climate.*` | Thermostat des Raums |
| `schedule` | `schedule.*` | Heizplan des Raums |
| `feuchte` | `sensor.*` | Feuchtesensor |
| `co2` | `sensor.*` | CO2-Sensor |
| `fenster` | `binary_sensor.*` | Fenstersensor |
| `schimmel`, `luftqualitaet` | `sensor.*` | nur PM Networks |
| `lueften` | `binary_sensor.*` | nur PM Networks |
| `absenk` | 5 bis 25 (0,5er-Schritte) | Absenktemperatur des Raums, nur Generisch |
| `ausblenden` | `true` | Raum nicht anzeigen |

Ein Override mit einem Kürzel, das nicht erkannt wurde, legt einen zusätzlichen Raum an. Die Reihenfolge der
Einträge bestimmt die Reihenfolge der Räume.

```yaml
raeume_override:
  - raum: wohnzimmer
    co2: sensor.wetterstation_kohlendioxid
  - raum: badezimmer
    name: Bad
  - raum: gaestezimmer_heizung
    name: Gästezimmer
    schedule: schedule.heizplan_gaestezimmer
  - raum: keller
    ausblenden: true
```

## Heizpläne

![Reiter Heizplan mit Wochenansicht und Zeitblöcken](https://raw.githubusercontent.com/D0GC/ha-pm-networks-apps/main/docs/images/pm-heizplan.webp)

Heizpläne sind `schedule`-Helfer von Home Assistant. Klima Studio bearbeitet sie im Reiter **Heizplan** als
Wochenansicht. Jeder Block trägt seine Solltemperatur in den Blockdaten als `temperatur`.

### Heizplan-Helfer anlegen

Klima Studio legt keine Heizplan-Helfer an. So legen Sie einen an:

1. **Einstellungen → Geräte & Dienste → Helfer → Helfer erstellen → Zeitplan**.
2. Namen so wählen, dass die Entität `schedule.heizplan_<raum>` heißt, z. B. „Heizplan Wohnzimmer“ für den Raum
   `wohnzimmer`. Alternativ die Entität über `raeume_override` (Feld `schedule`) zuordnen.
3. Blöcke und Temperaturen danach in Klima Studio eintragen. Klima Studio schreibt die Temperatur als Blockdaten
   `temperatur`.

In YAML angelegte Zeitpläne kann Klima Studio anzeigen, aber nicht bearbeiten. Im Reiter **Heizplan** erscheinen
nur Räume mit zugeordnetem Heizplan.

### Bedienung

- **Maus:** auf freier Fläche ziehen legt einen Block an. **Touch:** freie Fläche antippen.
- Blöcke verschieben (auch auf andere Tage), an Ober- oder Unterkante verlängern, antippen zum Bearbeiten.
- Temperatur 5 bis 25 °C in 0,5er-Schritten.
- Tagesname antippen: Tag auf Werktage, Wochenende oder einzelne Tage kopieren, Tag leeren.
- **Speichern** zeigt zuerst alle Änderungen. Unmittelbar vor dem Schreiben liest Klima Studio den Helfer neu.
  Wurde er inzwischen anderweitig geändert, erscheint eine Warnung mit den Unterschieden.
- Name und Symbol des Helfers bleiben erhalten, ebenso weitere Blockdaten neben `temperatur`.
- Ein vollständig leerer Plan wird nur nach einer ausdrücklichen Rückfrage gespeichert.
- Sekunden in vorhandenen Zeitangaben (z. B. 06:00:30) bleiben erhalten, solange der Block nicht verändert wird.

### Was außerhalb der Blöcke gilt

- **PM Networks:** die Grundtemperatur der Integration. Die Integration führt den Plan aus.
- **Generisch:** die Absenktemperatur (`absenk` des Raums, sonst Option `absenktemperatur`). Klima Studio führt
  den Plan aus, siehe [Plananwendung](#plananwendung-nur-generisch).

## Steuerung

Der Reiter **Steuerung** zeigt oben die Karte **Heizperiode** und darunter je Raum eine Karte mit Ist- und
Solltemperatur, Luftfeuchte, Status und den Bedienelementen. Die Karten werden alle 30 Sekunden aktualisiert,
solange der Reiter geöffnet ist. Jeder Befehl wird im Ereignisprotokoll der App vermerkt.

### Steuerung mit PM Networks

![Reiter Steuerung in der Betriebsart PM Networks](https://raw.githubusercontent.com/D0GC/ha-pm-networks-apps/main/docs/images/pm-steuerung.webp)

Jeder Befehl läuft ausschließlich über die Integration PM Klima: Klima Studio ruft die Dienste `pm_heizung.*` und
`climate.*` auf den Entitäten `climate.pm_<raum>` auf, nie ein Thermostat direkt.

| Betriebsart | Wirkung |
|---|---|
| Auto | Der Raum folgt seinem Heizplan. Nur hier ist eine zeitweise Abweichung möglich. |
| Hand | Der Raum hält dauerhaft einen festen Handwert (0,5-°C-Schritte), unabhängig vom Heizplan. |
| Aus | Der Raum bleibt aus, bis Sie ihn wieder auf Auto oder Hand stellen. Vor dem Ausschalten erscheint eine Rückfrage. |

- **Vorübergehend ändern** (nur in Auto): Temperatur wählen, Dauer **Bis Planwechsel**, **1 h**, **3 h** oder
  **Dauerhaft**, dann **Anwenden**.
- **Boost:** 30, 60 oder 120 Minuten auf die Höchsttemperatur des Thermostats. Die Betriebsart bleibt unverändert.
  Ein Boost wirkt auch in der Betriebsart Aus.
- **Zurück zum Plan** erscheint, solange eine Abweichung oder ein Boost läuft, und beendet beides.
- Jeder Wechsel der Betriebsart beendet eine laufende Abweichung und einen laufenden Boost.

Warum eine Abweichung nur in Auto: Die Integration führt sie nur dort als Abweichung vom Plan. In Hand würde sie den
Handwert dauerhaft ändern, in Aus den Raum dauerhaft auf Hand stellen. Klima Studio lehnt den Befehl deshalb
außerhalb von Auto ab.

Ist der Hauptschalter der Integration (`switch.pm_heizung_aktiv`) aus, zeigt der Reiter eine Warnung. Die
Integration ist dann pausiert. Sperrt die Integration das Heizen, wird der Grund ebenfalls angezeigt.

### Steuerung generisch

![Reiter Steuerung in der Betriebsart Generisch](https://raw.githubusercontent.com/D0GC/ha-pm-networks-apps/main/docs/images/gen-steuerung.webp)

Klima Studio schaltet die Thermostate direkt über die Standard-Dienste `climate.*` von Home Assistant. Je Raum
führt es einen eigenen Modus:

| Modus | Wirkung |
|---|---|
| Plan | Klima Studio wendet den Heizplan des Raums an (mit `plan_anwenden: true`). Zeitweise Abweichung möglich. |
| Hand | Klima Studio schreibt nichts von selbst. Den Handwert stellen Sie in der Karte ein. |
| Aus | Klima Studio schaltet das Thermostat aus (`off` bzw. `climate.turn_off`). Vor dem Ausschalten erscheint eine Rückfrage. |

- **Neue Räume starten im Modus Hand.** Klima Studio verändert ein Thermostat also erst, wenn Sie den Raum auf Plan
  stellen. Hat der Raum einen Heizplan, erinnert die Karte daran.
- **Vorübergehend ändern** (nur im Modus Plan): Dauer **Bis Planwechsel**, **1 h**, **3 h** oder **Dauerhaft**.
  Ohne bekannten nächsten Planwechsel gilt die Änderung dauerhaft, bis Sie **Zurück zum Plan** wählen.
- **Handwert** (nur im Modus Hand): Temperatur wählen und **Übernehmen**.
- **Boost** (30, 60 oder 120 Minuten): Bietet das Thermostat das Preset `boost`, nutzt Klima Studio dieses Preset.
  Sonst stellt es die Höchsttemperatur des Thermostats ein, höchstens 25 °C. Nach Ablauf kehrt es zum Plan-Soll bzw.
  zum vorherigen Wert und Preset zurück.
- **Zurück zum Plan** beendet eine zeitweise Änderung oder einen Boost sofort.
- Ist das Thermostat aus, sind zeitweise Änderung, Handwert und Boost gesperrt. Stellen Sie den Raum zuerst auf
  Plan oder Hand. Beim Einschalten stellt Klima Studio bevorzugt den Modus `heat` wieder her.
- Steht das Thermostat in einem Geräteprogramm (z. B. `auto`), stellt Klima Studio es beim Wechsel auf Plan oder
  Hand einmalig auf `heat`.
- Rück-Timer für zeitweise Änderung und Boost liegen in der Datenbank der App und überstehen einen Neustart.
- Schrittweite, Mindest- und Höchsttemperatur übernimmt Klima Studio vom Thermostat (Standardschritt 0,5 °C).

Nur angezeigt, nicht gesteuert werden:

- nicht verfügbare Thermostate,
- Thermostate ohne Solltemperatur,
- Thermostate mit Temperaturbereich (nur `target_temp_low`/`target_temp_high`, z. B. im Modus `heat_cool`).

Die Karte nennt dann den Grund.

### Plananwendung (nur Generisch)

Mit `plan_anwenden: true` (Standard) prüft Klima Studio jede Minute alle Räume:

- Angewendet wird nur in Räumen im Modus **Plan**, deren Thermostat im Zustand `heat` steht.
- Soll im Block: die Blocktemperatur `temperatur`. Außerhalb der Blöcke: die Absenktemperatur.
- Geschrieben wird nur, wenn sich das Plan-Soll ändert und das Thermostat nicht schon auf diesem Wert steht.
  Das schont Geräte mit begrenzten Schreibzugriffen (z. B. tado).
- Nicht geschrieben wird bei laufender zeitweiser Änderung, Boost, im Modus Hand oder Aus und in der Sommer-Pause.
- Der Plan wird nicht angewendet, wenn er nicht lesbar ist, keine Blöcke hat oder ein Block keine Temperatur hat.
  Auch die Absenkung entfällt dann. Die Raumkarte nennt den Grund.
- Jede Schreibaktion erscheint im Ereignisprotokoll.

Mit `plan_anwenden: false` wendet Klima Studio keine Heizpläne an. Zeitweise Änderung, Handwert, Boost und Aus
funktionieren weiter.

## Heizperiode und Sommerbetrieb

Die Karte **Heizperiode** im Reiter **Steuerung** schaltet zwischen Heizperiode und Sommerbetrieb um.

### Modi

| Modus | Wirkung |
|---|---|
| Automatik | Klima Studio entscheidet anhand der Außentemperatur. Standard. |
| Heizperiode | Heizperiode sofort und dauerhaft. |
| Sommer | Sommerbetrieb sofort und dauerhaft. Vor dem Umschalten fragt die Karte nach. |

### Regel der Automatik (beide Betriebsarten)

Grundlage ist das Tagesmittel der Außentemperatur (Option `aussentemperatur`) der letzten sieben abgeschlossenen
Kalendertage, aus der Langzeitstatistik, ersatzweise aus der Historie. Ein Tag zählt nur mit Werten aus mindestens
12 Stunden.

- Die Heizperiode beginnt, wenn das Tagesmittel an den letzten **2 Tagen** unter der **Heizgrenze von 13 °C** liegt.
- Sie endet, wenn das Tagesmittel an den letzten **3 Tagen** mindestens Heizgrenze plus **Hysterese von 2 K**
  erreicht, also 15 °C.
- Dazwischen bleibt der bisherige Zustand. Fehlen Tagesmittel, ändert Klima Studio nichts und nennt den Grund.

Heizgrenze (5 bis 20 °C), Hysterese (0 bis 5 K) und die Tage bis Beginn und Ende (je 1 bis 7) stellen Sie unter
**Einstellungen der Automatik** ein. Die Karte zeigt die Tagesmittel als Balken, die Entscheidung mit Begründung und
den letzten Wechsel. Klima Studio prüft beim Start und danach stündlich.

### Heizperiode mit PM Networks

Klima Studio schaltet nur eine Freigabe-Entität (Standard `input_boolean.pm_heizperiode`). Die Integration PM Klima
setzt die Sperre um:

- **an** = Heizperiode. Die Integration darf heizen.
- **aus** = Sommer. Die Integration sperrt das Heizen. Räume in Auto ohne laufende Abweichung werden ausgeschaltet.

**Einmalige Einrichtung**

1. In Klima Studio: **Steuerung → Heizperiode → Einrichten**. Klima Studio legt den Helfer
   `input_boolean.pm_heizperiode` („PM Heizperiode“) an. In der Automatik steht er danach auf an, außer die
   Tagesmittel zeigen eindeutig das Ende der Heizperiode. Der Knopf erscheint nur, solange die Entität fehlt.
2. In Home Assistant: **Einstellungen → Geräte & Dienste → PM Klima → Konfigurieren**, bis zum Schritt **Sperre**.
3. Als **Freigabe-Entität** `input_boolean.pm_heizperiode` auswählen, als **Sperrwirkung** „aus“.
4. Speichern.

Steht die Freigabe beim Verknüpfen auf aus, schaltet die Integration Räume im Modus Auto sofort ab. Solange die
Verknüpfung fehlt, hat das Umschalten keine Wirkung auf die Heizung. Klima Studio erkennt das im Sommerbetrieb am
Sperrgrund der Integration und zeigt dann einen Hinweis.

`binary_sensor.*` und `schedule.*` sind als Freigabe ebenfalls zulässig. Diese kann Klima Studio nur lesen. Die
Modi Heizperiode und Sommer stehen dann nicht zur Verfügung.

**Schutzmechanismen**

- Feste Modi werden bei jeder Prüfung durchgesetzt.
- Ein Schaltbefehl gilt erst als erledigt, wenn die Freigabe danach den neuen Zustand hat. Sonst wiederholt
  Klima Studio ihn bei der nächsten Prüfung.
- Schalten Sie den Helfer in Home Assistant von Hand um, bleibt das bestehen, bis die Automatik zu einer neuen
  Entscheidung kommt. Die Karte zeigt die Abweichung an.
- **Winterschutz:** Steht die Freigabe länger als 6 Stunden auf aus, obwohl die Automatik Heizperiode ermittelt,
  schaltet Klima Studio sie wieder ein.

**Was im Sommer weiterheizt:** Räume in der Betriebsart Hand, Räume mit laufender Abweichung und ein Boost. Die
Karte nennt Räume in Hand im Sommerbetrieb.

**Hinweise**

- Die Integration sperrt auch, wenn die eingetragene Freigabe-Entität fehlt oder nicht verfügbar ist. Löschen Sie
  den Helfer nicht, solange er in der Integration eingetragen ist.
- Der Hauptschalter `switch.pm_heizung_aktiv` ist kein Sommerschalter. Klima Studio zeigt ihn nur an.
- Die eigene Außentemperatur-Sperre der Integration bleibt unverändert und wirkt unabhängig von der Freigabe.

### Heizperiode generisch

Der Zustand der Heizperiode liegt intern in Klima Studio. Eine Freigabe-Entität ist nicht nötig.

- **Sommer:** Klima Studio pausiert die Plananwendung. Mit `sommer_aktion: plan_pausieren_und_aus` (Standard)
  schaltet es Räume im Modus Plan aus und merkt sich deren Modus. Mit `plan_pausieren` bleiben die Thermostate
  eingeschaltet. Klima Studio stellt dann einmal die Absenktemperatur ein. Das gilt auch für Thermostate, die sich
  nicht ausschalten lassen.
- **Heizperiode beginnt:** Klima Studio schaltet die gemerkten Räume wieder ein (bevorzugt `heat`) und wendet die
  Heizpläne an.
- Räume im Modus **Hand** bleiben unberührt und heizen im Sommer weiter. Die Karte nennt sie.
- Umgestellt werden nur Räume im Modus Plan, deren Thermostat im Zustand `heat` steht, und nur mit
  `plan_anwenden: true`. Kühl- und Lüftungsbetrieb (`cool`, `dry`, `fan_only`, `heat_cool`) bleiben unberührt.
- Die allererste Entscheidung der Automatik stellt keine Thermostate um. Sie legt nur den Zustand fest.
- Lässt sich ein Thermostat nach fünf Versuchen nicht umstellen, gibt Klima Studio auf und vermerkt das im
  Ereignisprotokoll.
- **Optionaler Spiegel:** Ist `heizperiode_entitaet` ein vorhandener `input_boolean`, schaltet Klima Studio ihn
  mit (an = Heizperiode, aus = Sommer), z. B. für eigene Automationen. Unter **Optional: Spiegel-Entität für eigene
  Automationen** lässt er sich anlegen.

## Sicherheitsmechanismen im generischen Modus

Weil Klima Studio im generischen Modus selbst schaltet, gelten zusätzliche Schutzregeln:

| Mechanismus | Verhalten |
|---|---|
| Nur `heat` | Heizpläne und Sommer-Aktion wirken nur auf Thermostate im Zustand `heat`. Geräteprogramme (z. B. `auto`) und Kühlbetrieb bleiben unberührt. Die Karte nennt den Grund. |
| Neue Räume in Hand | Ein neu erkannter Raum startet im Modus Hand. Klima Studio schreibt erst, wenn Sie Plan wählen. |
| Nur bei Änderung | Klima Studio schreibt nur, wenn sich das Plan-Soll ändert und das Thermostat einen anderen Wert hat. |
| Toleranz | Eine Abweichung am Thermostat zählt erst ab der halben Schrittweite, mindestens 0,25 °C. |
| Handänderung | Hat das Thermostat den geschriebenen Wert übernommen und zeigt danach einen anderen, gilt dieser als zeitweise Änderung bis zum nächsten Planwechsel. Klima Studio schreibt nicht dagegen. |
| Übernahmeprüfung | Hat das Thermostat einen Wert nach 5 Minuten nicht übernommen, schreibt Klima Studio ihn einmal erneut. Danach erscheint der Hinweis „nicht übernommen“ in der Karte. |
| Fenster | Bei offenem Fenster erhöht Klima Studio das Soll nicht. Eine Fensterabsenkung oder ein Preset des Geräts gilt nicht als Handänderung. |
| Nicht verfügbar | Ist das Thermostat nicht verfügbar, pausiert die Plananwendung für diesen Raum 5 Minuten. |
| Backoff | Nach Schreibfehlern wartet Klima Studio 5, 10, 20, 40 und höchstens 60 Minuten. |
| Boost | Ohne Preset `boost` höchstens 25 °C bzw. die Höchsttemperatur des Thermostats, mit Rück-Timer. |
| Ein Thermostat je Raum | Mehrere Thermostate im Bereich werden nicht gemeinsam geschaltet. Better-Thermostat-Quellgeräte werden nicht direkt gesteuert. |
| Hinweise | tado-Geräte mit einer anderen Standard-Übersteuerung als MANUAL erhalten einen Hinweis. |

## Übersicht und Auswertungen

Die **Übersicht** zeigt je Raum Ist- und Solltemperatur, Status, Heizzeit, Feuchte, Fenster und CO2 für 24 Stunden,
7 oder 30 Tage, dazu die Außenwerte und den nächsten Wochenbericht.

- **PM Networks:** zusätzlich die Empfehlungen der Integration und das Schimmelrisiko.
- **Generisch:** eine Karte **Betrieb** mit der Zahl der Räume in Plan, Hand und Aus, laufenden Boosts und
  zeitweisen Änderungen sowie der Sommer-Pause.

Der Reiter **Auswertung** zeigt je Raum:

- Heizstunden (Zeit mit `hvac_action: heating`), Soll und Ist, Fensterzeiten,
- Luftfeuchte mit den Schwellen 70 und 80 %,
- Schimmelrisiko (nur PM Networks),
- CO2 mit den Schwellen 1000 und 1400 ppm,
- Lüftungserfolg: Abfall von Feuchte und CO2 in den 30 Minuten nach dem Öffnen eines Fensters.

Soll, Ist und Fensterzeiten stammen aus der Recorder-Historie (Standard 10 Tage). Längere Zeiträume werden
entsprechend gekennzeichnet.

## Klima-Coach und KI

![Reiter Coach mit lokalen Hinweisen und KI-Ergebnis](https://raw.githubusercontent.com/D0GC/ha-pm-networks-apps/main/docs/images/gen-coach.webp)

**Aktuelle Hinweise** entstehen lokal aus einer Wissensdatenbank, die Teil der App ist. Die Regeln prüfen die
aktuellen Werte, die Wochenwerte und die Heizpläne jedes Raums sowie Außentemperatur, Wettervorhersage, Heizperiode
und Jahreszeit. Die Auswertung läuft vollständig in der App.

- Regeln je Betriebsart: 70 mit PM Networks, 61 generisch. Generische Regeln beziehen sich auf Plan, Hand und Aus,
  z. B. „Heizplan noch nicht aktiv“, wenn ein Raum mit Heizplan im Modus Hand steht.
- Sortierung nach Priorität: dringend, wichtig, Hinweis. Die Begründung lässt sich aufklappen.
- **Übernehmen** öffnet den Heizplan oder die Steuerung des Raums, bei Vorschlägen zur Steuerung mit Temperatur und
  Dauer vorausgefüllt. Geschaltet oder gespeichert wird erst nach Ihrer Bestätigung.
- **Erledigt** blendet den Hinweis bis zum nächsten Tag aus, **7 Tage ausblenden** für eine Woche.
- **Hinweise der Integration** aus `sensor.pm_klima_empfehlung` erscheinen nur mit PM Networks.

### KI-Coach

**Analyse anfordern** sendet einen Lagebericht an die KI-Aufgabe aus `coach_ki_entitaet` und zeigt eine
Zusammenfassung und bis zu acht Tipps.

- Die Analyse läuft nur auf Knopfdruck, nie automatisch. Sie kann bis zu drei Minuten dauern.
- Es läuft höchstens eine Analyse gleichzeitig.
- **Was wird übermittelt?** zeigt vorab genau den Lagebericht, der gesendet würde.
- Vorgeschlagene Maßnahmen werden nie automatisch ausgeführt.
- Die Anweisungen an die KI unterscheiden sich je Betriebsart.
- Frühere Auswertungen stehen im Verlauf. Kann die Antwort nicht gelesen werden, erscheint der Rohtext.
- Fehlt die KI-Entität, meldet Klima Studio das. Die lokalen Hinweise funktionieren auch ohne KI.

## Wochenbericht

Klima Studio erstellt den Wochenbericht standardmäßig sonntags um 18:00 Uhr (Optionen `bericht_tag`,
`bericht_uhrzeit`). Er enthält je Raum Heizzeit, Feuchte, CO2 und Fensterzeiten, mit PM Networks auch das
Schimmelrisiko, dazu auffällige Räume, die längsten Fensteröffnungen und Empfehlungen.

- Die letzten 12 Berichte liegen in `/data/berichte.json`.
- Push nur, wenn unter `bericht_notify` Dienste eingetragen sind.
- **Bericht jetzt erstellen** sendet nur dann einen Push, wenn **zusätzlich Push senden** angehakt ist.

## Datenschutz und Sicherheit

- Die lokalen Hinweise des Klima-Coachs entstehen ausschließlich in der App.
- Daten verlassen Home Assistant nur, wenn Sie **Analyse anfordern** wählen. Erst dann wird der Lagebericht über die
  KI-Aufgabe von Home Assistant an den dahinterliegenden KI-Dienst übermittelt, bei Claude an Anthropic.
- Der Lagebericht enthält Raumnamen, aktuelle Messwerte und Wochenwerte, die Heizpläne, Außenwerte und
  Wettervorhersage, den Stand der Heizperiode und der Steuerung sowie die Titel der lokalen Hinweise. Mit
  `coach_anwesenheit: true` (Standard) kommen Namen und Anwesenheit der Personen hinzu. Schalten Sie die Option aus,
  wenn Sie das nicht möchten.
- Der Lagebericht enthält keine Zugangsdaten, Tokens, IP-Adressen oder Geräte-IDs.
- Klima Studio speichert Einstellungen, Rückmeldungen zu Hinweisen, den Zustand der Räume (generisch), ein
  Ereignisprotokoll (höchstens 2000 Einträge) und die letzten 50 KI-Analysen lokal in `/data/coach.db`.
- Erreichbar ist die Oberfläche ausschließlich über Home-Assistant-Ingress. Der Supervisor-Watchdog prüft
  `/api/health`.
- Das Supervisor-Token wird nur aus der Umgebung gelesen und nicht gespeichert.
- Schrift, Symbole und Diagramme liegen im Image. Die Oberfläche lädt nichts von externen Servern.

## Fehlerbehebung

**Hinweis „Die Integration PM Klima ist in Home Assistant nicht geladen“**

Die Betriebsart steht auf `pm_networks`, PM Klima ist aber nicht eingerichtet oder nicht geladen. Prüfen Sie unter
**Einstellungen → Geräte & Dienste**, ob PM Klima geladen ist. Ohne PM Klima stellen Sie `betriebsart: generisch`
ein und starten die App neu. Klima Studio prüft das Vorhandensein von PM Klima etwa alle fünf Minuten.

**„Die Oberfläche ist veraltet. Bitte die Seite neu laden.“**

Der Browser hat nach einem Update noch Teile der alten Oberfläche geladen. Laden Sie die Seite neu. Hilft das
nicht, leeren Sie den Browser-Cache.

**iPhone- oder iPad-App zeigt eine alte Oberfläche**

Die Companion-App speichert das Frontend zwischen. Öffnen Sie in der App **Einstellungen → Companion App →
Debugging** und wählen Sie **Frontend-Cache zurücksetzen**. Schließen Sie die App danach vollständig und öffnen Sie
sie neu.

**Ein Raum lässt sich nicht steuern (Generisch)**

Die Karte nennt den Grund: Thermostat nicht verfügbar, keine Solltemperatur oder Temperaturbereich. Thermostate mit
Temperaturbereich zeigt Version 1.2.0 nur an.

**Der Heizplan wird nicht angewendet (Generisch)**

- Steht der Raum im Modus Plan? Neue Räume starten in Hand.
- Ist `plan_anwenden` eingeschaltet?
- Steht das Thermostat auf `heat`? Geräteprogramme wie `auto` werden nicht überschrieben.
- Hat jeder Block eine Temperatur? Blöcke, die in Home Assistant angelegt wurden, haben oft keine. Tragen Sie die
  Temperatur im Heizplan-Editor ein.
- Läuft eine zeitweise Änderung, ein Boost oder die Sommer-Pause?

Die Raumkarte zeigt den jeweiligen Grund.

**Ein Raum fehlt oder ist falsch zugeordnet**

- PM Networks: Heißt das Thermostat `climate.pm_<raum>`? Sonst `praefix_klima` anpassen.
- Generisch: Ist das Thermostat einem Bereich zugeordnet? Bei mehreren Thermostaten im Bereich wählen Sie das
  gewünschte über `raeume_override` (Feld `climate`).
- Der Heizplan fehlt: Heißt der Helfer `schedule.heizplan_<raum>`? Sonst Feld `schedule` im Override setzen.

**Die Heizperiode entscheidet nicht („Zu wenige Daten“)**

Für die letzten Tage fehlen Tagesmittel der Außentemperatur. Prüfen Sie, ob der Sensor aus `aussentemperatur`
Langzeitstatistiken schreibt (`state_class: measurement`).

**Die KI-Analyse schlägt fehl**

Prüfen Sie, ob die Entität aus `coach_ki_entitaet` existiert und in Home Assistant funktioniert. Die Analyse bricht
nach drei Minuten ab.

**Protokoll**

Das Protokoll der App finden Sie unter **Einstellungen → Apps → PM Klima Studio → Protokoll**. Für mehr Details
setzen Sie `log_level: debug`.

## Häufige Fragen

**Schaltet Klima Studio meine Thermostate selbst?**
Mit PM Networks nein. Alle Befehle laufen über die Integration PM Klima. Generisch ja, über die Standard-Dienste
`climate.*` von Home Assistant.

**Kann ich beide Betriebsarten mischen?**
Nein. Je Installation gilt eine Betriebsart. Im generischen Modus werden Entitäten von PM Klima nie als Thermostat
verwendet.

**Was passiert nach einem Neustart der App?**
Einstellungen, Rückmeldungen und Berichte liegen in `/data` und bleiben erhalten. Generisch gilt das auch für die
Modi der Räume und laufende Rück-Timer. Ein inzwischen abgelaufener Timer wird beim nächsten Durchlauf beendet.

**Brauche ich einen KI-Dienst?**
Nein. Der Klima-Coach arbeitet lokal. Die KI-Analyse ist eine Ergänzung auf Knopfdruck.

**Werden Daten übertragen?**
Nur wenn Sie **Analyse anfordern** wählen. Was übermittelt würde, zeigt **Was wird übermittelt?** vorab.

**Welche Thermostate funktionieren generisch?**
`climate`-Entitäten mit einer Solltemperatur, z. B. Heizkörperthermostate, Better Thermostat oder tado.
Klima-Geräte im Kühlbetrieb bleiben unberührt.

**Warum steht ein neuer Raum auf Hand?**
Damit Klima Studio kein Thermostat verändert, bevor Sie es wollen. Wählen Sie Plan, sobald der Heizplan stimmt.

**Lässt sich Klima Studio ohne Heizpläne nutzen?**
Ja. Übersicht, Auswertungen, Coach und Wochenbericht funktionieren ohne Heizplan. Generisch steuern Sie Räume dann
im Modus Hand.

## Lizenz

MIT. © 2026 PM Networks. Die Schrift Montserrat steht unter der SIL Open Font License 1.1.
