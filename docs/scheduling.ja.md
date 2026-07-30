# スケジュール実行ガイド

チームメンバーごとの `person.yml` で設定するスケジュール実行の詳細ガイドです。
概要と最小限の設定例は [README の「決まった作業を自動で実行する」](../README.ja.md#決まった作業を自動で実行する)を参照してください。

- [ルーチンコマンド](#ルーチンコマンド)
- [スケジュールタスク](#スケジュールタスク)
- [Cron表記の詳細](#cron表記の詳細)
- [スケジューラの内部動作](#スケジューラの内部動作)
- [設定例](#設定例)

スケジューラは 2 種類のコマンド実行方式をサポートしています。

- **ルーチンコマンド** (`routine_commands`): スケジューラの稼働中、継続的に繰り返し実行するコマンド
- **スケジュールタスク** (`task_schedules`): cron 表記で指定した時刻に実行するコマンド

## ルーチンコマンド

**ルーチンコマンド** (`routine_commands`) は、ラウンドロビン方式で継続的に実行されるコマンドです。

**特徴**:

- スケジューラがアクティブな間、毎分実行されます
- 複数のコマンドを指定した場合、順番に 1 つずつ実行されます
- 初期セットアップ時、新規のエージェントメンバーには `workflows/ticket_driven_workflow` が設定されます。`routine_commands` が未設定のメンバーはルーチンコマンドを実行しません

**設定例**:

```yaml
person_id: alice
name: Alice
is_active: true

# デフォルトのルーチンコマンドを上書き（オプション）
routine_commands:
  - workflows/ticket_driven_workflow
  - workflows/custom_workflow
```

**典型的な用途**:

- タスクボードの定期チェック（例: `workflows/ticket_driven_workflow`）
- 継続的な監視タスク
- 定期巡回型の処理

## スケジュールタスク

**スケジュールタスク** (`task_schedules`) は、cron 表記で定義された特定の時刻に実行されるコマンドです。

**特徴**:

- 毎分チェックされ、現在時刻がスケジュールに一致した時に実行されます
- 1 つのコマンドに複数のスケジュールパターンを設定できます
- ランダム化構文（ジッタ）をサポートします

**設定例**:

```yaml
person_id: alice
name: Alice
is_active: true

# 特定の時刻に実行するコマンドをスケジュール
task_schedules:
  - command: workflows/cleanup
    schedules:
      - "0 2 * * *" # 毎日午前2:00
      - "30 14 * * 5" # 毎週金曜日14:30
  - command: workflows/backup
    schedules:
      - "0 0 1 * *" # 毎月1日の午前0時
```

**典型的な用途**:

- 定期的なクリーンアップ処理
- バックアップやレポート生成
- 定時実行が必要なタスク

## Cron表記の詳細

GuildBotics は標準的な 5 フィールドの cron 表記を使用します:

```
* * * * *
│ │ │ │ │
│ │ │ │ └─── 曜日 (0-6, 日曜日=0)
│ │ │ └───── 月 (1-12)
│ │ └─────── 日 (1-31)
│ └───────── 時 (0-23)
└─────────── 分 (0-59)
```

**よく使う例**:

```yaml
schedules:
  - "0 9 * * *" # 毎日午前9:00
  - "*/15 * * * *" # 15分毎
  - "0 */2 * * *" # 2時間毎
  - "0 0 * * 0" # 毎週日曜日の午前0時
  - "30 8 1,15 * *" # 毎月1日と15日の午前8:30
  - "0 22 * * 1-5" # 平日の午後10:00
```

**ランダム化構文（ジッタ）**:

GuildBotics は標準 cron 記法を拡張し、ランダム化をサポートしています:

- `?`: デフォルト範囲内のランダムな値
- `?(min-max)`: 指定範囲内のランダムな値

**例**:

```yaml
schedules:
  - "? 9 * * *" # 毎日午前9:00-9:59のランダムな分
  - "?(0-30) 14 * * *" # 毎日14:00-14:30のランダムな分
  - "0 ?(9-17) * * 1-5" # 平日の9-17時のランダムな時刻（00分）
```

**ランダム化の用途**:

- 複数エージェント間での同時実行を回避します
- 人間らしい不規則なタイミングをシミュレートします
- 時間枠全体で負荷を分散します

## スケジューラの内部動作

スケジューラの動作（`guildbotics/drivers/task_scheduler.py` および `guildbotics/entities/task.py` より）:

**アーキテクチャ**:

1. **メンバー毎のワーカースレッド**: アクティブな各チームメンバーに専用のワーカースレッドが割り当てられます
2. **分単位のチェックサイクル**: 毎分、各ワーカースレッドは以下を行います:
   - 現在のメンバーの全 `task_schedules` をチェック
   - スケジュールが現在時刻に一致するコマンドを実行
   - ラウンドロビン順で 1 つの `routine_command` を実行

**ランダム化の処理**:

1. 初期化時に、ランダム化されたスケジュールの次回実行時刻を計算します
2. `?` フィールドについては、境界内でランダムな値をサンプリングします
3. 各実行境界に達した後、再サンプリングします

**エラーハンドリング**:

- 連続したコマンド失敗（デフォルト: 3回）でワーカースレッドを停止します
- 検索用の実行サマリーは `<workspace>/.guildbotics/data/run/diagnostics.jsonl`、
  実行ごとの全文記録は `run/sessions/` に保存されます

## 設定例

実際のスケジュール設定の例を紹介します。

### マルチエージェント・スケジュールワークフローの例

**シナリオ**: 異なるスケジュールを持つ 2 つのエージェント

**エージェント1** (`.guildbotics/config/team/members/agent1/person.yml`):

```yaml
person_id: agent1
name: Agent One
is_active: true

# チケット駆動ワークフローを定期実行
routine_commands:
  - workflows/ticket_driven_workflow

# 平日午前9時に朝会レポートを生成
task_schedules:
  - command: workflows/morning_standup
    schedules:
      - "0 9 * * 1-5" # 平日午前9:00
```

**エージェント2** (`.guildbotics/config/team/members/agent2/person.yml`):

```yaml
person_id: agent2
name: Agent Two
is_active: true

# コードレビューチェックを定期実行
routine_commands:
  - workflows/code_review_check

# 週次・月次のメンテナンスタスク
task_schedules:
  - command: workflows/cleanup_old_branches
    schedules:
      - "0 0 * * 0" # 日曜日午前0時
  - command: workflows/dependency_update_check
    schedules:
      - "?(0-59) 10 1 * *" # 毎月1日の午前10時台のランダムな分
```

**両方のエージェントを起動**:

```bash
guildbotics start
```

両エージェントは並行して動作し、それぞれがルーチンコマンドを継続的に実行し、スケジュールタスクを指定された時刻に実行します。

### 複数スケジュールパターンの例

1 つのコマンドに複数のスケジュールを設定する例:

```yaml
person_id: maintenance_bot
name: Maintenance Bot
is_active: true

task_schedules:
  # クリーンアップを平日の午前2時と週末の午前0時に実行
  - command: workflows/cleanup
    schedules:
      - "0 2 * * 1-5" # 平日午前2:00
      - "0 0 * * 0,6" # 週末午前0:00

  # バックアップを毎日の午前3時と月初の午前0時に実行
  - command: workflows/backup
    schedules:
      - "0 3 * * *" # 毎日午前3:00
      - "0 0 1 * *" # 毎月1日の午前0時（月次バックアップ）
```

### ランダム化を活用した例

複数エージェント間での競合を避けるためのランダム化設定:

```yaml
person_id: agent_alpha
name: Agent Alpha
is_active: true

task_schedules:
  # 午前9時台のランダムな時刻にチェックを実行
  - command: workflows/morning_check
    schedules:
      - "?(0-59) 9 * * 1-5" # 平日の9:00-9:59のランダムな分

  # 日中の時間帯にランダムにモニタリングを実行
  - command: workflows/health_check
    schedules:
      - "0 ?(9-17) * * *" # 毎日9-17時のランダムな時刻（00分）
```
