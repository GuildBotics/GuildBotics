# Host のファイルの golden fixture

Python 版と Rust 版の host が、同じファイルを同じ形で読み書きし、同じ答えを出すことを確かめる例です。
`golden/` の値は Python 版の実装から作り、次のテストで両方の言語に通します。

```bash
uv run --no-sync python -m pytest tests/guildbotics/test_records_golden.py tests/guildbotics/test_commands_golden.py tests/guildbotics/test_environment_golden.py tests/guildbotics/test_yaml_golden.py
cargo nextest run --locked -p gb-workspace -p gb-observability -p gb-config -p gb-environment
```

- `files.json`: マシンとワークスペースの小さなファイルのバイト列。
  `registry` は登録した順の名前と `data/workspaces.json`、`active_workspace` は
  `data/active-workspace.json`（Rust は読むだけ）、`workspace_identity` は
  `state/workspace.json`、`device_identity` は `data/device.json` です。
  実行ごとに変わる値は `<root>`・`<workspace_id>` などの名前で書きます。
- `diagnostics.json`: trace の中で 1 つのコマンドを実行し、成功・失敗・取り消しで
  終えたときの index（`local/run/diagnostics.jsonl`）と transcript
  （`local/run/sessions/<trace>.jsonl`）の行の値。trace の ID は `<trace>`、時刻は
  `<timestamp>` に置き換えます（時刻はローカル時刻とオフセットの ISO 8601 で、
  テストが形を確かめます）。`status` は Python 版の読み手がその trace に付ける状態です。
  JSON の空白とキーの順は問いません。
- `commands.json`: コマンドの名前がどのファイルに解決されるかと、コマンドの宣言と、チーム。
  入力（`python_sources` の `source`、`workspaces` の `config`・`identity`・`resolve`）は手で書き、
  答え（`bundled` と各 `expected`）は Python 版から作ります。同梱のコマンドか Python 版を変えたら
  `uv run --no-sync python -m tests.guildbotics.test_commands_golden` で答えを作り直します。
  - `bundled`: 同梱のコマンド全部の、名前と言語（`<name>@<language>`）ごとの解決先、ファイルごとの
    `read_only`・`inspects`（宣言が断られれば `null`）、`.py` の `COMMAND_METADATA` の値
    （Python 版が断る宣言は `null`）。
  - `python_sources`: `.py` の書き方の例ごとの、宣言の値と `read_only`・`inspects`。Python 版が
    宣言を断る例は、Rust も断ります。Python 版は読めない宣言を「宣言なし」として書き込みのできる
    コマンドとして動かしますが、Rust は断ります（集合・bytes・複素数・名前で書く文字の escape も、
    Rust は断ります）。そのような例は fixture に入れません。
  - `workspaces`: 小さなワークスペース（`config` と identity root の `identity` のファイル）での、
    名前と言語とメンバー（`<name>@<language>@<person>`、メンバー無しは `None`）ごとの解決先、
    ワークスペースのコマンドの宣言、チームの読み込みの結果（読めなければ `null`）。

- `environment.json`: コマンドの microVM に伝える host の事実と、起動の大きさ。入力は手で書き、
  答え（各 `expected`）は Python 版から作ります。Python 版を変えたら
  `uv run --no-sync python -m tests.guildbotics.test_environment_golden` で答えを作り直します
  （同時に、langcodes が別のものとして読む言語と地域のコードの一覧
  `crates/gb-environment/src/language_aliases.json` も作り直します）。
  - `tags`: OS の言語のタグごとの `LANGUAGE`（gettext の形、`ja_JP`。言語でなければ `null`）。
    Rust は、言語・文字・地域だけの形で、langcodes が読み替えないコードのタグにだけ答え、
    それ以外は `LANGUAGE` を渡しません（別の言語を伝えるより、伝えない方を選ぶ）。
    Rust が断る例は `crates/gb-environment/tests/golden.rs` の `DECLINED` に並べます。
  - `macos`: `defaults read -g AppleLanguages` の出力ごとのタグ。
  - `posix`: gettext の環境変数ごとのタグ。
  - `resources`: 宣言（`intelligences/agent_environment.yml`）ごとの memory・vCPU（宣言が断られれば
    `null`）。Rust は `resources` だけを読みます。

- `yaml.json`: PyYAML の plain scalar の型解決（int・float・null・bool）。入力の `source` だけを
  書き、`uv run --no-sync python -m tests.guildbotics.test_yaml_golden` で `expected` を生成します。
  `08`・`0o10` は文字列、`010` は 8、`1:30` は 90、`yes`・`ON` は真です。
  Rust は YAML の構造をイベントとして読み、型解決前の plain scalar を正規化してから
  serde-saphyr へ渡します。引用・ブロック文字列、UTF-8、アンカー・alias・merge・重複キーも
  同じ fixture で確認します。JSON で表せない非有限数は引き続き断ります。

- `screens.json`: コマンド画面と診断画面が読む API の応答。入力は小さな config と identity、
  答えは Python の CommandFileService、AppRuntime、diagnostics の読み書きから作ります。
  `uv run --no-sync python -m tests.guildbotics.test_screen_api_golden` で作り直します。
  同じ fixture を Python の比較テスト、Rust の gb-config / gb-observability、
  guildbotics のサーバー結合テストが読みます。実機の microVM は起動しません。
  一覧の解決順・詳細の ID / revision / arguments / inputs、成功・失敗・実行中の
  trace と出力の presentation を比較します。実行の受付は snapshot の無い環境で、
  表示したファイルの照合を通った要求だけが trace を開くことを確認します。

振る舞いを意図して変えるときは、両方の言語への影響を確かめて fixture も直します。
