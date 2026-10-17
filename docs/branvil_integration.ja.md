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
│ branvil serve                │            │ GuildBotics (forwards)       │
│   http://127.0.0.1:7878      │◀── SSH ────│   http://127.0.0.1:7878      │
│ GuildBotics                  │            │                              │
└──────────────────────────────┘            └──────────────────────────────┘
```

- Branvil は GuildBotics の Hub を置くマシンで 1 つだけ動かし、loopback でだけ待ち受けます。
- すべてのマシンが同じベース URL（既定 `http://127.0.0.1:7878`）で Branvil に届きます。
  Hub のマシンでは Branvil そのものです。ほかのマシンでは、GuildBotics（Desktop か
  `guildbotics start` のサービス）が、動いているあいだ、Hub 同期と同じ SSH 接続でそのローカル
  ポートを Hub へ転送します。そのマシンでほかに動かすものはありません。GuildBotics が動いていない
  あいだ（`git` や Branvil の GUI を使うとき）は、`branvil tunnel --via <hub>` が代わりに
  転送します。先にポートを取ったほうが転送し、もう一方は待ちます。ポートが空けば、GuildBotics は
  10 秒以内に引き継ぎます。転送するのは、ベース URL が loopback（`127.0.0.1` か `localhost`）の
  ときだけです。
- どのマシンでも同じベース URL であることで、記録した URL がどこでも有効になります。チケットは
  `{base}/{repository}/-/tickets/{n}`、pull request は `{base}/{repository}/-/pulls/{n}` で、
  共有するプロジェクト設定の `base_url` は 1 つです。リポジトリはチケット（`#1`）を
  pull request（`!1`）とは別に番号付けします。

どれも転送していないと、Branvil の操作はすべて「Branvil に届きません」で失敗し、
**設定 → 検証** は「Branvil に届きません」と報告します。

## Branvil を準備する

Branvil の README に従って、次を行います。

1. Hub のマシンで Branvil を動かす。ほかのマシンでは、GuildBotics が動いていればほかに要るものはない。
   `branvil init` は Branvil のオーナー（Branvil を使うただ一人の人）を作ります。これが
   GuildBotics の人間のメンバーです。オーナーは何でもでき、マージし、Branvil の GUI を使います。
2. メンバーが作業するリポジトリ（`acme/demo`。チケットに付けられるラベルも設定する）を
   Branvil の GUI で作る。ワークスペース（`acme`）はリポジトリをまとめ、ボードを 1 つ持ちます。
   チケットはリポジトリが持ち、マージで閉じられるのは pull request と同じリポジトリの
   チケットだけです。ワークスペースは GuildBotics からも作れます（下記）。

メンバーとそのトークンは Branvil で作りません。GuildBotics が作ります。

## GuildBotics を Branvil に接続する

Branvil が動いている Hub のマシンで、デスクトップアプリの **設定 → 連携** から:

1. コードホストとチケットに **Branvil** を選ぶ。どちらも 1 つの Branvil を、1 つの
   **ベース URL**（空欄なら `http://127.0.0.1:7878`）で使います。
2. **Branvil に接続** を押す。ブラウザで Branvil の同意画面（「GuildBotics に Branvil の管理を
   許可しますか?」）が開くので、**許可** を選ぶ。（ブラウザにまだ Branvil のセッションがなければ、
   このマシンで `branvil open` を実行するよう案内が出ます。入ると同意画面に戻ります。）画面に
   「Branvil にオーナーとして接続しています」と出ます。
3. メンバーが作業する **ワークスペース** を選ぶか、その場で作る。メンバーはその
   リポジトリにだけ書き込み、そのボードからチケットを取ります。読み取りは制限しません。

Branvil は GuildBotics に `app:GuildBotics` というラベルのオーナーのトークンを発行します。
GuildBotics はこれをワークスペースのシークレット `BRANVIL_ADMIN_TOKEN` として OS キーチェーンに
保存し、そのマシンの自分自身のプロセス（設定画面と検証）でだけ使います。メンバー・コマンド・
隔離環境には渡しません。取り消すには、Branvil の **設定 → メンバー → オーナー → トークン** で
そのトークンを削除し、もう一度接続します。

ほかのマシンでは接続し直す必要はありません。Hub のマシンからシークレットを送り、
**このマシンの認証情報** で取得します（管理トークンもメンバーのトークンも同じです）。
どのマシンも発行し直しません。

接続の横の **Branvil を開く** は、どのマシンからでも Branvil の GUI をシステムのブラウザで
開きます。GuildBotics が管理トークンで Branvil に一度だけ使える入口のリンクを求めます。

## メンバーを設定する

**設定 → メンバー → Branvil** で:

- AI メンバーは Branvil のエージェントです。**新しいエージェントを作る**（メンバーの ID の
  名前）か **既存のエージェントを選ぶ** を選びます。メンバーを保存すると、選んでいなければ
  エージェントを作り、トークン（ラベル `guildbotics`。前に発行したものと置き換え）を発行して、
  OS キーチェーンに `{PERSON_ID}_BRANVIL_TOKEN` として保存します。トークンを表示したり入力したり
  することはありません。トークンはメンバーの git のパスワードにもなり、git はエージェントの名前と
  トークンで認証します。以後の保存では、Branvil がエージェントのものとして受け付ける限り
  トークンをそのまま使います。**保存するときにトークンを再発行する** で置き換えられます。
- 人間のメンバーは Branvil のオーナーです。保存したときに、Branvil の名前にオーナーの名前が
  入ります。
- メンバーの **GitHub** タブも表示されます。AI メンバーにそこで GitHub のアカウント
  （マシンユーザーのトークンか GitHub App）があれば、保存のたびに Branvil に渡します。Branvil は
  そのアカウントとしてメンバーの pull request を出します（下記）。
- メンバーのアイコンは、Branvil ではそのエージェント（人間のメンバーならオーナー）の画像に
  なります。保存のたびと、アイコンをアップロードまたは取り込んだときに、GuildBotics が渡します。
  設定するのは GuildBotics だけです。
- AI メンバーを削除すると、そのエージェントの `guildbotics` のトークンを取り消します。
  エージェントは履歴が名前を指すので Branvil に残ります。

AI メンバーの保存と削除は Branvil に届く必要があるため、そのマシンから Branvil に届き、
GuildBotics が接続している必要があります。そうでなければ何も保存・削除せず、画面が理由を
示します。

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

各メンバーの `person.yml` に（メンバーの保存が書きます）

```yaml
account_info:
  branvil_username: aiko
```

と書きます。

**設定 → 検証** の **設定を検証** で、Branvil が応答すること、このマシンが GuildBotics の
接続を持ち Branvil がそれをオーナー（人間のメンバー）のものとして受け付けること、各 AI メンバーの
トークンが本人のものとして受け付けられること、ワークスペースを読み取れることを確かめられます。

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
  GitHub に pull request を開きます。そのため、AI メンバーごとの bot アカウントを、GuildBotics の
  メンバーの **GitHub** タブに設定してください。GuildBotics がそれを Branvil に渡します。GitHub の
  読み取りにも、これらの bot アカウントのどれか 1 つを使うので、人のメンバー自身の GitHub
  トークンは要りません。
- 出した head に対する GitHub のチェックは、pull request のチェックに `github/<name>` として
  ログ付きで並びます。GitHub で（Windows などで）失敗すると pull request は ready にならず、
  メンバーは Branvil で直します。その後の push は自動で GitHub に届きます。
- 承認とマージは GitHub で人が行います。Branvil は GitHub の既定ブランチに追従して pull request を
  マージ済みにするので、巡回はいつもどおりチケットを **完了** に移します。
- チケットには、それに当たる GitHub の Issue を記録できます。出した pull request はその Issue を
  閉じます。
