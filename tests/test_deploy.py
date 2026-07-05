"""End-to-end deploy tests against a mocked Cloudflare API (httpx.MockTransport).

No real account, no network: the mock emulates the full Direct Upload
conversation (project lookup, upload-token, check-missing, upload,
upsert-hashes, deployment) and records every request for assertions.
"""

from __future__ import annotations

import json

import httpx
import pytest

import cf_publish.pages as pages
from cf_publish.pages import PagesError, deploy


ACCOUNT = "acc123"
PROJECT = "my-site"


class FakeCloudflare:
    """Stateful mock of the Pages API."""

    def __init__(self, existing_projects=(), cached_hashes=(), fail_first=0):
        self.projects = set(existing_projects)
        self.cached = set(cached_hashes)
        self.fail_first = fail_first  # serve this many 503s before working
        self.uploaded: list[dict] = []
        self.upserted: list[str] = []
        self.deployments: list[dict] = []
        self.requests: list[str] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    @staticmethod
    def ok(result) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "errors": [], "result": result})

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append(f"{request.method} {path}")
        if self.fail_first > 0:
            self.fail_first -= 1
            return httpx.Response(503, json={"success": False, "errors": ["slow down"]})

        prefix = f"/client/v4/accounts/{ACCOUNT}/pages"
        if path == f"{prefix}/projects/{PROJECT}" and request.method == "GET":
            if PROJECT in self.projects:
                return self.ok({"name": PROJECT})
            return httpx.Response(404, json={"success": False, "errors": [{"code": 8000007}]})
        if path == f"{prefix}/projects" and request.method == "POST":
            self.projects.add(json.loads(request.content)["name"])
            return self.ok({"name": PROJECT})
        if path == f"{prefix}/projects/{PROJECT}/upload-token":
            return self.ok({"jwt": "test-jwt"})
        if path == "/client/v4/pages/assets/check-missing":
            hashes = json.loads(request.content)["hashes"]
            return self.ok([h for h in hashes if h not in self.cached])
        if path == "/client/v4/pages/assets/upload":
            batch = json.loads(request.content)
            self.uploaded.extend(batch)
            self.cached.update(item["key"] for item in batch)
            return self.ok(None)
        if path == "/client/v4/pages/assets/upsert-hashes":
            self.upserted = json.loads(request.content)["hashes"]
            return self.ok(None)
        if path == f"{prefix}/projects/{PROJECT}/deployments":
            self.deployments.append({"branch": "?"})
            return self.ok({"url": f"https://deadbeef.{PROJECT}.pages.dev"})
        raise AssertionError(f"unexpected request: {request.method} {path}")


@pytest.fixture
def site(tmp_path):
    (tmp_path / "index.html").write_text("<h1>hi</h1>")
    (tmp_path / "css").mkdir()
    (tmp_path / "css" / "style.css").write_text("body{}")
    return tmp_path


@pytest.fixture(autouse=True)
def creds(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "tok")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", ACCOUNT)
    monkeypatch.setattr(pages, "RETRY_DELAYS", (0.0, 0.0, 0.0))


def test_full_deploy_creates_project_and_uploads(site):
    cf = FakeCloudflare()
    result = deploy(site, PROJECT, transport=cf.transport())
    assert result.url.endswith("pages.dev")
    assert result.files == 2 and result.unique == 2 and result.uploaded == 2
    assert not result.dry_run
    assert PROJECT in cf.projects
    assert len(cf.uploaded) == 2
    assert sorted(cf.upserted) == sorted(i["key"] for i in cf.uploaded)
    assert len(cf.deployments) == 1


def test_cached_files_are_not_reuploaded(site):
    cf = FakeCloudflare(existing_projects=[PROJECT])
    deploy(site, PROJECT, transport=cf.transport())
    cf.uploaded.clear()

    result = deploy(site, PROJECT, transport=cf.transport())
    assert result.uploaded == 0
    assert cf.uploaded == []
    assert len(cf.deployments) == 2  # a new deployment still happens


def test_no_create_fails_on_missing_project(site):
    cf = FakeCloudflare()
    with pytest.raises(PagesError, match="no such project"):
        deploy(site, PROJECT, create=False, transport=cf.transport())
    assert cf.uploaded == []


def test_dry_run_uploads_and_deploys_nothing(site):
    cf = FakeCloudflare(existing_projects=[PROJECT])
    messages: list[str] = []
    result = deploy(site, PROJECT, dry_run=True, transport=cf.transport(),
                    on_progress=messages.append)
    assert result.dry_run and result.url == "" and result.uploaded == 2
    assert cf.uploaded == [] and cf.deployments == []
    assert any("would upload" in m for m in messages)


def test_retry_survives_transient_503(site):
    cf = FakeCloudflare(existing_projects=[PROJECT], fail_first=2)
    result = deploy(site, PROJECT, transport=cf.transport())
    assert result.url.endswith("pages.dev")


def test_retries_exhausted_raises(site):
    cf = FakeCloudflare(existing_projects=[PROJECT], fail_first=100)
    with pytest.raises(PagesError, match="HTTP 503"):
        deploy(site, PROJECT, transport=cf.transport())


def test_missing_credentials(site, monkeypatch, tmp_path):
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN")
    monkeypatch.setattr(pages, "ENV_FILE", tmp_path / "absent.env")
    with pytest.raises(PagesError, match="CLOUDFLARE_API_TOKEN"):
        deploy(site, PROJECT)


def test_env_file_fallback(site, monkeypatch, tmp_path):
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN")
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID")
    env = tmp_path / "pages.env"
    env.write_text(f"CLOUDFLARE_API_TOKEN=tok\nCLOUDFLARE_ACCOUNT_ID={ACCOUNT}\n")
    monkeypatch.setattr(pages, "ENV_FILE", env)
    cf = FakeCloudflare(existing_projects=[PROJECT])
    result = deploy(site, PROJECT, transport=cf.transport())
    assert result.url.endswith("pages.dev")
