# PM Klima Studio

Heizpläne, Steuerung, Auswertungen und Klima-Coach für Home Assistant. Mit der Integration **PM Klima**
(Betriebsart `pm_networks`) oder mit beliebigen Thermostaten (Betriebsart `generisch`).

- **Heizplan-Editor:** `schedule`-Helfer als Wochenansicht bearbeiten, mit Änderungsübersicht vor dem Speichern.
- **Steuerung je Raum:** Auto/Hand/Aus (PM Networks) bzw. Plan/Hand/Aus (Generisch), zeitweise Temperatur, Boost,
  „Zurück zum Plan“.
- **Heizperiode und Sommerbetrieb:** Automatik nach Tagesmitteln der Außentemperatur oder von Hand.
- **Generisch:** Klima Studio wendet die Heizpläne selbst an, mit Absenktemperatur, Fenster- und Verfügbarkeitsschutz.
  Neue Räume starten im Modus Hand.
- **Auswertungen:** Heizstunden, Soll/Ist, Feuchte, CO2, Fensterzeiten, Lüftungserfolg, mit PM Klima auch
  Schimmelrisiko.
- **Klima-Coach:** lokale Hinweise aus einer Wissensdatenbank, KI-Analyse nur auf Knopfdruck.
- **Wochenbericht:** wöchentlich, archiviert und auf Wunsch als Push.

Mit PM Networks laufen alle Steuerbefehle über die Integration PM Klima. Im generischen Modus schaltet Klima Studio
die Thermostate über die Standard-Dienste `climate.*`.

Architekturen: `aarch64`, `amd64`. Aufruf über die Seitenleiste „Klima Studio“ (Ingress).
Einzelheiten stehen im Reiter **Dokumentation** der App und in der
[Anleitung mit Screenshots](https://github.com/D0GC/ha-pm-networks-apps/blob/main/docs/ANLEITUNG.md).

© 2026 PM Networks
