# 文書管理システム — 外部仕様・アーキテクチャ（Phase 1）

## 背景

GuildBotics は「Slack や GitHub を通じて協調作業するチームを作ること」を目指して開発が行われている。しかし現在は、チームメンバー（AIエージェント）が作業内容を記録として残す仕組みを持たないため、一回の作業が終わると、そこで得た理解・失敗ノウハウはすべて消失してしまう。そのため人間チームが当然持つ「個々の経験の蓄積」と「チームでの知識共有」が欠けている。

そこで文書管理システムの層を足す。狙いは (1) タスクで得た情報・ノウハウを完了後も参照可能にする、(2) GitHub チケットに足りない文脈を過去のSlackでの会話記録から思い出して補う、(3) チーム内で知識を共有する、と言ったことが挙げられる。

---

## アーキテクチャ

このシステムが **コードで固定して提供する** のは、文書の置き場（§1〜§2）・`memory` コマンド（§3）・コンテキストへの載せ方（§4）。各モードでの使われ方は §6、それをエージェントのプロンプトに組み込む具体は §7。**何を残すか・どう分類するか** といった運用ルールは固定せず、チーム自身が育てる（§8 で詳述）。前者を **mechanism**、後者を **policy** と呼ぶ。以下、その mechanism を順に示す。

### 1. 文書・スコープ・保管場所

- **文書（document）**: 記録の単位。**1文書 = 1ディレクトリ**で、配下に本文 markdown・画像（`assets/`）・メタデータ（`meta.yml`）を置く。**doc-id** はそのディレクトリ名。
- **スコープ（scope）**: 文書は **personal**（メンバー個人用）と **team**（チーム共有用）の2つのスコープに分かれる。この2つだけをシステムが固定で用意し、粒度・分類はチームに委ねる。
- 保管場所は #192 で整理された workspace data root 配下:

```
<workspace-data-root>/documents/
  personal/<person_id>/             # personal スコープ（chat_state/<person_id> と同流儀）
      <doc-id>/                     # 1文書 = 1ディレクトリ
          meta.yml
          body.md
          assets/...                # 画像など
      archived/<doc-id>/            # 退避済み（recall/digest の対象外）
      recent.txt                    # MRU（recency インデックス、§4）
  team/                             # team スコープ
      <doc-id>/ ...
      archived/<doc-id>/ ...
      # recent.txt は置かない。recency は member ごとの personal recent.txt で管理する
```

### 2. メタデータ（meta.yml）

各文書のメタデータは `meta.yml` に持つ。主なフィールド:

- `title` / `summary` / `keywords`
- `source` … その文書の出自（**型つきエントリの配列＝多値**、0個も可）: `ticket` / `pr` / `channel` / `thread` を任意個。各エントリは url / number / title 等。url を `memory recall --query <url> --meta-only` で完全一致 grep すれば、その出自に紐づく文書をピンポイント想起できる（§4）。
- `created_at`
- `updated_at` … 本文が**変更**された時刻（陳腐化・自己修復のシグナル）
- `pinned` / `kind` … コンテキストへの載せ方とポリシー識別。意味は §4・§8 で定義。

例（issue #142 の作業中に Slack で相談し、PR #150 にまとめた過程で得たノウハウ）:

```yaml
# documents/personal/alice/auth-retry-pitfall/meta.yml
title: 認証リトライの落とし穴
summary: トークン更新が競合すると 401 ループになる。リトライ前に必ず再取得する。
keywords: [auth, retry, リトライ, 再試行, token, "401"] # 同義語・英日を入れ字句一致率を上げる

source: # 多値（型つきエントリの配列）。0個でも可
  - type: ticket
    url: https://github.com/acme/app/issues/142
    number: 142
    title: ログイン後に断続的な 401 が出る # title は記録時点のスナップショット（url/number は安定）
  - type: pr
    url: https://github.com/acme/app/pull/150
    number: 150
    title: 認証リトライ時のトークン再取得を修正
  - type: thread
    channel: dev
    url: https://acme.slack.com/archives/C0123/p1718800000123456

created_at: 2026-06-18T10:30:00Z
updated_at: 2026-06-19T09:00:00Z
pinned: false
kind: note
```

### 3. 操作: `guildbotics member memory ...`

記録・想起の操作を提供する純粋な mechanism（意味判定は埋め込まない）。**両ユースケース**——利用者と対話する **SKILL 経由**と、scheduler の **workflow から自律**——で同じコマンドを使い、違いは「誰が起動するか」だけ。`member_reference.py` に追記して単一ソース性を維持。

| コマンド                                                                        | 役割                                                                                                                                                       |
| ------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `memory record --person --scope --title --ticket/--pr/--channel/--thread ...`   | 文書を新規作成（記録）。body は file/stdin/pipe。doc-id を返す                                                                                             |
| `memory recall --person --query <text> [--query ...] [--meta-only] [--limit N]` | meta+body を字句 grep（複数 `--query` は OR、`--meta-only` で meta のみ）。`{doc-id,path,title,summary,スニペット}` を返す（全文は `memory get`）。詳細 §4 |
| `memory update --id ...`                                                        | 既存文書の改訂（**唯一の更新操作**）。本文と `meta.yml` を対象にする                                                                                       |
| `memory get --id`                                                               | 本文 + assets パス取得                                                                                                                                     |
| `memory touch --id`                                                             | その文書が今回 **有益で実際に使えた** ことを記録（recency を更新、内容は変えない）。recall/get は触らない                                                  |
| `memory archive --id`                                                           | 文書を `archived/` へ移動し MRU から外す（退避）。復元は逆操作                                                                                             |
| `memory promote --id`                                                           | personal → team へスコープ移動（**昇格操作**）                                                                                                             |

**呼び出しフロー例**

A. **ticket を処理しながら過去知識を活用し、終了時に手入れ**（workflow の典型）

```bash
# 1. 出自想起: このチケット由来の文書をピンポイントで
memory recall --person alice --query https://github.com/acme/app/issues/142 --meta-only
# 2. キーワードでも（同義語・英日を 1 回で OR）
memory recall --person alice --query リトライ --query retry
# 3. 候補の本文を読む
memory get --person alice --id auth-retry-pitfall
# 4. 役立った（内容は正しかった）→ recency を上げる
memory touch --person alice --id auth-retry-pitfall
# 5. 別の記憶は現実とズレていた → 直す
memory update --person alice --id old-deploy-note ...
# 6. 今回の新しい学びを出自つきで記録
memory record --person alice --scope personal --title "認証リトライの落とし穴" --ticket https://github.com/acme/app/issues/142 ...
```

B. **対話中、利用者の指示で記録・修正・チーム共有**

```bash
# 「これ覚えておいて」
memory record --person alice --scope personal --title "デプロイ手順メモ" ...
# 「さっきのここ違う、直して」
memory update --person alice --id deploy-howto ...
# 「これはチームに上げて」
memory promote --person alice --id deploy-howto
```

### 4. エージェントが記憶に触れる仕組み

エージェントが記憶に触れるための仕組みとして、 `member context`（毎回渡される）と `memory recall`（自分で引く）の2種類を用意する。

**A. `member context`**

現在、毎 run エージェントは必ず `member context` を呼んでいる。その出力に今回、以下のような形で新たに memory フィールドを追加する。

出力の `memory` フィールド（抜粋）:

```json
"memory": {
  "digest": [
    {
      "doc_id": "auth-retry-pitfall",
      "path": "documents/personal/alice/auth-retry-pitfall",
      "title": "認証リトライの落とし穴",
      "summary": "401 ループを避けるためリトライ前に必ず再取得…"
    }
  ],
  "pinned": [
    {
      "doc_id": "recording-policy",
      "path": "documents/team/recording-policy",
      "title": "記録ポリシー",
      "summary": "些末なログは残さない / 失敗ノウハウは team へ…"
    }
  ]
}
```

- **`digest`** = 最近 **作った（record）/ 直した（update）/ 使えた（touch）** 文書の meta（本文なし）を上位 N 件。本文を載せず「関連しそうな記憶が在る」と気づかせ、`recall`/`get` の引き金にするのが役割。`N = 20` 既定（N は機械パラメータで §8）。
- **`pinned`** = `meta.pinned: true` の文書。recency と無関係に**毎回必ず**載る。「気づきの種」ではなく「必ず従わせたいもの」用なので、要旨でなく **body 込み**で載せる（代表例は運用ポリシー、§8）。乱用するとコンテキストを圧迫するため少数に保つ。personal にも team にも置ける。

`digest` の並び（recency = 最近 `record`/`update`/`touch` した順）は **member ごとの MRU リスト**で決まる: `recent.txt`（`documents/personal/<person_id>/recent.txt`）に **1行 = doc-id、最新が先頭**で持つ。team 文書も、その member が `record`/`update`/`touch`/`promote` した場合は同じ `recent.txt` に doc-id を載せる。`record`/`update`/`touch` のたび当該行を先頭へ移して丸ごと書き直す（`recall`/`get` は触らない＝調査は recency にならない）。`promote` は doc-id を維持したまま同じ member MRU の先頭に上げ、`archive` は MRU から外す。digest は **先頭 N 行 + その meta** を読むだけで総文書数に依存しない。

`record` / `update` / `touch` / `archive` / `promote` は、MRU とは別に `documents/memory_events.jsonl` へ構造化 audit event を追記する。event には `person_id`、`doc_id`、scope、path、action、title、summary、source、変更フィールド、trace/run/task-run 相関 ID を入れる。本文全文は audit に重複保存せず、desktop の診断画面は必要に応じて現在の `body.md` から preview を読む。

この仕組みにより、直近で参照・更新した情報は自動的にコンテキスト内に展開されることになるため、特別な努力なしに情報にたどり着くことができる。

**B. `memory recall`**

`memory recall` は今の自分の頭の中（LLM/エージェントのコンテキスト）に存在しない情報を検索するための仕組み。

- デフォルトでは **`meta` + `body` を字句 grep**する。
- `--meta-only` オプションを追加すると `meta` だけを取得。PRのURLなどmetaに情報が存在することがあらかじめわかっている時にはこちらを利用する。
- 実装は `rg`（ripgrep）による fixed string 検索を優先する。複数 `--query` は `rg -F -i -e ...` 相当の OR 検索にし、`--meta-only` では `meta.yml` のみ、通常 recall では `meta.yml` と本文 markdown のみを対象にする。`rg` が見つからない環境では WARN ログを出して Python fallback に落とす（別 index は持たない）。
- 返り値はマッチ文書の配列 `{doc_id, path, title, summary, snippet}`で、全文を返すものではない。まずはこの戻り値の内容を確認し、有益な情報が得られそうだとエージェントが判断したときに、`memory get` で全文を取得するためのもの。
- **字句一致の限界とエージェント指示（SKILL / `member_reference` に明記する）**: grep は字句一致なので、同義語・英日・言い換えを取りこぼすため、このコマンドを呼び出すエージェントには必ず同義語・英日による言い換え指定をさせるようにする:
  - **同義語・英日を 1 回の `recall` に併記して OR 検索する**（複数回に分けない）。`--query` は繰り返し可で、複数指定は OR、各値はリテラル/フレーズ。（当たりやすさを上げる `keywords` の書き方は §5 `record` 参照）
  - 例:

    ```bash
    # 同義語・英日を 1 回で OR 検索（grep -F -e リトライ -e 再試行 -e retry に対応）
    memory recall --person alice --query リトライ --query 再試行 --query retry
    # 純粋な出自一致（本文の言及を巻き込まない）
    memory recall --person alice --query https://github.com/acme/app/issues/142 --meta-only
    ```

    返り値の例（1番目の OR 検索に対して。マッチ文書の配列。`path` の `personal/`・`team/` で scope を判別、全文は `memory get`）:

    ```json
    [
      {
        "doc_id": "auth-retry-pitfall",
        "path": "documents/personal/alice/auth-retry-pitfall",
        "title": "認証リトライの落とし穴",
        "summary": "トークン更新が競合すると 401 ループになる。リトライ前に必ず再取得する。",
        "snippet": "…リトライ前に必ずトークンを再取得する。競合すると 401 ループに…"
      },
      {
        "doc_id": "retry-policy",
        "path": "documents/team/retry-policy",
        "title": "リトライ方針（チーム合意）",
        "summary": "指数バックオフ + 最大3回。冪等でない操作はリトライ禁止。",
        "snippet": "…リトライは指数バックオフで最大3回まで…"
      }
    ]
    ```

- **workflow での出自想起**: workflow プロンプトが冒頭で `memory recall --query <ticket-url|thread-url> --meta-only` を叩く。

### 5. コマンドの詳細（`record` / `get` / `update` / `touch` / `archive` / `promote`）

`recall` は §4。ここでは残りのコマンドを、実装が割れやすい点を中心に定める。

**`memory record` — 新規文書を作る（記録）**

```bash
# issue/PR を出自に、同義語キーワード付きで新規記録する
memory record --person alice --scope personal \
  --title "認証リトライの落とし穴" \
  --keyword リトライ --keyword 再試行 --keyword retry \
  --summary "トークン更新が競合すると 401 ループになる。リトライ前に必ず再取得する。" \
  --ticket https://github.com/acme/app/issues/142 \
  --pr https://github.com/acme/app/pull/150 \
  --body-file note.md
```

渡すもの:

- **`--scope`** … `personal` / `team` のどちらに作るか。
- **`--title`** / **本文**（`--body-file` / stdin / パイプ）。保存時の本文ファイル名は `body.md` 固定。
- **`--keyword`**（繰り返し可）… キーワード。
  - **ポイント** … 同義語・英日を入れる（例: `リトライ` / `再試行` / `retry`）。recall は字句 grep（§4）なので、ここの語彙が後の当たりやすさを決める。
- **`--summary`** … digest と recall 一覧に出る1行。一目で「使えそうか」を判断できる要点を書く。
- **`--ticket` / `--pr` / `--channel` / `--thread`** … 出自（`source`）を任意個。workflow なら `Task`/`ChatEvent` から自動付与も可。
  - **ポイント** … 出自を漏れなく。後で `recall --query <url> --meta-only` でその ticket/PR/スレッド由来の文書を一発で引ける。
- **`--kind`** … 文書の種別。`note` / `policy` のいずれかを指定。未指定時のデフォルトは `note`。`policy`は運用ポリシー文書の予約値（§8）。
- **`--pin`** … boolean オプション。毎回必ず従わせたいものだけ指定する（乱用するとコンテキストを圧迫する）。これを指定すると meta.yml に `pinned: true` が書き込まれる

システムが自動でやること:

- **`doc-id`** を採番して返す（引数では指定しない。実装で扱いやすい短い一意 ID＝ULID 等。slug 化はしない。予約名 `recent.txt` / `archived/` とは衝突しない）。
- **`created_at` / `updated_at`** を自動設定。
- 作った文書を §4 の digest 先頭に置く。

**`memory get` — 本文と添付を取る**

```bash
memory get --person alice --id auth-retry-pitfall
```

- **返り値**: body 全文 + `assets/` のパス一覧 + meta。
- `recall` の候補から有益そうな1件を選び、全文を読むために使う。**読むだけ**（MRU 不変。使えたら別途 `touch`）。

**`memory update` — 既存文書を更新する**

```bash
# 本文を直す（get で現本文を取り、編集して全置換）
memory update --person alice --id auth-retry-pitfall --body-file fixed.md
# メタだけ直す: キーワードの追加・削除、常在化する
memory update --person alice --id recording-policy --add-keyword 命名 --remove-keyword naming --pin
```

- **変更できるもの**: 本文（`body.md`）と meta のフィールド（`title` / `summary` / `keywords` / `source` / `pinned` / `kind`）。同時にも個別にも変更でき、**指定しなかったものは不変**。本文は**全置換**（部分パッチはしない。`memory get` で現本文を取り、編集して全文を渡す）。
- **システムが自動でやること**: `updated_at` を現在時刻に更新（手動不可。陳腐化・recency のシグナル）、recency を上げる（MRU 先頭）。
- **変わらないもの**: `created_at` / `doc-id` / `path` / `scope`（scope 変更は `promote`、退避は `archive`）。

**`memory touch` — 「今回 有益で実際に使えた」と記録**

```bash
memory touch --person alice --id auth-retry-pitfall
```

- **効果**: recency を上げる（MRU 先頭）だけ。**内容は変えない**。
- `update`（直す）との対比で使う:「直す必要はないが、今回その文書が役立った」ことを示す唯一の手段。

**`memory archive` — 陳腐化した文書を退避**

```bash
memory archive --person alice --id dead-y
```

- **効果**: 文書ディレクトリを `archived/` へ移動し MRU から外す → `recall`/`digest` の対象外。復元は逆操作（戻すだけ）。
- 直しようがなく死んだ知識を、削除せず退避する（履歴・出自を残す）ために使う。
- チーム共有文書を対象にする場合は `--team` フラグを追加。

**`memory promote` — personal の文書を team へ昇格**

```bash
memory promote --person alice --id deploy-howto
```

- **効果**: `personal/<person_id>/<doc-id>/` → `team/<doc-id>/` へ**移動**（`doc-id` 維持、meta は触らない）。
- 個人の知識をチーム共有へ引き上げるために使う。team に同 `doc-id` が在る場合の扱いは詳細設計（既定はエラー）。

### 6. 利用シーン: 各モードで memory がいつ・どう呼ばれるか

memory コマンド（§3）は3つの実行モードから使われる。どのモードでも入口は `member context`（最初の必須コマンド）で、その出力には既に **pinned**（常在メモ＝policy など）と **digest**（最近使った／直したメモの見出し）が載っている（§4）。エージェントはまずこれを読み、足りなければ `recall` で自分から引く。

どのモードも骨格は共通で、**作業の頭で関連記憶を `recall` し、終わりに使った記憶を現実に合わせて直す**。この末尾の手当て——役立った記憶に `touch`、ズレていた記憶に `update`、新たな学びに `record`——を本書では **記憶の手入れ** と呼ぶ。

以下、モードごとに「何をきっかけに・どの順で」コマンドが走るかを示す。この順序を実際に生むプロンプト／コードの所在は §7。

**6.1 対話モード（SKILL）— Claude Code / Codex で人が使う**

利用者は「recall して」と指示する必要はない。SKILL.md の各 Flow が作業の頭で recall、終わりで手入れを行うため、「この issue を対応して」と頼むだけで内部的に recall →作業→ 手入れが走る。明示の言動があれば、その流れに record / update / promote が割り込む。

| 利用者の言動 / タイミング                        | 内部で呼ばれる memory コマンド                                     |
| ------------------------------------------------ | ------------------------------------------------------------------ |
| 「この issue / PR を対応して」（記憶の言及なし） | 作業の頭で自動 `recall`（出自URL → キーワード OR）、有望なら `get` |
| 「これ覚えておいて」「メモして」                 | `record --scope personal`                                          |
| 「さっきのここ違う、直して」                     | `update --id`（参照済みメモを全置換）                              |
| 「これはチームに上げて」                         | `promote --id`（personal → team）                                  |
| 「あの件どうだったか / 過去の知見ある?」         | `recall` → `get`                                                   |
| 作業の区切り・セッション終了                     | 役立ったメモに `touch`、ズレてたメモに `update`（手入れ）          |

**6.2 ticket-driven workflow — scheduler が自律実行（GitHub）**

人はいない。scheduler が対応すべき issue / PR を1件拾い、`handle_github_ticket` プロンプトを起動する（[ticket_driven_workflow.py:113](../../guildbotics/templates/commands/workflows/ticket_driven_workflow.py)）。タイミングはプロンプトのステップ順に固定される。各ステップの「いつ走るか」を明示する:

1. **`member context`（毎回）**: 出力の **pinned** は標準ルールとして必ず従う。**digest** は見出しだけ既にコンテキストにある——このチケットに関係しそうな項目があれば、recall を介さずステップ5で直接 `get` する候補として控える。
2. **`issue inspect` / `pr inspect --include-comments`（毎回）**: チケット内容（タイトル・本文・コメント・review thread）を取得。これは memory とは無関係に必須の主入力で、「内容はこのコマンドの出力を正とする」現行ステップ（[handle_github_ticket.ja.md](../../guildbotics/templates/commands/functions/handle_github_ticket.ja.md)）。**recall のキーワードはここで読んだ内容から作る**ため、recall はこの後に置く。
3. **`recall` — 出自一致（毎回）**: `recall --query {ticket_url} --meta-only`。`{ticket_url}` はプロンプト先頭で既知。このチケットを `source` に持つ文書だけを拾う。安価で誤検出が無いので必ず行う。
4. **`recall` — キーワード一致（関連が見込めるときだけ）**: ステップ2で読んだタイトル・本文・コメントから主要語（機能名・エラーコード・識別子など）を取り出し、その同義語・英日言い換えを添えて **1回の OR `recall`**（クエリ語の出所は inspect 結果、digest の見出しも参考）。digest と出自一致で既に十分なら省略してよい。
5. **`get` — 当たりだけ（空振りでは呼ばない）**: ステップ1の digest・ステップ3/4 の recall 戻り（`doc_id`/`title`/`summary`/`snippet`）を見て、有益そうな文書のみ `get` で全文取得。関連が無ければ `get` しない。
6. 取得した記憶を現実照合しながら編集・コミット・PR。
7. **完了前の手入れ**: 役立ったメモに `touch`、ズレてたメモに `update`、今回の学びを `record --ticket {ticket_url}`。
8. 必須の `task complete`。

要点: inspect と出自一致 recall は **毎回**、キーワード recall は **関連が見込めるとき**、`get` は **当たりがあるときだけ**。recall（探す）と `get`（読む）は別ステップ。

**6.3 chat workflow — scheduler が自律実行（Slack）**

Slack の incoming イベントが起点。`handle_chat_event` プロンプトが起動する（[chat_conversation_workflow.py:127](../../guildbotics/templates/commands/workflows/chat_conversation_workflow.py)）。ticket と同じ「recall（探す）／`get`（読む）を分け、各ステップの実行条件を明示」構成:

1. **`member context`（毎回）**: pinned は従う／digest は眺める。加えてプロンプトには `{latest_message}` と `{previous_thread_context}` が既に注入済み（[handle_chat_event.ja.md](../../guildbotics/templates/commands/functions/handle_chat_event.ja.md)）。
2. **`inspect thread`（毎回）**: 現在のスレッド全体を取得（現行の必須ステップ）。**recall のキーワードは `{latest_message}`・`{previous_thread_context}`・取得したスレッドから作る**ので recall はこの後。
3. **`recall` — 出自一致（毎回）**: スレッドの permalink（`{channel_id}` + `{thread_ts}` から構成）または `{thread_ts}` を `--query ... --meta-only`。このスレッドを `source` に持つ文書を拾う。
4. **`recall` — キーワード一致（関連が見込めるときだけ）**: メッセージ／スレッドの話題語（機能名・エラー・固有名）＋同義語・英日で **1回の OR `recall`**。十分なら省略可。
5. **`get` — 当たりだけ**: recall 戻り・digest から有益そうな文書のみ全文取得。無ければ `get` しない。
6. reply / reaction / no-op を判断して実行。
7. **完了前の手入れ**: 役立ったメモに `touch` / ズレてたメモに `update`。このスレッドで得た再利用価値のある文脈・ノウハウを `record`（「Slack 文脈を記録」の実体、§11）。
8. 必須の `chat complete`。

3モードに共通する骨格は **recall（頭）→ 現実照合 → 手入れ（末尾）**。この末尾の手入れ（**ホットパス**）は、その run で実際に使った記憶しか直せない。使われない記憶を拾うコールドパス保守は Phase 1 ではスコープ外で、別文書 [member_memory_cold_path_phase2.ja.md](member_memory_cold_path_phase2.ja.md) にまとめた。

### 7. 実装: どのファイルに何を書くか

§6 で示した recall → 現実照合 → 手入れの順序と §8 のポリシー育成が実際に動くのは、その手順を **エージェントが読むプロンプトに書いたとき** だけ。ここでは「どこに・どんな文面を足すか」を、コードで確認した配線に沿って具体化する。

肝心な制約が1つある。member capability の単一ソースである `member_reference.py` は、その docstring（[member_reference.py](../../guildbotics/capabilities/member_reference.py)）で「**コマンドが何をできるか** だけを書く。**いつ・どの順で呼ぶか**（タスク手順）は SKILL.md と workflow プロンプトに置き、共有リファレンスには絶対に置かない」と定めている。`member context` の出力に capabilities リファレンスが埋め込まれること（[member_github.py](../../guildbotics/capabilities/member_github.py)）、両モードの最初の必須コマンドが `member context` であることも確認済み。

つまり **「いつ recall し、いつ手入れするか」は共有化できない**。同じ手順を3つの入口——対話の SKILL.md、workflow の `handle_github_ticket`、workflow の `handle_chat_event`——にそれぞれ書く必要がある。これが §6 の利用シーンと §8 の主張を成立させる実装の本体である。

変更一覧:

| 何を変えるか                           | 対象                                              | 種別       | 根拠（コードで確認）                             |
| -------------------------------------- | ------------------------------------------------- | ---------- | ------------------------------------------------ |
| digest / pinned をコンテキストに載せる | `member context` の戻り dict                      | コード     | `MemberGitHubCapabilityService.context()`        |
| `memory` コマンド群を能力として載せる  | `member_reference._CAPABILITY_GROUPS`             | コード     | `member_reference.py`                            |
| 対話モードの recall→get→手入れ         | `skills/guildbotics/SKILL.md`                     | プロンプト | 各 Flow（GitHub Issue / PR Review / Slack Chat） |
| workflow(GitHub) の recall→get→手入れ  | `functions/handle_github_ticket.ja.md` / `.en.md` | プロンプト | `ticket_driven_workflow.py` が invoke            |
| workflow(Slack) の recall→get→手入れ   | `functions/handle_chat_event.ja.md` / `.en.md`    | プロンプト | `chat_conversation_workflow.py` が invoke        |

**7.1 digest / pinned をコンテキストに載せる（コード）**

`MemberGitHubCapabilityService.context()` の戻り dict（[member_github.py](../../guildbotics/capabilities/member_github.py)）に `memory` キーを足す。これだけで両モードの最初の `member context` に digest / pinned が乗る（注入点が1か所で済むのは、両入口が同じ `context()` を最初に呼ぶため）。

```python
# member_github.py の context() 戻り dict に追加するキー（§4 の JSON 形）
"memory": {
    "digest": load_digest(self.person, limit=policy_n),   # recent.txt 先頭 N 行の meta
    "pinned": load_pinned(self.person, self.team),         # pinned 文書（policy 等は body 込み、§8）
},
```

`load_digest` / `load_pinned` と `recent.txt` の読み書き（record/update/touch で先頭へ、§4）は新しい memory capability service に実装する。`pinned` は要旨でなく body も載せる（§8）。digest 件数 `policy_n` は `load_policy_params()` が policy 文書の `meta.yml` から得る（無ければ既定 20、§8）。

**7.2 `memory` コマンド群を能力リファレンスに載せる（コード・単一ソース）**

`member_reference.py` の `_CAPABILITY_GROUPS`（[member_reference.py](../../guildbotics/capabilities/member_reference.py)）に `"Memory"` グループを追加し、§3 の7コマンド（record / recall / get / update / touch / archive / promote）を usage + 1行説明で並べる。ここに書くのは「各コマンドが何をするか」だけ。「タスクのどこで呼ぶか」は次の 7.3〜7.5 の入口側に置く（docstring の制約）。

**7.3 対話モード: `skills/guildbotics/SKILL.md`（プロンプト）**

新セクション `## Memory Flow` を `## Safety Rules` の後に追加する。文面（既存スキルの英語スタイルに合わせる）:

```markdown
## Memory Flow

The `member context` output includes a `memory` block: `pinned` (always-on notes,
including team policy — treat as standing rules for this session) and `digest`
(recently used/updated notes — titles only; a hint that a relevant note may exist).

Before working a task, find prior notes. Recall (search) and get (read) are separate:

- Recall by source [always]: `guildbotics member memory recall --person <person_id> --query <ticket_or_thread_url> --meta-only` — cheap, pinpoints notes whose source is this exact ticket/thread.
- Recall by topic [when prior notes seem likely]: take key terms from the ticket/thread (feature names, error codes, identifiers) and add synonyms / EN-JA variants in one OR call: `... --query リトライ --query 再試行 --query retry`. Skip if digest + source recall already cover it.
- Get [only promising hits]: from `digest` and the recall results, read the few useful notes in full with `member memory get`. If nothing looks relevant, don't get.

While working, reality-check each note you read against the current code/ticket.
Do not trust a note blindly.

Before finishing (memory upkeep):

- A note was wrong → `member memory update --person <person_id> --id <doc-id> ...`
- A note was correct and actually helped → `member memory touch --person <person_id> --id <doc-id>`
- You learned something durable → `member memory record --person <person_id> --scope personal --title ... --ticket <url> ...`
- The user says "remember this" / "this is wrong, fix it" / "raise this to the team"
  → record / update / promote on their behalf. Never ask the user to edit files.

Policy is a pinned team note (`kind: policy`) and is human-gated. Change it only on the
user's instruction/approval, using `member memory update ... --policy-approved` (body for
rules, `--set <key>=<value>` for params, e.g. `--set digest_n=30`). When _you_ think a
policy should change, do not edit it — propose the change in your reply and apply it only
after the user approves.
```

加えて各 Flow に差し込む: GitHub Issue Flow / PR Review Flow は `issue inspect` / `pr inspect` の直後、Slack Chat Flow は `chat inspect` の直後に「上の Memory Flow に従って recall → get する」ステップ（実行条件は Memory Flow に記載）。各 Flow の末尾（最終コメント / reply の前）に「手入れ（touch / update / record）」ステップを置く。`## Workflow Marker Guardrail` は変更不要——workflow モードでは引き続きプロンプトが優先される。

**7.4 workflow(GitHub): `functions/handle_github_ticket.ja.md`（プロンプト）**

`<instructions>` に2ステップを挿入する。recall は inspect（ステップ8）の **後**（クエリ語を inspect 結果から作るため）かつ repository 準備（ステップ9）の前、手入れは必須の `task complete`（ステップ17）の直前。recall（探す）と `get`（読む）は1ステップ内で順に書き、それぞれの実行条件を文面に含める（§6.2 と対応）。

```text
（recall + get ステップ。現行ステップ8の後に挿入）
member context の `memory.pinned` は標準ルールとして必ず従ってください。そのうえで
過去の記憶を次の順で引きます。
(1) 出自一致【毎回】:
    `guildbotics member memory recall --person {person_id} --query {ticket_url} --meta-only`
    （このチケットを source に持つ文書を拾う。安価なので必ず実行）
(2) キーワード一致【関連が見込めるときだけ】: ステップ8で読んだ
    タイトル・本文・コメントから主要語（機能名・エラーコード・識別子）を取り出し、
    その同義語・英日言い換えを添えて 1 回の OR 検索（`--query` を複数指定）。
    digest と (1) で十分なら省略可。
(3) get【当たりだけ】: digest と (1)(2) の戻りを見て、有望な文書のみ
    `guildbotics member memory get` で全文取得。関連が無ければ get しない。
読んだ記憶は現在のコード／チケットと突き合わせ、鵜呑みにしないでください。

（手入れステップ。必須の task complete の直前に挿入）
`task complete` の前に記憶を手入れしてください: 今回 実際に役立った記憶は
`guildbotics member memory touch`、現実とズレていた記憶は `guildbotics member memory update`、
今回得た再利用価値のある学びは
`guildbotics member memory record --scope personal --ticket {ticket_url} ...`。
policy（`kind: policy`）は自律実行では変更できません（コマンドが拒否）。運用上の摩擦に
気づいたら、変更を ticket コメント（`issue comment` / `pr comment`）で提案してください
（新規 issue は立てず、直接 update もしない）。
記録に値するか・ズレているかの判断はあなた自身の推論で行ってください。
```

`{person_id}` / `{ticket_url}` は既にこのプロンプトで使われている placeholder なので新規配線は不要。**同じ追記を `handle_github_ticket.en.md` にも入れる**（AGENTS.md の i18n ルール: 片方の言語だけ欠落させない）。

**7.5 workflow(Slack): `functions/handle_chat_event.ja.md`（プロンプト）**

GitHub と同型（`memory.pinned` 遵守の前置き＋recall + get）で、これは `inspect thread`（ステップ4）の後（クエリ語は `{latest_message}`・`{previous_thread_context}`・スレッドから作る）、手入れは `chat complete`（ステップ13）の直前。条件も §6.3 と同じ——出自一致 recall は毎回、キーワード recall は関連が見込めるときだけ、`get` は当たりだけ。Slack 特有の点は3つ: (a) 出自一致 recall のクエリは thread の permalink（`{channel_id}` + `{thread_ts}` から構成）または `{thread_ts}`、(b) 手入れに「このスレッドで得た再利用価値のある文脈・ノウハウを `memory record` する」を明示する（§11 の「Slack 文脈を記録」はこの1行で実現する）、(c) policy（`kind: policy`）は自律実行では変更できないので、改善に気づいたら Slack スレッドへ `reply`/`post` で提案する（直接 update しない）。`.ja.md` と `.en.md` の両方に入れる。

### 8. ポリシーをエージェント自身が育てる

policy（何を残すか・粒度・昇格タイミング・recall 習慣など）を、人がファイルを編集して与えるのではなく、エージェント／チーム自身が育てる。新しい config レイヤーも新コマンドも作らないが、**無設定で済むわけではない**——既存の記憶機構へ数点の結線が要る。本節はその結線を具体化する。

**policy = `kind: policy` の team 文書**

policy は `documents/team/` 配下の pinned な文書で、`meta.kind: policy` で識別する。`meta.yml` はシステムが読む機械パラメータ、本文 markdown はエージェントが読む行動規約として分ける:

```yaml
# documents/team/recording-policy/meta.yml
title: 記録ポリシー
kind: policy
pinned: true
digest_n: 20 # ← 機械パラメータ（コードが読む）
updated_at: 2026-06-19T09:00:00Z
```

```markdown
# documents/team/recording-policy/body.md
- 些末なログ・一時的な試行は残さない。 # ← 行動規約（エージェントが読む）
- 失敗から得たノウハウは team へ上げる。
- secret・token は本文に書かない。
```

- **body（散文）** = エージェントが読む行動規約。
- **meta.yml** = システムが読む metadata と機械パラメータ（Phase 1 は `digest_n`。Phase 2 でコールドパス間隔等が加わる）。

**実装: 結線する5点**

1. **policy 文書を見つける**: `documents/team/` の `meta.kind == policy` 文書を読む（例 `load_team_policies()`）。**policy 文書はチームに1つに保つ**——`record --kind policy` は既存 policy 文書があればそれを更新し、2つ目を作らない。以降の2・3が使う。
2. **body をエージェントへ載せる**: pinned 文書は要旨でなく **body 全文** を `member context.memory.pinned` に含める（§7.1 の `load_pinned`）。policy は「必ず従う規約」なので summary では足りない。digest は従来どおり meta のみ（body 込みは pinned だけ。pinned は少数前提なのでコストは許容）。
3. **meta.yml の機械パラメータをコードへ渡す**: `load_policy_params()` が policy 文書の `meta.yml` を読み、既定値付きで返す。Phase 1 の読み手は digest 件数 `digest_n`（§7.1 の `load_digest`）の1つ。policy 文書はチームに1つなので（結線1）複数文書の衝突は起きない。policy 文書もキーも無ければコード既定（`digest_n = 20`）。
4. **編集経路を memory コマンドに通す**: policy も `memory update` で直す。body / summary / kind / pinned は既存フィールド（§5）。機械パラメータは `memory update --set <key>=<value>` で `meta.yml` に書く。`kind: policy` 文書では **機械パラメータの全キーを許可** する——変更は人間承認済み（結線5）のものしか通らないため、キーを制限する必要がない。
5. **policy 書き込みに人間承認ゲートを掛ける（コード）**: `kind: policy` 文書への書き込み——`record --kind policy` / `update` / `archive`——は `--policy-approved` フラグを必須にする。さらに **自律実行では `--policy-approved` を受け付けず拒否** する。自律実行は run-id env（ticket は `GUILDBOTICS_TASK_RUN_ID`、chat は `GUILDBOTICS_RUN_ID`。brain が subprocess へ注入）の有無で判定する。これでエージェントは自分で policy を承認できない。対話（SKILL）だけが、利用者の指示・承認を得てから `--policy-approved` を付ける。

**出荷時デフォルト（floor）と上書き**

- **規約（body）**: 出荷 baseline は SKILL / `member_reference` に固定文で持つ（下記）。team policy 文書の body は pinned として追加で載り、衝突時は team 文書を優先する（エージェントへの指示）。
- **パラメータ**: コード既定（`digest_n = 20`）。policy 文書の値が上書きする。

team policy 文書が無くても、両者の既定だけで破綻しない。

出荷時 baseline の規約本文（現時点の推奨。運用しながら調整する前提の初期値）:

```text
- 再利用価値のある学び（落とし穴・解決手順・設計判断の理由）だけを残す。
  些末なこと・一時的な試行錯誤・このタスク限りの情報は残さない。
- 失敗から得たノウハウは特に残す。チーム全体に効くものは team への昇格（promote）を提案する。
- 1文書1トピックに絞る。summary は「使えるか」を一目で判断できる1行にする。
- recall に当たるよう keywords に同義語・英日を入れる。
- secret・token・個人情報は本文に書かない。
```

既定パラメータ: `digest_n = 20`（コード既定）。

**memory ノートは自律、policy は人間ゲート**

2つを明確に分ける。これが暴走（人の知らぬ間に policy が更新され続ける）を断つ要。

- **memory ノート（`kind != policy`）**: AI 自身が蒸留した知識。§6 の手入れ（ホットパス）が **自律で** `update` / `archive` する（人手不要。使われない記憶のコールドパス保守は Phase 2・別文書）。
- **policy（`kind: policy`）**: 更新は **人間の指示・承認があるときだけ**。エージェントは自分では変えない（結線5 が自律実行の書き込みを拒否）。代わりに変更を **積極的に提案** する。

policy 変更の提案チャネル（モード別に明示する）:

- **対話(SKILL)**: 返信でそのまま提案する。**利用者の指示は承認とみなす**（別途の確認は挟まない）ので、指示を受けたら `memory update`（`--set` 含む）を `--policy-approved` 付きで反映。例:「digest を 30 に」→ `memory update --id recording-policy --set digest_n=30 --policy-approved`。
- **ticket workflow**: 自律実行なので policy は更新できない。気づいた改善は **ticket コメント**（`issue comment` / `pr comment`）で提案する（新規 issue は立てない）。
- **chat workflow**: 同じく更新不可。Slack スレッドへ `chat reply` / `post` で提案する。

提案には「現行 policy のどこを・どう・なぜ変えるか」を書く。反映するのは、承認を得たうえで `--policy-approved` を付けたときだけ。`record`・`promote` は policy 育成には使わない（既存 team 文書の改訂であって、新規作成・昇格ではない）。

**mechanism / policy の分担**

- **mechanism（システムが固定で提供）**: スコープ2種、`memory` コマンド、source の付与、recency / MRU / digest、pinned 常在化、`kind: policy` 予約と `meta.yml` の機械パラメータ読取、policy 書き込みの人間承認ゲート（自律実行は拒否）、record/update 時の secret 除去（§9）。
- **policy（エージェントが提案し、人間が承認して育てる文書）**: 何を残すか・粒度・分類・昇格・recall 習慣・digest の `N`。
- **誰が何をするか**:
  - **利用者(人)**: 会話で意図を伝え、policy 変更を承認する。コードも config も触らない。
  - **CLIエージェント**: 各 run で関連文書を recall → 作業 → 終了時に現実照合して memory ノートを直す（§7 の差し込み手順）。policy は自分で更新せず提案し、対話で承認を得たときだけ反映する。
  - **チーム**: memory を自律で育て、policy は提案と人間承認で育てる、人間＋エージェントの集合。

### 9. 秘匿・安全

- 記録は member capability 境界を通す。既存 `task-runs` の `_without_secrets` と同じく、record/update 時に既知の secret / token を本文と `meta.yml` の文字列値から best-effort で除去する。トークン値は本文・title・summary・keywords・source URL などに入れない。
- 可視範囲: `team/` はワークスペース内のチーム共有、`personal/<person_id>` は当人のみ（パスのスコープ + 実行時の person_id で担保）。他人の personal は触れない。
- 限界: 文書は平文ファイル。secret の除去は best-effort のガイドであって暗号化ではない。

### 10. LLM 判定の置き場所

意味判定（記録に値するか・関連するか・policy をどう直すか）は `memory` サブコマンドに入れない（AGENTS.md の鉄則）。判定は常に上流に置く:

- ホットパス → CLIエージェント自身の推論（もともと LLM なので自然。§7.4 / §7.5 の手入れステップで実行）。

これにより `member memory ...` は純粋に機械的なコマンドのままになる。コールドパスの LLM 判定（陳腐化判定など）は Phase 2・別文書。

### 11. 既存資産との関係

§7 で触れた配線（member context / member_reference / config）は再掲しない。ここでは §7 に出てこない隣接ストアとの関係だけを示す。

- **task-runs**: 別ストア。文書 meta が run_id / ticket を参照として持つ。task-runs = 機械的な実行ログ、documents = そこから蒸留した知識。
- **chat_state**: 直接は無関係。「Slack 文脈を記録」は、エージェントが `member chat inspect` の結果を `memory record` する形で行う（§7.5 の手入れステップ）。
- **Task / ChatEvent**: source 値の供給元。記録時に既存オブジェクトから自動付与でき、新規の配線は不要。
