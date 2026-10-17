# Branvil 連携ガイド

Branvil は自分で動かすコードホスト兼ボードです。Smart HTTP の Git リポジトリ、レビューと CI
付きの pull request、ボードに載るチケットのトラッカーを持ちます。GuildBotics は `branvil`
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
  `{base}/{tracker}/-/tickets/{KEY}`、pull request は `{base}/{repository}/-/pulls/{n}` で、
  共有するプロジェクト設定の `base_url` は 1 つです。

トンネルが切れていると、Branvil の操作はすべて「Branvil に届きません」で失敗し、
**設定 → 検証** は「Branvil に届きません」と報告します。

## Branvil を準備する

Branvil の README に従って、次を行います。

1. Hub のマシンで Branvil を、GuildBotics を動かすほかのマシンでトンネルを動かす。
2. メンバーが書き込む名前空間（例 `acme`）と、その中のリポジトリ（`acme/demo`）、チケットの
   トラッカー（`acme/demo-tickets`。マージでチケットを閉じられるよう、リポジトリを `acme/demo`
   にする）、ボード（`acme/board`）を作る。
3. GuildBotics の人間のメンバーは Branvil のオーナー（`branvil init` が作るただ一人の人）
   です。オーナーはすべてのロールを持ち、マージし、サインインせずに Branvil の GUI を使います
   （Hub マシンで `branvil open`）。AI メンバーごとに agent のメンバーを作り、名前空間に
   `write` を付与する。
4. agent のメンバーごとにトークンを発行する。トークンは一度だけ表示されます。

## GuildBotics を設定する

デスクトップアプリの **設定 → 連携** で、コードホストとチケットに **Branvil** を選び、次を
入力します。

- **ベース URL**: Branvil に届く URL（空欄なら `http://127.0.0.1:7878`）
- **名前空間**: メンバーが書き込む名前空間（`acme`）。メンバーはその直下のリポジトリと
  トラッカーにだけ書き込みます。読み取りは制限しません
- **ボード**（チケットのみ）: チケットを取るボード（`acme/board`）。ボードが無いと、どの
  チケットもメンバーの仕事として選ばれません

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
    owner: acme
  ticket_service:
    name: branvil
    base_url: http://127.0.0.1:7878
    owner: acme
    board: acme/board
```

各メンバーの `person.yml` に

```yaml
account_info:
  branvil_username: aiko
```

と書きます。

**設定 → 検証** の **設定を検証** で、Branvil が応答すること、各 AI メンバーのトークンが
本人のものとして受け付けられること、ボードを読み取れることを確かめられます。

## メンバーの動き

チケットのワークフローと pull request の巡回は、GitHub と同じように動きます。

- ボードの **Ready** レーンでメンバーに割り当てられたチケットを取り上げ、**In progress** に
  移します。**In progress** のチケットは、メンバーが最後に発言してから誰かが発言すると、
  もう一度取り上げます。チケットを参照する pull request ができると、巡回はその pull request を
  追い、マージでチケットは **Done** に移ります。
- メンバーの pull request は、メンバーが答えていない発言があると取り上げます。メンバーが
  レビューする pull request は、レビューを依頼されたとき、前回のレビューのあとに新しい
  コミットが来たとき、答えていない発言があるときに取り上げます。依頼によらない自動の
  再レビューは 3 回で止まり、巡回は pull request を Draft にしてその旨を書き込みます。続けるには
  Ready for review に戻してください。
- Draft の pull request には手を出しません。Draft を Ready for review に戻すのはオーナーだけ
  です。
- 失敗やレート制限で終わった実行は、チケットや pull request に状態の通知を残します。Branvil は
  それを通知として表示し、誰かが動くまでその項目を巡回から外します。
