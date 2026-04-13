# Changelog

Alle nennenswerten Änderungen an **PlantBot Bewässerung** (plantbot-HA) werden hier festgehalten.

## [1.2.9-epsilon] – 2026-03-22

### Behoben / Robustheit

- **HTTP `/status`:** Längeres Timeout (15 s) für den Abruf der Station über `GET http://<IP>/status`, damit langsame Antworten der Firmware (WLAN, Last) seltener als Fehler enden. Der Wert steht zentral als `STATUS_REQUEST_TIMEOUT` in `coordinator.py`.

### Repository / Wartung

- **macOS:** `.DS_Store` aus dem Git-Index entfernt und dauerhaft über `.gitignore` ausgeschlossen (keine Finder-Metadaten mehr im Repo).

---

## [1.2.9-delta] und früher

Ältere Versionen sind vor diesem Changelog nicht dokumentiert; siehe Git-Tags und Commit-Historie.
