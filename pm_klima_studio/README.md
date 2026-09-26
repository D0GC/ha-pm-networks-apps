# PM Klima Studio

Planungs- und Auswertungsoberfläche für die Integration **PM Klima** (`pm_heizung`).

- Heizpläne je Raum als Wochenansicht bearbeiten, direkt in den bestehenden `schedule`-Helfern.
- Räume steuern: Betriebsart Auto, Hand oder Aus, zeitweise Temperatur, Boost und „Zurück zum Plan“.
- Heizperiode und Sommerbetrieb automatisch nach Tagesmittel der Außentemperatur oder von Hand umschalten.
- Heizstunden, Soll/Ist, Feuchte, Schimmelrisiko, CO2, Fensterzeiten und Lüftungserfolg auswerten.
- Klima-Coach: lokale Hinweise aus einer Wissensdatenbank, KI-Analyse nur auf Knopfdruck.
- Wochenbericht jeden Sonntag, archiviert und auf Wunsch als Push.

Die App schaltet keine Thermostate selbst. Alle Steuerbefehle laufen über die Integration.

Architekturen: `aarch64`, `amd64`. Aufruf über die Seitenleiste „Klima Studio“ (Ingress).
Einzelheiten stehen in der Dokumentation (Reiter „Dokumentation“ der App).
