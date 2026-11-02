# Host / guest wire schemas

Python の型が正本です。次のコマンドで `schema/` を再生成し、型の変更と一緒にコミットします。

```bash
uv run --no-sync python scripts/generate-wire-schema.py
uv run --no-sync python scripts/generate-wire-schema.py --check
```

各 JSON は JSON Schema Draft 2020-12 の独立した文書で、参照先は同じ文書の
`$defs` に含みます。ファイル名と root の `title` は生成する型名です。
既存の dataclass・TypedDict・Pydantic model は直接 `TypeAdapter` に渡します。

`<Call>Arguments` は `HostWindow` の受け取り側の引数・既定値から生成します。
`<Call>Result` は生成スクリプトの `HOST_RESULTS` が指定する返却データの型です。
例えば `BeginTurnResult` は `HostTurn`、`ResolveResult` は `ConversationRecord` と
同じ形です。結果がない呼び出しの schema は `null` です。新しい呼び出しは
`HOST_RESULTS` にも登録する必要があり、窓口一覧との不一致は生成時に失敗します。

member CLI の JSON 出力は `ReadPage` と `Chat*Output`、ダウンロードの標準出力は
`Unpacked` です。任意のコマンド結果や resource の項目など、Python が `Any` や
汎用の辞書として運ぶ箇所は schema でも任意の JSON のままです。
型に表れない実行時の制約は schema の対象外です。
