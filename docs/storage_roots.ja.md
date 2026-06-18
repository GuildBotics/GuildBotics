# GuildBotics の storage / workspace / config root 設計

## 目的

GuildBotics は、設定ファイル、desktop / CLI のマシン状態、runtime の実行データ、CLI agent の作業領域を複数の場所に読み書きする。

このドキュメントは、それぞれの保存先を明確に分離し、desktop / CLI / scheduler / member capability / diagnostics の実装者が同じ規則で path を解決できるようにするための実装仕様である。

最も重要な原則は以下の 4 点である。

- machine state は workspace を選ぶ前に必要な状態なので、workspace 設定や workspace `.env` の影響を受けてはならない。
- workspace data は実行対象 workspace に紐づくデータなので、workspace ごとに分離する。
- `GUILDBOTICS_CONFIG_DIR` は config source を差し替えるための変数であり、workspace root や data root を表す変数として扱わない。
- workspace を適用する境界で effective workspace data root を確定し、`GUILDBOTICS_DATA_DIR` として process 環境へ反映する。以後の runtime / subprocess はこの値を正として使う。

## 用語

### Machine State Root

Machine state root は、ユーザーのマシン上で GuildBotics 自体が状態を保持するための固定 root である。

```text
$HOME/.guildbotics/data
```

この root は常に `HOME` から解決する。`GUILDBOTICS_DATA_DIR`、`GUILDBOTICS_CONFIG_DIR`、workspace `.env`、active workspace の影響を受けない。

Machine state root に置くものは、workspace を決定する前、または workspace と独立して必要になる状態だけである。

対象:

- active workspace state
- CLI daemon / scheduler の PID file
- workspace 選択前に参照する desktop / CLI 共有状態

### Runtime Workspace Root

Runtime workspace root は、現在の runtime が作業対象としている workspace の root である。

Desktop / App API では、選択された workspace に backend process が `chdir` するため、`Path.cwd()` が runtime workspace root になる。

CLI / member CLI では、以下の優先順で runtime workspace root を決める。

1. `guildbotics member --workspace <dir>` が指定されていれば `<dir>`
2. active workspace state が適用された場合は、その `workspace`
3. それ以外は command 実行時の cwd

Runtime workspace root は、config の保存場所とは別概念である。desktop の「ホーム共通」設定では、config は `$HOME/.guildbotics/config` に置かれるが、runtime workspace root は選択された作業ディレクトリである。

### Workspace Data Root

Workspace data root は、runtime workspace に紐づく実行データを保存する root である。

既定値:

```text
<runtime-workspace-root>/.guildbotics/data
```

workspace `.env` に `GUILDBOTICS_DATA_DIR` が設定されている場合、workspace data root はその値で上書きできる。

`GUILDBOTICS_DATA_DIR` は workspace data root の override であり、machine state root には効かない。

workspace を適用した process では、最終的に使う workspace data root を `os.environ["GUILDBOTICS_DATA_DIR"]` に必ず設定する。これにより、member CLI や CLI agent subprocess の cwd が runtime workspace root と異なる場合でも、同じ workspace data root を参照できる。

`os.environ["GUILDBOTICS_DATA_DIR"]` の書き換えは、workspace 適用境界でのみ行う。workspace 適用境界とは、App API の `set_workspace()`、CLI / member CLI の起動時 workspace 解決、`guildbotics run` / `start` の runtime 初期化のように、まだ member 作業や scheduler worker が走っていない地点を指す。scheduler の per-member worker や command / workflow の個別実行中に process-global な `os.environ` を書き換えてはならない。

per-invocation の値は、従来どおり subprocess 環境 overlay で渡す。例えば run id や CLI agent に渡す追加環境変数は `cli_agent_env` に入れ、process-global `os.environ` には書かない。これは scheduler の複数 worker 間で race を起こさないための不変条件である。

effective workspace data root の解決順は以下とする。

1. runtime workspace root の `.env` に `GUILDBOTICS_DATA_DIR` が設定されていれば、その値
2. process 起動時点で `GUILDBOTICS_DATA_DIR` が設定されていれば、その値
3. `<runtime-workspace-root>/.guildbotics/data`

上記の 1 または 2 が relative path の場合は、既存仕様と同じく `expanduser().resolve(strict=False)` で現在 process の cwd に対して解決する。workspace ごとに別ボリュームへ逃がしたい場合は、workspace `.env` に絶対 path を設定することを推奨する。

process 起動時点の `GUILDBOTICS_DATA_DIR` は global fallback として働く。同じ process 内で複数 workspace を切り替える場合、各 workspace の `.env` に override がなければ、それらの workspace data は同じ directory に集約される。この挙動は headless / test / 特殊運用向けの escape hatch であり、通常の desktop 利用では workspace ごとの `<workspace>/.guildbotics/data` を使う。

App API のように 1 process 内で workspace を切り替える runtime は、process 起動時点の `GUILDBOTICS_DATA_DIR` を別途保持する。workspace 適用後に `os.environ["GUILDBOTICS_DATA_DIR"]` を effective workspace data root へ更新するため、次回 workspace 切替時に現在の `os.environ` を process 起動時点の fallback として読んではならない。

対象:

- member workspace
- task run / chat run evidence
- prompt trace
- diagnostics records
- chat state
- runtime 実行中に workspace と一緒に扱う永続データ

### Config Root

Config root は、GuildBotics の設定ファイルを読むための root である。

Primary config root は `GUILDBOTICS_CONFIG_DIR` が設定されていればその値、未設定なら cwd の `.guildbotics/config` である。

Fallback config root は `$HOME/.guildbotics/config` である。

Package template は最後の fallback として扱う。

Config root は workspace root ではない。次のような config root は有効だが、workspace root を表しているとは限らない。

```text
$HOME/.guildbotics/config
/etc/guildbotics/config
/tmp/guildbotics-config
```

## Root 解決 API

実装では root の意味を関数名で明確に分ける。

### `get_machine_state_root()`

Machine state root を返す。

```python
def get_machine_state_root() -> Path:
    return Path.home() / ".guildbotics" / "data"
```

この関数は環境変数を読まない。

### `get_machine_state_path(*parts)`

Machine state root 配下の path を返す。

```python
def get_machine_state_path(*parts: str) -> Path:
    return get_machine_state_root().joinpath(*parts)
```

使用例:

```python
active_workspace_file = get_machine_state_path("active-workspace.json")
pid_file = get_machine_state_path("run", "scheduler.pid")
```

### `get_runtime_workspace_root()`

現在の runtime workspace root を返す。

App API runtime では `Path.cwd()` を返す。

member CLI のように workspace state を明示的に解決する経路では、適用済み `WorkspaceState.workspace` を優先できるようにする。

workspace を適用する境界では、runtime workspace root と effective workspace data root を同時に確定する。具体的には、`apply_workspace_for_cli()`、`guildbotics member --workspace`、App API の `set_workspace()` のような入口で `GUILDBOTICS_DATA_DIR` を effective workspace data root に設定する。

これにより、workspace data path の呼び出し元は cwd から workspace root を再推定しない。cwd が member workspace や任意の作業 directory に変わっても、`GUILDBOTICS_DATA_DIR` が権威ある workspace data root として機能する。

### `resolve_workspace_data_root(workspace_root: Path, env_file: Path | None = None, inherited_data_dir: str | None = None)`

workspace 適用時に effective workspace data root を計算する。

解決順:

1. `env_file` が存在し、その中に `GUILDBOTICS_DATA_DIR` があればその値
2. process 起動時点の `GUILDBOTICS_DATA_DIR` があればその値
3. `<workspace_root>/.guildbotics/data`

この関数は `.env` 全体の dotenv override semantics に依存してはならない。desktop は現状 `.env` を `override=True` で読み込み、CLI `member` は `override=False` で読み込むため、単純に `os.environ` を見るだけだと経路ごとに結果が変わる。`GUILDBOTICS_DATA_DIR` については、workspace data root の仕様として `.env` の値を明示的に parse し、全経路で同じ優先順位にする。

`inherited_data_dir` には process 起動時点の `GUILDBOTICS_DATA_DIR` を渡す。App API のように workspace を複数回切り替える process では、workspace 適用後の `os.environ["GUILDBOTICS_DATA_DIR"]` を fallback として再利用してはならない。

戻り値は `expanduser().resolve(strict=False)` 済みの path とする。

実装の流れは、workspace 適用境界では `inherited_data_dir` と workspace `.env` から `resolve_workspace_data_root()` で effective root を計算し、その結果を `os.environ["GUILDBOTICS_DATA_DIR"]` に書く。それ以外の通常の読み取り箇所では `get_workspace_data_root()` が現在の `os.environ["GUILDBOTICS_DATA_DIR"]` を読む。この 2 段階を混同しない。

### `get_workspace_data_root(workspace_root: Path | None = None)`

Workspace data root を返す。

解決順:

1. 現在の process 環境に `GUILDBOTICS_DATA_DIR` が設定されていれば、その値を `expanduser().resolve(strict=False)` して返す。
2. `workspace_root` が渡されていれば `<workspace_root>/.guildbotics/data` を返す。
3. `workspace_root` が渡されていなければ `Path.cwd()/.guildbotics/data` を返す。

```python
def get_workspace_data_root(workspace_root: Path | None = None) -> Path:
    if data_dir := os.getenv("GUILDBOTICS_DATA_DIR", "").strip():
        return Path(data_dir).expanduser().resolve(strict=False)
    root = workspace_root if workspace_root is not None else Path.cwd()
    return root.expanduser().resolve(strict=False) / ".guildbotics" / "data"
```

`GUILDBOTICS_DATA_DIR` は workspace `.env` から読み込まれる可能性がある。これは workspace data root の override として許可する。

ただし、通常の runtime 入口では先に `resolve_workspace_data_root()` で effective root を計算し、その値を `GUILDBOTICS_DATA_DIR` に設定してからこの関数を使う。`workspace_root=None` の cwd fallback は、workspace 適用前の低レベル fallback と test 用に限定する。

### `get_workspace_data_path(*parts, workspace_root=None)`

Workspace data root 配下の path を返す。

```python
def get_workspace_data_path(
    *parts: str,
    workspace_root: Path | None = None,
) -> Path:
    return get_workspace_data_root(workspace_root).joinpath(*parts)
```

使用例:

```python
diagnostics_file = get_workspace_data_path("run", "diagnostics.jsonl")
prompt_trace_file = get_workspace_data_path("run", "prompt_trace.jsonl")
chat_state_dir = get_workspace_data_path("chat_state")
member_workspace = get_workspace_data_path("workspaces", person_id)
```

## 保存先の仕様

### Active Workspace State

保存先:

```text
<machine-state-root>/active-workspace.json
```

既定の実パス:

```text
$HOME/.guildbotics/data/active-workspace.json
```

このファイルは、desktop app または `guildbotics workspace use <dir>` が最後に選択した workspace を記録する。

このファイルは workspace を選ぶ前に読む必要があるため、workspace `.env` の `GUILDBOTICS_DATA_DIR` に影響されてはならない。

### Scheduler PID File

保存先:

```text
<machine-state-root>/run/scheduler.pid
```

既定の実パス:

```text
$HOME/.guildbotics/data/run/scheduler.pid
```

PID file は CLI daemon の単一性確認と stop / kill のための machine state である。workspace data root には置かない。

Desktop sidecar runtime は process 内で lifecycle を管理するため、この PID file を作らない。

### Workspace `.env`

保存先:

```text
<runtime-workspace-root>/.env
```

desktop の設定ファイル保存先が「作業ディレクトリ内」でも「ホーム共通」でも、`.env` は runtime workspace root に置く。

`.env` は secrets と runtime 設定を保持する。

`.env` に `GUILDBOTICS_DATA_DIR` が設定されている場合、その値は workspace data root の override としてのみ使う。machine state root には影響させない。

`GUILDBOTICS_DATA_DIR` の解決は、dotenv の `override=True/False` の違いに依存させない。workspace 適用処理は `.env` を parse し、`.env` 内の `GUILDBOTICS_DATA_DIR` があれば process 環境へ明示的に反映する。

### Project / Member Config

設定ファイルは config root 配下に置く。

Desktop 設定画面の保存先ごとの挙動:

| 保存先 | `config_dir` | `env_file_path` | runtime workspace root |
| --- | --- | --- | --- |
| 作業ディレクトリ内 | `<workspace>/.guildbotics/config` | `<workspace>/.env` | `<workspace>` |
| ホーム共通 | `$HOME/.guildbotics/config` | `<workspace>/.env` | `<workspace>` |

`config_dir` は config source であり、workspace data root の根拠にはしない。

### Member Workspace

保存先:

```text
<workspace-data-root>/workspaces/<person_id>
```

この配下に ticket / PR 用の clone が作られる。

workflow が CLI agent を起動するときは、`cwd` をこの member workspace にする。

CLI agent subprocess には現在の workspace data root を `GUILDBOTICS_DATA_DIR` として必ず渡す。これは利便性ではなく正しさのために必須である。CLI agent の cwd は member workspace なので、この値を渡さないと子プロセスが `<member-workspace>/.guildbotics/data` のような入れ子の誤った data root を計算する。

### Task Run / Chat Run Evidence

保存先:

```text
<workspace-data-root>/task-runs/<run_id>.jsonl
```

ticket workflow / chat workflow / member capability の証跡は workspace data に属する。

workflow は run id を agent subprocess に環境変数で渡す。

使用する環境変数:

- `GUILDBOTICS_TASK_RUN_ID`
- `GUILDBOTICS_RUN_ID`

### Diagnostics Store

保存先:

```text
<workspace-data-root>/run/diagnostics.jsonl
```

diagnostics は workspace に対する実行履歴であるため、workspace data root に置く。

workspace を切り替えたら、diagnostics の既定表示対象も切り替わる。

実装上の注意: `DiagnosticsStore` のように生成時に path を保持する store は、App API 作成時に workspace data path を凍結してはならない。workspace 切替後に diagnostics の表示対象を切り替えるには、次のいずれかを実装する。

- store が record / query のたびに `get_workspace_data_path("run", "diagnostics.jsonl")` を遅延解決する。
- App API の `set_workspace()` で diagnostics store を新しい workspace data root に rebind し、EventBus も新しい store を参照する。

どちらを採用してもよいが、`create_app()` 時点の cwd で `diagnostics.jsonl` の path を確定し、そのまま保持する実装にしてはならない。

### Prompt Trace

既定の保存先:

```text
<workspace-data-root>/run/prompt_trace.jsonl
```

`GUILDBOTICS_PROMPT_TRACE_PATH` が設定されている場合は、その path を優先する。

`GUILDBOTICS_PROMPT_TRACE_PATH` は workspace `.env` に保存してよい。この値は prompt trace の出力先だけを変える。

### Chat State

保存先:

```text
<workspace-data-root>/chat_state
```

Slack / chat workflow の cursor、processed event、thread state、pending event は workspace data として扱う。

`FileConversationStateStore` のように生成時に base directory を保持する store は、workflow 実行時に作るか、workspace 切替時に rebind する。App API の process 起動時に作った store を workspace 切替後も使い続けてはならない。

### Command Output

`to_html` / `to_pdf` など、command の明示的な output 指定はこの root 設計とは別扱いである。

相対 output path は command cwd 基準で解決し、絶対 path や `~` は指定通りに扱う。

## 実行経路ごとの挙動

### Desktop App / App API

1. ユーザーが workspace を選ぶ。
2. App API は `set_workspace(workspace)` で `os.chdir(workspace)` する。
3. active workspace state を machine state root に保存する。
4. `<workspace>/.env` があれば読み込む。
5. `GUILDBOTICS_CONFIG_DIR` は `<workspace>/.guildbotics/config` に設定する。
6. process 起動時点に保存しておいた inherited `GUILDBOTICS_DATA_DIR` と `<workspace>/.env` から effective workspace data root を解決し、`GUILDBOTICS_DATA_DIR` に設定する。
7. workspace data root は `GUILDBOTICS_DATA_DIR` の値になる。

Desktop で設定保存先を「ホーム共通」にした場合でも、runtime workspace root は選択 workspace のままである。config root だけが `$HOME/.guildbotics/config` になる。

### `guildbotics workspace use <dir>`

1. `<dir>` が directory であることを確認する。
2. machine state root の `active-workspace.json` に workspace state を保存する。

この command は workspace data root を作成しなくてよい。

### `guildbotics member --workspace <dir> ...`

1. `<dir>` を runtime workspace root とする。
2. `GUILDBOTICS_CONFIG_DIR=<dir>/.guildbotics/config` を設定する。
3. `<dir>/.env` が存在すれば `GUILDBOTICS_ENV_FILE=<dir>/.env` として読み込む。
4. effective workspace data root を解決し、`GUILDBOTICS_DATA_DIR` に設定する。
5. workspace data root は `GUILDBOTICS_DATA_DIR` の値になる。

### `guildbotics member ...` で `--workspace` なし

1. 明示的な `GUILDBOTICS_CONFIG_DIR` または cwd の `.guildbotics/config` が存在する場合、それを config source として使う。
2. 上記がない場合、machine state root の active workspace state を読む。
3. active workspace があれば、その workspace を runtime workspace root として扱う。
4. `.env` は `GUILDBOTICS_ENV_FILE` が指す絶対 path を優先し、なければ runtime workspace root または cwd の `.env` を読む。
5. runtime workspace root が active workspace または cwd として確定したら、effective workspace data root を解決し、`GUILDBOTICS_DATA_DIR` に設定する。

明示的な `GUILDBOTICS_CONFIG_DIR` があるだけでは runtime workspace root は確定しない。この場合は cwd を runtime workspace root として扱い、cwd の `.env` または `GUILDBOTICS_ENV_FILE` から workspace data root を解決する。

### `guildbotics run`

`guildbotics run` は one-shot runtime として扱う。

runtime workspace root は `--cwd` が指定されていればその directory、未指定なら process cwd である。

`.env` は runtime workspace root の `.env` を読み込む。`GUILDBOTICS_ENV_FILE` が明示されている場合は、その絶対 path を優先する。

workspace data root は runtime workspace root の `.guildbotics/data` になる。ただし effective workspace data root 解決で `GUILDBOTICS_DATA_DIR` があればその値を使う。

実装時は、command 実行前に runtime workspace root を確定し、その directory を基準に `.env` と workspace data root を解決する。`--cwd` が指定されているのに process cwd の `.env` を読む実装にしてはならない。

### `guildbotics start`

CLI daemon の PID file は machine state root に置く。

runtime workspace root は process cwd である。

`.env` は cwd の `.env` を読み込む。

workspace data root は cwd の `.guildbotics/data` になる。ただし effective workspace data root 解決で `GUILDBOTICS_DATA_DIR` があればその値を使う。

`guildbotics start` を `$HOME` から起動した場合、workspace data root は `$HOME/.guildbotics/data` となり、machine state root と物理的に同じ directory になる。この場合でも、PID file は `run/scheduler.pid`、diagnostics は `run/diagnostics.jsonl` のようにファイル名で分離される。従来の home 基準運用では path が変わらないため、既存インストールは移行なしで動作する。

## 実装移行手順

### 1. Root 解決関数を追加する

`guildbotics/utils/fileio.py` または専用 module に以下を追加する。

- `get_machine_state_root()`
- `get_machine_state_path(*parts)`
- `resolve_workspace_data_root(workspace_root: Path, env_file: Path | None = None, inherited_data_dir: str | None = None)`
- `get_workspace_data_root(workspace_root: Path | None = None)`
- `get_workspace_data_path(*parts, workspace_root: Path | None = None)`
- 必要なら `get_member_workspace_path(person_id, workspace_root: Path | None = None)`

既存の `get_storage_path()` は意味が曖昧なので、新規コードでは使わない。

互換性のために `get_storage_path()` を残す場合は、段階的に呼び出し元を置き換えたあと、用途を限定する。

### 2. Machine state を置き換える

以下は machine state root を使う。

- `active_workspace_file()`
- scheduler pid file

置き換え例:

```python
def active_workspace_file() -> Path:
    return get_machine_state_path("active-workspace.json")
```

```python
def _pid_file_path() -> Path:
    return get_machine_state_path("run", "scheduler.pid")
```

### 3. Workspace data を置き換える

以下は workspace data root を使う。

- member workspace
- task runs
- diagnostics store
- prompt trace 既定 path
- file chat state store

置き換え例:

```python
def get_workspace_path(person_id: str, workspace_root: Path | None = None) -> Path:
    return get_workspace_data_path("workspaces", person_id, workspace_root=workspace_root)
```

```python
def default_store_path(workspace_root: Path | None = None) -> Path:
    return get_workspace_data_path("run", "diagnostics.jsonl", workspace_root=workspace_root)
```

ただし、App API process の lifetime をまたいで保持する store は、単純な path 置き換えだけでは不十分である。`DiagnosticsStore` のように `__init__` で path を確定する class は、workspace 切替時に古い path を持ち続ける。以下のどちらかに変更する。

- store 内で毎回 path を遅延解決する。
- `set_workspace()` 後に store と、それを参照する `EventBus` / runtime を rebind する。

workflow 実行時に都度作る store は、実行前に `GUILDBOTICS_DATA_DIR` が effective workspace data root に設定されていれば、そのまま `get_workspace_data_path()` を使える。

### 4. App API の status 表示を更新する

`ConfigStatus.storage_dir` は意味が曖昧なので、以下のように分ける。

- `machine_state_dir`
- `workspace_data_dir`

既存 frontend 互換が必要なら `storage_dir` は一時的に残し、`workspace_data_dir` と同じ値を返す。ただし UI 表示や新規実装では新しい field を使う。

### 5. `.env` ロード順を明確にする

workspace `.env` は runtime workspace root が決まった後に読み込む。

`.env` に含まれる `GUILDBOTICS_DATA_DIR` は、以後の workspace data root にだけ影響する。

machine state root を解決する関数では `.env` 由来の環境変数を参照しない。

`GUILDBOTICS_DATA_DIR` については、desktop と CLI の dotenv override semantics 差分を持ち込まない。workspace 適用処理は `.env` を明示的に parse し、`.env` 内に `GUILDBOTICS_DATA_DIR` があればそれを優先して `os.environ["GUILDBOTICS_DATA_DIR"]` に設定する。`.env` に値がなければ、process 起動時点に保存しておいた inherited `GUILDBOTICS_DATA_DIR` を使う。どちらもなければ `<workspace>/.guildbotics/data` を設定する。

App API のように workspace を切り替えられる process は、初期化時に inherited `GUILDBOTICS_DATA_DIR` を保存する。`set_workspace()` で `os.environ["GUILDBOTICS_DATA_DIR"]` を更新した後、その値を次の workspace の inherited fallback として扱ってはならない。

この処理は `apply_workspace_for_cli()`、App API `set_workspace()`、`guildbotics run` / `start` の runtime 初期化で共通化する。

既存の `_load_env_from_cwd()` は、`guildbotics run --cwd <dir>` の runtime workspace root と `.env` 読み込み元を一致させる形に変更する。`run` では `--cwd` または cwd から runtime workspace root を先に決め、その root の `.env` を読む。`GUILDBOTICS_ENV_FILE` が明示されている場合は、その絶対 path を優先する。`start` は cwd が runtime workspace root なので cwd の `.env` を読む。

workspace 適用境界以外では、`os.environ["GUILDBOTICS_DATA_DIR"]` を書き換えない。workflow / command / scheduler worker 内で一時的に値を変える必要がある場合は、subprocess env overlay または明示引数を使う。

### 6. Workflow subprocess へ workspace data root を伝播する

ticket / chat workflow が CLI agent を起動するとき、`cli_agent_env` に現在の workspace data root を渡す。

```python
cli_agent_env={
    GUILDBOTICS_DATA_DIR: str(get_workspace_data_root()),
    GUILDBOTICS_TASK_RUN_ID: workflow_run_id,
}
```

これにより、CLI agent の cwd が member workspace になっても、`guildbotics member ...` は同じ task-runs 保存先を使う。

この伝播は必須である。伝播しない場合、子プロセスは member workspace を cwd として `<member-workspace>/.guildbotics/data` を計算し、親 workflow が期待する task run evidence を見つけられなくなる。

## テスト方針

### Unit test

Root 解決は unit test で網羅する。

検証するケース:

- `get_machine_state_root()` は `GUILDBOTICS_DATA_DIR` に影響されない。
- `resolve_workspace_data_root()` は workspace `.env` の `GUILDBOTICS_DATA_DIR` を process env より優先する。
- `resolve_workspace_data_root()` は workspace `.env` に `GUILDBOTICS_DATA_DIR` がなければ process env の `GUILDBOTICS_DATA_DIR` を使う。
- `resolve_workspace_data_root()` はどちらもなければ `<workspace>/.guildbotics/data` を返す。
- workspace A 適用後に `os.environ["GUILDBOTICS_DATA_DIR"]` が workspace A の値になっていても、workspace B 適用時は process 起動時点の inherited value を fallback として使い、workspace A の値を引き継がない。
- `get_workspace_data_root()` は `GUILDBOTICS_DATA_DIR` がなければ `<workspace>/.guildbotics/data` を返す。
- `get_workspace_data_root()` は `GUILDBOTICS_DATA_DIR` があればその値を返す。
- `GUILDBOTICS_DATA_DIR` の `~` と relative path が既存仕様通り展開・resolve される。
- active workspace file は常に machine state root 配下になる。
- scheduler pid file は常に machine state root 配下になる。
- `apply_workspace_for_cli()` は active workspace または `--workspace` 適用時に `GUILDBOTICS_DATA_DIR` を effective workspace data root に設定する。

### App API integration test

検証するケース:

- `set_workspace()` 後、`machine_state_dir` は `$HOME/.guildbotics/data` を指す。
- `set_workspace()` 後、`workspace_data_dir` は `<workspace>/.guildbotics/data` を指す。
- `<workspace>/.env` に `GUILDBOTICS_DATA_DIR=/tmp/custom-data` がある場合、`workspace_data_dir` は `/tmp/custom-data` を指す。
- 同じ条件でも active workspace state は `$HOME/.guildbotics/data/active-workspace.json` に保存される。
- workspace 切替後、diagnostics store は新しい workspace data root の `run/diagnostics.jsonl` を使う。
- workspace A で記録した diagnostics が、workspace B の diagnostics 一覧に混ざらない。
- `guildbotics run --cwd <workspace>` または同等の runtime 初期化で、workspace data root が `<workspace>/.guildbotics/data` に設定される。

### Desktop component / integration test

検証するケース:

- config 保存先が「作業ディレクトリ内」の場合、config は `<workspace>/.guildbotics/config`、`.env` は `<workspace>/.env`、workspace data は `<workspace>/.guildbotics/data` と表示される。
- config 保存先が「ホーム共通」の場合、config は `$HOME/.guildbotics/config`、`.env` は `<workspace>/.env`、workspace data は `<workspace>/.guildbotics/data` と表示される。
- UI 上で `GUILDBOTICS_CONFIG_DIR` を workspace data root の根拠として表示しない。

### Workflow test

検証するケース:

- ticket workflow は member workspace を `<workspace-data-root>/workspaces/<person_id>` に作る。
- chat workflow も同じ member workspace root を使う。
- `guildbotics member ...` が記録する evidence は `<workspace-data-root>/task-runs/<run_id>.jsonl` に入る。
- CLI agent subprocess の cwd が `<workspace-data-root>/workspaces/<person_id>` の場合でも、`GUILDBOTICS_DATA_DIR` 伝播により evidence は親 workflow と同じ `<workspace-data-root>/task-runs` に入る。

## ドキュメント更新方針

README / README.ja.md では、以下を明記する。

- active workspace と scheduler PID は `$HOME/.guildbotics/data` に保存される。
- workspace 実行データは既定で `<workspace>/.guildbotics/data` に保存される。
- `GUILDBOTICS_DATA_DIR` は workspace data root の override であり、active workspace や scheduler PID の保存先は変えない。
- process 起動時点の `GUILDBOTICS_DATA_DIR` は、workspace `.env` に override がない場合、複数 workspace の data を同じ directory に集約する。
- `GUILDBOTICS_CONFIG_DIR` は config source override であり、workspace root ではない。

Desktop UI では、少なくとも以下の 3 つを別々に表示する。

- config dir
- env file
- workspace data dir

必要に応じて、machine state dir は diagnostics / advanced view に表示する。
