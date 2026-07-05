import json

import pytest

import cf_publish.cli as cli
from cf_publish.pages import DeployResult, PagesError


RESULT = DeployResult(url="https://x.pages.dev", files=3, unique=3,
                      uploaded=1, duration=0.5)


def test_json_output(monkeypatch, capsys):
    monkeypatch.setattr(cli, "deploy", lambda *a, **kw: RESULT)
    cli.main(["site", "--project", "p", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert out["url"] == "https://x.pages.dev" and out["uploaded"] == 1


def test_quiet_prints_only_url(monkeypatch, capsys):
    monkeypatch.setattr(cli, "deploy", lambda *a, **kw: RESULT)
    cli.main(["site", "--project", "p", "--quiet"])
    assert capsys.readouterr().out.strip() == "https://x.pages.dev"


def test_pages_error_exits_1(monkeypatch, capsys):
    def boom(*a, **kw):
        raise PagesError("bad token")
    monkeypatch.setattr(cli, "deploy", boom)
    with pytest.raises(SystemExit) as exc:
        cli.main(["site", "--project", "p"])
    assert exc.value.code == 1
    assert "bad token" in capsys.readouterr().err


def test_options_are_forwarded(monkeypatch):
    seen = {}

    def fake(directory, project, **kw):
        seen.update(kw, directory=directory, project=project)
        return RESULT

    monkeypatch.setattr(cli, "deploy", fake)
    cli.main(["site", "--project", "p", "--branch", "preview",
              "--no-create", "--exclude", "*.map", "--dry-run"])
    assert seen["project"] == "p"
    assert seen["branch"] == "preview"
    assert seen["create"] is False
    assert seen["exclude"] == ["*.map"]
    assert seen["dry_run"] is True
