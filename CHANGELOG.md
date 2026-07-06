# Changelog

## 0.1.1 (2026-07-06)

### Fixed

- **`_headers` / `_redirects` are now actually applied.** Root-level
  `_headers` and `_redirects` were uploaded as ordinary assets, so Cloudflare
  Pages served them as static files and silently ignored every rule. They are
  now excluded from the asset manifest and attached to the deployment request
  as form fields, matching wrangler's behaviour. Copies in subdirectories are
  still treated as ordinary assets. Found during the first real-world deploy
  (ecitizen.jp, 6,190 files).

## 0.1.0 — unreleased

Initial release, extracted from the aiseed-dev/website deploy tooling.

- Deploy a folder to Cloudflare Pages via the Direct Upload API
  (wrangler-compatible content hashing; unchanged files are never re-sent).
- Concurrent batch uploads (3 workers) with exponential backoff on 429/5xx
  and transport errors.
- Pre-flight validation of Pages limits (25 MiB/file, 20,000 files).
- `--dry-run`, `--exclude`, `--quiet`, `--json`, `--branch`, `--no-create`.
- Credentials from environment or `~/.config/cloudflare/pages.env`.
- Importable core (`cf_publish.deploy`) that raises `PagesError` and reports
  progress via callback — no prints, no exits.
