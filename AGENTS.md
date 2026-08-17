# AGENTS.md

このファイルは、このリポジトリで作業する AI/自動化エージェント向けの実装ベースの作業ガイドです。

## 最重要ルール

- ソースコードを正とする（README / `docs/*.md` は参考情報）
- `.gitignore` 対象のファイル・ディレクトリは参照しない
- 挙動変更を行った場合は、関連ドキュメントも必要に応じて更新する
- コードの修正に際しては「最小限の変更量」ではなく、「変更後のコード量が最小になること」を最優先事項とし、場当たり的対応ではなくあるべき姿の美しいコードとなることを心がける
- 後方互換性を維持するためのコード（移行フラグ、フォールバック読み込み、旧形式の並行サポート、deprecation 期間の新旧併存パス）を追加しない。互換性のために冗長なコードを残すより、常にあるべき姿のコードを維持することを優先する。破壊的な非互換が起こりうることは README の「重要な注意（免責事項）」で宣言済みであり、利用者は実質的にメンテナ本人のみである。設定形式・実行パス・命名などを変更する際は、互換レイヤーではなく直接切り替えと旧実装の削除を選ぶ
- 意味判定・分類・採用可否のような自然言語理解が必要な処理を、キーワード列挙や場当たり的な文字列マッチで実装しない。既存の LLM 判定基盤（例: `guildbotics/intelligences/functions.py` と `guildbotics/templates/commands/functions/*`）を優先し、必要なら汎用的な判定関数を追加する。
- 責務境界を越えた実装をしない。GuildBotics における具体的な責務境界は「重要な実装ポイント」の「責務境界」を参照する。

このリポジトリの `.gitignore` では、少なくとも以下を無視対象にしています（抜粋）:

- `tmp/`
- `memo/`
- `.guildbotics/`
- `dist/`
- `__pycache__/`, `.pytest_cache/`, `.mypy_cache/`, `.coverage*`, `coverage.xml`
- `guildbotics/_version.py`

## プロジェクト概要（実装ベース）

- 言語: Python 3.12+
- パッケージ: `guildbotics`
- CLI: Click (`guildbotics.cli:main`)
- 主用途: AI エージェント協調のための CLI / スケジューラ / カスタムコマンド実行基盤

エントリポイント:

- インストール後 CLI: `guildbotics` (`pyproject.toml` の `project.scripts`)
- モジュール側: `guildbotics/cli/__init__.py`
- Desktop 向け Local API: `python -m guildbotics.app_api`（FastAPI。全リクエストに `X-GuildBotics-Session-Token` ヘッダが必要、`/health` で疎通確認）

### パッケージ全体マップ

- `guildbotics/cli/*` … Click コマンド。`member.py` に member capability の入口
- `guildbotics/app_api/*` … Desktop 向け Local API（FastAPI + EventBus + normalizer）
- `guildbotics/drivers/*` … スケジューラ、command runner、workflow dispatcher
- `guildbotics/capabilities/*` … member の git / github / chat / memory 操作と domain event 記録
- `guildbotics/commands/*` … コマンド種別（md/py/sh/yml + inline）の実行基盤
- `guildbotics/editions/*` … Edition 抽象と Simple edition（setup_service は GUI からも再利用）
- `guildbotics/integrations/*` … GitHub / Slack など外部サービス client（capability から使う）
- `guildbotics/intelligences/*` … brains（`agno_agent` / `cli_agent`）、LLM 判定関数（`functions.py`）、LLM provider / AI CLIツールカタログ（`llm_providers.py` / `cli_agents.py`）
- `guildbotics/observability/*` … diagnostics record の記録・永続化（`diagnostics_store.py`）、trace 相関、interactive session
- `guildbotics/runtime/*` … `Context`、member 解決、brain / integration / loader の factory
- `guildbotics/workspace/*` … Workspace storage。Workspace ID / device ID（`identity.py`）、共有ファイルの種別別 validation（`validation.py`）、Config の blob ID compare-and-set（`config_repository.py`）。共有書き込みを直列化する lock は `utils/shared_write_lock.py`（`observability` からも取れる必要があるため `utils` にある）
- `guildbotics/sync/*` … Workspace Sync Port の唯一の購読者。ローカル同期 repository（`local_repository.py`）、commit 境界（`commits.py`）、同期 queue / 自動収束 / rejected ref（`manager.py`）、更新不採用の Activity 記録（`rejections.py`）、Hub への接続と参加（`enrollment.py`）、queue の install（`activation.py`）
- `guildbotics/hub/*` … Hub。bare repository の作成と fast-forward only 設定（`host.py`）、device から Hub への到達（`connection.py`）。中身の意味は知らない
- `guildbotics/entities` / `guildbotics/loader` / `guildbotics/utils` … ドメインモデル、YAML ローダ、設定解決ほか共通基盤

依存方向のハードルール（`tests/guildbotics/test_layer_boundaries.py` で担保）:

- `guildbotics/app_api/*` は最上位層であり、他の guildbotics package から import してはならない。app_api と core の両方が必要とする知識（provider / AI CLIツールカタログなど）は core 側（例: `guildbotics/intelligences/*`）に置き、app_api は API model への変換だけを持つ
- `guildbotics/observability/*` は `utils` 以外に依存しない記録基盤であり、app_api や capability の都合を知らない
- `guildbotics/workspace/*` は `utils` と `entities` 以外に依存しない storage 層であり、capability / driver / app_api の都合を知らない
- `guildbotics/hub/*` は `utils` 以外に依存しない。Hub は repository の入れ物と OpenSSH 経路だけを知り、共有 record の意味を知らない
- `guildbotics/sync/*` は `utils` / `entities` / `workspace` / `observability` にだけ依存する。逆に import してよいのは **composition root だけ**で、その一覧は `tests/guildbotics/test_layer_boundaries.py` の `SYNC_COMPOSITION_ROOTS` が正本（現在は `app_api/workspace_sync.py` の1つだけ）。capability / driver / integration / それ以外の app_api module は Workspace Sync Port 越しにだけ同期へ届く。**composition root を増やさない。** activation の防御は module state なので process をまたいで効かず、2 process が同じ Workspace を activate すると同じ repository に queue が2本走る（`service.lock` は Desktop が scheduler 開始時にしか取らないので、この衝突を防がない）。マシン全体の所有者ができる（#418 の Device Agent）までは Desktop backend の1本だけとする

リポジトリ直下では `desktop/`（Tauri + React frontend）と `skills/guildbotics/SKILL.md`（エージェント向け作業スキル）も対象。

## 重要な実装ポイント

### 1. 責務境界

GuildBotics では、実装場所を「その処理を知ってよい層」で決める。近い場所に場当たり的に置かず、以下の境界に従う。

#### CLI (`guildbotics/cli/*`)

責務:

- Click の引数・オプション定義、入力ファイル/stdin 読み取り、出力形式の選択
- workspace / member 解決、capability 呼び出し、CLI 用エラー変換
- interactive session の開始・touch、member command の開始/終了/failure という CLI 実行イベントの記録

禁止:

- GitHub / Slack / Git など provider 固有 payload の組み立て
- PR / Issue / commit など domain event の構造決定
- provider URL の生成・分類
- activity history 表示用の title / label / link kind 正規化

例: `guildbotics member git push` は push capability の結果を受けて記録関数を呼ぶだけにし、`github.push` payload の構造は `guildbotics/capabilities/*` 側へ置く。

#### Capability (`guildbotics/capabilities/*`)

責務:

- GitHub / Git / Slack / memory など外部サービスや domain 操作の実行
- provider API / URL / payload / credential など provider 固有知識の保持
- capability の結果 payload と、必要な domain event payload の組み立て
- provider 固有 URL 生成。例: GitHub commit URL は Git/GitHub capability 側で扱う

禁止:

- desktop / activity history の画面都合に合わせた表示文言の決定
- FastAPI response model や React component の都合を直接知ること

#### Observability (`guildbotics/observability/*`)

責務:

- diagnostics record の統一スキーマでの記録・永続化（`diagnostics_store.py`、`run/diagnostics.jsonl`）
- trace / span の相関（`correlation_fields`）と correlated event の記録（`diagnostics_events.py`）
- interactive session の管理（`interactive_sessions.py`）

禁止:

- `guildbotics.utils` 以外の guildbotics package への依存
- 表示用の title / label / link kind の決定（それは app_api の normalizer の仕事）
- provider 固有 payload の解釈

#### Git Sync Manager (`guildbotics/sync/*`)

責務:

- ローカル同期 repository（`<workspace>/.guildbotics` 自体を独立 Git repo にする）の初期化と境界検証
- Workspace Sync Port の唯一の購読者として、device ごとに1本の同期 queue を回す
- commit / fetch / 自動収束 / push、first-committer-wins、後着 commit の `refs/guildbotics/rejected/<rejection_id>` への退避
- 送信前と受信時に同じ `validate_shared_file()` を通し、通らないローカル変更は「送信できない変更」として保留、受信側で通らなければ共有データ異常として停止（検証の中身は「4.1 共有 state の書き込み」を参照）
- `await_pushed(change_id)` の同期 barrier と `GitSyncStatus` の算出
- 作業ツリーに触れる区間（commit と、converge の全体）で `shared_write_lock()` を保持する。網羅範囲と理由は「4.1 共有 state の書き込み」を参照
- Hub への接続（新規登録 / 既存 Workspace への参加 / 複製の取得）と、参加前の差分の算出

禁止:

- composition root 以外の package から import されること（同期へは Workspace Sync Port 越しにだけ届く）
- 呼び出し元から repository path を受け取ること（検証済み Workspace root から毎回導出する）
- ファイル内容の domain 知識を持つこと
- 退避内容を Activity / API へ載せること（`rejection_id` と対象 path だけを記録し、回復は変更元 device 上の手動手順）
- ambient に選択中の Workspace を解決すること。manager が扱う Workspace root を、Activity の保存先と write 通知の path 解決まで引き回す（別 root の初期参加・切替と競合するため）
- 参加前の差分表示（preview）で remote を設定すること。実行しなかった preview が「同期有効」に見えると、次の起動で queue が回りはじめる

参加フローの規約:

- 参加は上書きではない。**このマシンの内容を先に commit してから** Hub の内容を採用し、押し出された commit は rejected ref と Activity に残す
- **参加（`_join`）も全体が1つの lock 区間で、その最初に commit 境界を再実行する。** Hub へ到達する区間は lock を持てない（network を跨ぐため）ので、その間に**正しく lock を取って**保存された変更が未 commit のまま `restore_from_index` の checkout で消えうる。未 commit なので rejected ref にも残らない。converge と同じ問題・同じ対処であり、再 commit 後の head に対して分類し直す
- **preview するのは参加のときだけ。** 新規登録は比較する相手がいないので、preview のために repository を作らない（作ると、有効化しなかった Workspace に `.git` と Workspace ID が残る）
- preview と実行は同じ前半（`initialize` → identity → commit）と同じ分類関数を共有する。preview が実行と違う起点や違う結論を語らないようにするため
- 同じ path は Hub 側を採用、Hub に無い path は保持して送信、Workspace ID は Hub 側を採用する
- **tree の直接比較でよいのは履歴を共有していない相手だけ。** 共通の commit がある相手（Hub 再構築後の再接続など）では「両方が持っている」は何も意味しない。`merge_base` があれば通常の収束（manager）へ委譲し、参加側で別の規則を作らない
- **検証を通らないファイルを Hub の内容で上書きしない。** これは競争に負けた側ではなく、まだ送れていない利用者の編集であり、commit されていないので rejected ref にも残らない。`restore_from_index` の対象から必ず引く（manager 側と同じ）
- **接続に失敗したら remote を残さない。** 残すと次の起動が「同期有効」と判定して、利用者が使えないと言われた Hub に対して queue が回りはじめる
- **Git の例外を境界の外へ出さない。** Hub が落ちている・鍵が未登録・アドレスが違うは最も普通の異常系なので、`EnrollmentError` などへ変換して API が利用者に見せられる形にする。`_HUB_FAILURES` のような一覧は「利用者に見せられるもの」の宣言であって、下位の例外を捕まえる網ではない
- **停止しなかった queue を手放さない。** timeout した worker は repository を掴んだままなので、忘れると次の activate が同じ repository に2本目を作る。`stop()` の戻り値を呼び出し側まで返し、Workspace 切替はそれで中止する

#### Hub (`guildbotics/hub/*`)

責務:

- `~/.guildbotics/hub/` の作成と、Workspace ごとの bare repository（`host.py`）
- fast-forward only（`receive.denyNonFastForwards` / `denyDeletes`）の適用。並行更新の自動収束はこの拒否が支えている
- device から Hub への到達（`connection.py`）。接続先の解析、Git remote URL、host key の確認と登録、device 公開鍵、Hub 上の `guildbotics hub` コマンドの SSH 実行
- Hub 自身の操作は Hub マシンの `guildbotics hub` コマンドが行う（sshd から実行される前提。Windows の PATH 設定は README で案内する）

禁止:

- 共有 record の意味を知ること（`utils` 以外へ依存しない）
- Workspace root の path を保存すること（Hub は Workspace ID だけで対応づける）
- 接続先文字列から port や path を受け取ること（Hub 内の配置は GuildBotics が決める）
- 生の Git command を Hub へ ssh で送り込むこと（Hub 側の CLI を呼ぶ）
- **OpenSSH の判定を自前で再現すること。** `probe_host_key` の `trusted` は「無印の `known_hosts` entry が、提示された鍵のどれかを持っている」だけを主張する。`@revoked` / `@cert-authority` などの marker 付き行は一律で候補から外す（fail-closed）。marker の意味を parser で解こうとすると、OpenSSH が拒否する鍵を trusted と言ってしまう。**権威は接続そのもの**であり、この probe は「trusted と言うなら ssh も通る」側へだけ保守的であればよい
- 接続先や Workspace ID を検証せずに path / コマンド引数へ渡すこと。Workspace ID は**正規形の UUID だけ**を受け付ける（`urn:uuid:` や大文字は同じ UUID の別表記で、1つの Workspace が複数 directory へ割れる）。接続先は先頭 `-` を拒否する（`ssh` の option として解釈される）

#### App API (`guildbotics/app_api/*`)

責務:

- Desktop 向け runtime API（FastAPI）と event bus の集約
- observability store の読み出しと、trace, log, event, memory audit, prompt trace など複数 source の統合
- API response model への変換
- provider payload を activity history 用の provider-neutral な event/link/title/detail に変換する normalizer / translator

禁止:

- 他の guildbotics package から import される API を持つこと（依存方向は「パッケージ全体マップ」参照）
- CLI と同じ domain event payload を別途組み立てること
- GitHub URL の `/pull/` や `/issues/` のような path 文字列だけで PR/Issue を分類すること
- provider API 呼び出しや credential 処理を持つこと

例: activity history では raw diagnostics record を直接 UI 向けに解釈せず、`activity_events.py` / `activity_links.py` のような専用 normalizer に寄せる。

#### Desktop frontend (`desktop/src/*`)

責務:

- API response の表示、ユーザー操作、画面状態管理
- 表示密度、hover/click、responsive layout など UI 固有の振る舞い

禁止:

- provider raw payload を独自に再分類すること
- GitHub URL path から PR/Issue/commit 種別を推測すること
- backend と同じ title / label / link kind 正規化を再実装すること

#### 共通判断ルール

- 同じ判断が CLI と API、または API と frontend に重複しそうな場合は、より domain に近い backend module へ寄せる
- provider 固有知識は capability か provider normalizer に閉じ込める
- activity history / diagnostics 表示に必要な変換は、raw payload を保存する層ではなく表示用 normalizer に置く
- 新しい event/link 種別を追加するときは、記録 payload（capability → observability）、normalizer / API model（app_api）、frontend 表示（desktop）の責務を分けて実装する

### 2. CLI コマンド

`guildbotics/cli/__init__.py` に主要コマンドがあります。コマンド・オプションの完全な一覧は
Click 定義から生成される `docs/cli_reference.md` を参照する（`scripts/generate-cli-reference.py`
で再生成。drift は CI の `generate-cli-reference.py --check` ステップと
`tests/guildbotics/cli/test_cli_reference.py` が検出する）。CLI の説明文は Click 定義の
help / docstring が正であり、member コマンドの一行説明は
`guildbotics/capabilities/member_reference.py` のカタログから注入される。

補足（実装ポイント）:

- `run` は `--person` または `<command>@<person_id>` でメンバー指定可能
- `workspace use` は active workspace を `~/.guildbotics/data/active-workspace.json` に保存する
- `member` group は `--workspace <dir>` を受け取り、AI CLIツール / skill 経由の member capability の入口になる。サブグループは `guildbotics/cli/member.py` にあり、`memory`（record/recall/get/update/touch/archive/promote）、`chat`（identity/inspect/post/reply/reaction/noop/complete）、`git`（prepare/commit/push/publish）、`github`（issue/pr/reaction）、`context`、`help`
- `start` と Desktop Service は共通の OS advisory lock
  `~/.guildbotics/data/run/service.lock` を使い、background service
  （scheduler worker / event listener）の machine-wide な二重起動を防ぐ。
  ファイル内の PID / owner / workspace は診断・停止用メタデータであり、
  ファイルの存在自体は稼働判定に使わない
- `stop` / `kill` は `service.lock` の owner が CLI の場合だけ記録 PID を使って
  プロセス停止する。Desktop owner は sidecar を signal せず、Desktop からの停止を要求する

### 3. コマンド実行基盤（最重要）

中心実装:

- `guildbotics/drivers/command_runner.py`
- `guildbotics/commands/*`

流れ:

1. `run_command()` が対象メンバーを解決
2. `CommandRunner` がメインコマンドを解決
3. `CommandSpecFactory` が `CommandSpec` を構築
4. 子コマンド（`commands:`）を先に実行
5. 結果を `Context.shared_state` と `Context.pipe` に反映

`Context.pipe` はコマンド間の標準入力/標準出力的な受け渡し文字列として使われます。

#### サポートされるコマンド種別（`guildbotics/commands/registry.py`）

- `.md` (`MarkdownCommand`)
- `.py` (`PythonCommand`)
- `.sh` (`ShellScriptCommand`)
- `.yml` / `.yaml` (`YamlCommand`, 定義用)

インライン専用コマンド:

- `print`
- `to_html`
- `to_pdf`

### 4. 設定ファイル解決（実装依存）

設定ファイル解決は `guildbotics/utils/fileio.py` が担当します。

重要事項:

- 優先順は「一次設定 (`GUILDBOTICS_CONFIG_DIR` or cwd の `.guildbotics/config`) → パッケージテンプレート (`guildbotics/templates`)」の2段（`fileio._get_config_path`）。`~/.guildbotics/config` のような home 設定階層は無い（`fileio.py` の `Path.home()` 参照は `~/.guildbotics/data` のデータ用のみ）
- `guildbotics member ...` は `guildbotics/utils/workspace_state.py` も使う。`--workspace` があればその workspace を最優先し、明示的な `GUILDBOTICS_CONFIG_DIR` または cwd の `.guildbotics/config` が無い場合だけ active workspace を適用する
- desktop runtime は workspace 選択時に active workspace を保存し、workspace の `.guildbotics/config` から `GUILDBOTICS_CONFIG_DIR` を設定する
- ローカライズ対応ファイルは `.<lang>` → `.en` → 素のファイル名の順で探索
- メンバー別コマンドは `team/members/<person_id>/...` を優先し、なければ共通設定へフォールバック
- シークレット（API キー / トークン）は `guildbotics/utils/secret_store.py` の SecretStore 経由で扱う。バックエンドは OS キーチェーンだけで、平文への fallback は無い（`.guildbotics/config/secrets.yml` はキー名インデックスのみで値を持たない）。キーチェーンが使えなければ `SecretStoreError`。解決優先順位は実環境変数 > キーチェーンの 2 段で、ワークスペースの `.env` は読まない（詳細: `docs/ARCHITECTURE.md` の「Secret Storage (SecretStore)」）。テストでは `tests/conftest.py` の autouse fixture `fake_keyring` が in-memory キーチェーンを入れるため、実 OS キーチェーンには触れない
- 環境変数が認証情報を運ぶかの判定は `secret_store.is_secret_env_key()` が正本。名前パターン（`TOKEN` / `SECRET` / `PASSWORD` / `PRIVATE_KEY` / `API_KEY` を含むか）と、SecretStore に保存されたキー名の provenance レジストリ（`register_secret_env_keys()`。`env_loader.read_workspace_secrets()` が skip 判定・値取得より前に登録し、プロセス生存中は単調増加）の和集合で判定する。AI CLI 子プロセスの環境からの除去（`intelligences/agent_runtime/environment.py`）と member memory の redaction（`capabilities/member_memory.py`）は両方ここから導出する。除去対象を名前の列挙で持たない（列挙は「追加を忘れた秘密」だけを残す）し、非規約名の secret（例: `DATABASE_URL`）を断片リストへの追加で塞がない（provenance が塞ぐ）

### 4.1 共有 state の書き込み（Workspace Sync Port）

`<workspace>/.guildbotics/config` と `state` はマシン間で共有する領域、`local` はこの device 限定。**マシンの形をしたものは `local` へ置く。** 絶対パス（`clones/`）と同じ意味で、ホットキー（`local/hotkeys.yml`）もそう扱う: ある組み合わせが空いているかは OS 標準ショートカット・他アプリ・キーボード配列で決まり、Workspace ではなくマシンの性質である。共有領域への書き込みは
`guildbotics/utils/workspace_sync_port.py` の `write_shared_*` / `delete_shared_path` / `notify_shared_state_changed` を通し、
完了後に `ChangeSet` を Workspace Sync Port へ通知する。

- 保存側（capability / observability / integration）は Git を知らない。port の購読者は同期実装だけとし、個別機能から同期を直接呼ばない
- `local/` 配下の path は port が自動的に落とすため、呼び出し側で共有・非共有を判定しない
- 共有 JSON は `dump_shared_json`（sort_keys + 末尾改行）で統一する。device ごとにバイト列がぶれると不要な並行更新になる
- device 固有 field を共有 record へ入れない境界は、field 名のブロックリストではなく pydantic の `extra="forbid"`（`SharedRecord`）とサイズ上限で構造的に守る
- 楽観ロック（blob ID の compare-and-set）は Config だけ。memory / Conversation / Activity / TaskRun の保存 API へ revision 引数を足さない
- **directory 全体を reconcile する画面（intelligences）は、読んだファイルの revision だけでなく path 集合そのものも申告する**（`tree_revisions()` が返す `<dir>/` の entry）。読んだ時点で存在しなかったファイルには名前が無く、file 単位の比較では表せないため、他 device が足したファイルを黙って prune できてしまう。空 mapping を返すと `guarded_config_write()` が検査自体を省略する点にも注意する（team defaults を継承中の member がこれに当たる）
- **書き込み API は「書き込み後の revision」を応答に載せる**（`ConfigWriteResponse.revisions`）。画面は保存後も開いたままで次の保存をしうるので、refetch を待たせず応答で cache を更新する。refetch 頼みだと保存が refetch を追い越したときに偽 409 になる
- **共有ファイルへの書き込みは port が `guildbotics/utils/shared_write_lock.py` の `shared_write_lock()` を取る。** 同期は「Hub の内容の checkout〜commit」の全体を保持する。片方だけが慎重でも意味が無く、比較を通った保存が、その最中に採用された他 device の内容の上に着地しうる。これは Git から見れば普通のローカル書き込みなので、次の cycle で commit / push され、失われたことがどこにも残らない。network 区間では保持しない（保存が Hub を待つことになる）
- **「この writer は lock が要るか」を writer ごとに判断しない。** 共有 path への変更は全部 port を通り、port の書き込み helper（`write_shared_*` / `append_shared_text` / `delete_shared_path` / `update_shared_text` / `update_shared_json`）が lock を**取る**。読まずに書く writer は宣言すべき span を持たないので、writer の分類テーブルは存在しない。`local/` と Workspace 未選択では取らない（sync port が change を落とすのと同じ判断であり、待たせても何とも順序づかない）
- **`notify_shared_state_changed` は lock を取らない。** 呼ばれた時点でファイルは既に変わっており、そこから queue を締め出しても何も守らない。rename や自前の unlink をする writer は、変更と announce をまとめて `shared_write_lock()` の中に置く
- **read-modify-write は `update_shared_text(path, apply)` / `update_shared_json(path, apply)` を使う。** helper が lock を取り、読み、`apply` に渡し、返ってきた内容を書く（`None` を返すと削除、同一内容なら書かない）。**完成した文字列を渡す形にしない**——渡す時点で呼び出し側は既に読み終わっており、その読みが span に入っていたかは `with` の置き場所次第になる。`ConfigRepository.write(apply, expected, report)` と同じ形であり、同じ理由（並べ損ねる場所を作らない）
- **これは config に限らない。** Conversation 制御状態（`state/chat_state`）と member memory（`state/documents`）は Git を知らないまま同じ喪失をする側で、失われるのは Slack の応答1回分と memory 文書まるごとである。process 内の `threading.Lock` / `RLock` は同じ runtime の thread しか並べないので、別 process の member CLI にも同期 queue にも効かない
- **lock で塞ぐのは「無記録の喪失」だけ。** memory / Conversation へ CAS（revision 引数）を広げない（「楽観ロックは Config だけ」と同じ理由）。記録付きの first-committer-wins は設計どおり残す
- **1つの書き込みより span が広い操作だけが、自分で `shared_write_lock()` を取る。** 該当するのは、追記か全書き換えかを大きさで決める journal（`MemoryAuditStore.record`。closure 形にすると毎回 8MB を読み書きすることになる）、書いてよいかを別ファイルから決める操作（`RunStore.complete_run` の evidence 検査）、directory 走査で決める操作（`MemberMemoryService.record(kind="policy")` の一意性判定）、複数ファイルや rename がひとまとまりの操作（memory の各公開操作、setup service、`KeyringSecretStore`）。**理由を docstring に書く**
- **`shared_write_lock` は同一 thread 内で再入する。** 「呼び出し元がすでに持っているか」を writer ごとに答えるのをやめるため。config 保存は複数ファイルの比較と書き込みにまたがって保持し、その内側の writer は test・CLI・将来の呼び出し元からも直接呼ばれる。外側の span が内側を包含する。別 thread は従来どおり待つ
- **Workspace が選択されていないときは lock を取らない**（`shared_write_lock` が素通りする）。共有ファイルも queue も無い状態であり、sync port が `shared_relative_path` で change を落とすのと同じ判断。ここで例外にすると、ambient に Workspace を解決する writer の数だけ同じ try/except が増える
- **2つの lock を取る順序を writer ごとに変えない。** 共有 lock が外側、module 内の `threading.Lock` / `RLock` が内側で統一する。片方の writer だけ逆順にすると、そこが deadlock になる（`FileConversationStateStore.append_thread_message` は device-local な cache の span と共有 state の span を分けてこれを避けている）
- **raw に `open()` / `write_text()` で共有ファイルへ書かない。** journal の追記も `append_shared_text` を通す。通さないと lock が掛からない
- **Config の書き込みは `ConfigRepository.write(apply, expected, report)` 1本に通す。** lock の取得・比較・書き込み・応答用 revision の観測を、呼び出し側が組み立てない。**部品にすると、並べ損ねる場所が writer の数だけできる**（実際に、比較を省略した writer・lock の外で revision を読む writer・応答が実在しない状態を述べる writer が同時に生まれた）。app_api 側は `config_revisions.py` の `apply_config_write()` だけを使い、**config を書く経路は1つ残らずそこを通す**。比較する対象が無い（`expected=None`）ことは、mutex が要らないことを意味しない
- **writer の母集団を URL の prefix で決めない。** config は `/config` 配下からだけ書かれるわけではない（`/commands/files*` と `/commands/author/apply` は `config/commands`、`/transcripts/settings` は `config/transcripts.yml`、`guildbotics secrets` CLI は `config/secrets.yml` を書く）。`tests/guildbotics/app_api/test_config_write_boundary.py` は **routing table の POST/PUT/DELETE 全部**を「共有 config を書く」「書かない」へ分類させるので、新しい endpoint は分類しないと落ちる。「書く」側は `ConfigRepository.write` を通ることと、lock 競合時に 503 を返すことの両方を検査する（lock 保持そのものは port が保証するので、この検査の対象は比較の方である）
- **broad な `except Exception` を挟む経路では、`SharedWriteBusyError` を先に再送出する。** そうしないとその経路だけ 500 になり、症状はその1本にしか出ない
- **commit 境界は「これから commit するもの」を検証する。** stage してから index の内容（`:0:<path>`）を検証し、不合格を unstage して held へ回す。disk を2回読む（検証で1回、`git add` で1回）形だと、その間に変わった内容がそのまま履歴になる。削除として列挙された path が commit までに再作成される経路も、同じ変更で閉じている
- **converge は全体が1つの lock 区間で、その最初に commit 境界を再実行する。** fetch 区間は lock を持たない（network を跨ぐため）ので、その間に**正しく lock を取って**保存された変更が未 commit のまま checkout で消えうる。これは writer 側 lock では閉じられない唯一のケース。先に commit すれば、採用されるか rejected として記録されるかのどちらかになる。commit ができたら `local` は古いので cycle をやり直す。**採用の判断（`merge_base` / `changed_paths` / rejected の記録）を lock の外や区間の切れ目に置かない**——converge に network 呼び出しは1つも無く、そこで開いた窓に入った writer は、正しく lock を取っていても未 commit のまま checkout に消される（rejected にも残らない）
- **1つの操作が2つの保存へ連鎖する画面では、前段が失敗したら後段を走らせない。** 前段が「保存しませんでした」と言ったのに後段だけ適用されるのは、部分適用であり、利用者に見せた文言とも矛盾する（frontend は前段の戻り値を `ConfigRevisions | null` にして表現する）
- **lock 競合は `SharedWriteBusyError`（`OSError` 系譜の外）**。`LockTimeoutError` は `TimeoutError` → `OSError` なので、そのままだと同期の「環境障害」を捕まえる網に吸われて Hub 不達と表示される。呼び出し側ごとに変換して回らず、型で網から外し、API 側は exception handler 1個で 503 にする
- **同期境界の検証（`guildbotics/workspace/validation.py`）に、種別ごとの意味検証を足さない。** 共有 record はすべて GuildBotics 自身がコードで形を決めて書くため、境界で field を再確認しても writer が既に保証していることの繰り返しにしかならない（それは writer の test の仕事）。利用者が書くファイル（commands、手で編集する設定）は壊れていても製品の通常経路でどの device でも同じように失敗するので、書きかけを「送信できない変更」にすると同期を下手にするだけ
- 境界が見るのは3つだけ。**(1) 共有 root 内・サイズ上限・decode・構文**（サイズは同期が負う2つの保証の一方＝履歴を肥大させない）、**(2) `schema_version` が現在値より新しい record**（新しい build が書いたものは古い build には読めない。writer もローカルの test も捕まえられず、受け取った device にしか分からない）、**(3) `config/secrets.yml` の構造**（もう一方の保証＝Secret 値を共有履歴へ入れない）
- 新しい共有 record を追加しても、原則として validation.py に手を入れる必要はない。`schema_version` を現在値で持たせれば世代差は自動的に検知される
- 読み手が壊れた入力を黙って skip する形（例: ID / timestamp 欠落の pending event）を見つけたら、**同期境界ではなく読み手を直す**。境界にチェックを足すと読み手の沈黙が温存され、同じ device 内の同じ欠陥は残る
- 共有 record は `schema_version` を現在値へ固定する。旧 schema の fallback 読み込みは作らない
- **世代の正本は `guildbotics/utils/workspace_sync_port.py` の `SHARED_RECORD_SCHEMA_VERSION` 1つだけ。** 種別ごとに定数を持たせない。境界は送信側でも同じ検査を通すので、1種別だけ版を上げると**書いた device 自身が自分の record を拒否して queue が止まる**（`ACTIVITY_EVENT_SCHEMA_VERSION` が実際にこの形だった）。`utils` に置くのは、writer が層をまたぐため（`observability` は `utils` にしか依存できない）
- **`state/` 配下の構造化 record は全部 `schema_version` を持つ。** 母集団は `tests/guildbotics/workspace/test_shared_schema_version.py` が実際の writer を走らせて `state/` を走査することで決まる。record ではない拡張子（`.md` / `.txt`）だけが理由つきで除外され、新しい record 種別は分類しないと落ちる。付与は書き込みの choke point で行い（chat state なら `_write_json`、memory meta なら `_write_doc`）、呼び出し側に持たせない
- **`config/secrets.yml` だけは例外で `schema_version` を持てない。** 境界の `_validate_secret_index` が top-level を `{store_id, keys}` に限定するため、付けると境界が拒否する。Secret 値の入る余地を構造的に無くすための制約なので正しい取引だが、このファイルだけ世代検知が効かない（`validation.py` の docstring に明記）
- 共有 payload の Secret マスキングとサイズ上限は `guildbotics/utils/shared_redaction.py` の `redact_for_sharing()` に一本化する。field 名の列挙で守らない（Activity event と task run journal が利用者）
- 「値の入る余地がそもそも無い」形で構造的に守れるものは、そちらを選ぶ。例: `config/secrets.yml` は key 名と generation 以外の field を拒否するため、Secret 値の混入を内容検査なしに防げる
- **書き手は、境界が拒否するものを受理しない。** サイズ上限は境界にしか見えない唯一のクラス（構文や schema と違い、製品の通常経路はどこも失敗しない）なので、書き手側の上限を境界の定数から導出する。例: `MAX_AVATAR_BYTES = MAX_SHARED_AVATAR_BYTES`、`DEFAULT_MEMORY_AUDIT_MAX_BYTES = MAX_SHARED_JOURNAL_BYTES`、`MAX_COMMAND_FILE_BYTES = MAX_SHARED_FILE_BYTES`。同じ資産に書き込み経路が複数ある場合（アップロードと URL 取り込みなど）は全部に掛ける
- `tests/guildbotics/workspace/test_shared_size_limits.py` が2つを見る。**(1)** 導出のペアを名前つきで突き合わせ、alias が数値に書き戻されたら落とす。**(2)** package 内の `*_BYTES` / `*_CHARS` を全列挙し、「共有ファイルを縛る（境界定数から導出する）」「共有されないものを縛る（何を）」へ分類させる。**上限を書き直すのは読みにくい重複ではなく欠陥で、2つの差分がそのまま「保存は成功するが永久に送信できない」範囲になる**。共有と無関係な上限（log の末尾、agent へ渡す prompt）にも1行の分類を課すが、この検査が立っているのは**まだ書かれていない writer** が独自の数値を持ち込む場所であり、そこを見張るものは他に無い
- 1つの文書が複数ファイルに分かれる場合（memory の `meta.yml` と `body.md`）は、**どちらも書く前に両方を測る**。片方が着地して片方が拒否された文書は、古い文書でも新しい文書でもない

### 5. スケジューラ

中心実装:

- `guildbotics/drivers/task_scheduler.py`
- `guildbotics/drivers/utils.py`

実装上のポイント:

- アクティブなメンバーごとにスレッドを起動
- 各スレッド内で asyncio イベントループを使用
- 定期コマンドと routine コマンドを実行
- 連続エラー数でワーカーループ停止（`consecutive_error_limit`）

既定 routine コマンド（Simple edition）:

- `workflows/ticket_driven_workflow` (`SimpleEdition.get_default_routines()`)

### 6. Edition 切替

`guildbotics/editions/__init__.py#get_edition()` で `GUILDBOTICS_EDITION` を見て Edition を切り替えます。`Edition` は `get_context()` と `get_default_routines()` のみを提供する実行時の抽象です。

- 既定: `simple`
- 実体: `guildbotics/editions/simple/simple_edition.py`
- 設定書き込みロジック（GUI が再利用）: `guildbotics/editions/simple/setup_service.py`

### 7. member プロンプト層モデル

member 向けエージェント指示（SKILL / workflow プロンプト）は層モデルで管理する。

- 全体共通（コマンドカタログ、標準作業手順、memory / communication style などの横断契約）は `guildbotics/capabilities/member_reference.py` に置き、`member context` / `member help` で runtime 配信する
- workflow 共通の封筒（実行モードマーカー、isolated workspace、complete 必須、AgentResponse 規定）は `guildbotics/templates/locales/commands/workflows/common.{en,ja}.yml` の `workflow_contract` に置き、各 workflow が `{workflow_contract}` としてテンプレートへ注入する
- trigger 固有契約（完了コマンドの具体形、判断ポリシー）だけを `functions/handle_github_ticket` / `functions/handle_chat_event` に書く
- 対話封筒（共有ワークスペース、`--workspace-mode current`、対話 DOD）だけを `skills/guildbotics/SKILL.md` に書く
- 同じ文が 2 ファイル以上に現れたら、より深い層へ移す。層境界と en/ja 整合は `tests/guildbotics/templates/commands/functions/test_prompt_layer_boundaries.py` が担保する

## 開発時の基本コマンド（CI 準拠）

CI (`.github/workflows/ci.yml`) で使われている手順:

```bash
uv sync --extra test --extra dev
uv run --no-sync python scripts/generate-cli-reference.py --check
uv run --no-sync ruff format --check guildbotics tests
uv run --no-sync ruff check guildbotics
uv run --no-sync mypy guildbotics
uv run --no-sync pylint guildbotics
uv run --no-sync python -m pytest tests/ --cov=guildbotics --cov-report=xml
```

Markdown の内部リンク・見出しアンカー検査（リポジトリルートで実行。CI と同じ
[`lychee` v0.24.2](https://github.com/lycheeverse/lychee/releases/tag/lychee-v0.24.2)
をインストールする。Rust toolchain がある場合は
`cargo install lychee --version 0.24.2 --locked` で導入できる）:

```bash
lychee --no-progress --scheme file --include-fragments \
  --exclude-path 'desktop[\\/]node_modules' \
  './*.md' './docs/**/*.md' './desktop/**/*.md' './skills/**/*.md'
```

`--scheme file` により外部 URL は検査せず、CI の `markdown-links` job と同じく
相対パスと GitHub 形式の見出しアンカーだけを検査する。

必要に応じて:

```bash
uv sync --extra dev
```

desktop frontend (`desktop/`) の品質確認:

```bash
cd desktop
npm ci
npm run format:check
npm run lint
npm run typecheck
npm run duplicates
npm run test
```

まとめて実行する場合:

```bash
cd desktop
npm run quality
```

Playwright E2E（実ブラウザ + 実 Local API backend。`npm run quality` / 通常 push CI には含めない）:

```bash
cd desktop
npm ci
npm run e2e:install   # 初回のみ: chromium を取得
npm run e2e           # desktop/e2e/*.spec.ts を headless chromium で実行
```

- harness（`desktop/e2e/start-stack.mjs`）が backend を `uv run python -m guildbotics.app_api` で temp workspace 起動するため、事前にリポジトリルートで `uv sync --extra test --extra dev` 済みであること。
- 詳細・journey 一覧は `desktop/README.md` の「テスト」節を参照。

desktop packaging / Tauri 変更時の確認:

- `scripts/desktop-build-backend.sh` は PyInstaller で `guildbotics-app-api` と `guildbotics-cli` の 2 本を build し、`desktop/src-tauri/binaries/*-<target>` に配置する
- `scripts/desktop-dev-tauri.sh` は `scripts/desktop-write-dev-binaries.sh` で Local API / CLI の開発用 wrapper を生成する
- Rust/Tauri 側を変更したら `cargo fmt --check`、`cargo check`、必要に応じて `scripts/desktop-test-rust.sh` を実行する。test wrapper は Tauri の `externalBin` をテスト時だけ無効化するため、sidecar の事前 build は不要
- sidecar / packaging script を変更したら `bash -n scripts/desktop-build-backend.sh scripts/desktop-build-frontend.sh scripts/desktop-dev-tauri.sh scripts/desktop-write-dev-binaries.sh scripts/desktop-target.sh scripts/desktop-smoke-sidecars.sh scripts/desktop-test-rust.sh` と、可能なら `scripts/desktop-build-backend.sh` による smoke を行う

エージェント作業時の品質確認:

- 確認コマンドは、CI とこのファイルに明記された範囲、および変更内容に直接対応する関連テストに限定する。指定範囲より広い確認（例: CI で `ruff check` 対象外の `tests/` に対する `ruff check`、関連範囲を超える全量 E2E、packaging smoke など）は、ユーザーが明示した場合、または事前に目的・追加コストを説明して承認を得た場合だけ実行する。「念のため」の広範囲チェックを無断で追加しない。
- Python コードを変更したら、原則として `ruff format --check` と `ruff check` と `mypy` と関連 `pytest` を実行してから完了報告する（`ruff check` と `ruff format --check` は別物。`ruff format --check` の対象は `guildbotics` と `tests`、`ruff check` / `mypy` / `pylint` の対象は `guildbotics` のみ。整形漏れは CI の `test` ジョブで落ちるため、`ruff format --check` を必ず含める。整形が必要なら `uv run --no-sync ruff format guildbotics tests` を実行）
- 重複コード確認は `uv run --no-sync pylint guildbotics` を使う（`pyproject.toml` で `duplicate-code` のみ有効化）
- 対象範囲（リポジトリ直下、`docs/`、`desktop/`、`skills/`）の Markdown を変更したら、上記の `lychee` コマンドを実行する
- 最低限の確認コマンドは上記「開発時の基本コマンド（CI 準拠）」と同じ（`pytest` は関連範囲に絞ってよい）
- 型エラーや lint エラーを回避するためだけの `# type: ignore` や noqa は、理由が明確でない限り追加しない

desktop TypeScript 開発時の品質確認:

- Node.js は CI と合わせて 24 系を前提にする
- frontend の依存更新は `desktop/package-lock.json` も更新する
- TypeScript / React コードを変更したら、原則として `npm run format:check`、`npm run lint`、`npm run typecheck`、`npm run duplicates`、関連する `npm run test` を実行してから完了報告する
- 整形が必要な場合は `desktop` で `npm run format` を実行し、Prettier の結果を正とする
- React コンポーネントの挙動変更時は React Testing Library によるコンポーネントテストを追加・更新する
- 純粋関数、入力変換、API payload 生成、trace / scheduler 表示ロジックなどの分岐を変更した場合は Vitest のユニットテストを追加・更新する
- 重複コード抑止は `npm run duplicates` (`jscpd`) を使う。重複検出を避けるためだけの不自然な分割ではなく、UI とロジックの責務が自然に分かれる形へ整理する
- Tauri / Rust 側や生成物を frontend 品質チェックへ巻き込まない。対象は `desktop/src` と frontend 設定ファイルを基本とする
- 実ブラウザ + 実 backend を貫く critical user journey（setup→作成 / scheduler start-stop / command 実行+ストリーム / diagnostics / backend down→retry）を変更したら、`desktop/e2e/*.spec.ts` の該当 Playwright journey を更新する。E2E は `npm run quality` / push CI には含めず、ローカルの `npm run e2e` または専用の `Desktop E2E` workflow で実行する
- E2E に振る舞いパターンを総当たりで持ち込まない。分岐網羅は Vitest の unit / component（mock 境界）に置き、Playwright は jsdom では検証できない実ブラウザ + 実ワイヤ契約（`client.ts ↔ FastAPI ↔ EventBus`）+ 実ファイル書き込みに絞る

## テスト実装の考え方

このリポジトリでは、テストピラミッドに従ってテストを維持する。修正や機能追加の際には「既存テストが通ること」だけでは不十分で、変更した振る舞いを検出できるテストが追加・更新されていることを完了条件に含める。

基本方針:

- Unit test を最も厚くする。純粋関数、入力変換、validator、payload 生成、状態遷移、エラー変換、ファイル解決順はまず unit test で網羅する
- Component / service integration test は、UI 操作、API endpoint、config 書き込み、runtime lifecycle など境界をまたぐ主要 workflow に限定して追加する
- ブラウザ E2E（Playwright, `desktop/e2e/`）は lean-but-real。実ブラウザ engine + 実 Local API backend でしか検証できない critical user journey（setup→実ファイル書き込み、scheduler start/stop、command 実行+`/events` ストリーム、diagnostics、backend down→retry）に絞り、振る舞いパターンの総当たりはしない（分岐網羅は unit / component に委譲）。通常の push CI には含めず、ローカルの `npm run e2e` または専用の `Desktop E2E` workflow で実行する
- Tauri ネイティブ / packaging smoke は最小限に保ち、実 OS + Tauri runtime が要るもの（sidecar 起動 / `backend_info` / file picker など）は workflow_dispatch / release workflow に隔離する
- LLM、GitHub、Slack、外部 CLI などへの実通信は通常 CI のテストに入れない。既存抽象化、stub、mock、fixture を使い、送信 payload、判定結果、エラー処理を検証する
- テストは決定論的かつ hermetic に保つ。時間・乱数・環境変数・cwd・HOME・I/O は `monkeypatch` / `tmp_path` で制御し、実 home ディレクトリや外部サービスに触れない
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
- desktop の cross-boundary user journey（実 backend を貫く setup / runtime / commands / diagnostics / 起動失敗）を変更したら、`desktop/e2e/` の該当 Playwright spec を追加・更新する（実ブラウザ + 実 backend で検証。総当たりはせず代表 journey に絞る）
- i18n 文言や翻訳キーを変更したら、キー経由の検証を行い、片方の言語だけ欠落しないことを確認する
- bug fix では、先に再現テストまたは同等の failing assertion を追加し、そのテストが修正後に通ることを確認する

テスト追加を省略してよいのは、コメント修正、内部ドキュメントだけの変更、format のみ、型だけの機械的変更など、実行時の振る舞いが変わらないと明確に説明できる場合に限る。その場合も完了報告で「テスト追加不要の理由」を明記する。

### 失敗したテスト・チェックの扱い

自分が実行したテストやチェックが失敗した場合、原因が自分の変更かどうかに関わらず放置しない。「今回の変更とは無関係」は切り分けの結論であって、対応を終えてよい理由ではない。

- まず切り分ける。推測で結論せず、変更を戻した状態（`git stash` や main）で同じ対象を実行し、自分の変更が原因かを確認する。自分のせいだと決めつけることも、自分のせいではないと決めつけることも、同じように避ける
- 自分の変更が原因なら直す。原因でないと確認できた場合も、そこで止めずに失敗の原因まで特定する
- 「環境依存」「flaky」で説明を終えない。なぜその環境でだけ失敗するのかまで追う。環境差そのものが欠陥を隠していることがある（例: ローカルでは実 CLI ツールを起動して失敗し、CI では未インストールのため即座に失敗して緑になる）
- 原因を特定したら、その時点で issue の下書きを起こしてユーザーへ提示し、判断を仰ぐ。issue の登録も、今回の変更に含めての修正も、勝手に始めない
- 下書きはそのまま登録できる体裁で書く。タイトル、再現条件、環境ごとの見え方、影響、やること、完了条件、関連を含める。この下書きが報告そのものなので、別途要約した短い報告で代替しない
- 今回の変更に含めるか、別 issue にするか、見送るかはユーザーが決める。判断材料として、その失敗が今回の変更と性質・完了条件を共有するかどうかを示す
- ユーザーの応答を待てない実行（workflow など）では、同じ下書きを完了報告や ticket / thread のコメントへそのまま載せ、登録はせずに判断を委ねる。「無関係なので無視した」で終わらせない

## 変更時の実務ルール

- ソースコード内のコメントと docstring は英語で書く。docstring は Google スタイルを使う
- 挙動変更時は、対応テストを `tests/guildbotics/...` に追加・更新する
- コマンド仕様変更時は、`docs/custom_command_guide.en.md` / `docs/custom_command_guide.ja.md` の整合性も確認する
- CLI のオプションやコマンド変更時は、新しいオプションに help を書き、`uv run --no-sync python scripts/generate-cli-reference.py` で `docs/cli_reference.md` を再生成して commit する（忘れると CI の `--check` ステップと `tests/guildbotics/cli/test_cli_reference.py` が fail する）。あわせて `README.md` / `README.ja.md` の使用例も確認する
- ユーザー向け文言を Python にハードコードしない。既存実装に合わせて `guildbotics.utils.i18n_tool.t()` を使い、翻訳キーを `guildbotics/templates/locales/...`（必要なら `*.ja.yml` / `*.en.yml`）へ追加する
- i18n 文言を変更・追加した場合は、既存テストに合わせて翻訳キー経由で検証する（文言直書き前提のテストにしない）
- `Context.pipe` / `shared_state` の更新順序はワークフロー互換性に直結するため、変更時は特に注意する
- コマンド解決順 (`get_person_config_path`, `get_config_path`) を壊さない

## 参照優先度（このリポジトリでの推奨）

実装確認の優先順:

1. `guildbotics/cli/__init__.py`
2. `guildbotics/drivers/command_runner.py`
3. `guildbotics/commands/*`
4. `guildbotics/runtime/context.py`
5. `guildbotics/utils/fileio.py`
6. desktop / diagnostics まわりは `guildbotics/app_api/runtime.py`、`guildbotics/observability/*`、`desktop/src/*`
7. `tests/guildbotics/...`

ドキュメントは補助として利用し、矛盾があればソースに合わせて修正すること。`docs/ARCHITECTURE.md` はアーキテクチャ概観と中心コンセプトの説明で、実装より古い場合がある。実装計画は文書としてリポジトリへ残さない: 未実装の将来計画は GitHub issue で管理し、実装が完了した計画書は中心的なコンセプトを `docs/ARCHITECTURE.md` などの恒久ドキュメントへ移した上で削除する。
