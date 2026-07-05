# cf-publish

ローカルのフォルダを **Cloudflare Pages** へ直接デプロイする Python 製 CLI。
wrangler も npm も Node.js も不要。`pip install` ひとつで使える。

```bash
pip install cf-publish
export CLOUDFLARE_API_TOKEN=...    # 「Cloudflare Pages: 編集」権限のトークン
export CLOUDFLARE_ACCOUNT_ID=...   # ダッシュボード概要ページに表示される ID
cf-publish ./public --project my-site
```

`./public` の中身がそのままサイトになる。プロジェクトが無ければ初回に作成される。

## なぜ作ったか

Direct Upload デプロイの公式手段は wrangler（Node.js 一式）だけ。ビルドが
Python のサイト（あるいはただのフォルダ）のために Node を入れるのは大げさなので、
wrangler と同じアップロードプロトコルを Python ~300 行・依存 2 つ
（httpx / blake3）で実装した。

- **内容アドレスなアップロード** — wrangler と同一方式のハッシュなので、
  変更のないファイルは二度と送られない（再デプロイが速い）
- 429/5xx への指数バックオフつき**並列アップロード**
- 送信前に Pages の制限（25 MiB/ファイル、20,000 ファイル/デプロイ）を**事前検証**
- `_headers` / `_redirects` はそのまま通る

## 使い方

```
cf-publish 公開ディレクトリ --project 名前 [オプション]

--branch BRANCH     main は本番、それ以外はプレビュー URL（既定: main）
--no-create         プロジェクトが無いとき作らずエラーにする
--exclude PATTERN   除外パターン（fnmatch。相対パスとファイル名に適用、複数可）
--dry-run           何が送られるかの表示だけで、デプロイしない
--quiet             デプロイ URL だけを表示
--json              結果を JSON で表示（url / files / unique / uploaded / duration）
```

進捗は stderr、結果は stdout に出るので、CI やパイプで扱いやすい。

### 認証情報

環境変数が優先。無ければ `~/.config/cloudflare/pages.env`（`KEY=VALUE` 形式）を読む。
トークンは dash.cloudflare.com → My Profile → API Tokens で
**Cloudflare Pages: 編集** 権限のものを作る。

### ライブラリとして

```python
from cf_publish import deploy, PagesError

result = deploy("./public", "my-site", on_progress=print)
print(result.url, result.uploaded, result.duration)
```

コアは `sys.exit()` も `print()` もせず、想定エラーは `PagesError` を投げる。

## 注意

- **非公式ツール**。Cloudflare とは無関係。wrangler が内部で使う半公式 API を
  話しているため、API が変わったら wrangler か Git 連携へ退避すること。
  ハッシュ方式は固定値テストで釘付けにしてあり、変更があれば静かに壊れず
  テストで検知できる。
- 隠しファイル・隠しディレクトリ（`.` 始まり）は送られない。
- シンボリックリンクは実体のコピーとして配信される（循環は検知して停止）。

## ロードマップ

- `cf-publish r2 sync` — 25 MiB 超のデータ配布向けに R2 への同期を同じ操作感で
- デプロイ一覧 / ロールバック

## ライセンス

MIT
