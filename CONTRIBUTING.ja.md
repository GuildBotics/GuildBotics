# Contributing Guidelines

## Project Structure & Module Organization
- `guildbotics/`: コアパッケージ。キーとなるモジュールには `drivers/` (スケジューラー)、`workflows/` (タスクオーケストレーション)、`modes/` (タスクタイプごとの動作)、`intelligences/` (ブレイン)、`integrations/`、`loader/`、`runtime/`、`entities/`、`utils/`、`templates/` が含まれます。
- `tests/`: Pytestスイート。ユニットテストは `tests/guildbotics/` の下でパッケージパスをミラーリング；統合テストは `tests/it/` にあり、サンプル設定は `tests/it/config/` にあります。
- `docs/`: アーキテクチャとデザイン (`docs/ARCHITECTURE.*.md`)。
- `main.py`: スケジューラーを実行するためのエントリーポイント。
- `.env`, `.env.example`: ローカル設定。

## Build, Test, and Development Commands
- 依存関係の同期: `uv sync --extra test`
- テスト実行とカバレッジレポート作成: `uv run --no-sync python -m pytest tests/ --cov=guildbotics --cov-report=xml`  (生成物: `coverage.xml`)。

注: このリポジトリは固定された依存ファイル `pyproject.toml` を提供します。uv を使用して必要なライブラリを `uv sync` でインストールしてください。

## Coding Style & Naming Conventions
- Python 3.11+；4スペースインデント；完全な型ヒントを優先。
- Blackでフォーマット (88列)。例: `python -m black .`
- インポート: stdlib、サードパーティ、ローカル (グループ化およびソート)。
- 命名: モジュール/関数/変数 `snake_case`、クラス `PascalCase`、定数 `UPPER_SNAKE_CASE`。
- ログを構造化して保持: `%(asctime)s [%(levelname)s] [%(threadName)s] %(message)s`。
- ソースコード内のコメントを英語で記述。Googleスタイルのdocstringを使用。

## Core Engineering Principles
- スコープの規律: すべての変更を厳密に焦点化；明示的な合意なしにスコープを広げない。
- シンプルさ優先: KISSを適用；投機的な抽象化を避ける (YAGNI)。
- 実用的SOLID: 特にSingle Responsibility—肥大化した関数/モジュールを避ける。
- DRY: コピー&ペーストの重複なし；共有ロジックを `utils/` または適切な共有モジュールにファクタリング。
- 一方向依存: 循環インポート/アーキテクチャサイクルを防ぐ；低レベルモジュール (`entities/`、`utils/`) は高レベルオーケストレーションレイヤー (`workflows/`、`drivers/`) に依存しない。
- 既存アーキテクチャを尊重: 境界を変更する前に `docs/ARCHITECTURE.*.md` をレビュー。
- パフォーマンスマインドセット: 時期尚早の最適化を避けるが、発見された明らかな非効率性 (N+1呼び出し、無駄なI/O、過度の複雑さ) を修正。

## Testing Guidelines
- フレームワーク: pytest。テストを `tests/` の下に `test_*.py` という名前で配置。
- ユニットテストのパッケージ構造をミラーリング；ワークフロー/統合シナリオには `tests/it/` を使用。
- `monkeypatch` を時間、ランダム性、I/Oに使用；テストを決定論的に保つ。
- カバレッジを維持または改善；ローカルで `coverage.xml` の更新を確認。
- 結果を正直に報告；失敗が発生したときに成功を述べない。
- 環境制限を早期に開示 (不足している資格情報、無効化されたサービス) し、重要なロジックを黙ってスキップしない。
- テスト容易性を考慮した設計: 小さな純粋関数、明確な副作用境界、役立つ場合の明示的な依存注入。

## Commit & Pull Request Guidelines
- Conventional Commitsを使用: `feat:`、`fix:`、`chore:`、`refactor:` など。短い、命令形の件名；詳細は本文に。英語または日本語で可。
- PR: 明確な説明、リンクされたイシュー (`#123`)、関連するスクリーンショット/ログ、再現とテストステップ、環境/設定変更の注記。
- レビューをリクエストする前に `pytest` が合格することを確認。

## Code Review Etiquette
- すべてのフィードバックに対処 (実装または明確化)；コメントを無視しない。
- PRスコープを厳密に保つ；スコープ外のリファクタにはフォローアップイシュー/PRを開く。
- 代替ソリューションを選択する際に簡潔な根拠を提供。
- diffをまとまりがあり、合理的に小さく保つ；大規模リファクタを分割。
- 変更に不可欠でない限り、無関係なスタイルやリネームの変更を避ける。

## Documentation & Markdown Guidelines
- トーン: 簡潔で、形式的な技術ビジネスライティング (別のスタイルが明示的にリクエストされない限り)。
- 見出し: 明確な階層 (H1–H3を優先) でコンテンツを構造化。レベルをスキップしない。
- 対象者適応: 対象ロール (例: PM、アーキテクト、UX) に合わせて語彙、強調、深さを調整 (指定された場合)。
- メタデータ: 初期セクションでドキュメントタイプ、目的、対象者、主要要件、未解決の質問を表面化。
- リストとテーブル: 構造化データには箇点リストまたはテーブルを優先し、散文段落を避ける。
- ダイアグラム: フローや関係を明確にする場合、Mermaidフェンスコードブロックを使用；コミット前にMermaidツールで検証。
- Mermaid規約: 特殊文字を含むラベルを引用、参照前にノードを宣言、サブグラフ/方向を閉じる、ダイアグラムを最小限で読みやすく保つ。
- 未解決項目: 未解決または不明な点を "🔶 Pending" トークンでマークして容易にトリアージ。
- 言語: リポジトリ標準が指示しない限り、ユーザープロンプトの言語 (日本語または英語) をデフォルト。
- スライド: プレゼンテーションスタイルのアーティファクトには、プレーンMarkdownではなくMarp Markdown (https://marp.app/) を使用。
- 出力規律: 余分なコメントやツールノイズなしの単一の自己完結型ドキュメントを生成。
- 再利用: 既存ドキュメントを重複させない—正典ソース (例: アーキテクチャドキュメント) にリンク。

## Security & Configuration Tips
- 秘密情報をコミットしない。
- ドライバー、ワークフロー、モードの相互作用については `docs/ARCHITECTURE.en.md` をレビュー；一部のランタイムデータは `~/.guildbotics/data/` の下に保存される場合がある。
- 該当する場合、外部入力を検証；明確なエラーで迅速に失敗。
- 最小権限資格情報を使用；疑いまたは露出時に秘密情報をローテーション。
