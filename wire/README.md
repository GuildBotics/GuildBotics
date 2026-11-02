# Host / guest wire schemas

Python の型が正本です。次のコマンドで `schema/` を再生成し、型の変更と一緒にコミットします。

```bash
uv run --no-sync python scripts/generate-wire-schema.py
uv run --no-sync python scripts/generate-wire-schema.py --check
```

各 JSON は JSON Schema Draft 2020-12 の独立した文書で、参照先は同じ文書の
`$defs` に含みます。ファイル名と root の `title` は生成する型名です。
既存の dataclass・TypedDict・Pydantic model は直接 `TypeAdapter` に渡します。
判別付き union の注釈も保持します。union 内の variant では、判別に必要な tag を
必須にするため、schema の `required` に追加します。Python の既定値と、単独の
variant の schema は変更しません。

`<Call>Arguments` は `HostWindow` の受け取り側の引数・既定値から生成します。
`<Call>Result` は生成スクリプトの `HOST_RESULTS` が指定する返却データの型です。
例えば `BeginTurnResult` は `HostTurn`、`ResolveResult` は `ConversationRecord` と
同じ形です。結果がない呼び出しの schema は `null` です。新しい呼び出しは
`HOST_RESULTS` にも登録する必要があり、窓口一覧との不一致は生成時に失敗します。

member CLI の JSON 出力は `ReadPage` と `Chat*Output`、ダウンロードの標準出力は
`Unpacked` です。任意のコマンド結果や resource の項目など、Python が `Any` や
汎用の辞書として運ぶ箇所は schema でも任意の JSON のままです。
型に表れない実行時の制約は schema の対象外です。

## 動作の golden fixture

`golden/` は Python と Rust が共通で読む、実装の現在の動作の例です。
`uv run --no-sync pytest tests/guildbotics/test_wire_golden.py` で Python の
送る側・受ける側に通します。型の網羅は `schema/` が担当します。

- `http.json`: 要求（method・path・headers・json または text）、host の操作結果
  （action）、HTTP 応答、client が取り出す結果または例外。`oversized` は受信側の
  出力上限を超える文字列を返す操作です。Bearer はテスト専用のダミーです。
- `entry.json`: 入口への要求、コマンドの戻り値、stdout 全体、stderr の行。
  stdout は末尾改行付きの CommandReply 一行だけで、JSON 内の改行はエスケープされます。
  コマンドの print と子プロセスの出力も stderr に流れます。host は複数回の read を
  つないでこの一行を読みます。stdout にログが先行する形式は含めません。
- `worktree.json`: copy / changes の argv、写す前と変更後のファイル、削除するパス、
  コピー結果と変更結果の JSONL。argv の `{source}` と `{destination}` だけを実行時の
  パスに置き換えます。text は UTF-8、content は base64、sha256 は元ファイルのバイト列の
  ハッシュです。JSONL は行ごとの値と行順を比較し、JSON の空白とキー順は問いません。
- `admits.json`: mounts と cwd から expected を得る表。最深の mount を採用します。
  通常の入力は呼び出し側が正規化した絶対パスです。cwd と mount のキーに `..` を含む
  入力の判定結果は共通契約の対象外とし、移植先に現在の字句的判定の再現を求めません。
  境界の例として末尾 `/`、`.`、連続 `/` の表記と相対 cwd の拒否も固定します。

fixture に一時パスや実際の資格情報を入れず、両言語が同じ JSON の値を読みます。
動作を意図して変えるときは両言語への影響を確認して fixture も更新します。
