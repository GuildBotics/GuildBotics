# GuildBotics Desktop (macOS)

GuildBotics の macOS 向けデスクトップ GUI です。Tauri v2 + TypeScript frontend と、Python backend（Local API daemon）を sidecar として同梱します。

- 正式対応: macOS Apple Silicon (arm64)
- v1 は手動更新前提（自動更新なし）
- 設計方針の詳細は [../docs/desktop_app_plan.ja.md](../docs/desktop_app_plan.ja.md) を参照

---

## クイック操作スクリプト

リポジトリルートから以下を実行できます。通常の再テストではこちらを使うと、§2 / §3 の手順を直接打たずに済みます。

### ビルド

```bash
# Python sidecar のみ
scripts/desktop-build-backend.sh

# Tauri / DMG のみ（事前に sidecar が必要）
scripts/desktop-build-frontend.sh

# sidecar と Tauri / DMG をまとめて build
scripts/desktop-build-all.sh
```

`desktop-build-frontend.sh` は `desktop/src-tauri/binaries/guildbotics-app-api-aarch64-apple-darwin` を使って DMG を作ります。別ターゲットを使う場合は `DESKTOP_TARGET` を指定してください。

```bash
DESKTOP_TARGET=aarch64-apple-darwin scripts/desktop-build-all.sh
```

### 開発モード起動

```bash
# Local API backend のみ（既定: http://127.0.0.1:8765 / token: dev-token）
scripts/desktop-dev-backend.sh

# Vite frontend のみ（backend は別途起動しておく）
scripts/desktop-dev-frontend.sh

# backend と frontend をまとめて起動
scripts/desktop-dev-all.sh
```

backend の接続先は必要に応じて変更できます。

```bash
GUILDBOTICS_APP_API_PORT=8877 \
GUILDBOTICS_APP_API_TOKEN=local-token \
scripts/desktop-dev-all.sh
```

---

## テスト

### frontend unit / component（Vitest + React Testing Library）

```bash
cd desktop
npm ci
npm run test          # 一括実行
npm run test:watch    # ウォッチ
npm run quality       # format:check + lint + typecheck + duplicates + test（E2E は含まない）
```

### E2E（Playwright・実ブラウザ + 実 Local API backend）

jsdom では検証できない「実ブラウザ engine + 実 `client.ts ↔ FastAPI ↔ EventBus(websocket)` + 実ファイル書き込み」を貫く critical user journey を検証します。Tauri は使わず browser-preview mode で実 backend に接続し、各 journey を専用の temp workspace と backend / frontend ポートで隔離して起動します。

前提:

- リポジトリルートで Python 依存を解決済みであること（harness が `uv run python -m guildbotics.app_api` を起動するため）。

  ```bash
  uv sync --extra test --extra dev
  ```

- 初回のみ chromium を取得する。

  ```bash
  cd desktop
  npm run e2e:install
  ```

実行:

```bash
cd desktop
npm run e2e
```

`npm run e2e` は backend（temp workspace）と Vite を自動起動 → headless chromium で以下の journey を実行 → プロセスを停止します（ライフサイクルは `desktop/e2e/start-stack.mjs`、構成は `desktop/playwright.config.ts`）。

| spec | journey |
|---|---|
| `e2e/setup.spec.ts` | ① 初回 setup → 作成 → backend が `project.yml` を実書き込み |
| `e2e/service.spec.ts` | ③ scheduler / events を start → running → stop |
| `e2e/commands.spec.ts` | ④ command 実行 → `/commands/run` + `/events` ストリーム → history 反映 |
| `e2e/members.spec.ts` | ② member 追加 → `person.yml` 実永続 |
| `e2e/diagnostics.spec.ts` | ⑤ verify / scenario diagnostics 実行 → 結果描画 |
| `e2e/failure.spec.ts` | ⑥ backend down → Bootstrap error → 復帰 → retry |

補足:

- レポート / 成果物は `desktop/playwright-report/` と `desktop/test-results/`（いずれも `.gitignore` 済み）。
- E2E は `npm run quality` と通常 push CI には含めず、専用ジョブ（workflow_dispatch / nightly）で回す方針（`docs/test_gap_analysis.ja.md` 参照）。
- 接続先 host / ポートは `GUILDBOTICS_E2E_*` 環境変数で上書き可能（既定値は `playwright.config.ts`）。
- Tauri ネイティブ（packaged app / sidecar 起動 / file picker）の smoke は別ティアで、実 macOS + Tauri runtime が必要（§2〜§3 のビルド手順を参照）。

---

## 1. 前提ツール

| ツール | バージョン目安 | 用途 |
|---|---|---|
| [uv](https://docs.astral.sh/uv/) | 最新 | Python 依存解決・sidecar build |
| Node.js | 24 以上（CI は 24） | frontend build / Tauri CLI |
| Rust (rustup) | **stable 1.85 以上** | Tauri 本体の build |
| Xcode Command Line Tools | — | macOS 向けリンク・署名 |

### Rust ツールチェーンの注意（重要）

Tauri の依存に `edition2024` を要求するクレートがあるため、**Cargo 1.85 以上**が必要です。Homebrew 版の `rust`（standalone）が PATH 上で `rustup` より優先されていると、古い Cargo が使われて次のエラーで失敗します。

```
error: failed to download `idna_adapter vX.Y.Z`
  ...
  feature `edition2024` is required
```

対処（いずれか）:

```bash
# rustup の stable を最新化し、PATH 上で優先されるようにする
rustup default stable
rustup update
rustc --version   # 1.85 以上であることを確認

# arm64 ターゲットを追加
rustup target add aarch64-apple-darwin
```

`which -a cargo` で Homebrew 版（`/opt/homebrew/bin/cargo`）が先に来る場合は、ビルド時だけ rustup のツールチェーンを優先させることもできます。

```bash
PATH="$HOME/.rustup/toolchains/stable-aarch64-apple-darwin/bin:$PATH" \
  npm run tauri build -- --target aarch64-apple-darwin
```

---

## 2. 配布物（DMG）のローカルビルド手順

リポジトリのルートで実行します。

### 2.1 Python sidecar（Local API）を build する

GuildBotics は config 経由で brain / command を動的解決するため、PyInstaller の static import graph だけでは不足します。収集設定は [sidecar/guildbotics-app-api.spec](sidecar/guildbotics-app-api.spec) にまとめてあります。

```bash
# リポジトリルートで実行
uv sync --extra test --extra dev

uv run --with pyinstaller python -m PyInstaller \
  desktop/sidecar/guildbotics-app-api.spec \
  --noconfirm --clean \
  --distpath dist --workpath build/sidecar

# Tauri sidecar の配置規則に合わせてコピー（ターゲット triple のサフィックスが必須）
mkdir -p desktop/src-tauri/binaries
cp dist/guildbotics-app-api \
  desktop/src-tauri/binaries/guildbotics-app-api-aarch64-apple-darwin
```

生成される sidecar は onefile で約 200MB 強です。

> **動作確認（任意）**: 配置前に sidecar 単体を起動して health を確認できます。
>
> ```bash
> dist/guildbotics-app-api --host 127.0.0.1 --port 8765 --token dev-token &
> curl -H "X-GuildBotics-Session-Token: dev-token" http://127.0.0.1:8765/health
> # => {"status":"ok"}
> ```

### 2.2 frontend 依存をインストールして DMG を build する

```bash
cd desktop
npm install
npm run tauri build -- --target aarch64-apple-darwin
```

成果物:

```
desktop/src-tauri/target/aarch64-apple-darwin/release/bundle/dmg/GuildBotics_<version>_aarch64.dmg
```

（例: `GuildBotics_0.1.0_aarch64.dmg`。`<version>` は [src-tauri/tauri.conf.json](src-tauri/tauri.conf.json) の `version`）

DMG 内の `GuildBotics.app/Contents/MacOS/` に desktop 本体と sidecar `guildbotics-app-api` が同梱されます。secrets を設定していないローカルビルドは **ad-hoc 署名（実質 unsigned）** です。署名・notarization は次節を参照。

---

## 3. 開発モード（`tauri dev`）

毎回 PyInstaller を build せずに開発したい場合は、sidecar の配置先に「ソースを直接実行する薄いシェルスクリプト」を置くと高速に反復できます。

`desktop/src-tauri/binaries/guildbotics-app-api-aarch64-apple-darwin` を以下の内容で作成し、実行権限を付与します。

```sh
#!/bin/sh
set -eu
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
REPO_ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/../../.." && pwd)"
cd "$REPO_ROOT"
if command -v uv >/dev/null 2>&1; then
  exec uv run --no-sync python -m guildbotics.app_api "$@"
fi
exec python3 -m guildbotics.app_api "$@"
```

```bash
chmod +x desktop/src-tauri/binaries/guildbotics-app-api-aarch64-apple-darwin
cd desktop && npm run tauri dev
```

> `desktop/src-tauri/binaries/` は `.gitignore` 対象です。配布物を作る前には、必ず 2.1 の手順で本物の PyInstaller バイナリへ置き換えてください。

---

## 4. DMG のインストール手順（利用者向け）

1. `GuildBotics_<version>_aarch64.dmg` をダブルクリックしてマウントする。
2. 開いたウィンドウで **`GuildBotics.app` を `Applications` フォルダへドラッグ**する。
3. マウントしたディスクイメージを取り出す（Finder のサイドバーで取り出し、または `⌘E`）。

### 初回起動（unsigned / 未 notarized の場合）

ローカルビルドや署名なしの DMG は Gatekeeper にブロックされます。次のいずれかで開きます。

- **方法 A（推奨）**: `Applications` の `GuildBotics.app` を **右クリック → 開く** → 警告ダイアログで再度 **開く**。
- **方法 B**: 一度起動を試みた後、**システム設定 → プライバシーとセキュリティ** を開き、下部の「"GuildBotics" は…」の項目で **このまま開く** を押す。
- **方法 C（CLI）**: quarantine 属性を外す。

  ```bash
  xattr -dr com.apple.quarantine /Applications/GuildBotics.app
  ```

> signed + notarized された DMG ではこの操作は不要です。

### 起動後

- 初回起動時、アプリは同梱の sidecar（Local API）を起動します。**onefile sidecar の自己展開のため、初回は起動完了まで約 10 秒かかります**（2 回目以降は速くなります）。
- backend が立ち上がると、設定状態（config / `.env` / storage path）が画面に表示されます。

---

## 5. コード署名・notarization（任意）

CI（[../.github/workflows/desktop-macos.yml](../.github/workflows/desktop-macos.yml)）は、以下の GitHub Secrets が設定されている場合だけ署名・notarization を行います。未設定なら unsigned DMG を生成します。

| Secret | 内容 |
|---|---|
| `APPLE_CERTIFICATE` | Developer ID Application 証明書（base64 化した `.p12`） |
| `APPLE_CERTIFICATE_PASSWORD` | `.p12` のパスワード |
| `APPLE_KEYCHAIN_PASSWORD` | CI 上の一時 keychain 用パスワード |
| `APPLE_SIGNING_IDENTITY` | 署名 identity（例: `Developer ID Application: Name (TEAMID)`） |
| `APPLE_ID` | notarization 用 Apple ID |
| `APPLE_PASSWORD` | notarization 用 app-specific password |
| `APPLE_TEAM_ID` | Apple Developer Team ID |

`APPLE_CERTIFICATE` が無ければ署名 step を skip、`APPLE_CERTIFICATE` と `APPLE_ID` が揃った時だけ notarization を実行します。

---

## 6. トラブルシュート

| 症状 | 原因 / 対処 |
|---|---|
| `feature 'edition2024' is required` / `idna_adapter` の download 失敗 | Cargo が古い。rustup の stable 1.85+ を使う（§1 参照）。 |
| `tauri build` で sidecar が見つからない | §2.1 を実行し、`desktop/src-tauri/binaries/guildbotics-app-api-aarch64-apple-darwin` が存在するか確認。 |
| PyInstaller 実行時に `ModuleNotFoundError` | 動的 import されるモジュールが収集されていない。[sidecar/guildbotics-app-api.spec](sidecar/guildbotics-app-api.spec) の `hiddenimports` / `collect_all` 対象に追加する。 |
| GUI で PDF 変換（`to_pdf`）が使えない | v1 既知の制約。sidecar は `weasyprint` を同梱しない。PDF が必要な場合は native dependency を入れた CLI を使う。 |
| 「開発元を確認できません」で起動できない | §4 の初回起動手順（右クリック → 開く / `xattr` で quarantine 解除）。 |
| 初回起動が遅い | onefile sidecar の自己展開のため。約 10 秒待つ。 |
