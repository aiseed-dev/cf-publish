"""Core logic for deploying a folder to Cloudflare Pages (Direct Upload API).

This module is UI-agnostic: it returns values, raises :class:`PagesError`,
and reports progress through an ``on_progress`` callback, so it can back a
CLI, a GUI, or a build script equally well.

It talks to the same endpoints wrangler uses internally. The asset endpoints
(``check-missing`` / ``upload`` / ``upsert-hashes``) are not part of the
documented public API; if Cloudflare ever changes them, fall back to wrangler
or the Git integration.
"""

from __future__ import annotations

import base64
import fnmatch
import json
import mimetypes
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

import httpx
from blake3 import blake3

API = "https://api.cloudflare.com/client/v4"
MAX_FILE_SIZE = 25 * 1024 * 1024  # Pages hard limit: 25 MiB per file
MAX_FILES = 20_000  # Pages hard limit: files per deployment
SPECIAL_FILES = ("_headers", "_redirects")  # Pages config, attached to the deployment
BATCH_BYTES = 30 * 1024 * 1024  # raw bytes per upload call (base64 inflates ~1.33x)
BATCH_FILES = 500
UPLOAD_CONCURRENCY = 3  # same as wrangler
RETRY_DELAYS = (2.0, 4.0, 8.0)
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
ENV_FILE = Path.home() / ".config" / "cloudflare" / "pages.env"

ProgressFn = Callable[[str], None]


class PagesError(Exception):
    """Expected deployment failure: bad input, missing auth, or an API error."""


@dataclass
class DeployResult:
    url: str  # deployment URL ("" for a dry run)
    files: int  # files collected
    unique: int  # unique contents (after dedup by hash)
    uploaded: int  # files actually sent (rest were already cached)
    duration: float  # seconds
    dry_run: bool = False


def _noop(_msg: str) -> None:
    pass


def _user_agent() -> str:
    try:
        from importlib.metadata import version

        v = version("cf-publish")
    except Exception:
        v = "dev"
    return f"cf-publish/{v} (+https://github.com/aiseed-dev/cf-publish)"


def load_env_file(path: "Path | None" = None) -> None:
    """Read a KEY=VALUE env file; real environment variables take precedence."""
    if path is None:
        path = ENV_FILE
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def file_hash(data: bytes, suffix: str) -> str:
    """Content hash compatible with wrangler: blake3(base64(data) + suffix)[:32]."""
    b64 = base64.b64encode(data).decode()
    return blake3((b64 + suffix).encode()).hexdigest()[:32]


def _excluded(rel: str, patterns: Iterable[str]) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(name, p) for p in patterns)


_PAGES_DEFAULT = object()  # sentinel: resolve the Pages limits at call time


def collect(root: Path, exclude: Iterable[str] = (), *,
            max_file_size=_PAGES_DEFAULT, max_files=_PAGES_DEFAULT) -> "dict[str, Path]":
    """Map URL paths to files under ``root``.

    Hidden files and directories are skipped. Symlinks are followed (Pages has
    no notion of links, so they are served as copies); cycles are broken by
    tracking visited real paths. ``exclude`` holds fnmatch patterns tested
    against both the relative POSIX path and the bare filename. The limits
    default to the Pages hard limits; the R2 sync passes its own.
    """
    if max_file_size is _PAGES_DEFAULT:
        max_file_size = MAX_FILE_SIZE
    if max_files is _PAGES_DEFAULT:
        max_files = MAX_FILES
    exclude = tuple(exclude)
    files: dict[str, Path] = {}
    seen_dirs: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        real = os.path.realpath(dirpath)
        if real in seen_dirs:
            dirnames[:] = []
            continue
        seen_dirs.add(real)
        dirnames[:] = sorted(n for n in dirnames if not n.startswith("."))
        d = Path(dirpath)
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            p = d / name
            if not p.is_file():  # follows links; broken links are dropped here
                continue
            rel = p.relative_to(root).as_posix()
            if _excluded(rel, exclude):
                continue
            if p.stat().st_size > max_file_size:
                raise PagesError(
                    f"{p} exceeds the {max_file_size // (1024 * 1024)} MiB per-file limit")
            files["/" + rel] = p
    if not files:
        raise PagesError(f"no files to deploy under {root}")
    if max_files is not None and len(files) > max_files:
        raise PagesError(
            f"{len(files)} files exceed the {max_files} files-per-deployment Pages limit"
        )
    return files


def _request(client: httpx.Client, method: str, url: str, **kw) -> httpx.Response:
    """One HTTP call with exponential backoff on 429/5xx and transport errors."""
    last = ""
    for attempt, delay in enumerate((*RETRY_DELAYS, None)):
        try:
            resp = client.request(method, url, **kw)
        except httpx.TransportError as exc:
            last = f"network error: {exc}"
        else:
            if resp.status_code not in RETRY_STATUS:
                return resp
            last = f"HTTP {resp.status_code}"
        if delay is None:
            break
        time.sleep(delay)
    raise PagesError(f"{last} after {len(RETRY_DELAYS)} retries: {method} {url}")


def _ok(resp: httpx.Response) -> dict:
    try:
        body = resp.json()
    except ValueError:
        raise PagesError(
            f"non-JSON response (HTTP {resp.status_code}): {resp.request.url}"
        ) from None
    if not body.get("success"):
        raise PagesError(
            f"API error: {resp.request.url}\n"
            f"{json.dumps(body.get('errors'), ensure_ascii=False)}"
        )
    return body.get("result")


class Pages:
    """Thin client for the project-level Pages endpoints (documented API)."""

    def __init__(self, account_id: str, token: str,
                 transport: Optional[httpx.BaseTransport] = None):
        self.base = f"{API}/accounts/{account_id}/pages"
        self.client = httpx.Client(
            timeout=120.0,
            headers={"Authorization": f"Bearer {token}",
                     "User-Agent": _user_agent()},
            transport=transport,
        )

    def project_exists(self, project: str) -> bool:
        resp = _request(self.client, "GET", f"{self.base}/projects/{project}")
        return resp.status_code == 200 and resp.json().get("success", False)

    def create_project(self, project: str) -> None:
        _ok(_request(self.client, "POST", f"{self.base}/projects",
                     json={"name": project, "production_branch": "main"}))

    def upload_token(self, project: str) -> str:
        return _ok(_request(self.client, "GET",
                            f"{self.base}/projects/{project}/upload-token"))["jwt"]

    def deploy(self, project: str, manifest: "dict[str, str]", branch: str,
               special: "dict[str, bytes] | None" = None) -> dict:
        """Create the deployment.

        ``special`` holds the contents of root-level ``_headers`` /
        ``_redirects``. Like wrangler, they are attached to the deployment
        request as form fields so Pages parses and applies them — uploading
        them as ordinary assets would make Pages serve them as static files
        and ignore the rules entirely.
        """
        files: dict = {"manifest": (None, json.dumps(manifest))}
        for name, content in (special or {}).items():
            files[name] = (name, content)
        return _ok(_request(
            self.client, "POST", f"{self.base}/projects/{project}/deployments",
            data={"branch": branch},
            files=files,
        ))


def _build_batches(missing: "list[str]", by_hash: "dict[str, Path]") -> "list[list[dict]]":
    batches: list[list[dict]] = []
    batch: list[dict] = []
    size = 0
    for h in missing:
        p = by_hash[h]
        data = p.read_bytes()
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        batch.append({
            "key": h,
            "value": base64.b64encode(data).decode(),
            "metadata": {"contentType": ctype},
            "base64": True,
        })
        size += len(data)
        if size >= BATCH_BYTES or len(batch) >= BATCH_FILES:
            batches.append(batch)
            batch, size = [], 0
    if batch:
        batches.append(batch)
    return batches


def check_missing(client: httpx.Client, by_hash: "dict[str, Path]") -> "list[str]":
    return _ok(_request(client, "POST", f"{API}/pages/assets/check-missing",
                        json={"hashes": list(by_hash)}))


def upload_assets(token: str, by_hash: "dict[str, Path]",
                  on_progress: ProgressFn = _noop,
                  transport: Optional[httpx.BaseTransport] = None) -> int:
    """Upload contents missing from the Pages cache. Returns the upload count."""
    client = httpx.Client(
        timeout=300.0,
        headers={"Authorization": f"Bearer {token}", "User-Agent": _user_agent()},
        transport=transport,
    )
    missing = check_missing(client, by_hash)
    on_progress(f"uploading {len(missing)} / {len(by_hash)} files "
                f"(the rest are already cached)")

    batches = _build_batches(missing, by_hash)
    lock = threading.Lock()

    def send(batch: "list[dict]") -> None:
        _ok(_request(client, "POST", f"{API}/pages/assets/upload", json=batch))
        with lock:
            on_progress(f"  sent {len(batch)} files")

    with ThreadPoolExecutor(max_workers=UPLOAD_CONCURRENCY) as pool:
        for future in [pool.submit(send, b) for b in batches]:
            future.result()  # re-raise the first failure

    # Tell Pages the cached hashes are still in use (same as wrangler)
    _ok(_request(client, "POST", f"{API}/pages/assets/upsert-hashes",
                 json={"hashes": list(by_hash)}))
    return len(missing)


def deploy(directory: "str | Path", project: str, branch: str = "main",
           create: bool = True, exclude: Iterable[str] = (),
           dry_run: bool = False, on_progress: ProgressFn = _noop,
           transport: Optional[httpx.BaseTransport] = None) -> DeployResult:
    """Deploy ``directory`` to a Pages project and return a :class:`DeployResult`.

    Credentials come from ``CLOUDFLARE_API_TOKEN`` / ``CLOUDFLARE_ACCOUNT_ID``
    environment variables, falling back to ``~/.config/cloudflare/pages.env``
    (KEY=VALUE lines). With ``dry_run=True`` everything up to and including the
    cache check runs, but nothing is uploaded or deployed.
    """
    started = time.monotonic()
    load_env_file()
    token = os.environ.get("CLOUDFLARE_API_TOKEN")
    account = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
    if not token or not account:
        raise PagesError(
            "set CLOUDFLARE_API_TOKEN (a 'Cloudflare Pages: Edit' token) and "
            f"CLOUDFLARE_ACCOUNT_ID in the environment or in {ENV_FILE}"
        )

    root = Path(directory)
    if not root.is_dir():
        raise PagesError(f"not a directory: {root}")

    files = collect(root, exclude)

    # Root-level _headers/_redirects are Pages configuration, not assets:
    # they ride along on the deployment request (see Pages.deploy) instead of
    # being uploaded — otherwise Pages serves them as static files and the
    # rules never take effect. Copies in subdirectories stay ordinary assets,
    # which matches wrangler.
    special: dict[str, bytes] = {}
    for name in SPECIAL_FILES:
        p = files.pop("/" + name, None)
        if p is not None:
            special[name] = p.read_bytes()

    manifest: dict[str, str] = {}
    by_hash: dict[str, Path] = {}
    for url_path, p in files.items():
        h = file_hash(p.read_bytes(), p.suffix.lstrip("."))
        manifest[url_path] = h
        by_hash[h] = p
    on_progress(f"{len(files)} files ({len(by_hash)} unique)"
                + (f" + {', '.join(sorted(special))}" if special else ""))

    pages = Pages(account, token, transport=transport)
    exists = pages.project_exists(project)
    if not exists:
        if dry_run:
            on_progress(f"project does not exist yet: {project} "
                        f"({'would create' if create else 'error without --create'})")
        elif create:
            pages.create_project(project)
            exists = True
            on_progress(f"created project: {project}")
        else:
            raise PagesError(f"no such project: {project}")

    jwt = pages.upload_token(project) if exists else None

    if dry_run:
        uploaded = 0
        if jwt is not None:
            client = httpx.Client(
                timeout=120.0,
                headers={"Authorization": f"Bearer {jwt}",
                         "User-Agent": _user_agent()},
                transport=transport,
            )
            missing = check_missing(client, by_hash)
            uploaded = len(missing)
            for h in missing:
                on_progress(f"  would upload {by_hash[h].relative_to(root)}")
        on_progress("dry run: nothing was uploaded or deployed")
        return DeployResult(url="", files=len(files), unique=len(by_hash),
                            uploaded=uploaded,
                            duration=time.monotonic() - started, dry_run=True)

    uploaded = upload_assets(jwt, by_hash, on_progress, transport=transport)
    result = pages.deploy(project, manifest, branch, special)
    url = result.get("url", "")
    on_progress(f"deployed: {url or '(no URL in response)'}")
    return DeployResult(url=url, files=len(files), unique=len(by_hash),
                        uploaded=uploaded, duration=time.monotonic() - started)
