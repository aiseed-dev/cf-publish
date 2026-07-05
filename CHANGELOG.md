# Changelog

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
