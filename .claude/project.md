# GuildBotics Project

マルチエージェント・タスクスケジューリング＆コマンド実行フレームワーク。複数のAIエージェントを管理し、任意のコマンドをスケジュール実行します。

## 必読ドキュメント（優先順位順）

1. **[AGENTS.md](../AGENTS.md)** - AIエージェントの運用ポリシー（ワークフロー、出力規約、検証ポリシー）
2. **[CONTRIBUTING.md](../CONTRIBUTING.md)** - コーディング規約、テスト、ドキュメント、セキュリティガイドライン
3. **[README.md](../README.md)** - プロジェクト概要、セットアップ、実行方法
4. **[docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md)** - アーキテクチャ、レイヤー境界、依存関係の原則

## プロジェクト構造

```
guildbotics/
├── drivers/          # スケジューラ
├── intelligences/    # LLMブレイン
├── integrations/     # 外部サービス連携
├── loader/           # 設定ローダー
├── runtime/          # ランタイム
├── entities/         # エンティティ
├── utils/            # ユーティリティ
└── templates/        # テンプレート

tests/
├── guildbotics/      # ユニットテスト（パッケージ構造をミラー）
└── it/               # 統合テスト
    └── config/       # テスト用サンプル設定

docs/                 # アーキテクチャドキュメント
```

## クイックリファレンス

### 開発コマンド
```bash
# 依存関係の同期
uv sync --extra test

# テスト実行（カバレッジ付き）
uv run --no-sync python -m pytest tests/ --cov=guildbotics --cov-report=xml

# コードフォーマット
python -m black .
```

### コーディング規約（要約）
- Python 3.11+、4スペースインデント、型ヒント必須
- Black（88カラム）でフォーマット
- インポート順序: 標準ライブラリ → サードパーティ → ローカル
- 命名: `snake_case`（モジュール/関数/変数）、`PascalCase`（クラス）、`UPPER_SNAKE_CASE`（定数）
- Docstring: Google スタイル、英語で記述

### アーキテクチャ原則
- **スコープ規律**: 変更は厳密に焦点を絞る。明確な合意なしにスコープを広げない
- **シンプル第一**: KISS原則。投機的抽象化を避ける（YAGNI）
- **SOLID原則**: 特に単一責任原則。肥大化した関数/モジュールを避ける
- **DRY**: コピペ重複を排除。共有ロジックは`utils/`または適切な共有モジュールへ
- **一方向依存**: 循環インポート/循環アーキテクチャを防ぐ。下位レベルモジュール（`entities/`、`utils/`）は上位オーケストレーションレイヤー（`commands/`, `templates/`, `drivers/`）に依存してはならない

### テスト方針
- pytest使用、`tests/`配下に`test_*.py`で配置
- ユニットテストはパッケージ構造をミラー、統合テストは`tests/it/`
- 時間、乱数、I/Oは`monkeypatch`で制御、テストは決定的に
- カバレッジを維持または向上させる（`coverage.xml`をローカルで確認）
- 環境制約（認証情報不足、無効化サービス）は早期に明示

### コミット規約
- Conventional Commits: `feat:`, `fix:`, `chore:`, `refactor:`など
- 簡潔な命令形の件名、詳細は本文に記載
- 英語または日本語どちらでも可

### セキュリティ
- シークレットは絶対にコミットしない
- 外部入力は検証、明確なエラーで早期失敗
- 最小権限の認証情報を使用

## プロジェクト固有の重要事項

### CLI エージェント統合
- Google Gemini CLI、OpenAI Codex CLI、Claude Code に対応
- CLI エージェント設定: `intelligences/cli_agents/*.yml`
- CLI エージェントマッピング: `intelligences/cli_agent_mapping.yml`

### 設定ファイル
- プロジェクト設定: `team/project.yml`
- メンバー設定: `team/members/<person_id>/person.yml`
- 環境変数: `.env`（`.env.example`を参照）

### ワークフロー
- デフォルトルーチン: `workflows/ticket_driven_workflow`
- カスタムコマンド: チケット本文またはコメントの最初の行を`//`で開始

## よく使うファイルパス

- エントリーポイント: [main.py](../main.py)
- CLIエントリー: [guildbotics/cli/__init__.py](../guildbotics/cli/__init__.py)
- 設定例: `.env.example`（存在する場合）
- エラーログ: `~/.guildbotics/data/error.log`（実行時）

## 注意事項

- **サンドボックス制約**: ワークスペース内への書き込みに制限あり。ネットワークアクセスが制限される場合あり
- **承認モード**: 重要な変更や制限で失敗するコマンドは承認を得て再実行
- **破壊的操作**: `rm`、履歴変更、外部書き込みは明示的な許可なしで実行しない
- **検証優先**: 変更後は可能な限りテストやビルドを実行し、結果を正直に報告
