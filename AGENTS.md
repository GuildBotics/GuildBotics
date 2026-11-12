# AGENTS.md

このファイルはリポジトリで作業するエージェント向けのガイドです。

## 最重要ルール

- ソースコードを正とし、変更後のコード量が最小になる設計を選ぶ。
- `.gitignore` 対象は参照しない。互換用フォールバックや旧形式の並行サポートを追加しない。
- 自然言語の意味判定は既存のLLM判定基盤を使う。キーワード列挙で代替しない。
- コメントとdocstringは英語で書く。
- 画面・CLI・API が利用者に返すエラーや案内は `guildbotics/templates/locales/` のキーで出す
  （Rust は `crates/gb-server/src/i18n.rs`、Python は `guildbotics.utils.i18n_tool.t()`）。固定文を書かない。

## 開発用の起動

```sh
scripts/dev.sh --data-dir /absolute/path/to/dev-data
```

開発用のデータとポート（両サービス共通で 8866）で両方の画面を開く。GuildBotics の画面は `/-/guildbotics/`、API（WebSocket を含む）は `/-/guildbotics/api` の下にある。作業用ワークスペースを選ぶ。ポートを変える場合も、本番のデータやワークスペースを指定しない。本番Hubの起動・更新は [macOS Hubの起動と更新](docs/hub-server.md) を参照する。HubはApple Silicon搭載のMac、CIはLinux。Windowsの手順はOpenSSHでHubへ接続するクライアントに限る。

## 実装と責務境界

| 変更するもの | 入口 |
| --- | --- |
| `guildbotics` の引数、端末での入出力、CLI・スキル導入 | `crates/guildbotics/src/main.rs`、`install_cli.rs`。CLIからサーバーを呼ぶ処理は `gb-server/src/client/` |
| API、画面のデータ、手動実行、巡回・定期実行・チャット | `gb-server/src/api.rs`、`dispatcher.rs`、`execution.rs`、`ticket_selector/`、`native_scheduler.rs` |
| コマンド、設定の優先順、チーム、brainスロット | `gb-config/src/commands.rs`、`catalog.rs`、`team.rs`、`resolved_guest.rs` |
| microVM、アクセス契約、認証情報の中継、スナップショット | `gb-environment/src/contract.rs`、`runtime.rs`、`broker.rs`、`gateway.rs`、`recipe.rs` |
| メンバーの操作、サービスの契約、実装の選択 | `gb-member`、`gb-ports`、`gb-integrations/src/factory.rs` |
| 設定保存、ワークスペースとidentity、共有書き込み、記録 | `gb-setup`、`gb-config/src/repository.rs`、`gb-workspace`、`gb-base/src/shared_writes.rs`、`gb-observability` |
| コマンドの実行、プロンプト、AI CLIのアダプター | `guildbotics/guest/entry.py`、`guildbotics/commands/`、`guildbotics/intelligences/` |

表のcrateは `crates/` 配下にある。Rustの依存は各crateの `Cargo.toml`、Pythonの区分は `pyproject.toml` の `[tool.importlinter]` を読む。記録のcrateは操作や画面を知らず、画面はproviderの応答を解釈しない。APIで画面向けの値を整え、データの判断は操作側へ置く。依存や公開範囲を広げる前に、既存の下位層の型や関数で表せるか確認する。

### コマンドと隔離環境

- Rust hostがコマンドの解決・宣言、チームとメンバー、スロット、許可範囲を決める。Python guestは受け取った `ResolvedGuest` の表で子コマンドを解決し、実行順・テンプレート・`Context.pipe`・`shared_state` を扱う。優先順やアクセス判定を二重に実装しない。設定と同梱テンプレートはデータとして両側から参照する。
- hostは1つのコマンドにmicroVMを1つ起動し、`guildbotics.guest.entry` に要求を渡す。子コマンドとAI CLI turnもその中で動く。返答は標準出力の最後の1行、コマンドのログは標準エラー。wireの正本は `guildbotics/intelligences/agent_runtime/wire.py`。変更時は `uv run --no-sync python scripts/generate-wire-schema.py` で生成し、`--check` で確認する。Rustの `gb-wire` が生成済みschemaを使う。[wireの仕様](wire/README.md) を参照する。
- guestのホームは `/opt/guildbotics/home`（`GUEST_HOME`）。hostのパスは元の絶対パスに1回だけマウントする。hostのホーム配下の明示的な許可先は、guestのホームに同名のリンクを作る。入れ子の許可先は一番外側だけリンクする。`guest_path()` は `/opt/guildbotics` または `/run/guildbotics` と重なるhostパスを拒否する。利用者には実体のパス（`realpath`）を伝える。
- hostの環境変数をguestへ継承しない。時刻と言語は `TZ` と `LANGUAGE` で渡す。実トークンやAPIキーはhostで保持し、許可した要求だけをメンバー窓口や認証ゲートウェイで中継する。1コマンドにつき一度に1 turn。終了時に貸し出しを取り消す。
- Pythonの共有パッケージは `commands` → `intelligences` → `runtime` → `loader` → `entities` → `utils` の順で依存する。共有はguestをimportせず、全共有パッケージはguestの入口から使われる。`lint-imports` と `tests/guildbotics/test_package_map.py` で、名前で読み込むbrainと同梱コマンドも確かめる。
- AI CLIの固定版は `guildbotics/intelligences/cli_agents.py`、Node.jsとスナップショットの固定値は `guildbotics/templates/environment/recipe.json`。Rustは生成したカタログとrecipeを読む。更新は [update-dependencies](.agents/skills/update-dependencies/SKILL.md) に従う。

### メンバー・provider・実行

- `gb-ports` の契約でコードホスト、チケット、チャットを扱う。チケットとPRはURL、コメント・レビュー・ジョブなどは項目のURLと組で意味を持つ不透明なID。任意機能に対応しないときはunsupportedを返す。空の成功は実際に0件の場合だけに使う。
- providerの選択は `gb-integrations/src/factory.rs` に置く。書き込みは対象サービスの設定が指す範囲を検査し、member gitはpushの資格情報を渡す前に同じ境界を通す。member gitにはユーザーのcredential helperを継承しない。判定は共通の巡回・readiness・選択処理に置き、providerは読み取った状態を渡す。契約テストではprovider間で同じ操作を確かめる。参照providerの `local` は実サービス固有のIDや名前空間をまねない。
- チケットの候補選択と実行前後の処理はticket selectorを通す。チャットは発生時刻を時間窓、`position` を順序に使う。返信や外部書き込みの前に依頼元スレッドの新着を確認する。実行が止まったときは作業の出どころに記録する。接続できないサービスで見送った予定は消化せず、連続失敗に数えない。
- 実行の記録とメンバー単位の排他は共通の実行境界に置く。手動・定期・巡回・イベントで別の完了判定を作らない。PRの完了判定は現在のheadのreadinessを使う。
- メンバーの手順の正本は [参照カタログ](guildbotics/templates/member/reference.md)。workflowの封筒、起動元ごとの情報、手順の本文を分ける。複数のプロンプトが同じ規則を使うときは共通の層に置く。

### 保存・秘密・記録

- 共有ファイルの更新は `gb-base/src/shared_writes.rs` の書き込み境界を通す。読み取り・変更・保存、rename、削除を同じプロセス間lockの中で行う。identityとworkspaceの両方に触る場合はidentityが先、共有lockと機能ごとのlockでは共有が先。端末固有の `local/` は共有の対象外。
- 設定の比較・書き込み・応答revisionの取得は `gb-config/src/repository.rs` で1つに扱う。設定以外へrevisionを広げない。書き込み経路のテストはURLの接頭辞でなく、何を書き換えるかで分類する。
- GuildBoticsの秘密の値は `local/secrets.enc.json` に暗号化して保存し、鍵はOSキーチェーンに置く。`config/secrets.yml` はキー名の索引。メンバーの資格情報はidentityに置き、秘密名のプレフィックスが別ワークスペースのIDとも衝突しないよう確認する。壊れた索引を空として扱わない。Branvilの上流の資格情報とwebhookのURLも、`gb-server/src/branvil_vault.rs` がidentityのstoreに `branvil_` で始まる小文字の名前で置く（メンバーの秘密名は大文字なので衝突しない）。
- 共有記録には端末固有の値を入れない。値を持たせずに済む項目は型で防ぎ、記録のマスクとサイズ制限は共通の処理で行う。診断の文脈を捨てるためにエラー全体を隠さない。

GuildBotics の共有記録を古い版で読めない形へ変えるときは `SHARED_RECORD_SCHEMA_VERSION` を上げ、`crates/gb-server/src/hub_data.rs` の `server hub-data upgrade` に直前の1版からの変換と `check` の検査を追加する。それより古い形式を通常読み取りや変換で並行サポートしない。変換は明示した退避の写しだけでも実行できる形にし、失敗時に本物へ切り替えないことと、旧ビルドの検査で停止することをテストする。項目追加だけだった版1〜4は変換せず検査する。

### HTTP 経路の認証分類

- 新しい経路は登録と同時に `loopback_auth::RouteAuth` で分類する。Branvil は `classified_routes!`、GuildBotics API と画面外の経路は `api.rs` の `routes!` を使う。画面は `ui.rs` の共通パス定義へ加える。分類表とは別に `.route()` で追加しない。
- 分類にはオーナーの Bearer またはセッション＋CSRF、メンバー・アプリ・ランナーの Bearer またはセッション＋CSRF、セッション、オーナーか同意トークン、Git Basic、hook secret、認証なしを使う。メンバー内の権限は domain が検査する。
- 全経路の Host／Origin 検査を分類テストで固定する。hook は正確な登録パスだけを secret 認証の例外とし、WebSocket はハンドシェイク後の専用検査を維持する。Branvil の経路を `/-/guildbotics/` に登録しない。

### Branvil

- `branvil` crateはGuildBoticsのcrateに依存しない。`branvil` と `loopback-auth` のテストも、両 crate の `Cargo.toml`・`src`・`tests`・`examples` の外のファイルを読まない（CI の `rust-test-branvil` は、GuildBotics の crate だけを変えた PR の head では流れない）。データの入口は同じサービスの公開API（`api::DataApi` と認証後の `MemberApi`）。HTTP APIはその上にあり、CLIと画面はHTTPで使う。組み込みproviderは別のstoreを開かない。停止中に直接データを扱うのは `guildbotics branvil init`、`admin check|reindex|upgrade`、`restore`。
- 保存形式を変える場合は互換コードを追加しない規則の例外として、`crates/branvil/src/store.rs` の `SCHEMA_VERSION` を上げ、`upgrade.rs` に直前の1版からの変換を置く。`admin upgrade` と `restore` が使う。古い版を通常の読み取りで扱わない。Branvilの `secrets/` にはowner tokenだけを置く。ほかの秘密は起動するプロセスが渡す `vault::Vault` 越しに保存し、平文のファイルへ逃がさない（`branvil` 単体では `BRANVIL_VAULT` のディレクトリに、名前ごとに `BRANVIL_VAULT_KEY` で暗号化したファイル）。カーソルの署名鍵はプロセスごとにメモリで作る。実データやtokenの中身を参照しない。
- HTTP契約を変えたら [API文書](crates/branvil/docs/api.md) とRustの組み込みproviderを合わせ、`scripts/branvil-integration.sh` で公開APIとの結合を確認する。上流サービス固有の処理は `upstream::Host` の実装に置く。自然言語の意味判定はGuildBoticsが扱う。
- loopbackだけで待ち受ける。リモート接続はOSが管理するOpenSSHの転送を使う。HubはmacOS、CIはLinux。サービスのGitは `src/git.rs` を通し、システム・ユーザーのGit設定や `GIT_*` を継承しない。テストでは `tests/common/mod.rs` の `git` / `git_as` を使う。自前で起動する場合は `GIT_CONFIG_NOSYSTEM=1`、空の `GIT_CONFIG_GLOBAL`、`GIT_TERMINAL_PROMPT=0`、`-c credential.helper=` を指定し、トークンは `GIT_ASKPASS` と環境変数で渡す。URLや引数に含めず、テストのメンバー名は架空のものにする。
- 画面のテーマ・トークン・Mantineの版はGuildBotics Desktopとそろえる。共通部品は `packages/ui/` に置く。詳細は [Branvilの構成](crates/branvil/docs/architecture.md) と [文書一覧](crates/branvil/README.md#documents) を参照する。

## Hub の手作業が必要な更新

ポート変更など、更新後にオーナーの作業が必要な変更を入れるチケットでは、ルートの `hub-update.json` の `manual_update_version` を直前の番号より上げ、`instructions` に `docs/` 配下の手順書への相対パスを書く。番号は最後に手作業が必要だった版を表し、その後の通常更新でも下げない。宣言はビルドに埋め込まれ、`guildbotics server hub-migration` が読む。宣言のない版の番号は 0 とする。コマンドの契約は [Hub の起動と更新](docs/hub-server.md#自動更新用の内部コマンド) を参照する。

## 開発時の基本コマンド（CI準拠）

Pythonは `.python-version` の3.15、Rustは `rust-toolchain.toml` の固定版を使う。CIの開発イメージも同じ版にそろえる。Rustのテストには `cargo install --locked cargo-nextest` でnextestを用意する。BranvilのGit lockはプロセス内に1つなので、テストごとにプロセスを分ける。通常の `cargo test` はスレッド数を1にしても同じプロセスを使う。

```bash
uv sync --extra dev --extra test
uv run --no-sync ruff format --check guildbotics tests
uv run --no-sync ruff check guildbotics
uv run --no-sync mypy guildbotics
uv run --no-sync pylint guildbotics
uv run --no-sync lint-imports
uv run --no-sync python scripts/generate-wire-schema.py --check
cargo build --locked -p gb-base --example worktree-list
uv run --no-sync pytest tests/ --ignore=tests/blackbox --ignore=tests/integration -n auto
cargo fmt --all --check
cargo clippy --locked --workspace --all-targets -- -D warnings
cargo nextest run --locked --workspace
lychee --no-progress --scheme file --include-fragments --extensions md './*.md' docs desktop skills crates
```

CI は Rust のテストを 2 つのジョブに分ける。`rust-test-branvil` が `branvil` と `loopback-auth` を、`rust-test` が残りの crate を流す。PR の head では `paths` に合う変更があるジョブだけが流れるので、各ジョブの `paths` には、テストする crate がビルドするワークスペースの crate をすべて含める（対象と `paths` は `tests/test_cargo_workspace.py` が固定する）。

黒箱テストは `GUILDBOTICS_BLACKBOX_SERVER` と `GUILDBOTICS_BLACKBOX_CLI` で
Rustの入口へ向ける形で残す。サーバーには `cargo build --locked -p gb-server --example browser-test-server`
で作る `target/debug/examples/browser-test-server` を指定し、API の接頭辞は製品と同じ `/-/guildbotics/api` とする。OS キーチェーンを使わない。
`GUILDBOTICS_BLACKBOX_MEMBER_CLI` にも example の絶対パスを指定する。member は
同じ example 内の製品クライアントを使い、実行ファイル hash の照合を保持する。
その他の CLI 操作は `GUILDBOTICS_BLACKBOX_CLI` の製品バイナリを直接使う。サーバー単独の黒箱テストも CI で実行する。
desktop journey は Rust HTTP API と一時 vault で実行する。golden の本番コピー比較は
CI に含めず、ワークフローで実行する。隔離環境を含むjourneyと画面の目視は利用者の立ち会いで行う。
Branvilの画面E2Eは継続する。

CLIを変更したら `cargo run --locked -p guildbotics -- generate-cli-reference` で [CLIリファレンス](docs/cli_reference.md) を再生成し、READMEの例も照合する。同梱コマンドの仕様を変更したらカスタムコマンドガイドの両言語を確認する。Hubのスクリプトは `sh -n scripts/hub-server.sh` と `cargo nextest run --locked -p guildbotics --test hub_server` で検査し、LaunchAgentの実登録は承認を得て行う。Branvilの実microVMは `BRANVIL_REAL_EXECUTOR=1` の任意実行。

## 品質とテスト

エージェント作業時の品質確認:

- 確認コマンドは、CI とこのファイルに明記された範囲、および変更内容に直接対応する関連テストに限定する。指定範囲より広い確認（例: CI で `ruff check` 対象外の `tests/` に対する `ruff check`、関連範囲を超える全量 E2E、LaunchAgent の実機での登録など）は、ユーザーが明示した場合、または事前に目的・追加コストを説明して承認を得た場合だけ実行する。「念のため」の広範囲チェックを無断で追加しない。
- Python コードを変更したら、原則として `ruff format --check` と `ruff check` と `mypy` と `lint-imports` と関連 `pytest` を実行してから完了報告する（`ruff check` と `ruff format --check` は別物。`ruff format --check` の対象は `guildbotics` と `tests`、`ruff check` / `mypy` / `pylint` の対象は `guildbotics` のみ。整形漏れは CI の `test` ジョブで落ちるため、`ruff format --check` を必ず含める。整形が必要なら `uv run --no-sync ruff format guildbotics tests` を実行）
- Rust のコードを変更したら、原則として `cargo fmt --all --check` と `cargo clippy --locked --workspace --all-targets -- -D warnings` と関連する `cargo nextest run` を実行してから完了報告する（Branvil の結合テストは 1 つの test crate なので、1 ファイルだけなら `cargo nextest run -p branvil --test main <file>::`）。重複は `npx --yes jscpd@5.0.14 --config jscpd.rust.json` で確かめる。`crates/branvil/web` を変更したら、そこで `npm run lint`・`npm run typecheck`・関連する `npm run test` を実行する
- 重複コード確認は `uv run --no-sync pylint guildbotics` を使う（`pyproject.toml` で `duplicate-code` のみ有効化）
- 対象範囲（リポジトリ直下、`docs/`、`desktop/`、`skills/`、`crates/`）の Markdown を変更したら、上記の `lychee` コマンドを実行する
- 最低限の確認コマンドは上記「開発時の基本コマンド（CI 準拠）」と同じ（`pytest` は関連範囲に絞ってよい）
- 型エラーや lint エラーを回避するためだけの `# type: ignore` や noqa は、理由が明確でない限り追加しない

desktop TypeScript 開発時の品質確認:

- Node.js は CI と合わせて 24 系を前提にする
- frontend の依存更新は `package-lock.json` も更新する
- TypeScript / React コードを変更したら、原則として `npm run format:check`、`npm run lint`、`npm run typecheck`、`npm run duplicates`、関連する `npm run test` を実行してから完了報告する
- 整形が必要な場合は `desktop` で `npm run format` を実行し、Prettier の結果を正とする
- React コンポーネントの挙動変更時は React Testing Library によるコンポーネントテストを追加・更新する
- 純粋関数、入力変換、API payload 生成、trace / scheduler 表示ロジックなどの分岐を変更した場合は Vitest のユニットテストを追加・更新する
- 重複コード抑止は `npm run duplicates` (`jscpd`) を使う。重複検出を避けるためだけの不自然な分割ではなく、UI とロジックの責務が自然に分かれる形へ整理する
- 生成物を frontend 品質チェックへ巻き込まない。対象は `desktop/src` と frontend 設定ファイルを基本とする
- 実ブラウザ + 実 backend を貫く critical user journey（setup→作成 / scheduler start-stop / command 実行+ストリーム / diagnostics / backend down→retry）を変更したら、`desktop/e2e/*.spec.ts` の該当 Playwright journey を更新する。E2E は `npm run quality` / push CI には含めず、ローカルの `npm run e2e` または専用の `Desktop E2E` workflow で実行する（command 実行+ストリームは隔離環境を持てる端末で `GUILDBOTICS_E2E_ENVIRONMENT_FROM` を指定したときだけ走る）
- E2E に振る舞いパターンを総当たりで持ち込まない。分岐網羅は Vitest の unit / component（mock 境界）に置き、Playwright は jsdom では検証できない実ブラウザ + 実ワイヤ契約（`client.ts ↔ Rust HTTP API ↔ events`）+ 実ファイル書き込みに絞る

## テスト実装の考え方

このリポジトリでは、テストピラミッドに従ってテストを維持する。修正や機能追加の際には「既存テストが通ること」だけでは不十分で、変更した振る舞いを検出できるテストが追加・更新されていることを完了条件に含める。

基本方針:

- Unit test を最も厚くする。純粋関数、入力変換、validator、payload 生成、状態遷移、エラー変換、ファイル解決順はまず unit test で網羅する
- Component / service integration test は、UI 操作、API endpoint、config 書き込み、runtime lifecycle など境界をまたぐ主要 workflow に限定して追加する
- ブラウザ E2E（Playwright, `desktop/e2e/`）は lean-but-real。実ブラウザ engine + 実 Local API backend でしか検証できない critical user journey（setup→実ファイル書き込み、scheduler start/stop、command 実行+`/events` ストリーム、diagnostics、backend down→retry）に絞り、振る舞いパターンの総当たりはしない（分岐網羅は unit / component に委譲）。通常の push CI には含めず、ローカルの `npm run e2e` または専用の `Desktop E2E` workflow で実行する
- LLM、GitHub、Slack、外部 CLI などへの実通信は通常 CI のテストに入れない。既存抽象化、stub、mock、fixture を使い、送信 payload、判定結果、エラー処理を検証する
- どのテストも既定で 120 秒で時間切れになり、止まらずに失敗する（`pyproject.toml` の `[tool.pytest.ini_options]` の `timeout`。pytest-timeout）。実機の隔離環境と provider CLI を使う任意実行のテストは、module の `pytestmark` に `pytest.mark.real_device("GUILDBOTICS_CONTRACT_PROBE")` のように opt-in 環境変数を渡す。このマーカーのテストは 30 分で時間切れになる
- テストは決定論的かつ hermetic に保つ。時間・乱数・環境変数・cwd・HOME・I/O は `monkeypatch` / `tmp_path` で制御し、実 home ディレクトリや外部サービスに触れない
- `tests/conftest.py` が通常テストの HOME・USERPROFILE と workspace を一時ディレクトリへ隔離する。
- 通常の pytest は収集時から loopback（`127.0.0.1` / `::1` / `localhost`）以外の Python socket 接続と名前解決を失敗させる。子プロセスには到達不能な HTTP proxy と `GIT_ALLOW_PROTOCOL=file` を渡す。これらを無視する CLI（直接起動した `ssh` など）はテスト側で stub にする。接続試行の調査には `--network-audit` を使う。この監査で記録できるのは Python プロセス内の接続試行で、子プロセスの通信は含まない。`real_device` を付けたテストは opt-in が有効なときだけ実行し、その実行だけをネットワーク制限から除外する。opt-in は通常、指定した環境変数の値が `1` のとき有効になる。`GUILDBOTICS_E2E_ENVIRONMENT_FROM` は空でないワークスペースのパスを指定したとき有効になる
- `real_device` を付けた実機・provider 接続テストは、通常の CI、定期ジョブ、エージェントによる一括テストに含めず、pytest-xdist で並列実行しない。個別に実行する前に、対象と外部通信・実機の認証状態への影響を示してユーザーの明示的な承認を得る。ユーザーが実行に立ち会い、進行と結果を確認できる状態でのみ実行する。opt-in 環境変数の設定は承認や立ち会いの代わりにならない
- snapshot のみで品質を担保しない。ユーザーが観測する文言・状態、生成 request、保存 file/env、publish event、return value を具体的に assert する
- テストコードも本体コードと同じ品質対象とする。重複 fixture や場当たり的 mock が増えた場合は helper / factory へ整理する

変更種別ごとの必須確認:

- 純粋関数、入力変換、validation、parser、serializer を変更したら、正常系・境界値・不正入力の unit test を追加・更新する
- API endpoint、実行境界、setup、設定書き込みを変更したら、処理単体のテストとRust HTTP APIの結合テスト の両方を検討する
- scheduler / event listener / websocket / lifecycle を変更したら、状態遷移、二重起動防止、停止、timeout、event/log publish を検証する
- command runner / command spec / command discovery を変更したら、`Context.pipe`、`shared_state`、person-specific fallback、localized file precedence、child command failure を検証する
- desktop の API client を変更したら、URL、method、header、body、query parameter、error response、websocket status を検証する
- desktop の React component を変更したら、React Testing Library でユーザー操作と表示状態を検証する。implementation detail の state ではなく role/text/value/payload を assert する
- desktop の setup / commands / diagnostics / service runtime の workflow を変更したら、component test または mock API integration test を追加・更新する
- desktop の cross-boundary user journey（実 backend を貫く setup / runtime / commands / diagnostics / 接続断）を変更したら、`desktop/e2e/` の該当 Playwright spec を追加・更新する（実ブラウザ + 実 backend で検証。総当たりはせず代表 journey に絞る）
- i18n 文言や翻訳キーを変更したら、キー経由の検証を行い、片方の言語だけ欠落しないことを確認する
- bug fix では、先に再現テストまたは同等の failing assertion を追加し、そのテストが修正後に通ることを確認する

テスト追加を省略してよいのは、コメント修正、内部ドキュメントだけの変更、format のみ、型だけの機械的変更など、実行時の振る舞いが変わらないと明確に説明できる場合に限る。その場合も完了報告で「テスト追加不要の理由」を明記する。

### 失敗したテスト・チェックの扱い

自分が実行したテストやチェックが失敗した場合、原因が自分の変更かどうかに関わらず放置しない。「今回の変更とは無関係」は切り分けの結論であって、対応を終えてよい理由ではない。

- まず切り分ける。推測で結論せず、変更を戻した状態（`git stash` や main）で同じ対象を実行し、自分の変更が原因かを確認する。自分のせいだと決めつけることも、自分のせいではないと決めつけることも、同じように避ける
- 自分の変更が原因なら直す。原因でないと確認できた場合も、そこで止めずに失敗の原因まで特定する
- 「環境依存」「flaky」で説明を終えない。なぜその環境でだけ失敗するのかまで追う。環境差そのものが欠陥を隠していることがある（例: ローカルでは実 CLI ツールを起動して失敗し、CI では未インストールのため即座に失敗して緑になる）
- 原因を特定したら、issue の下書きを書く前に、`member ticket read --resource tracker_tickets --subject <tracker> --params '{"text":"<検索語>","state":"all"}'` で同じ tracker を検索する。`text` は全語一致なので、長い文を一度だけ検索せず、部品名やコマンド名＋症状など短い語の組を複数、同義語・英語/日本語の言い換えでも試す。語の組ごとに同じ条件で continuation がなくなるまで読む。失敗・不完全な検索は「該当なし」ではない。これは起票の提案全般にも適用する
- 同じ件の開いているチケットがあれば、下書きを提示せず、新しくわかったことをそのチケットにコメントし、依頼者に URL を伝える。閉じたチケットだけが一致したら、開き直しもコメントもせず、新しい下書きの「関連」で参照する。同じ件がなければ、これまでどおり下書きを提示する。issue の登録も、今回の変更に含めての修正も、勝手に始めない
- 開いた同件が見つかっておらず、検索が失敗・不完全だった場合も、完全な下書きを提示し、試した検索条件と既存チケットとの重複を否定できなかったことを明記する。見つかった閉じた同件は「関連」で参照する。既に開いた同件が見つかっていれば、上のコメントと URL 報告を優先する
- 下書きはそのまま登録できる体裁で書く。タイトル、再現条件、環境ごとの見え方、影響、やること、完了条件、関連を含める。この下書きが報告そのものなので、別途要約した短い報告で代替しない
- 今回の変更に含めるか、別 issue にするか、見送るかはユーザーが決める。判断材料として、その失敗が今回の変更と性質・完了条件を共有するかどうかを示す
- ユーザーの応答を待てない実行（workflow など）でも先に検索する。既存の開いたチケットにコメントした場合はその URL を報告し、下書きが必要な場合は完了報告や ticket / thread のコメントへ全文を載せ、登録はせずに判断を委ねる。「無関係なので無視した」で終わらせない


## レビュー指摘への対応

手順の正本の節は「Handling review feedback / レビュー指摘への対応」。

`.agents/skills/handle-review-feedback/SKILL.md` または `.claude/skills/handle-review-feedback/SKILL.md` を読み、
[手順の正本](guildbotics/templates/member/reference.md) に従う。
失敗した検査は原因を特定し、今回の変更なら直す。無関係ならtrackerを検索し、
既存のopenチケットへ証拠を追記する。起票は人の明示的な依頼を必要とする。

## 参照優先度

実装とテストを先に読む。旧Python hostの知見を確認する場合は `git show origin/python-host:AGENTS.md` を使う。
未実装の計画はリポジトリに残さずチケットで管理する。
