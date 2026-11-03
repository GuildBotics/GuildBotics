# キーチェーンから暗号化 store への移行

Python 版から Rust 版へ切り替えるときに一度だけ実行する保守用ツール。
本番実行は #97、写しによるリハーサルは #108 で行う。通常起動からは呼ばれない。
現在の OS アダプターは macOS 用で、Windows / Linux では実行を拒否する。

## 実行

1. Python / Rust のサーバー、CLI の書き込み、git credential helper を停止する。
   書き込み用 lock も取得するが、旧実装の読み取りや git helper はその lock を取らないため、
   停止が必要。移行後に Python の旧 store を再開してはいけない。
2. identity root と、値を保持している**全ての** workspace root を確認する。
   指定するのは `.guildbotics/config` の親ではなく、`.guildbotics` が入っている root。
3. 旧キーチェーン項目へのアクセスを許可されている Python 実行ファイルで実行する。

```sh
python -m guildbotics.environment.secret_migration \
  --identity /path/to/identity \
  --workspace /path/to/workspace-a \
  --workspace /path/to/workspace-b \
  --writers-stopped
```

端末の鍵 `GuildBotics / agent-environment-credentials` が必要。
このツールは鍵の作成・更新・削除をしない。
macOS Security.framework から generic-password の属性だけを一覧取得し、
指定した store に属さない GuildBotics 項目があれば書き込み前に停止する。
全ての保存先を保存・復号・索引照合してから元項目を削除する。
成功時の JSON は store 数・削除数・残存数のみで、`remaining: 1` は鍵だけが残ったことを表す。
キーの値は標準出力・エラー・一時ファイルへ書かない。

## 保存形式と切り替え側の読み取り

identity / workspace とも、既存の `local/secrets.json` の `store_id` と
`config/secrets.yml` のキー名を保持し、`local/secrets.enc.json` に保存する。
AES-256-GCM、12 byte nonce、AAD `guildbotics.secrets.v1\0<store_id>`、
base64 の nonce / ciphertext と `version: 1` は Rust の
`gb_config::secrets::SecretStore` と同じ形式。

人の git トークン（旧サービス `GuildBotics/branvil-git`、account は接続先 URL）は
identity store に保存する。論理キー名は `git_token_key(origin)` が正本で、
`BRANVIL_GIT_TOKEN_` に元 account の UTF-8 SHA-256（大文字16進数）を続ける。
URL の正規化はしない。Rust 側の git credential の移行では、この命名規則で取得すること。
このツールは旧 Python の git helper の読み取り先を切り替えない。

## 中断した場合

エラーには処理段階（`stage`）と、値を含まない停止理由・次の確認先が表示される。
バックエンドが返した任意の例外文言は、値を含む可能性があるため表示しない。
`unmapped` に相当する所属不明項目と、最終監査で残った項目は、
service / account の属性だけを JSON で列挙する。これは値の一覧ではない。

所属不明の `GuildBotics/<store_id>` がある場合は、次の順で確認する。

1. 各 root の `local/secrets.json` の `store_id` と表示された service を照合し、
   渡し忘れた workspace があれば `--workspace` に追加する。
2. 削除済み workspace や作り直された名前空間なら、バックアップから元の root の
   名前空間・キー名の索引を復元して移行対象に含められるか確認する。
3. どの root にも戻せない項目は、オーナーが用途と必要性を確認する。
   値をまだ使う場合は停止を維持し、復元・移行先を決める。不要だとオーナーが確認した
   項目だけを macOS の「キーチェーンアクセス」で service / account を照合して手動削除し、
   同じ引数で再実行する。端末鍵の項目は削除しない。

未知の項目をこのツールが自動的に破棄するオプションは設けていない。
所属を確認せずに一覧の項目をまとめて削除しないこと。

書き込みを停止したまま、指定した root、キーチェーンへのアクセス、索引と暗号文を確認する。
移行前の値と移行済み暗号文が異なる場合は上書きせず停止する。
削除途中の中断は、検証済み暗号文に値が全て残るので同じ引数で再実行できる。
書き込み途中も元項目を削除していない。壊れた既存ファイルを空として扱ったり、
平文へ逃がしたりしない。ツールが失敗した状態で旧サーバーを起動しないこと。

## テスト用 fixture

実キーチェーンに触れず Python の移行処理から合成値の fixture を再生成する。

```sh
uv run --no-sync python -m tests.guildbotics.environment.test_secret_migration
```

Python のテストは移行結果と fixture の一致を、Rust の `gb-config` テストは
そのファイルを実際の SecretStore で読めることを検証する。
OS の項目一覧取得・アクセス許可・削除の実機確認はリハーサルで行う。
