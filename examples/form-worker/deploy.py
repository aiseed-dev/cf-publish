#!/usr/bin/env python3
"""deploy.py — one-shot tool to deploy worker.js + D1 to Cloudflare.

Standard library only (urllib, json, secrets, pathlib). Talks to the Cloudflare
REST API directly — no wrangler, no external packages. Re-runnable: the D1
database and PULL_TOKEN are reused across runs (PULL_TOKEN is saved next to this
file in deploy-state.json), and worker.js is overwritten. Running it again is how
you push a worker.js change.

Environment variables:
  CF_API_TOKEN      Cloudflare API token (permissions: Workers Scripts:Edit, D1:Edit)
  CF_ACCOUNT_ID     Cloudflare account id
  TURNSTILE_SECRET  Turnstile widget secret key (from the Turnstile dashboard)
  ALLOWED_ORIGIN    Origin of the site hosting the form, e.g. https://example.com

Usage:
  CF_API_TOKEN=... CF_ACCOUNT_ID=... TURNSTILE_SECRET=... \
    ALLOWED_ORIGIN=https://example.com python3 deploy.py
"""

import json
import os
import secrets
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.cloudflare.com/client/v4"
D1_NAME = "collection-point"
SCRIPT_NAME = "formrescue"
COMPAT_DATE = "2024-11-01"
WORKER_FILE = Path(__file__).with_name("worker.js")
STATE_FILE = Path(__file__).with_name("deploy-state.json")

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS inbox ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT,"
    "created_at TEXT NOT NULL DEFAULT (datetime('now')),"
    "name TEXT NOT NULL DEFAULT '',"
    "email TEXT NOT NULL DEFAULT '',"
    "body TEXT NOT NULL DEFAULT '',"
    "payload TEXT NOT NULL,"
    "ip TEXT"
    ")"
)


def api(method, path, token, data=None, headers=None, raw=False):
    """Call the Cloudflare API. `data` is JSON unless raw=True (bytes body)."""
    hdrs = {"Authorization": "Bearer " + token}
    if headers:
        hdrs.update(headers)
    body = None
    if raw:
        body = data
    elif data is not None:
        body = json.dumps(data).encode()
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(API + path, data=body, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise SystemExit(
            f"API {method} {path} failed: HTTP {e.code}\n{e.read().decode()}"
        )


def get_or_create_d1(token, account):
    res = api("GET", f"/accounts/{account}/d1/database", token)
    for db in res.get("result", []):
        if db.get("name") == D1_NAME:
            return db["uuid"]
    res = api("POST", f"/accounts/{account}/d1/database", token, {"name": D1_NAME})
    return res["result"]["uuid"]


def apply_schema(token, account, uuid):
    api(
        "POST",
        f"/accounts/{account}/d1/database/{uuid}/query",
        token,
        {"sql": SCHEMA},
    )


def upload_worker(token, account, uuid, pull_token, turnstile_secret, allowed_origin):
    script = WORKER_FILE.read_text(encoding="utf-8")
    metadata = {
        "main_module": "worker.js",
        "compatibility_date": COMPAT_DATE,
        "bindings": [
            {"type": "d1", "name": "DB", "id": uuid},
            {"type": "plain_text", "name": "ALLOWED_ORIGIN", "text": allowed_origin},
            {"type": "secret_text", "name": "PULL_TOKEN", "text": pull_token},
            {"type": "secret_text", "name": "TURNSTILE_SECRET", "text": turnstile_secret},
        ],
    }
    boundary = "----formrescue" + secrets.token_hex(16)
    b = boundary.encode()
    parts = [
        b"--" + b + b"\r\n",
        b'Content-Disposition: form-data; name="metadata"\r\n',
        b"Content-Type: application/json\r\n\r\n",
        json.dumps(metadata).encode() + b"\r\n",
        # The script part MUST be a file part whose Content-Type is
        # application/javascript+module, and its name must equal main_module.
        b"--" + b + b"\r\n",
        b'Content-Disposition: form-data; name="worker.js"; filename="worker.js"\r\n',
        b"Content-Type: application/javascript+module\r\n\r\n",
        script.encode("utf-8") + b"\r\n",
        b"--" + b + b"--\r\n",
    ]
    api(
        "PUT",
        f"/accounts/{account}/workers/scripts/{SCRIPT_NAME}",
        token,
        data=b"".join(parts),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        raw=True,
    )


def enable_workers_dev(token, account):
    api(
        "POST",
        f"/accounts/{account}/workers/scripts/{SCRIPT_NAME}/subdomain",
        token,
        {"enabled": True},
    )


def workers_dev_url(token, account):
    res = api("GET", f"/accounts/{account}/workers/subdomain", token)
    sub = (res.get("result") or {}).get("subdomain")
    return f"https://{SCRIPT_NAME}.{sub}.workers.dev" if sub else None


def load_or_make_pull_token():
    if STATE_FILE.exists():
        try:
            tok = json.loads(STATE_FILE.read_text())["pull_token"]
        except (ValueError, KeyError):
            # 黙って新トークンを生成するとWorker側だけ回転し、設定済みの
            # 管理アプリが全部401になる。壊れていることを伝えて止める。
            raise SystemExit(
                f"{STATE_FILE} が読めません(破損?)。トークンを回転して"
                "よければこのファイルを削除して再実行してください。"
            )
        STATE_FILE.chmod(0o600)  # 旧バージョンが緩い権限で作った分も締める
        return tok
    tok = secrets.token_urlsafe(32)
    # トークンを含むファイルなので所有者のみ読み書き可(0600)で作る
    STATE_FILE.touch(mode=0o600)
    STATE_FILE.write_text(json.dumps({"pull_token": tok}, indent=2))
    return tok


def main():
    token = os.environ.get("CF_API_TOKEN")
    account = os.environ.get("CF_ACCOUNT_ID")
    turnstile_secret = os.environ.get("TURNSTILE_SECRET")
    allowed_origin = os.environ.get("ALLOWED_ORIGIN")

    missing = [
        k
        for k, v in {
            "CF_API_TOKEN": token,
            "CF_ACCOUNT_ID": account,
            "TURNSTILE_SECRET": turnstile_secret,
            "ALLOWED_ORIGIN": allowed_origin,
        }.items()
        if not v
    ]
    if missing:
        raise SystemExit("Missing environment variables: " + ", ".join(missing))
    if not WORKER_FILE.exists():
        raise SystemExit(f"worker.js not found next to deploy.py ({WORKER_FILE})")
    if not allowed_origin.startswith("https://"):
        raise SystemExit("ALLOWED_ORIGIN must start with https://")

    pull_token = load_or_make_pull_token()

    print("1/5 D1 データベースを用意 ...")
    uuid = get_or_create_d1(token, account)
    print(f"    collection-point uuid = {uuid}")
    print("2/5 スキーマを適用 ...")
    apply_schema(token, account, uuid)
    print("3/5 worker.js をアップロード ...")
    upload_worker(token, account, uuid, pull_token, turnstile_secret, allowed_origin)
    print("4/5 workers.dev のURLを有効化 ...")
    enable_workers_dev(token, account)
    print("5/5 完了")

    url = workers_dev_url(token, account)
    print()
    print("=== FormRescue デプロイ完了 ===")
    if url:
        print(f"Worker URL      : {url}")
        print(f"  フォームの fetch 先 : {url}/submit")
        print(f"  管理アプリの Worker URL: {url}")
    else:
        print(
            "workers.dev のサブドメインが未登録です。"
            "ダッシュボード(Workers & Pages)で一度登録してから再実行してください。"
        )
    # トークンそのものは表示しない(ターミナルのログ・スクロールバック・
    # CI出力に平文で残るため)。実体は 0600 の state ファイルにだけ置く。
    print(f"PULL_TOKEN      : {pull_token[:4]}…(全体は {STATE_FILE.name})")
    print(f"ALLOWED_ORIGIN  : {allowed_origin}")
    print()
    print(
        f"PULL_TOKEN は {STATE_FILE.name}(所有者のみ読める0600)にある。"
        "管理アプリの設定にはそこからコピーする(再実行時も同じトークンを再利用)。"
    )


if __name__ == "__main__":
    main()
