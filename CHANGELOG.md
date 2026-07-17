# Changelog

## 0.2.2 (2026-07-17)

### Fixed

- **R2: SigV4 署名とリクエストのクエリ符号化を一致させた。** これまで
  `params=` 経由で httpx が独自にクエリを再符号化しており(空白が `%20`
  でなく `+` になる)、署名した正規化形と送信バイト列がずれるため、
  空白を含む `--prefix` などで RFC3986 どおりに検証するサーバから 403
  になり得た。正規化済みクエリ文字列をそのまま URL に載せるよう修正し、
  受信側で署名を再計算する回帰テストを追加。
- **`cf-publish --version` が一つ前のバージョンを表示していた。**
  pyproject.toml と `__init__.py` の二重定義が食い違っていたため
  (0.2.1 リリース時に `__init__.py` が 0.2.0 のまま)。hatch の
  dynamic version に一本化し、実体は `__init__.py` の `__version__` だけに。
- 関数スコープの `httpx.Client` を `with` で確実にクローズ
  (`upload_assets` と dry-run の照会)。

### Added

- **`examples/form-worker/`** — WordPress から分離した問い合わせフォームの
  受信箱(Cloudflare Worker + D1 + Turnstile)とデプロイスクリプト。
  bunko/forms から移設。セキュリティレビューを実施し、初回収録時点で
  次を修正済み: 既知フィールド名に一致しない正規送信が 400 になる問題、
  PULL_TOKEN の定数時間比較化と state ファイルの 0600 化(トークンは
  stdout に出さない)、ALLOWED_ORIGIN 未設定時の fail-closed 化、
  siteverify の hostname 突き合わせ、内部エラー文言の非開示、
  Content-Length の事前検査、破損 state ファイル時の明示エラー。

## 0.2.1 (2026-07-09)

### Fixed

- README: link to README.ja.md is now an absolute GitHub URL so it resolves
  on the PyPI project page (relative links only work on GitHub).

## 0.2.0 (2026-07-07)

### Added

- **`cf-publish r2 sync DIR BUCKET[/PREFIX]`** — sync a local folder to a
  Cloudflare R2 bucket over the S3-compatible API. AWS SigV4 implemented with
  the standard library (no boto3); diffing compares remote ETags against
  local MD5 so unchanged files are never re-sent; `--delete` removes
  remote-only objects; `--dry-run` / `--exclude` / `--quiet` / `--json` work
  like the Pages command. Credentials: R2_ACCESS_KEY_ID /
  R2_SECRET_ACCESS_KEY / CLOUDFLARE_ACCOUNT_ID. Single-PUT only (~5 GB/object).

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
