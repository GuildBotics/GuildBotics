# Branvil 連携ガイド

Branvil は自分で動かすコードホスト兼ボードです。ワークスペースにまとめた Smart HTTP の Git
リポジトリ、レビューと CI 付きの pull request、ワークスペースのボードに載る各リポジトリの
チケットを持ちます。GuildBotics は `branvil`
provider を通じて、GitHub の代わりにコードホスト（`services.code_hosting_service`）と
チケット（`services.ticket_service`）として使います。チャットはプロジェクトが選んだもの
（Slack など）のままです。

このガイドは、GuildBotics が Branvil に求めるものを説明します。Branvil のインストールと起動、
`branvil` コマンドの正確な使い方は Branvil の README を参照してください。

## 全体の構成

```text
Hub のマシン                                ほかのマシン
┌──────────────────────────────┐            ┌──────────────────────────────┐
│ branvil serve                │            │ branvil tunnel --via <hub>   │
│   http://127.0.0.1:7878      │◀── SSH ────│   http://127.0.0.1:7878      │
│ GuildBotics                  │            │ GuildBotics                  │
└──────────────────────────────┘            └──────────────────────────────┘
```

- Branvil は GuildBotics の Hub を置くマシンで 1 つだけ動かし、loopback でだけ待ち受けます。
- すべてのマシンが同じベース URL（既定 `http://127.0.0.1:7878`）で Branvil に届きます。
  Hub のマシンでは Branvil そのもの、ほかのマシンでは Branvil のトンネルが、Hub 同期と同じ
  SSH 接続で同じローカルポートを Hub へ転送します。GuildBotics 自身はトンネルを持ちません。
- どのマシンでも同じベース URL であることで、記録した URL がどこでも有効になります。チケットは
  `{base}/{repository}/-/tickets/{n}`、pull request は `{base}/{repository}/-/pulls/{n}` で、
  共有するプロジェクト設定の `base_url` は 1 つです。リポジトリはチケット（`#1`）を
  pull request（`!1`）とは別に番号付けします。

トンネルが切れていると、Branvil の操作はすべて「Branvil に届きません」で失敗し、
**設定 → 検証** は「Branvil に届きません」と報告します。

## Branvil を準備する

Branvil の README に従って、次を行います。

1. Hub のマシンで Branvil を、GuildBotics を動かすほかのマシンでトンネルを動かす。
2. メンバーが作業するワークスペース（例 `acme`）と、その中のリポジトリ（`acme/demo`。
   チケットに付けられるラベルも設定する）を作る。ワークスペースはボードを 1 つ持ちます。
   チケットはリポジトリが持ち、マージで閉じられるのは pull request と同じリポジトリの
   チケットだけです。
3. GuildBotics の人間のメンバーは Branvil のオーナー（`branvil init` が作るただ一人の人）
   です。オーナーは何でもでき、マージし、サインインせずに Branvil の GUI を使います
   （Hub マシンで `branvil open`）。AI メンバーごとに agent のメンバーを作る。agent は
   すべてを読み、チケット・pull request・保護されていないブランチで作業します。
   ワークスペースごとの権限付与はありません。
4. agent のメンバーごとにトークンを発行する。トークンは一度だけ表示されます。

## GuildBotics を設定する

デスクトップアプリの **設定 → 連携** で、コードホストとチケットに **Branvil** を選び、次を
入力します。

- **ベース URL**: Branvil に届く URL（空欄なら `http://127.0.0.1:7878`）
- **ワークスペース**: メンバーが作業するワークスペース（`acme`）。メンバーはその
  リポジトリにだけ書き込み、そのボードからチケットを取ります。読み取りは制限しません

続いて **設定 → メンバー → Branvil** で、各メンバーに **Branvil ユーザー名**（人間のメンバーは
オーナー名）を、AI メンバーには **Branvil トークン** も設定します。トークンは OS キーチェーンに
`{PERSON_ID}_BRANVIL_TOKEN` として保存され、メンバーの git のパスワードにもなります。git は
ユーザー名とトークンで認証します。

GUI を使わない場合、同じ設定は `team/project.yml` に

```yaml
services:
  code_hosting_service:
    name: branvil
    base_url: http://127.0.0.1:7878
    workspace: acme
  ticket_service:
    name: branvil
    base_url: http://127.0.0.1:7878
    workspace: acme
```

各メンバーの `person.yml` に

```yaml
account_info:
  branvil_username: aiko
```

と書きます。

**設定 → 検証** の **設定を検証** で、Branvil が応答すること、各 AI メンバーのトークンが
本人のものとして受け付けられること、ワークスペースを読み取れることを確かめられます。

## メンバーの動き

チケットのワークフローと pull request の巡回は、GitHub と同じように動きます。

- チケットの担当者は 1 人まで（そのチケットに取り組むエージェント）です。別のメンバーに割り当てると
  担当者が入れ替わるので、2 人のメンバーが同じチケットを取り上げることはありません。
- ワークスペースのボードの **着手可** レーンでメンバーに割り当てられたチケットを取り上げ、
  **作業中** に移します。**作業中** のチケットは、メンバーが最後に発言してから誰かが発言
  すると、もう一度取り上げます。チケットを参照する pull request ができると、巡回はその pull request を
  追い、マージでチケットは **完了** に移ります。
- メンバーの pull request は、メンバーが答えていない発言があると取り上げます。メンバーが
  レビューする pull request は、レビューを依頼されたとき、前回のレビューのあとに新しい
  コミットが来たとき、答えていない発言があるときに取り上げます。依頼によらない自動の
  再レビューは 3 回で止まり、巡回は pull request を Draft にしてその旨を書き込みます。続けるには
  Ready for review に戻してください。
- Draft の pull request には手を出しません。Draft を Ready for review に戻すのはオーナーだけ
  です。
- 失敗やレート制限で終わった実行は、チケットや pull request に状態の通知を残します。Branvil は
  それを通知として表示し、誰かが動くまでその項目を巡回から外します。

## 上流が GitHub のリポジトリ

Branvil のリポジトリは、GitHub のリポジトリに追従できます（*上流*。Branvil の README を参照）。
このとき正本は GitHub のままで、Branvil はメンバーが試行錯誤する場所になります。

- メンバーは上と同じように Branvil で作業します。その間、GitHub には何も出ません。
- pull request ができあがったら、オーナーは Branvil の GUI でマージの代わりに GitHub に出します。
  Branvil はブランチを push し、最終的なタイトルと本文で、**作成者の GitHub アカウントとして**
  GitHub に pull request を開きます。そのため、AI メンバーごとの bot アカウントを Branvil にも
  登録してください（`branvil member account <name>`。GuildBotics に登録する Branvil のトークンとは
  別）。GitHub の読み取りにも、これらの bot アカウントのどれか 1 つを使うので、人のメンバー自身の
  GitHub トークンは要りません。
- 出した head に対する GitHub のチェックは、pull request のチェックに `github/<name>` として
  ログ付きで並びます。GitHub で（Windows などで）失敗すると pull request は ready にならず、
  メンバーは Branvil で直します。その後の push は自動で GitHub に届きます。
- 承認とマージは GitHub で人が行います。Branvil は GitHub の既定ブランチに追従して pull request を
  マージ済みにするので、巡回はいつもどおりチケットを **完了** に移します。
- チケットには、それに当たる GitHub の Issue を記録できます。出した pull request はその Issue を
  閉じます。
