# Contributing Guidelines

本ドキュメントは**人間のコントリビューションプロセス**を扱います。リポジトリ構造・環境構築・CI 相当の build/test/lint コマンド・コーディング標準・テスト戦略については、以下を**正典（single source of truth）**とします。これらは CI と同期して常に最新化されるため、本ファイルでは重複保持しません（重複起因の陳腐化を防ぐため）。

- **`AGENTS.md`** — リポジトリ作業ガイド: モジュール構成、設定解決、CLI、CI 相当コマンド、テスト戦略。
- **`desktop/README.md`** — desktop アプリの build / 開発 / テスト（Vitest unit/component + Playwright E2E）。
- **`.github/workflows/ci.yml`** — CI が実際に実行するチェック。

テストは `tests/guildbotics/` 配下にパッケージ構成をミラーして配置します（`tests/it/` という別ツリーは存在しません）。CLI のエントリーポイントは `guildbotics`（`guildbotics.cli:main`）で、`main.py` はありません。

## PR を出す前に — CI 相当のチェックを実行する

正典の一覧は `AGENTS.md`（「開発時の基本コマンド（CI 準拠）」）にあります。クイックリファレンス:

```bash
# Python（リポジトリルート）
uv sync --extra test --extra dev
uv run --no-sync ruff format --check guildbotics
uv run --no-sync ruff check guildbotics
uv run --no-sync mypy guildbotics
uv run --no-sync pylint guildbotics
uv run --no-sync python -m pytest tests/ --cov=guildbotics --cov-report=xml

# desktop frontend（desktop/ を触る場合）
cd desktop && npm ci && npm run quality
# desktop E2E（実ブラウザ + 実 Local API backend。`npm run quality` / push CI には含めない）:
#   npm run e2e:install   # 初回のみ
#   npm run e2e
```

`uv sync` で `pyproject.toml` に固定された依存をインストールします。

## Coding Style & Naming Conventions
- Python 3.12+（`requires-python = ">=3.12,<3.14"`）；4スペースインデント；完全な型ヒントを優先。
- フォーマット/Lint は Ruff が正典。CI は `ruff format --check` と `ruff check` を実行。ローカル整形は `uv run --no-sync ruff format guildbotics`。
- インポート: stdlib、サードパーティ、ローカル (グループ化およびソート；Ruff が処理)。
- 命名: モジュール/関数/変数 `snake_case`、クラス `PascalCase`、定数 `UPPER_SNAKE_CASE`。
- ログを構造化して保持: `%(asctime)s [%(levelname)s] [%(threadName)s] %(message)s`。
- ソースコード内のコメントを英語で記述。Googleスタイルのdocstringを使用。

## Core Engineering Principles
- スコープの規律: すべての変更を厳密に焦点化；明示的な合意なしにスコープを広げない。
- シンプルさ優先: KISSを適用；投機的な抽象化を避ける (YAGNI)。
- 実用的SOLID: 特にSingle Responsibility—肥大化した関数/モジュールを避ける。
- DRY: コピー&ペーストの重複なし；共有ロジックを `utils/` または適切な共有モジュールにファクタリング。
- 一方向依存: 循環インポート/アーキテクチャサイクルを防ぐ；低レベルモジュール (`entities/`、`utils/`) は高レベルオーケストレーションレイヤー (`templates/`、`commands/`、`drivers/`) に依存しない。
- 既存アーキテクチャを尊重: 境界を変更する前に `docs/ARCHITECTURE.md` をレビュー。
- パフォーマンスマインドセット: 時期尚早の最適化を避けるが、発見された明らかな非効率性 (N+1呼び出し、無駄なI/O、過度の複雑さ) を修正。

## Testing Guidelines
テスト戦略の全体（どの層をカバーするか、テストピラミッド、desktop の lean-but-real な E2E 方針）は `AGENTS.md`「テスト実装の考え方」が正典です。プロセス上の要点:

- フレームワーク: pytest。`tests/guildbotics/` 配下にパッケージ構成をミラーして `test_*.py` で配置。統合テスト（FastAPI `TestClient`・temp workspace）も同じ配下（例: `tests/guildbotics/app_api/`）。`tests/it/` という別ツリーは存在しない。
- desktop frontend: unit/component は Vitest + React Testing Library、加えて `desktop/e2e/` の実ブラウザ Playwright journey を少数（`desktop/README.md` 参照）。
- `monkeypatch` / `tmp_path` を時間・ランダム性・env・cwd・HOME・I/O に使用し、決定論的かつ hermetic に保つ（実 home や外部サービスに触れない）。
- カバレッジを維持または改善；ローカルで `coverage.xml` の更新を確認。
- 結果を正直に報告；失敗が発生したときに成功を述べない。
- 環境制限を早期に開示 (不足している資格情報、無効化されたサービス) し、重要なロジックを黙ってスキップしない。
- テスト容易性を考慮した設計: 小さな純粋関数、明確な副作用境界、役立つ場合の明示的な依存注入。

## Commit & Pull Request Guidelines
- Conventional Commitsを使用: `feat:`、`fix:`、`chore:`、`refactor:` など。短い、命令形の件名；詳細は本文に。英語または日本語で可。
- PR: 明確な説明、リンクされたイシュー (`#123`)、関連するスクリーンショット/ログ、再現とテストステップ、環境/設定変更の注記。
- レビューをリクエストする前に、CI 相当のチェック（「PR を出す前に」参照）が合格することを確認: Python は `ruff format --check`・`ruff check`・`mypy`・`pylint`・`pytest`、frontend を触る場合は `desktop/` で `npm run quality`（cross-boundary な journey を変える場合は `npm run e2e` も）。

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
- 該当する場合、外部入力を検証；明確なエラーで迅速に失敗。
- 最小権限資格情報を使用；疑いまたは露出時に秘密情報をローテーション。
