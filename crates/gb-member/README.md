# Member capabilities

ticket・PR・repository 情報・chat・memory と、member context/help の共通処理。
サービスの契約は [gb-ports](../gb-ports/src/lib.rs)、provider の実装は
[gb-integrations](../gb-integrations/src/factory.rs) にある。
CLI の引数解析、資格情報の取得、git 操作、guest への転送はこの crate の責務に含まない。

`RepositoryServices` はサービスを遅延取得する。ticket の人による承認、入力の検証、
`WriteGuard::ensure_current` が済むまで provider を生成しない。
read は更新確認を必要とせず、write は更新確認を通す。provider のエラーは
`MemberError::Service` のまま保持し、failure / not found / unavailable / unsupported を区別する。
案内文は `MemberError::render` に locale renderer を渡して得る。

`Chat` は public account と participant labels を扱い、チャンネル名、URL、thread の解決を
provider に任せる。操作の返り値は送信・reaction の receipt であり、呼び出し側が
実行中 run の evidence として保存する。`await_checks` の notice は元の work identity と
確認した head を status に保持する。run の完了記録も呼び出し側が保存する。

`Memory::open` は文書へ触る前に device identity を解決する。同期 API は blocking worker
から使う。書き換えは read・変更・文書二ファイルの保存・recency を一つの共有 transaction に
収める。policy の既存文書の検索もその範囲内で行う。監査は文書の commit 後の best effort
であり、I/O エラーや共有ロックの競合を理由に、完了済み文書を失敗扱いにしない。
secret snapshot は呼び出し側が `Redactor` として渡す。文書本文は省略せずにマスクする。

`events` は payload を組み立て、明示的に渡された correlation と合わせて既存の
diagnostics / activity store に記録する。`target.recorded` や review limit の失敗診断は
diagnostics だけに残し、domain event だけを共有 activity に残す。
board の完了履歴は読み込み後、重複確認と記録を同じ transaction で行う。

download は host ではバイト列と metadata を返す。`archive` のファイル書き込み関数は
送り先の環境で使う。guest が指定したパスを host 上で開かない。
ZIP の展開と metadata の解釈はライブラリに任せるが、ライブラリが同名エントリを索引で
置き換えるため、元の central header 数と索引数を展開前に照合する。

[凍結版テスト対応表](tests/frozen-map.txt) は関数ごとの移植先、再利用先、範囲外の理由を記す。
検索は Rust の Unicode 対応実装だけを使うため、Python の rg subprocess、CP932 decode、
rg 不在時の fallback 警告という機構は持たない。検索の結果、metadata のみの検索、
Unicode、空白 query、監査は Rust で検証する。文書の形式は `meta.yml` と `body.md` である。
context/help は [参照カタログ](../../guildbotics/templates/member/reference.md) を直接読む。
凍結版の stdin 非掲載という assertion は、カタログをデータ化した後の契約に合わせて除く。

```bash
cargo nextest run --locked -p gb-member
cargo nextest run --locked -p gb-integrations --lib 'factory::local::'
cargo nextest run --locked -p gb-server --lib 'i18n::'
```
