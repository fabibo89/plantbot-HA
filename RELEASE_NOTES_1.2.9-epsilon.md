# Release Notes – PlantBot Bewässerung **1.2.9-epsilon**

**Datum:** 2026-03-22

## Behoben / Robustheit

- **HTTP `/status`:** Längeres Timeout (15 s) für den Abruf der Station über `GET http://<IP>/status`, damit langsame Antworten der Firmware (WLAN, Last) seltener als Fehler enden. Der Wert steht zentral als `STATUS_REQUEST_TIMEOUT` in `coordinator.py`.

## Repository / Wartung

- **macOS:** `.DS_Store` aus dem Git-Index entfernt und dauerhaft über `.gitignore` ausgeschlossen (keine Finder-Metadaten mehr im Repo).

---

Ältere Versionen (z. B. **1.2.9-delta** und früher) sind hier nicht im Detail beschrieben; siehe Git-Tags und Commit-Historie.
