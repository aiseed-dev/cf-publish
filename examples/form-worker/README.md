# FormRescue worker

Cloudflare側の2ファイル。設計書「1. Cloudflare」「deploy.py 仕様」の実装。

- **worker.js** — Cloudflare Worker本体。`POST /submit`(ブラウザ直POST、Turnstile
  siteverify + CORS)、`GET /items`(ページング取得、Bearer PULL_TOKEN)、
  `POST /ack`(人の確認後の削除。唯一の削除経路)。フレームワーク・npm不使用。
- **deploy.py** — worker.js と D1 をCloudflareに設置する道具。Python標準ライブラリ
  のみ、REST APIを直接呼ぶ。手元で一回実行する。

## 事前準備

1. Cloudflareアカウント(無料)。WorkerとD1は無料枠で足りる
2. Turnstileウィジェットを作成し、**site key**(HTMLに埋める)と **secret key**
   (下の TURNSTILE_SECRET)を取得
3. APIトークンを作成(権限: Workers Scripts:Edit、D1:Edit)

## デプロイ

```sh
CF_API_TOKEN=... CF_ACCOUNT_ID=... \
  TURNSTILE_SECRET=... ALLOWED_ORIGIN=https://example.com \
  python3 deploy.py
```

出力される **Worker URL** と **PULL_TOKEN** を管理アプリに設定する。PULL_TOKEN は
`deploy-state.json` に保存され(gitignore済み)、**再実行時も同じトークンを再利用**
する。worker.js を直したら同じコマンドで再実行すれば上書き更新される。

## 動作確認

```sh
# /items: 未設定なら {"items":[]}、トークン違いなら 401
curl "https://<worker>/items?after=0&limit=500" -H "Authorization: Bearer <PULL_TOKEN>"

# /submit: Turnstileトークンが無いので 403 が返れば siteverify が効いている
curl -X POST https://<worker>/submit \
  -H "Origin: https://example.com" -H "Content-Type: application/json" \
  -d '{"your-name":"テスト","your-message":"テスト送信"}'
```

正常系(200)は有効なTurnstileトークンが要るため、実ブラウザのフォーム送信で確認する。

## バインディング(deploy.pyが自動設定)

| 名前 | 種別 | 中身 |
|---|---|---|
| DB | D1 | データベース `collection-point` |
| ALLOWED_ORIGIN | plain_text | フォームを置くサイトのオリジン |
| TURNSTILE_SECRET | secret | Turnstileの secret key |
| PULL_TOKEN | secret | 生成した取得用トークン |
