"""R2 sync tests: SigV4 pinned to the AWS documentation vector, sync logic
against a mocked S3 API (httpx.MockTransport)."""

from __future__ import annotations

import hashlib

import httpx
import pytest

import cf_publish.pages as pages
from cf_publish.pages import PagesError
from cf_publish.r2 import sign_v4, sync


def test_sigv4_aws_documentation_vector():
    """The canonical S3 GET Object example from the AWS SigV4 docs
    (examplebucket/test.txt, 20130524). If this breaks, every request
    signature is wrong."""
    empty = hashlib.sha256(b"").hexdigest()
    out = sign_v4(
        "GET", "examplebucket.s3.amazonaws.com", "/test.txt", {},
        {"range": "bytes=0-9"}, empty,
        "AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        region="us-east-1", amz_date="20130524T000000Z")
    assert out["Authorization"].endswith(
        "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41")
    assert "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date" in out["Authorization"]


ACCOUNT = "acc123"
LIST_XML = """<?xml version="1.0" encoding="UTF-8"?>
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
  <Name>data</Name>{contents}
</ListBucketResult>"""


class FakeR2:
    def __init__(self, objects: "dict[str, bytes]"):
        self.objects = dict(objects)
        self.puts: list[str] = []
        self.deletes: list[str] = []

    def transport(self):
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.headers.get("Authorization", "").startswith("AWS4-HMAC-SHA256")
        path = request.url.path
        if request.method == "GET" and path == "/data":
            contents = "".join(
                f"<Contents><Key>{k}</Key>"
                f"<ETag>&quot;{hashlib.md5(v).hexdigest()}&quot;</ETag></Contents>"
                for k, v in sorted(self.objects.items()))
            return httpx.Response(200, text=LIST_XML.format(contents=contents))
        key = path.split("/", 2)[2]
        if request.method == "PUT":
            self.objects[key] = request.content
            self.puts.append(key)
            return httpx.Response(200)
        if request.method == "DELETE":
            self.objects.pop(key, None)
            self.deletes.append(key)
            return httpx.Response(204)
        raise AssertionError(f"unexpected {request.method} {path}")


@pytest.fixture(autouse=True)
def creds(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", ACCOUNT)
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "rk")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "rs")
    monkeypatch.setattr(pages, "RETRY_DELAYS", (0.0,))


@pytest.fixture
def tree(tmp_path):
    (tmp_path / "a.txt").write_bytes(b"alpha")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.bin").write_bytes(b"beta")
    return tmp_path


def test_sync_uploads_new_and_changed_skips_same(tree):
    r2 = FakeR2({"pre/a.txt": b"alpha",        # 一致 → skip
                 "pre/sub/b.bin": b"STALE",    # 不一致 → 再アップロード
                 "pre/gone.txt": b"x"})        # ローカルに無い → delete 指定時のみ
    res = sync(tree, "data/pre", transport=r2.transport())
    assert (res.files, res.uploaded, res.skipped, res.deleted) == (2, 1, 1, 0)
    assert r2.puts == ["pre/sub/b.bin"]
    assert "pre/gone.txt" in r2.objects  # --delete なしでは残る


def test_sync_delete_removes_remote_only(tree):
    r2 = FakeR2({"pre/gone.txt": b"x"})
    res = sync(tree, "data/pre", delete=True, transport=r2.transport())
    assert res.deleted == 1 and r2.deletes == ["pre/gone.txt"]
    assert sorted(r2.puts) == ["pre/a.txt", "pre/sub/b.bin"]


def test_sync_dry_run_touches_nothing(tree):
    r2 = FakeR2({"pre/gone.txt": b"x"})
    msgs: list[str] = []
    res = sync(tree, "data/pre", delete=True, dry_run=True,
               transport=r2.transport(), on_progress=msgs.append)
    assert res.dry_run and res.uploaded == 2 and res.deleted == 1
    assert r2.puts == [] and r2.deletes == []
    assert any("would upload" in m for m in msgs) and any("would delete" in m for m in msgs)


def test_sync_no_prefix(tree):
    r2 = FakeR2({})
    res = sync(tree, "data", transport=r2.transport())
    assert sorted(r2.puts) == ["a.txt", "sub/b.bin"]
    assert res.prefix == ""


def test_missing_r2_credentials(tree, monkeypatch, tmp_path):
    monkeypatch.delenv("R2_ACCESS_KEY_ID")
    monkeypatch.setattr(pages, "ENV_FILE", tmp_path / "absent.env")
    with pytest.raises(PagesError, match="R2_ACCESS_KEY_ID"):
        sync(tree, "data")


def _server_side_verify(request: httpx.Request) -> None:
    """受信したバイト列から、サーバがやるのと同じ手順で署名を再計算して照合。"""
    from urllib.parse import parse_qsl
    raw = request.url.raw_path.decode()
    path, _, qs = raw.partition("?")
    q = dict(parse_qsl(qs, keep_blank_values=True))
    extra = {}
    if request.method == "PUT" and "content-type" in request.headers:
        extra["content-type"] = request.headers["content-type"]
    payload = hashlib.sha256(request.content).hexdigest()
    expected = sign_v4(request.method, request.url.host, path, q, extra,
                       payload, "rk", "rs",
                       amz_date=request.headers["x-amz-date"])
    assert request.headers["Authorization"] == expected["Authorization"], raw


def test_query_and_path_bytes_match_signature(tree):
    """送信バイト列が署名した正規化形と一致する（空白入りprefixの回帰）。

    以前は params= 経由で httpx が再符号化しており、空白が + になって
    署名（%20）とずれ、RFC3986どおりに検証するサーバでは403になり得た。
    """
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        _server_side_verify(request)
        calls.append(request.url.raw_path.decode())
        if request.method == "GET":
            return httpx.Response(200, text=LIST_XML.format(contents=""))
        return httpx.Response(200)

    sync(tree, "data/a b", transport=httpx.MockTransport(handle))
    listing = next(c for c in calls if "?" in c)
    assert "prefix=a%20b%2F" in listing
    assert "+" not in listing.split("?", 1)[1]
    # PUT のパスも空白が %20 のまま(署名と同一バイト)で送られている
    assert any(c.startswith("/data/a%20b/") for c in calls if "?" not in c)
