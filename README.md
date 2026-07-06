# cf-publish

Deploy a local folder to **Cloudflare Pages** from Python — no wrangler,
no npm, no Node.js. One `pip install`, one command.

```bash
pip install cf-publish
export CLOUDFLARE_API_TOKEN=...    # token with "Cloudflare Pages: Edit"
export CLOUDFLARE_ACCOUNT_ID=...   # shown on the dashboard overview page
cf-publish ./public --project my-site
```

That's it. The contents of `./public` become the site. The project is created
on first deploy if it doesn't exist.

日本語の説明は [README.ja.md](README.ja.md) にあります。

## Why

The only official way to do a
[Direct Upload](https://developers.cloudflare.com/pages/get-started/direct-upload/)
deploy is wrangler, which drags in the whole Node.js toolchain. If your build
pipeline is Python (or just a folder of files), that's a lot of machinery for
one HTTP conversation. `cf-publish` implements the same upload protocol in
~300 lines of Python with two dependencies (`httpx`, `blake3`).

- **Content-addressed uploads** — files are hashed the same way wrangler
  hashes them, so unchanged files are never re-uploaded (fast repeat deploys,
  and the cache is shared with wrangler).
- **Concurrent uploads** with retry and exponential backoff on 429/5xx.
- **Pre-flight validation** of the Pages limits (25 MiB/file, 20,000
  files/deployment) before anything is sent.
- Root-level `_headers` / `_redirects` are attached to the deployment the
  way wrangler does it, so Pages actually parses and applies the rules
  (uploading them as plain assets would serve them as static files instead).

## Usage

```
cf-publish DIRECTORY --project NAME [options]

--branch BRANCH     'main' deploys to production, anything else gets a
                    preview URL (default: main)
--no-create         fail if the project doesn't exist instead of creating it
--exclude PATTERN   fnmatch pattern to skip, matched against the relative
                    path and the filename; repeatable (e.g. --exclude '*.map')
--dry-run           show what would be uploaded, deploy nothing
--quiet             print only the deployment URL
--json              print a JSON result (url, files, unique, uploaded,
                    duration, dry_run)
```

Progress goes to stderr, results to stdout, so both `--quiet` and `--json`
compose cleanly with shell pipelines and CI.

### Credentials

Environment variables win; otherwise `~/.config/cloudflare/pages.env` is read
(plain `KEY=VALUE` lines):

```
CLOUDFLARE_API_TOKEN=...
CLOUDFLARE_ACCOUNT_ID=...
```

Create the token at dash.cloudflare.com → My Profile → API Tokens with the
**Cloudflare Pages: Edit** permission. Nothing else is needed.

### As a library

```python
from cf_publish import deploy, PagesError

result = deploy("./public", "my-site", on_progress=print)
print(result.url, result.uploaded, result.duration)
```

The core raises `PagesError` on expected failures and never calls
`sys.exit()` or prints, so it embeds cleanly in build scripts and GUIs.

## Notes and caveats

- **Unofficial.** This project is not affiliated with Cloudflare. It speaks
  the same semi-official Direct Upload endpoints wrangler uses internally
  (`upload-token` / `check-missing` / `upload` / `upsert-hashes`). If
  Cloudflare changes them, fall back to wrangler or the Git integration —
  the hash algorithm is pinned by a fixed-value test so a breakage is caught
  loudly, not silently.
- Hidden files and directories (names starting with `.`) are never uploaded.
- Symlinks are followed and served as copies (Pages has no symlinks);
  cycles are detected and broken.

## Roadmap

- `cf-publish r2 sync` — push large data files to R2 with the same
  ergonomics (Pages caps files at 25 MiB; R2 is the natural home for data
  distribution and has free egress).
- Deployment list / rollback.

## License

MIT
