# セットアップ画面 UX 提案モックアップ

`docs/desktop_app_plan.ja.md` の **Session 6（Desktop UI を初期セットアップ中心に作る）** 着手前に、レイアウト/UX を合意するための提案モックアップです。実装はまだ行わず、進行中の `desktop/` コードには一切触れていません。

## 開き方

```bash
open docs/mockups/setup/setup-mockup.html
```

ブラウザで直接開けます（ビルド不要・自己完結HTML）。画面上部のトグルで 3 状態を切替。状態②では左のセクション一覧をクリックして各セクションのフォームを確認できます。

## 提案の核：「単一の設定画面 + 初回ガイド層」

初期設定 Wizard と設定画面を別々に作ると「Wizard で設定したあの項目はどこで直すの？」という混乱が起きます。本案では **セットアップ画面＝設定画面** とし、別 Wizard を作りません。

- 設定ごとの置き場は常に 1 つ（左の**セクション一覧**）。
- **初回**は、その同じ画面の上に「進捗バナー（◯項目中◯項目）＋未設定セクションの `!` バッジ」というガイド層が乗るだけ。`次へ →` で次の未設定セクションへ進める（Wizard 的な導線）。
- **設定後**は、まったく同じ一覧から任意のセクションを開いて編集（設定画面的な導線）。Setup 画面は入力と保存の成否を主役にし、軽量診断は Overview の補助機能に分離する。
- どちらのモードでも**同じフォーム**を使うため、初回と変更時で操作を覚え直す必要がない。

`POST /verify` は read-only の軽量診断として扱う。GUI 初期設定ではフォーム validation、CLI agent 検出、保存処理の成否、保存後の再読み込みで大半を担保するため、Setup の完了導線には含めない。必要な場合だけ Overview から補助的に実行する。

初期設定作成後は、保存成功メッセージを表示し、同じ画面を設定済み状態へ切り替える。選択した作業ディレクトリは desktop app 側に保存し、backend sidecar の cwd として再起動時に復元する。これにより、作業ディレクトリ内に `.guildbotics/config` と `.env` を作成した後に app を再起動しても、未設定画面へ戻らない。

初期設定中に追加した member は、`project.yml` 作成前でも draft として一覧に表示し、編集・削除できるようにする。`project.yml` 作成後は通常の team 読み込み結果へ合流する。

## 初期設定のセクション順

README（リポジトリ直下）が示す通り、**GitHub 連携は数ある利用例の 1 つ**であり必須ではありません（"GuildBotics can be used for any scheduled automation tasks without GitHub integration."）。ただし初期設定では、メンバー設定へ進む前に「このプロジェクトで GitHub を使う / 使わない」を明示的に決める方が自然です。

- セクション一覧はグループ分けせず、**プロジェクト → LLM・CLIエージェント → GitHub → メンバー** の順に並べる。
- GitHub セクションでは「使う / 使わない」を必須選択にする。
- 「使わない」を選んだ場合、GitHub URL 入力は不要で初期設定を完了できる。
- `ticket_driven_workflow` など GitHub が必要な routine は、GitHub 未設定では起動できないように guard する。

### LLM と CLI エージェントは 1 画面に統合 ＋ チーム既定／メンバー上書き

両者はコード上も `intelligences/`（`model_mapping.yml` / `cli_agent_mapping.yml`）に同居する「エージェントの頭脳」設定で、どちらも「デフォルトを 1 つ選ぶ」操作です。別画面にせず **「LLM・CLIエージェント」1 セクション**にまとめ、内部を「LLM プロバイダ」「CLI エージェント」の 2 サブセクションに分けています。

このセクションは**チーム全体の既定（デフォルト）**であることを画面上に明記しています（タイトル横の「チーム既定」チップ、サブ見出し「デフォルトの…」、注記）。**メンバーごとの上書き**は、コードの per-agent override（`team/members/<person_id>/intelligences/`）に対応し、メンバー編集フォームの **「LLM・CLIエージェント」タブ**で `既定に従う` / `個別に指定` を選べます。

#### 設定画面では「単純な選択」では足りない — intelligences の 4 層を反映

`guildbotics/templates/intelligences/` を確認した結果、この領域は「プロバイダ 1 つ＋CLI 1 つ」より遥かに richer でした。初期設定（ガイド）では単純な選択で十分ですが、**設定画面では以下の 4 層**を progressive disclosure（折りたたみ）で開けるようにしています。

1. **デフォルト**: `model_mapping.yml` の `default`（**具体的なモデル**＝gpt-5-mini 等。プロバイダではなくモデル単位）＋ `cli_agent_mapping.yml` の `default`。
2. **機能ごとのブレイン割り当て**（`brain_mapping.yml`）: `default` / `file_editor` / `task_planner` / `agno` / `cli` などの**機能**を、それぞれ「LLM モデル」か「CLI エージェント」に割り当て。コマンドは frontmatter の `brain:` で機能名を指定する（例: ファイル編集・計画は CLI、一般思考は LLM）。**ここが従来モックで欠けていた最重要要素**。
3. **モデル定義**（`models/<provider>/<model>.yml`）: `model_class` / モデル `id`。named slot（default/gemini/openai/anthropic）から参照。出荷済みに gpt-5-mini/nano・claude-haiku/sonnet/opus・gemini 各種・ローカル lmstudio など。
4. **CLI エージェント定義**（`cli_agents/*.yml`）: env と呼び出し **shell スクリプト**（編集可・上級者向け）。

メンバーの「LLM・CLIエージェント」タブも同構造で、`既定に従う` / `個別に指定` に加え、`brain_mapping` の**機能単位の上書き**（例: このメンバーだけ file_editor を Codex に）を折りたたみで提供します。

### 「シークレット」セクション名は保存先に依存させない

将来 OS キーチェーンなど、より安全な保存先に対応する想定のため、セクション名から `(.env)` を外して **「シークレット」** としています。「.env」はあくまで現時点の保存バックエンドであり、セクション内の info-box で「現在の保存先は .env／将来より安全な保存先を選べる予定」と明示。書き込み方法（追記/上書き/書き込まない）と `.env` パスはセクション内に残します。

### 作業ディレクトリ（起動ディレクトリ）を明示する

CLI の `config init` は設定の展開先を「ホーム」か「カレントディレクトリ」で選びますが、**GUI 起動ではカレントディレクトリが不可視で不親切**です。そこでプロジェクト画面に **「作業ディレクトリ（コマンドの実行基点・起動ディレクトリ）」をフォルダ選択（参照…）で明示**し、設定保存場所はその上で **「ホーム共通」か「作業ディレクトリ内」** から選ぶ形にしました。

- 作業ディレクトリ＝GUI が backend / コマンドを実行する cwd。`.env` 読込やプロジェクト固有コマンド（`.guildbotics/config/commands`）の基点。
- 「作業ディレクトリ内」を選ぶと設定は `<作業ディレクトリ>/.guildbotics/config`、`.env` は `<作業ディレクトリ>/.env` に置かれる（CLI の cwd 相当を明示化したもの）。

### Slack はメンバー画面の中へ

Slack は仕様上**メンバー単位**（`{PERSON_ID}_SLACK_BOT_TOKEN` / `_APP_TOKEN` と `message_channels`＝`person.yml`）です。独立セクションにせず、**メンバー編集フォーム内の「チャット連携（Slack）」サブセクション**に置きました。これにより、**GitHub も Slack も「プロジェクト単位＝連携セクション / メンバー単位＝メンバーフォーム」という同じ分割**になり一貫します（GitHub のメンバー認証＝App/トークンも従来どおりメンバーフォーム内）。シークレット セクションは引き続き全キーの棚卸し一覧として機能します。

## 保存（Save）ボタンの扱い：原則オートセーブ

現代的な設定 UI に合わせ、**常設の設定は変更即時保存（オートセーブ）** とし、Save ボタンは置きません。

- **常設セクション**（言語・.env 方針・プロバイダ/エージェント選択・GitHub URL・各トグル・役割など）→ 変更時に自動保存し、`✓ 保存済み` を控えめに表示。
- **初回ガイド**の `次へ →` は**保存ではなく画面遷移**（保存は既に完了している）。
- **メンバーの追加/編集だけ**は例外的に明示的な `追加する` / `変更を保存` ＋ `キャンセル` を持つ。ドロワー内で 1 つの対象を新規作成/編集する操作単位は、現代的 UI でも確定アクションを置くのが自然なため（GitHub の "Add" ダイアログ等と同様）。

## 3 つの画面状態

| 状態 | 内容 | 意図 |
| --- | --- | --- |
| ① 初回ガイド | 進捗バナー（強）＋ プロジェクト / LLM・CLIエージェント / GitHub / メンバーの順次入力 | 初回ユーザーが迷わず次へ進める。GitHub は使う/使わないを明示 |
| ② 設定済み（各セクション） | 細バナー＋全コア ✓。**左一覧クリックで 5 セクションのフォームを確認可** | ①と同じ一覧・同じフォームで後から変更。オートセーブ表示 |
| ③ メンバー追加/編集 | メンバーカード一覧＋共有フォーム（**タブ構成**） | `config add` の繰り返し操作。追加と編集が同一フォーム |

状態②で確認できるセクション: プロジェクト / LLM・CLIエージェント / GitHub / メンバー / シークレット。Slack はメンバーフォーム（状態③）内で設定。

### メンバーフォームはタブで整理

設定項目が多いため、メンバー編集フォームを 4 タブに分けました。**保存ボタンと「.env追記」トグルはタブ外（常時表示）** に固定します。

- **基本**: person_id / フルネーム / 役割 / 会話スタイル / character / 稼働(is_active)
- **LLM・CLIエージェント**: `既定に従う` / `個別に指定`（per-agent override）＋ 機能単位（brain_mapping）の上書き
- **GitHub**: GitHub 連携なし / 人間 / マシンユーザー / GitHub Apps / 代理エージェントの選択、identity 解決、種類に応じた認証（Apps: installation/app id/private key、マシン/代理: access token）
- **チャット (Slack)**: 有効化トグル ＋ Bot/App トークン ＋ 監視チャンネル

タブ順は「基本 → LLM・CLIエージェント → GitHub → チャット (Slack)」。

（accordion でも実現可能ですが、項目数とグループ独立性からタブを採用。実装時に好みで差し替え可。）

## CLI フィールドとの対応

GUI のセクションは現行 CLI のセットアップに対応します。文言は `guildbotics/cli/simple/locales/cli.ja.yml` に準拠。

### コア設定（`guildbotics config init` 相当）

| セクション | フィールド | CLI 対応 |
| --- | --- | --- |
| プロジェクト | 言語 / **作業ディレクトリ** / 設定保存場所 / 説明 | en / ja。**作業ディレクトリ**＝CLI の cwd を GUI でフォルダ選択として明示化。保存場所＝ホーム共通 or 作業ディレクトリ内（`GUILDBOTICS_CONFIG_DIR` 上書き可）。project.yml `description` |
| LLM・CLIエージェント（**チーム既定**） | デフォルト（モデル/CLI/APIキー）＋ 機能割り当て ＋ モデル定義 ＋ CLI スクリプト | `model_mapping.yml` / `cli_agent_mapping.yml` の `default`、`brain_mapping.yml`、`models/*`（id）、`cli_agents/*`。**同梱せず PATH 検出のみ**。メンバーで上書き可 |
| シークレット | 保存先・書き込み方法 + プロジェクト共通キー一覧 | append / overwrite / skip。**LLM プロバイダキーのみ**をマスク表示。メンバー個別の認証は member フォームへ集約（重複排除）。保存先名はラベルから外す |

### GitHub（任意・プロジェクト単位）

| セクション | フィールド | CLI / 仕様対応 |
| --- | --- | --- |
| GitHub | 使う/使わない / プロジェクトURL / リポジトリURL / アクセス方法 / ステータス対応 | GitHub を使わない場合 URL は不要。使う場合は project URL / repo URL / HTTPS or SSH。ステータス対応は `config verify` 相当（任意） |

### メンバー（`guildbotics config add` 相当）

| フィールド | CLI 対応 / 備考 |
| --- | --- |
| GitHub 連携 | GitHub 連携なし / 人間 / マシンユーザー / GitHub Apps / 代理エージェント。GitHub タブで選択し、種類で下の入力欄を出し分け |
| GitHubユーザー名 または GitHub Apps URL | GitHub 連携時のみ。種類に応じて切替（GitHub Apps は設定ページURL） |
| メンバーID (person_id) | `^[a-z0-9_-]+$`。GitHub アカウントとは独立した GuildBotics 上の識別子 |
| フルネーム | 必須 |
| 役割（複数選択） | product_owner ほか 9 種 |
| 会話スタイル | プロフェッショナル / フレンドリー / マシン（AIエージェント種別のみ） |
| 稼働（is_active） | このメンバーを AI エージェントとして稼働させるか |
| GitHub 認証 | GitHub Apps: installation ID / App ID / private key path、マシン/代理: access token |
| LLM・CLIエージェント※GUI拡張 | `既定に従う` or `個別に指定`（モデル/CLI）＋ 機能単位の上書き。`team/members/<person_id>/intelligences/` の per-agent override に対応 |
| チャット連携（Slack）※任意 | メンバー単位。Bot/App トークン（`{PERSON_ID}_SLACK_*`）＋ 監視チャンネル（`message_channels`）。トグルで有効化 |
| シークレットを .env に追記 | `add_secret`。値はマスク表示し生値は出さない |

## 実装方針（決定事項）

実装前合意。基準は「**最終的な自前コード量が少ない**」「**コーディングエージェントが扱いやすい（情報量が多くバグりにくい）**」。

- **UI ライブラリ: Mantine（v7 系でメジャー固定）**
  - 採用理由: バッテリー同梱で本モックの部品（`SegmentedControl` / `Tabs` / `Accordion` / `Drawer` / `Switch` / `MultiSelect` / `NumberInput` / `PasswordInput` / `Tooltip`）が素で揃い、グルーコードが最少。単一の一貫 API で連携ライブラリが少なく、エージェントの統合バグ面が小さい。
  - 不採用: shadcn/ui（Tailwind＋Radix＋RHF＋zod の多ライブラリ合成で配線バグが出やすく、取り込みコードで自前行数も増える）、MUI（Material 脱却の再スタイルで①に逆行）。
  - フォーム: `@mantine/form` ＋ `mantine-form-zod-resolver` で zod 検証。zod スキーマは Pydantic（`ProjectSetupInput` / `PersonSetupInput`）と対称に保つ。
  - テーマ: 現行 `styles.css` のパレット（sidebar `#172026`、accent `#1f2937`、green `#0d6b47` / amber `#7a4f00`）を Mantine テーマトークンへ移植し、現行の見た目を維持。
  - 既存資産: TanStack Query / lucide-react は継続利用。
- **ルーティング: react-router 導入**
  - 画面: `Overview` / `Commands` / `Setup`。Setup 内のセクション切替もネストルートで表現可能（不足項目へのディープリンクに有効）。
- **コンポーネント構成**（再利用単位）
  - 汎用プリミティブ（多くは Mantine ＋薄いラッパ）: `StatusBadge` / `SecretRow` / `OptionCardGroup`＋`DetectionStatus` / `InfoCallout` / `AutosaveIndicator` / `FolderPicker`（Tauri dialog）。`SegmentedControl`・`Switch`・`MultiSelect` 等は Mantine 標準を直接利用。
  - ドメイン: `SetupSectionNav` / `SetupStatusBanner` / `MemberCard` / `MemberFormDrawer`（タブ: `MemberBasicTab` / `MemberIntelligenceTab` / `MemberGitHubAuthTab` / `MemberSlackTab`）/ `BrainMappingTable`・`ModelDefsTable`・`CliAgentsTable` / 各セクション（`ProjectSection` / `IntelligenceSection` / `SecretsSection` / `MembersSection` / `GitHubIntegrationSection`）。
  - データ/フック: `client.ts` 書き込み系拡張、`useConfigStatus` / `useProjectConfig` / `useMembers` / `useUpsertMember` / `useIntelligence`、`useAutosave`、`useSetupStatus`（フォーム状態＋`/config/status`＋保存後再読み込みで判定）。

## 実装時の留意点

- **書き込み系 API が未実装**: `guildbotics/app_api/api.py` は現在 `GET /config/status` / `GET /team` / `POST /verify` 等の読み取り・診断系のみ。Session 6 では `config init` / `config add` 相当の **書き込みエンドポイント（例: `POST /config/init`、`POST /config/members`）を新規追加**し、既存の `SimpleProjectSetupService` / `SimplePersonSetupService`（`guildbotics/cli/simple/setup_service.py`）を呼ぶ薄い API 層にする。CLI と GUI が同じ service を共有する。
- **GitHub を任意にするための確認事項**: 現行 CLI `config init` は GitHub project/repo URL を必須プロンプトとして収集する。GUI でコア設定から外すには、`project.yml` の `services.ticket_manager` / `code_hosting_service` を未設定でも生成・起動できるか（`SimpleProjectSetupService` 側）を Session 6 で確認・対応する必要がある。
- **作業ディレクトリの配線**: GUI で選んだ作業ディレクトリを、backend sidecar / コマンド実行の **cwd** として渡す必要がある（現状は app バンドルの cwd に依存しがち）。`.env` 読込・`GUILDBOTICS_CONFIG_DIR`・プロジェクト固有コマンドの解決がこの cwd を基準に動くため、Session 6 で「作業ディレクトリ → backend 起動/コマンド実行の cwd」を明示的に結線する。
- **シークレット保存先の抽象化（将来）**: v1 は `.env` 固定（desktop 方針どおり）。ただし UI 文言・データモデルは保存先非依存にしておき、将来 OS キーチェーン等を追加しても画面構造を変えずに済むようにする。
- **intelligences の編集範囲**: 設定画面は `brain_mapping.yml` / `model_mapping.yml` / `models/*` / `cli_agent_mapping.yml` / `cli_agents/*` を読み書きする。初期設定では `default` スロットのみ触れば足り（既存 `SimpleProjectSetupService` 互換）、機能割り当て・モデル定義・CLI スクリプトは折りたたみの詳細設定として段階的に開く。書き込み API は intelligences 配下のファイル単位編集を許容する設計にする。CLI スクリプトは任意コマンドを含むため、編集 UI は上級者向けである旨を明示し、検証（verify）の CLI 検出と連動させる。
- **オートセーブの実装**: 常設フィールドは onChange/onBlur で書き込みエンドポイントを呼ぶ。バリデーション（GitHub URL parse、person_id 形式）が通った値のみ保存し、失敗時はインライン表示。書き込み API は部分更新（セクション単位 PATCH）を許容する設計が望ましい。
- **正となる入力モデル**: `ProjectSetupInput` / `PersonSetupInput`（`setup_service.py`）。バリデーション・条件分岐の正は `guildbotics/cli/simple/simple_setup_tool.py`。
- **シークレットの扱い**: `.env` の生値は API レスポンスでも UI でも返さない（masked summary のみ）。プライベートキーパス・トークンは「設定済み ✓ / 置き換える」の UI に留める。
- **CLIエージェントは同梱しない**: desktop 方針（`desktop_app_plan.ja.md`）通り、GUI は検出・状態表示・設定支援のみを提供する。Setup では検出済みの CLI agent だけを選択可能にし、`/verify` は Overview の補助診断として扱う。
- **見た目**: 配色・コンポーネントは `desktop/src/styles.css` のパレットに合わせている（sidebar `#172026`、accent `#1f2937`、status green `#0d6b47` / amber `#7a4f00`）。実装時は同 CSS のクラスへ寄せる。
- **状態切替トグル**はモック専用の説明用 UI であり、実装には含めない。
