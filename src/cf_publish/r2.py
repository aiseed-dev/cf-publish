"""Sync a local directory to a Cloudflare R2 bucket (S3-compatible API).

Same design contract as :mod:`cf_publish.pages`: UI-agnostic, returns values,
raises :class:`PagesError`, reports progress via callback. AWS Signature V4
is implemented with the standard library only (hashlib/hmac) — no boto3.

Diffing: remote ETags from ListObjectsV2 are compared against local MD5
(for single-part uploads R2's ETag *is* the content MD5). Multipart ETags
(containing ``-``) never match and are re-uploaded. Single PUT only, so the
per-object ceiling is R2's ~5 GB single-upload limit.
"""

from __future__ import annotations

import hashlib
import hmac
import mimetypes
import os
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import quote

import httpx

from .pages import (ENV_FILE, PagesError, ProgressFn, _noop, _request,
                    _user_agent, collect, load_env_file)

R2_MAX_SINGLE_PUT = 5 * 1024 * 1024 * 1024  # single-PUT ceiling (no multipart)
UPLOAD_CONCURRENCY = 3
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


@dataclass
class R2SyncResult:
    bucket: str
    prefix: str
    files: int      # local files considered
    uploaded: int   # PUT (new or changed)
    skipped: int    # unchanged (ETag == local MD5)
    deleted: int    # removed remotely (--delete only)
    duration: float
    dry_run: bool = False


# ---------------------------------------------------------------- SigV4

def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def sign_v4(method: str, host: str, path: str, query: "dict[str, str]",
            headers: "dict[str, str]", payload_hash: str,
            access_key: str, secret: str, *, region: str = "auto",
            amz_date: "str | None" = None) -> "dict[str, str]":
    """Return headers with an AWS SigV4 ``Authorization`` added.

    ``path`` must already be URI-encoded per segment. Pinned against the
    AWS documentation test vector in tests/test_r2.py.
    """
    amz_date = amz_date or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    scope_date = amz_date[:8]
    all_headers = dict(headers)
    all_headers["host"] = host
    all_headers["x-amz-content-sha256"] = payload_hash
    all_headers["x-amz-date"] = amz_date

    signed_names = sorted(all_headers, key=str.lower)
    canonical_headers = "".join(
        f"{n.lower()}:{all_headers[n].strip()}\n" for n in signed_names)
    signed_header_list = ";".join(n.lower() for n in signed_names)
    canonical_query = "&".join(
        f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in sorted(query.items()))
    canonical = (f"{method}\n{path}\n{canonical_query}\n"
                 f"{canonical_headers}\n{signed_header_list}\n{payload_hash}")

    scope = f"{scope_date}/{region}/s3/aws4_request"
    to_sign = (f"AWS4-HMAC-SHA256\n{amz_date}\n{scope}\n"
               f"{hashlib.sha256(canonical.encode()).hexdigest()}")
    key = _hmac(_hmac(_hmac(_hmac(f"AWS4{secret}".encode(), scope_date),
                            region), "s3"), "aws4_request")
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()

    out = dict(all_headers)
    out["Authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
        f"SignedHeaders={signed_header_list}, Signature={signature}")
    return out


# ---------------------------------------------------------------- client

class R2Client:
    """Minimal S3-compatible client for R2: list / put / delete."""

    def __init__(self, account_id: str, access_key: str, secret: str,
                 transport: Optional[httpx.BaseTransport] = None):
        self.host = f"{account_id}.r2.cloudflarestorage.com"
        self.access_key = access_key
        self.secret = secret
        self.client = httpx.Client(
            base_url=f"https://{self.host}", timeout=300.0,
            headers={"User-Agent": _user_agent()}, transport=transport)

    def _call(self, method: str, path: str, query: "dict[str, str]" = {},
              body: bytes = b"", headers: "dict[str, str]" = {}) -> httpx.Response:
        payload_hash = hashlib.sha256(body).hexdigest() if body else _EMPTY_SHA256
        signed = sign_v4(method, self.host, path, query, headers, payload_hash,
                         self.access_key, self.secret)
        resp = _request(self.client, method, path,
                        params=query, content=body, headers=signed)
        if resp.status_code >= 400:
            raise PagesError(
                f"R2 API error HTTP {resp.status_code}: {method} {path}\n"
                f"{resp.text[:500]}")
        return resp

    def list_objects(self, bucket: str, prefix: str) -> "dict[str, str]":
        """{key: etag} for every object under prefix (paginated)."""
        out: dict[str, str] = {}
        token = None
        ns = "{http://s3.amazonaws.com/doc/2006-03-01/}"
        while True:
            q = {"list-type": "2", "prefix": prefix, "max-keys": "1000"}
            if token:
                q["continuation-token"] = token
            root = ET.fromstring(self._call("GET", f"/{bucket}", q).text)
            for c in root.findall(f"{ns}Contents"):
                out[c.find(f"{ns}Key").text] = c.find(f"{ns}ETag").text.strip('"')
            token = root.findtext(f"{ns}NextContinuationToken")
            if not token:
                break
        return out

    def put_object(self, bucket: str, key: str, data: bytes) -> None:
        ctype = mimetypes.guess_type(key)[0] or "application/octet-stream"
        self._call("PUT", f"/{bucket}/{quote(key)}", body=data,
                   headers={"content-type": ctype})

    def delete_object(self, bucket: str, key: str) -> None:
        self._call("DELETE", f"/{bucket}/{quote(key)}")


# ---------------------------------------------------------------- sync

def sync(directory: "str | Path", bucket_and_prefix: str, *,
         delete: bool = False, exclude: Iterable[str] = (),
         dry_run: bool = False, on_progress: ProgressFn = _noop,
         transport: Optional[httpx.BaseTransport] = None) -> R2SyncResult:
    """Sync ``directory`` into ``bucket[/prefix]`` and return the tally.

    Credentials: ``R2_ACCESS_KEY_ID`` / ``R2_SECRET_ACCESS_KEY`` /
    ``CLOUDFLARE_ACCOUNT_ID`` from the environment or ~/.config/cloudflare/
    pages.env. These are R2 *S3 API* tokens (dashboard → R2 → Manage API
    Tokens), not the Pages API token.
    """
    started = time.monotonic()
    load_env_file()
    account = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
    access_key = os.environ.get("R2_ACCESS_KEY_ID")
    secret = os.environ.get("R2_SECRET_ACCESS_KEY")
    if not (account and access_key and secret):
        raise PagesError(
            "set R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY and CLOUDFLARE_ACCOUNT_ID "
            f"in the environment or in {ENV_FILE} (R2 S3-API token, not the "
            "Pages token)")

    bucket, _, prefix = bucket_and_prefix.partition("/")
    if not bucket:
        raise PagesError(f"invalid bucket: {bucket_and_prefix!r}")
    prefix = f"{prefix.rstrip('/')}/" if prefix else ""

    root = Path(directory)
    if not root.is_dir():
        raise PagesError(f"not a directory: {root}")
    files = collect(root, exclude, max_file_size=R2_MAX_SINGLE_PUT, max_files=None)
    local = {prefix + url_path.lstrip("/"): p for url_path, p in files.items()}

    r2 = R2Client(account, access_key, secret, transport=transport)
    remote = r2.list_objects(bucket, prefix)

    to_upload = []
    skipped = 0
    for key, p in local.items():
        etag = remote.get(key)
        if etag and "-" not in etag and etag == hashlib.md5(p.read_bytes()).hexdigest():
            skipped += 1
            continue
        to_upload.append((key, p))
    to_delete = sorted(set(remote) - set(local)) if delete else []
    on_progress(f"{len(local)} files: upload {len(to_upload)}, "
                f"unchanged {skipped}, delete {len(to_delete)}")

    if dry_run:
        for key, _p in to_upload:
            on_progress(f"  would upload {key}")
        for key in to_delete:
            on_progress(f"  would delete {key}")
        return R2SyncResult(bucket, prefix, len(local), len(to_upload), skipped,
                            len(to_delete), time.monotonic() - started, dry_run=True)

    lock = threading.Lock()

    def send(item):
        key, p = item
        r2.put_object(bucket, key, p.read_bytes())
        with lock:
            on_progress(f"  put {key}")

    with ThreadPoolExecutor(max_workers=UPLOAD_CONCURRENCY) as pool:
        for f in [pool.submit(send, it) for it in to_upload]:
            f.result()
    for key in to_delete:
        r2.delete_object(bucket, key)
        on_progress(f"  deleted {key}")

    on_progress(f"synced to r2://{bucket}/{prefix}")
    return R2SyncResult(bucket, prefix, len(local), len(to_upload), skipped,
                        len(to_delete), time.monotonic() - started)
