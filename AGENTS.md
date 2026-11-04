# AGENTS.md

このファイルはリポジトリで作業するエージェント向けのガイドです。

## 最重要ルール

- ソースコードを正とし、変更後のコード量が最小になる設計を選ぶ。
- `.gitignore` 対象は参照しない。互換用フォールバックや旧形式の並行サポートを追加しない。
- 自然言語の意味判定は既存のLLM判定基盤を使う。キーワード列挙で代替しない。
- コメントとdocstringは英語で書く。

## 現在の実装と境界

Python は guest の入口 `guildbotics.guest.entry` とコマンド実行・共有型を持つ。
Python の host・CLI・HTTP サーバーは削除済み。host の実装と知見は凍結ブランチ
`origin/python-host`（`fea9e05aaf8b4bca96e19d615b6811ea0eacb7d0`）の
`git show origin/python-host:<path>` で読む（当時のガイドは AGENTS.md）。
Rust の host は `crates/*` の Cargo workspace。Python は3.15、Rustは
`rust-toolchain.toml` に固定した版を使い、CI imageの版と合わせる。
共有は `commands`・`intelligences`・`runtime`・`loader`・`entities`・`utils`。
共有はguestの実装をimportせず、全共有パッケージはguestから使われる。
`lint-imports` と `tests/guildbotics/test_package_map.py` で検査する。
`templates/` はRustも読むデータなので保持する。

wire の正本は `intelligences/agent_runtime/wire.py`。変更時は
`uv run --no-sync python scripts/generate-wire-schema.py` で生成し `--check` で確認する。
Rust の `gb-wire` はこのschemaから型を生成する。詳細は [wire](wire/README.md)。
member の手順は [参照カタログ](guildbotics/templates/member/reference.md)、
provider/CLI名の検査データは同じディレクトリの catalog.json。
CLIの実物との照合は #92 で戻す。
snapshotの固定値・requirementsは `guildbotics/templates/environment/`、
AI CLIの固定版は `guildbotics/intelligences/cli_agents.py`。
変更時は `.agents/skills/update-dependencies/SKILL.md` に従う。

## 開発時の基本コマンド

```bash
uv sync --extra dev --extra test
uv run --no-sync ruff format --check guildbotics tests
uv run --no-sync ruff check guildbotics
uv run --no-sync mypy guildbotics
uv run --no-sync pylint guildbotics
uv run --no-sync lint-imports
uv run --no-sync python scripts/generate-wire-schema.py --check
uv run --no-sync pytest tests/ --ignore=tests/blackbox -n auto
cargo fmt --all --check
cargo clippy --locked --workspace --all-targets -- -D warnings
cargo nextest run --locked --workspace
lychee --no-progress --scheme file --include-fragments --extensions md './*.md' docs desktop skills crates
```

黒箱テストは `GUILDBOTICS_BLACKBOX_SERVER` と `GUILDBOTICS_BLACKBOX_CLI` で
Rustの入口へ向ける形で残す。CIへの復帰は #91・#92。
desktop journey は #91 まで停止し、Desktop E2E gateは成功を返す。
Branvilの画面E2Eは継続する。

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
- E2E に振る舞いパターンを総当たりで持ち込まない。分岐網羅は Vitest の unit / component（mock 境界）に置き、Playwright は jsdom では検証できない実ブラウザ + 実ワイヤ契約（`client.ts ↔ FastAPI ↔ EventBus`）+ 実ファイル書き込みに絞る

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
- API endpoint、`AppRuntime`、setup service、config 書き込みを変更したら、service unit test と FastAPI `TestClient` integration test の両方を検討する
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

実装とテストを先に読む。hostの凍結点、wire/recordsのREADME、チケットの順で補う。
未実装の計画はリポジトリに残さずチケットで管理する。
