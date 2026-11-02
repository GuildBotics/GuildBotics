# Host の記録ファイルの golden fixture

Python 版と Rust 版の host が、同じファイルを同じ形で読み書きすることを確かめる例です。
`golden/` の値は Python 版の実装から作り、次のテストで両方の言語に通します。

```bash
uv run --no-sync python -m pytest tests/guildbotics/test_records_golden.py
cargo nextest run --locked -p gb-workspace -p gb-observability --test golden
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

振る舞いを意図して変えるときは、両方の言語への影響を確かめて fixture も直します。
