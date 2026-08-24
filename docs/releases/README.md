# Release notes

Markdown files in this folder document PlantBot HA releases.

Naming: use the GitHub tag as filename, e.g. tag `v1.2.10-alpha` → `v1.2.10-alpha.md`.

On release publish, workflow `fill-release-notes.yml` copies that file into the
GitHub Release body (`gh release edit … --notes-file`). `release-on-publish.yml`
uses the same file when updating the release.

If no matching file exists, auto-generated GitHub notes are used as fallback
(and `fill-release-notes.yml` leaves the body unchanged).
