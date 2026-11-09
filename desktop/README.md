# GuildBotics の画面（`desktop/`）

画面の外枠は npm workspace の `@guildbotics/ui`（`../packages/ui`）に置きます。
`AppShell`、Mantine のテーマ、トークン・部品の CSS、フォーム部品、i18next の初期化、
Markdown の安全な HTML 化・表示・図の描画を共有します。ルート、API、翻訳辞書と
provider ごとの設定は各アプリ側が持ちます。CSS は Mantine、共有トークン、アプリ固有、
共有部品の順に読み込みます。`npm run quality` は共有パッケージの検査も実行します。

PR の目視確認用画像は `GUILDBOTICS_SCREENSHOT_PHASE=before npm run e2e -- visual.spec.ts`
（変更後は `after`）で `~/Documents/GuildBotics/screenshots/desktop/` に保存します。
画像は一時的なレビュー資料として扱い、ソースへコミットしません。保存先の親ディレクトリは
`GUILDBOTICS_SCREENSHOT_DIR`（desktop からの相対パス、または絶対パス）で指定できます。
実 backend の一時ワークスペースを使い、サービス・コマンド・診断・設定を light / dark で撮ります。
撮影時は一時環境の System health alerts を非表示にし、Workspace 欄をマスクします。
診断は動的なログを避けてメモリータブを撮ります。通常の E2E は撮影をスキップします。
CI で画像を artifact にする場合は、撮影を明示的に有効化した実行で
`GUILDBOTICS_SCREENSHOT_DIR=test-results/visual` とし、そのディレクトリを収集します。
既存の Desktop E2E workflow は撮影を有効化せず、desktop-quality も画像を生成しません。

GuildBotics の画面（React + TypeScript）です。Vite でビルドした `desktop/dist` を、GuildBotics のサーバー（`guildbotics server start`）がブラウザへ配ります（`crates/gb-server/src/ui.rs`）。サーバーの導入・起動・更新は、リポジトリの [README](../README.ja.md#インストール) を参照してください。

- 必要なもの: [uv](https://docs.astral.sh/uv/)（Python 環境）、Node.js 24 以上（CI は 24）
- アーキテクチャ上の位置づけ（Local API 境界）は [../docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md) を参照

---

## 画面をビルドする

サーバーが配る画面は、チェックアウトの `desktop/dist` です。画面のソースを変えたら、ブラウザで見る前にビルドし直します（`scripts/hub-server.sh update` も同じビルドを行います）。

```bash
npm ci
cd desktop
npx vite build
```

## 開発モード起動

本番とは別のデータディレクトリを指定して、リポジトリのルートからRustのサーバーを起動します。

```sh
scripts/dev.sh --data-dir /absolute/path/to/dev-data --no-open
```

既定の開発用ポートはGuildBoticsが8866、Branvilが7879です。画面だけを編集しながら確認する場合は、別のターミナルでViteの画面を起動します。

```sh
GUILDBOTICS_SERVER_PORT=8866 scripts/desktop-dev-frontend.sh
```

ブラウザ preview では、Vite dev server（`http://127.0.0.1:1420`）が画面を配り、`/api`（`/api/events` の WebSocket を含む）を backend へ転送します。ブラウザのセッションは入口リンクからしか始まりません。`guildbotics open --print`（同じ `GUILDBOTICS_SERVER_PORT` で実行）が表示する入口リンクの `http://127.0.0.1:<port>` を `http://127.0.0.1:1420` に置き換えて、60 秒以内に開きます。

コマンド画面の「新規作成」は、ダイアログ内で「AIに作成させる」と「自分で作成する」を切り替えます。AIアシスタントは質問には回答だけを返し、エディタやファイルを変更しません。ソース変更を依頼した場合は、現在のコマンドの更新と必要な補助コマンドの新規作成をまとめて提案します。提案ソースはチャット履歴には展開せず、エディタ側の読み取り専用ファイルタブへ「現在」「新規作成」「更新」を区別して表示します。「更新」タブは追加行と削除行を行番号付きの差分として表示し、「新規作成」タブは完成版ソースを表示します。各表示は領域内で縦横にスクロールできます。内容を確認して「変更を適用」を押した時点で、リビジョン競合と全ソースの妥当性を確認したうえで反映されます。新規ファイルだけを適用した場合、開いていた既存コマンドは切り替えません。自力作成ではコマンド名と形式を指定して従来どおり初期ファイルを作成します。通常の手動編集ではバリデーションエラーのある途中のソースも保存できますが、有効なソースへ直すまでは実行できません。自由記述本文を処理するコマンドは、個別の `text` 引数ではなく実行画面の「入力文」を使います。

---

## 最後の turn のモデル記録を確認

メンバー設定の「デフォルトの AI CLIツール」のカードには、このマシンに保持された
そのメンバーとツールの最新 turn 記録から、モデル名、設定による指定の有無、effort、実行日時を表示します。
「チーム既定を使う」が有効なメンバーにも、同じ情報を閲覧専用のカードで表示します。
ツールをメンバー個別に変更する場合は、このスイッチをオフにします。
「このマシン」の実行記録に基づく値で、別のマシンのログインや実行は含みません。
アクティビティ画面では、最新記録にモデル名がある場合、メンバーの既定のツール名の
右横にその名前を表示し、effort も記録されていれば `gpt-5.6-sol · high` のように併記します。
長い名前は末尾を省略し、カーソルを合わせると全体を確認できます。

カードにはモデル名の下に `Effort: high` と表示します。最新の turn に effort が記録されていない
場合は `Effort: 不明` と表示し、アクティビティでは effort を省略します。過去の turn の値や
現在の設定から補完しません。「デフォルト選択／明示的に設定」はモデルの指定元を表します。

モデル指定の有無は実行時に記録するため、後から設定を変えても過去の表示は変わりません。
この情報を含む実行記録が保持されていないツールは、カードに「このマシンに実行記録がありません」
と表示します。最新記録にモデル名が無い場合は、カードに「モデル名を取得できませんでした」
と表示し、アクティビティにはモデル名を表示しません。失敗した turn も最新記録になり得ます。

## テスト

### frontend unit / component（Vitest + React Testing Library）

```bash
npm ci
cd desktop
npm run test          # 一括実行
npm run test:watch    # ウォッチ
npm run quality       # format:check + lint + typecheck + duplicates + test（E2E は含まない）
```

入力停止から一定時間後の処理を検証するテストは、
必要なデータの取得と画面の表示を待ってから仮想時計を止めます。
`fireEvent`で入力やフォーカスのイベントを送り、`act`内の`vi.advanceTimersByTimeAsync`で
本番の期限の直前と到達時点まで時計を進めます。入力操作に掛かった実時間が待ち時間に含まれると、
遅い環境で入力途中の値が先に処理されるためです。

### E2E（Playwright・実ブラウザ + 実 Local API backend）

jsdom では検証できない「実ブラウザ engine + 実 `client.ts ↔ Rust Local API ↔ /api/events` + 実ファイル書き込み」を貫く critical user journey を検証します。browser-preview mode で実 backend に接続し、journeyごとに専用の一時ワークスペースとloopbackポートを使います。ブラウザはテストサーバーが同じポートで配る画面を開きます。

前提:

- リポジトリのルートで `npm ci` を実行し、`rust-toolchain.toml` のRustを用意します。テストは `gb-server` の `browser-test-server` をビルドして、実HTTP APIと使い捨てのメモリ内キーチェーンを起動します。

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

`npm run e2e` は最初に Chromium の起動と page 作成を preflight し、成功した場合だけ画面を build（`vite build` で `desktop/dist`）→ スタックごとに backend（temp workspace）を自動起動 → headless chromium で以下の journey を実行 → プロセスを停止します（preflight は `desktop/e2e/preflight.mjs`、ライフサイクルは `desktop/e2e/start-stack.mjs`、構成は `desktop/playwright.config.ts`）。画面は本番と同じく backend が配ります。各テストは `guildbotics open` と同じ入口から入ります。harness が owner token（temp HOME の `~/.guildbotics/data/server/<port>/owner.token`）で `POST /api/session/links` を呼び、返った入口リンクを Playwright が開いてセッションの cookie を得ます（`desktop/e2e/stack-context.ts` の `test`）。Chromium を起動できない sandbox では、journey を開始せず infrastructure failure として報告します。

| spec                      | journey                                                                                                                                                                                                                              |
| ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `e2e/setup.spec.ts`       | ① 初回 setup → 作成 → backend が `project.yml` を実書き込み                                                                                                                                                                          |
| `e2e/service.spec.ts`     | ③ scheduler / events を start → running → stop                                                                                                                                                                                       |
| `e2e/commands.spec.ts`    | ④ コマンド編集: 新規作成ダイアログのAI／自力切替、AIアシスタント Drawer の開閉、自力作成 → source 編集 → 保存（`/commands/files`）→ 実 file 反映。隔離環境を持てないこのスタックの端末では、要件「隔離環境」が理由つきで実行を止める |
| `e2e/members.spec.ts`     | ② member 追加 → `identity.yml`（identity root）と `member.yml`（workspace）の実永続                                                                                                                                                  |
| `e2e/diagnostics.spec.ts` | ⑤ verify / scenario diagnostics 実行 → 結果描画、トラブルシューティングAIドロワー → 実 `/diagnostics/troubleshoot` → エラー描画                                                                                                      |
| `e2e/decisions.spec.ts`   | チャット判定の共通 brain 割り当て → 保存 → reload 後も復元                                                                                                                                                                           |
| `e2e/failure.spec.ts`     | ⑥ 画面を開いたまま backend が止まる → 「届かない」通知 → 再起動 → retry で復帰                                                                                                                                                       |
| `e2e/environment.spec.ts` | ⑪ `GUILDBOTICS_E2E_ENVIRONMENT_FROM` を指定したときだけ: 自力作成したコマンドに貼り付けた添付を渡して実行 → 実 microVM で動き、結果が `/events` で画面に届く                                                                         |

補足:

- レポート / 成果物は `desktop/playwright-report/` と `desktop/test-results/`（いずれも `.gitignore` 済み）。
- 各スタックの backend は temp workspace / temp HOME で動きます。harness は temp HOME のエージェント隔離環境の runtime 置き場（`~/.guildbotics/data/msb`）にただのファイルを置き、その端末を「runtime を置けない端末」にします。製品はこの状態を fail-closed に扱うので、service を start しても snapshot の build（container image の取得）は始まらず、`brain: agent` の journey（⑤のトラブルシューティングAI）は turn の開始前に端末の拒否として失敗します。加えて harness は AI CLIツール（`codex` など）を即失敗するスタブで置き換えた bin ディレクトリを PATH 先頭に置き、その呼び出しログが空のままであることで実バイナリが一度も起動していないことを spec が確認します。
- ⑪ だけは、隔離環境が ready なワークスペースを `GUILDBOTICS_E2E_ENVIRONMENT_FROM` で指定したときに、その端末の隔離環境を借りて実 microVM を起動します。harness は宣言をコピーし、そのワークスペースの snapshot と、端末の runtime（`GUILDBOTICS_HOME`、無ければ `~/.guildbotics` の `data/msb`）をリンクするので、build も取得も始まりません。runtime は HOME の下に Unix ソケットを開き、macOS はそのパスを 103 バイトに制限するので、このスタックは `~/.gbe/<6 文字>` を HOME にし、停止時に消します。実機の runtime を使うので、利用者の承認を得て実行します。指定しなければこのスタックは起動せず、⑪ は理由つきで skip します（CI も同じ）。

- E2E は通常 push CI には含めません。`.github/workflows/desktop-e2e.yml` の専用 workflow が、関連ファイル（`desktop/**`、`guildbotics/**`、`pyproject.toml`、`uv.lock`、workflow 自体）を変更する pull request、手動実行、nightly で実行します。workflow はすべての pull request で起動し、関連ファイルの変更が無ければ Playwright を省略します。required check は常に報告される `Desktop E2E gate` だけで、E2E が不要なら成功、必要なら head/base の両方が成功した場合だけ成功します。pull request と手動実行では head/base を別ジョブで検証するため、同一 workflow 上で結果を比較できます。手動実行では `head_ref` と `base_ref` に branch、tag、または commit SHA を指定します（テスト戦略の全体は `AGENTS.md`「テスト実装の考え方」参照）。
- 各スタックの backend のポートは `GUILDBOTICS_E2E_*` 環境変数で上書き可能（既定値は `playwright.config.ts`）。backend は `127.0.0.1` だけで待ち受けます。
- 各スタックの owner token はその temp HOME の下に backend が作り、spec は stack context file（`<OS tmpdir>/guildbotics-e2e/<stack>.json`、0600）が示す temp HOME から読みます。
- `npx playwright test` を直接実行するときは、先に `npx vite build` で画面を build しておきます（harness は `desktop/dist/index.html` が無ければ起動しません）。
