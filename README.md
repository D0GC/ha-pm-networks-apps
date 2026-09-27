<p align="center">
  <img src="docs/images/banner.png" alt="PM Klima Studio – Heizpläne, Steuerung, Auswertungen und Klima-Coach für Home Assistant" width="100%">
</p>

<p align="center">
  <a href="pm_klima_studio/CHANGELOG.md"><img src="https://img.shields.io/badge/Version-1.2.0-784295?style=flat-square" alt="Version 1.2.0"></a>
  <img src="https://img.shields.io/badge/Home%20Assistant-%E2%89%A5%202026.9-443171?style=flat-square&logo=homeassistant&logoColor=white" alt="Home Assistant ab 2026.9">
  <img src="https://img.shields.io/badge/Architektur-aarch64%20%7C%20amd64-262252?style=flat-square" alt="Architekturen aarch64 und amd64">
  <a href="LICENSE"><img src="https://img.shields.io/badge/Lizenz-MIT-191537?style=flat-square" alt="Lizenz MIT"></a>
</p>

<p align="center">
  <a href="https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2FD0GC%2Fha-pm-networks-apps"><img src="https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg" alt="Repository zu Home Assistant hinzufügen"></a>
</p>

# PM Klima Studio

Klima Studio ist eine App (ehemals Add-on) für Home Assistant. Sie bearbeitet Heizpläne als Wochenansicht, steuert
Räume, schaltet zwischen Heizperiode und Sommerbetrieb, wertet Temperatur, Feuchte, CO2 und Fensterzeiten aus und
gibt mit dem Klima-Coach konkrete Hinweise. Einmal pro Woche fasst ein Bericht alles zusammen.

Klima Studio arbeitet mit der Integration **PM Klima** oder, in der Betriebsart **Generisch**, mit beliebigen
Thermostaten in Home Assistant.

![Klima Studio auf Desktop und Smartphone](docs/images/titelbild.webp)

## Funktionen

| | |
|---|---|
| **Heizplan-Editor** <br> Wochenansicht der `schedule`-Helfer. Blöcke ziehen, verschieben, verlängern, Tage kopieren. Vor dem Speichern zeigt Klima Studio alle Änderungen und warnt bei Fremdänderungen. | ![Heizplan](docs/images/pm-heizplan.webp) |
| **Steuerung** <br> Je Raum Betriebsart, zeitweise Temperatur, Handwert, Boost und „Zurück zum Plan“. Dazu die Heizperiode mit Automatik nach Tagesmitteln der Außentemperatur. | ![Steuerung](docs/images/gen-steuerung.webp) |
| **Auswertungen** <br> Heizstunden, Soll und Ist, Luftfeuchte, Schimmelrisiko, CO2, Fensterzeiten und Lüftungserfolg für 24 Stunden, 7 und 30 Tage. | ![Auswertung](docs/images/pm-auswertung.webp) |
| **Klima-Coach** <br> Lokale Hinweise aus einer Wissensdatenbank. Auf Wunsch eine KI-Analyse über eine `ai_task`-Entität, mit Vorschau der übermittelten Daten. | ![Coach](docs/images/pm-coach.webp) |
| **Wochenbericht** <br> Zusammenfassung je Raum, auffällige Räume und Empfehlungen. Archiv der letzten 12 Berichte, auf Wunsch als Push. | ![Wochenbericht](docs/images/pm-berichte.webp) |

## Zwei Betriebsarten

Sie wählen die Betriebsart in den App-Optionen (`betriebsart`).

| | **PM Networks** (`pm_networks`, Standard) | **Generisch** (`generisch`) |
|---|---|---|
| Voraussetzung | Integration PM Klima | beliebige `climate`-Entitäten mit Solltemperatur |
| Räume | aus `climate.pm_<raum>` | aus den Bereichen von Home Assistant |
| Thermostate schalten | die Integration PM Klima | Klima Studio über `climate.*`-Dienste |
| Heizpläne ausführen | die Integration | Klima Studio (`plan_anwenden`) |
| Raum-Betriebsarten | Auto, Hand, Aus | Plan, Hand, Aus |
| Boost | über die Integration | Preset `boost` des Thermostats, sonst höchstens 25 °C mit Rück-Timer |
| Heizperiode | schaltet eine Freigabe-Entität | interner Zustand, pausiert die Heizpläne, auf Wunsch Räume aus |
| Schimmelrisiko, Luftqualität | ja | nein |
| Hinweise der Integration | ja | nein |
| Klima-Coach | 70 Regeln | 61 Regeln |
| Heizplan-Editor, Auswertungen, KI-Analyse, Wochenbericht | ja | ja |

Im generischen Modus gelten zusätzliche Schutzregeln: Klima Studio schreibt nur auf Thermostate im Zustand `heat`,
neue Räume starten im Modus Hand, bei offenem Fenster wird nicht erhöht, und Handänderungen am Gerät werden
respektiert. Einzelheiten stehen in der [Anleitung](docs/ANLEITUNG.md#sicherheitsmechanismen-im-generischen-modus).

<details>
<summary><b>Alle Reiter in beiden Betriebsarten</b></summary>

| Reiter | PM Networks | Generisch |
|---|---|---|
| Übersicht | ![Übersicht PM Networks](docs/images/pm-uebersicht.webp) | ![Übersicht Generisch](docs/images/gen-uebersicht.webp) |
| Steuerung | ![Steuerung PM Networks](docs/images/pm-steuerung.webp) | ![Steuerung Generisch](docs/images/gen-steuerung.webp) |
| Heizplan | ![Heizplan PM Networks](docs/images/pm-heizplan.webp) | ![Heizplan Generisch](docs/images/gen-heizplan.webp) |
| Auswertung | ![Auswertung PM Networks](docs/images/pm-auswertung.webp) | ![Auswertung Generisch](docs/images/gen-auswertung.webp) |
| Coach | ![Coach PM Networks](docs/images/pm-coach.webp) | ![Coach Generisch](docs/images/gen-coach.webp) |
| Berichte | ![Berichte PM Networks](docs/images/pm-berichte.webp) | ![Berichte Generisch](docs/images/gen-berichte.webp) |

</details>

<details>
<summary><b>Auf dem Smartphone</b></summary>

<p align="center">
  <img src="docs/images/mobil-pm-uebersicht.webp" alt="Übersicht auf dem Smartphone" width="24%">
  <img src="docs/images/mobil-gen-steuerung.webp" alt="Steuerung auf dem Smartphone" width="24%">
  <img src="docs/images/mobil-pm-heizplan.webp" alt="Heizplan auf dem Smartphone" width="24%">
  <img src="docs/images/mobil-pm-coach.webp" alt="Coach auf dem Smartphone" width="24%">
</p>

</details>

## Schnellstart

1. Repository hinzufügen: Knopf oben anklicken. Oder von Hand: **Einstellungen → Apps → App-Store → Menü →
   Repositories** und `https://github.com/D0GC/ha-pm-networks-apps` eintragen.
2. **PM Klima Studio** installieren. Das Image wird lokal gebaut. Auf einem Raspberry Pi dauert das einige Minuten.
3. Im Reiter **Konfiguration** die Betriebsart wählen:
   ```yaml
   betriebsart: pm_networks   # mit der Integration PM Klima
   # betriebsart: generisch   # ohne PM Klima, beliebige Thermostate
   ```
4. App starten und **Klima Studio** in der Seitenleiste öffnen.
5. Mit PM Networks: **Steuerung → Heizperiode → Einrichten** und die Freigabe in PM Klima eintragen.
   Generisch: Zeitplan-Helfer `schedule.heizplan_<bereich>` anlegen, Blöcke im Reiter **Heizplan** eintragen und
   die Räume in der **Steuerung** auf **Plan** stellen.

Updates erscheinen danach unter **Einstellungen → System → Updates**.

## Dokumentation

- [Ausführliche Anleitung mit Screenshots](docs/ANLEITUNG.md): Installation, Optionen, Räume, Heizpläne, Steuerung,
  Heizperiode, Coach, Fehlerbehebung und häufige Fragen.
- [Dokumentation der App](pm_klima_studio/DOCS.md): derselbe Inhalt, wie er in Home Assistant im Reiter
  **Dokumentation** erscheint.
- [Änderungen](pm_klima_studio/CHANGELOG.md)

## Datenschutz

Klima Studio läuft lokal in Home Assistant. Die Oberfläche lädt nichts von externen Servern, der Klima-Coach
wertet lokal aus. Daten verlassen Home Assistant nur, wenn Sie im Coach **Analyse anfordern** wählen. Dann geht ein
Lagebericht ohne Zugangsdaten, Tokens, IP-Adressen und Geräte-IDs an den KI-Dienst Ihrer `ai_task`-Entität.
**Was wird übermittelt?** zeigt ihn vorab. Personen und Anwesenheit lassen sich mit `coach_anwesenheit: false`
ausschließen.

## Enthaltene Apps

| App | Beschreibung |
|---|---|
| [PM Klima Studio](pm_klima_studio/) | Heizplan-Editor, Steuerung, Klima-Auswertungen, Klima-Coach und Wochenbericht, mit PM Klima oder beliebigen Thermostaten. |

## Lizenz

MIT, siehe [LICENSE](LICENSE). Die Schrift Montserrat steht unter der SIL Open Font License 1.1.

<p align="center"><sub>© 2026 PM Networks</sub></p>
