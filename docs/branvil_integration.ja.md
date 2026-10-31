# Branvil 連携ガイド

Branvil は自分で動かすコードホスト兼ボードです。ワークスペースにまとめた Smart HTTP の Git
リポジトリ、レビューと CI 付きの pull request、ワークスペースのボードに載る各リポジトリの
チケットを持ちます。GuildBotics は `branvil`
provider を通じて、GitHub の代わりにコードホスト（`services.code_hosting_service`）と
チケット（`services.ticket_service`）として使います。チャットはプロジェクトが選んだもの
（Slack など）のままです。

このガイドは、GuildBotics が Branvil に求めるものを説明します。Branvil のインストールと起動、
`branvil` コマンドの正確な使い方は Branvil の README を参照してください。

チケットのメンション判定には、Branvil が本文とコメントに返す `mentions` を使います。
名前は Branvil のメンバーとして本文の保存時に解決されます。対応する Branvil（保存形式22）を
先に導入してください。
巡回は `GET /workspaces/{workspace}/tickets?mentions=me` も読み、全チケットのコメントを
個別に取得せず、担当外の未応答メンションを探します。この作業はチケットへのコメントで応答し、
レーンと担当を変えません。他のメンバーだけを宛先にしたコメントでは担当者を起こさず、
担当者の作業に関わる応答は担当者をメンションします。

人が対話（Branvil のチケットの `interactive`）にしたチケットと、その子孫のチケット
（祖先のどれかが対話のもの。Branvil の `interactive_ancestor`）は、その人がエージェントと
対話で進めます。巡回はどのきっかけ（ready・作業中のレーン、コメント、担当内外のメンション）でも
そのチケットを選びません。そのチケットにリンクした pull request のレビューは続けますが、
メンバー自身が作成者のものは、指摘への対応も、待っていた CI が終わったあとの再開も行いません。

## 全体の構成

```text
Hub のマシン                                ほかのマシン
┌──────────────────────────────┐            ┌──────────────────────────────┐
│ branvil serve                │            │ OpenSSH (OS user service)    │
│   http://127.0.0.1:7878      │◀── SSH ────│   http://127.0.0.1:7878      │
│ GuildBotics                  │            │                              │
└──────────────────────────────┘            └──────────────────────────────┘
```

- Branvil は Hub（GuildBotics が動くマシン）で 1 つだけ動かし、loopback でだけ待ち受けます。
- すべてのマシンが同じベース URL（既定 `http://127.0.0.1:7878`）で Branvil に届きます。
  Hub のマシンでは Branvil そのものです。ほかのマシンでは、下記の
  [SSH 転送サービス](#hub-以外のマシンで-ssh-転送を設定する)を一度設定します。OpenSSH が
  ログイン時に起動し、1 本の接続で loopback のポートを転送します。GuildBotics や Branvil の
  クライアントとは独立して動き、そのマシンのブラウザ、git、Branvil の CLI がこれを使います。
- どのマシンでも同じベース URL であることで、記録した URL がどこでも有効になります。チケットは
  `{base}/{repository}/-/tickets/{n}`、pull request は `{base}/{repository}/-/pulls/{n}` で、
  GuildBotics の Branvil への接続が持つベース URL は 1 つです。リポジトリはチケット（`#1`）を
  pull request（`!1`）とは別に番号付けします。

どれも転送していないと、Branvil の操作はすべて「Branvil に届きません」で失敗し、
**設定 → 検証** は「Branvil に届きません」と報告します。

実行中のコマンドの broker を通した member コマンドは、実行に残っている時間の範囲で Branvil への
接続をやり直します。たとえば持ち時間 300 秒のうち 50 秒が過ぎていれば、Branvil の操作も、
fetch・push の前の `member git` も、残りの 250 秒まで待てます。要求を始め直しても締切は延びず、
接続中の要求も締切で打ち切ります。持ち時間を使い切ると `Unreachable` を返し、未接続が確定していれば
後述の `ServiceUnavailable` としても扱います。

呼び出し側に締切がない経路（スケジューラの巡回や、対話で直接実行した member CLI）は、要求ごとに
300 秒の持ち時間を設けます。5 分は、1 回のサービス要求がワーカーや対話 CLI を占有してよい上限で、
Branvil の再起動にかかる時間の見込みではありません。host がチケットを選ぶ処理や、レーン移動、
ワークフローの状態コメントにも適用します。1 回の接続には通常の HTTP タイムアウトが適用されます。
接続待ちを使い切ると、同じ process の全 client が、その base URL への再接続待ちをやめます。
context の作り直しや別メンバーの巡回でも待ち直しません。member・admin・health のどの client でも
応答を受ければ解除し、失敗した admin・health の確認ではこの印を付けません。
Branvil がすでに受け取ったかもしれない要求は送り直さないので、書き込みが二重に
なることはありません。設定画面と、Branvil が応答するかの確認は待たず、いま応答するかを報告します。

接続できなかったことが確定した場合は、provider 共通の `ServiceUnavailable` を返します。
自動巡回と定期実行はその回の作業を見送り、ワークフローの失敗コメントを残さず、開始済みの実行は
`deferred` と記録します。Activity では情報として表示し、ワーカーの連続エラー数は増減させません。
チャットなどの他の作業は続き、次の巡回で待ち直さずに Branvil を確認し、応答すれば作業を再開します。
見送った定期実行の予定は実行待ちのまま保ち、次のスケジューラの周期で再試行します。
対話で実行したコマンドは従来どおりエラーを返します。認証拒否、未接続と断定できない時間切れ、要求が届いた
可能性のある通信失敗は、見送りとして自動再試行せず、これまでどおり失敗として扱います。

## Branvil を準備する

Branvil の README に従って、次を行います。

1. Hub で Branvil を動かす。ほかのマシンでは、下記の SSH 転送を設定する。
   `branvil init` は Branvil のオーナー（Branvil を使うただ一人の人）を作ります。これが
   GuildBotics の人間のメンバーです。オーナーは何でもでき、マージし、Branvil の GUI を使います。
2. メンバーが作業するリポジトリ（`acme/demo`。チケットに付けられるラベルも設定する）を
   Branvil の GUI で作る。ワークスペース（`acme`）はリポジトリをまとめ、ボードを 1 つ持ちます。
   チケットはリポジトリが持ち、マージで閉じられるのは pull request と同じリポジトリの
   チケットだけです。ワークスペースは GuildBotics からも作れます（下記）。

メンバーとそのトークンは Branvil で作りません。GuildBotics が作ります。

## Hub 以外のマシンで SSH 転送を設定する

Hub 以外の各マシンで、チェックアウトに同梱されたスクリプトを実行します。必要なのは
OpenSSH と OS のサービス管理だけで、Python や GuildBotics の CLI は使いません。
Hub 自体は、利用者の SSH 鍵で接続できれば追加設定はありません。同じサービスで、
[ブラウザ](../README.ja.md#ブラウザから使う)のための GuildBotics サーバーのポート（8765）も
転送します。転送するポートをすべて指定してください。

1. 手動や OS のサービスで `branvil tunnel` を動かしていれば止める。
2. `ssh my-hub` で一度接続し、確認済みのホスト鍵を受け入れる。`my-hub` は自分の SSH 設定の
   別名か `[user@]host` に置き換える。鍵やホスト名は既存の OpenSSH の設定を使う。
   そのホストの項目に **`LocalForward` を書かない**。通常の `ssh my-hub` もサービスと
   ポートを取り合うためである。
3. パスフレーズのない鍵か、ssh-agent に読み込み済みの鍵を使う。パスフレーズなしの
   `~/.ssh/id_ed25519` がよく使われる。サービスは
   `BatchMode=yes` と厳密なホスト鍵確認を使い、入力を求めない。Windows では OpenSSH
   クライアントを有効にし、agent を使うなら OpenSSH Authentication Agent サービスも有効にする。
4. 次のコマンドで登録し、状態を確認する。

macOS（launchd のユーザーエージェント）と Linux（systemd のユーザーサービス）:

```sh
sh scripts/hub-forward.sh install --hub my-hub --port 7878 --port 8765
sh scripts/hub-forward.sh status
# 更新は、必要なすべてのポートを指定して install をもう一度実行する。
sh scripts/hub-forward.sh uninstall
```

Linux のユーザーサービスには、ログインシェルの `SSH_AUTH_SOCK` は渡りません。
`--agent-socket` の指定がなければ agent を使わないため、パスフレーズのない鍵が必要です。
agent を使う場合は、その**固定の絶対パス**を指定します。

```sh
sh scripts/hub-forward.sh install --hub my-hub --port 7878 --port 8765 --agent-socket "$SSH_AUTH_SOCK"
```

スクリプトがサービスの環境変数 `SSH_AUTH_SOCK` に設定します。ログインのたびに socket の
パスが変わる agent では、新しいパスで登録し直すか、固定パスを設定してください。
macOS では、OS が launchd に渡す agent を使います。

Windows（タスクスケジューラー、Windows PowerShell 5.1。管理者権限は不要）:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\hub-forward.ps1 -Action install -Hub my-hub -Port "7878,8765"
powershell -ExecutionPolicy Bypass -File scripts\hub-forward.ps1 -Action status
# 更新は、必要なすべてのポートを指定して install をもう一度実行する。
powershell -ExecutionPolicy Bypass -File scripts\hub-forward.ps1 -Action uninstall
```

実行ポリシーの指定は、この実行に限って署名なしのスクリプトを許可します。タスクはログイン中だけ、
コンソールを表示せずに動きます。タスクの引数に収めた小さな PowerShell のループが、
ssh の失敗時も 15 秒あけて起動し直します。実行時間の上限はありません。タスクを置き換えたり
削除したりすると、その ssh も終了します。macOS の定義は
`~/Library/LaunchAgents/local.hub-forward.plist`（ssh のエラーは
`~/Library/Logs/hub-forward.log`）、Linux の定義は
`${XDG_CONFIG_HOME:-~/.config}/systemd/user/hub-forward.service`（ログは
`journalctl --user -u hub-forward.service`）です。SSH 設定、鍵、known_hosts は変更しません。
サービスはユーザーごとに 1 つです。install は Hub とポート一覧を丸ごと置き換えます。
複数ポートは POSIX で `--port` を繰り返し、PowerShell では `-Port "7878,8765"` と指定します。

転送はすべて `127.0.0.1` だけで待ち受け、Hub の `127.0.0.1` の同じポートにつなぎます。
切断は SSH の keepalive で検出して張り直します。Linux と Windows は終了から 15 秒あけ、
macOS は `ThrottleInterval` により起動が 15 秒に 1 回を超えないよう抑制します。status は OS の
プロセスやタスクと終了結果の確認であり、Branvil 自体の疎通確認ではありません。Windows では
タスクが実行中でも SSH の再試行待ちの場合があります。Branvil も確かめるには、ブラウザで
`http://127.0.0.1:7878` を開いてください。

同じポートの転送サービスを複数動かさないでください。スクリプト自体はアプリの名前や既定ポートを
持ちません。

複数の GuildBotics のワークスペースで 1 つの Branvil を使えます。Branvil への接続と、メンバーごとの
エージェントとトークンは、すべてのワークスペースで共有するメンバーのアイデンティティの側に
置くので、一度接続すればすべてのワークスペースで使えます。メンバーが作業する Branvil の
ワークスペースは、ワークスペースごとに選びます。

## GuildBotics を Branvil に接続する

Hub で、デスクトップアプリ（またはブラウザの同じ画面）の **設定 → 連携** から:

1. コードホストとチケットに **Branvil** を選ぶ。どちらも GuildBotics が接続している
   1 つの Branvil を使います。
2. **Branvil に接続** の横の **ベース URL**（変更しなければ `http://127.0.0.1:7878`）を
   確かめてから、**Branvil に接続** を押す。ブラウザで Branvil の同意画面（「GuildBotics に Branvil の管理を
   許可しますか?」）が開くので、**許可** を選ぶ。（ブラウザにまだ Branvil のセッションがなければ、
   このマシンで `branvil open` を実行するよう案内が出ます。入ると同意画面に戻ります。）画面に
   「Branvil にオーナーとして接続しています」と出ます。接続とそのベース URL はすべての
   ワークスペースで共通なので、Branvil を選んだほかのワークスペースもすでに接続しています。
3. メンバーが作業する **ワークスペース** を選ぶか、その場で作る。メンバーはその
   リポジトリにだけ書き込み、そのボードからチケットを取ります。読み取りは制限しません。

Branvil は GuildBotics に `app:GuildBotics` というラベルのオーナーのトークンを発行します。
GuildBotics はこれを `BRANVIL_ADMIN_TOKEN` として、すべてのワークスペースで共有する
アイデンティティのストア（OS キーチェーン）に保存し、ベース URL をその横の
`~/.guildbotics/identity/.guildbotics/config/connections/branvil.yml` に保存します。
トークンはそのマシンの自分自身のプロセス（設定画面と検証）でだけ使い、メンバー・コマンド・
隔離環境には渡しません。取り消すには、Branvil の **設定 → メンバー → オーナー → トークン** で
そのトークンを削除し、もう一度接続します。

接続の横の **Branvil を開く** は、Branvil の GUI をブラウザで開きます。GuildBotics が管理トークンで Branvil に一度だけ使える入口のリンクを求めます。
サイドメニューの **設定** の下にある **Branvil** も、外部リンクのアイコンから同じ方法で開きます。
保存済みのコードホスティングかチケットのどちらかが Branvil のときに表示され、連携設定を保存すると
その場で表示が切り替わります。開けなかった場合は、項目の下に理由を表示します。

## メンバーを設定する

**設定 → メンバー → Branvil** で:

- AI メンバーは Branvil のエージェントです。**新しいエージェントを作る**（メンバーの ID の
  名前）か **既存のエージェントを選ぶ** を選びます。メンバーを保存すると、選んでいなければ
  エージェントを作り、トークン（ラベル `guildbotics`）を発行して、
  OS キーチェーンに `{PERSON_ID}_BRANVIL_TOKEN`（その ID を `{PERSON_ID}_BRANVIL_TOKEN_ID`）として
  保存します。置き換えた前のトークンは、保存が終わってから取り消します。保存できなかったときは、
  そのために発行したトークンを取り消し、前のトークンをそのまま使います。トークンを表示したり入力したり
  することはありません。トークンはメンバーの git のパスワードにもなり、git はエージェントの名前と
  トークンで認証します。エージェントとトークンはすべてのワークスペースでそのメンバーのもので、
  どのワークスペースで保存しても、Branvil がエージェントのものとして受け付ける限り
  トークンをそのまま使います。**保存するときにトークンを再発行する** で置き換えられます。
- 人間のメンバーは Branvil のオーナーです。保存したときに、Branvil の名前にオーナーの名前が
  入ります。
- メンバーの **GitHub** タブも表示されます。AI メンバーにそこで GitHub のアカウント
  （マシンユーザーのトークンか GitHub App）があれば、保存のたびに Branvil に渡します。Branvil は
  そのアカウントとしてメンバーの pull request を出します（下記）。
- メンバーのアイコンは、Branvil ではそのエージェント（人間のメンバーならオーナー）の画像に
  なります。保存のたびと、アイコンをアップロードまたは取り込んだときに、GuildBotics が渡します。
  設定するのは GuildBotics だけです。
- メンバーをワークスペースから外しても（**このワークスペースから外す**）、何も取り消しません。
  ほかのワークスペースはそのトークンを使い続けます。AI メンバーを削除すると（**削除**）、
  そのエージェントの `guildbotics` のトークンを取り消します。エージェントは履歴が名前を指すので
  Branvil に残ります。

AI メンバーの保存と削除は Branvil に届く必要があるため、Branvil に届き、
GuildBotics が接続している必要があります。そうでなければ何も保存・削除せず、画面が理由を
示します。

GUI を使わない場合、同じ設定は `team/project.yml` に

```yaml
services:
  code_hosting_service:
    name: branvil
    workspace: acme
  ticket_service:
    name: branvil
    workspace: acme
```

各メンバーの `identity.yml` に（メンバーの保存が書きます）

```yaml
account_info:
  branvil_username: aiko
```

と書きます。

**設定 → 検証** の **設定を検証** で、Branvil が応答すること、GuildBotics が
接続を持ち Branvil がそれをオーナー（人間のメンバー）のものとして受け付けること、各 AI メンバーの
トークンが本人のものとして受け付けられること、ワークスペースを読み取れることを確かめられます。

## 自分のチェックアウトで Branvil を使う

人のメンバー（あなた）は Branvil のオーナーです。Hub では、自分の
チェックアウトの `origin` を Branvil にできます。Branvil の git はメンバーの名前とトークンを
求めます。GuildBotics があなたの名前とトークンを git に渡すので、git がユーザー名やパスワードを
聞いてくることはありません:

1. 接続した状態で **設定 → 連携 → Branvil** を開くと、**このマシンのあなたの git** に
   追加する行が出ます。コピーします。
2. `git config --global --edit` でグローバルな git の設定を開き、**最後に**貼り付けます:

   ```ini
   [credential "http://127.0.0.1:7878"]
   	helper =
   	helper = "!'/Users/you/.guildbotics/bin/guildbotics' git-credential"
   ```

   空の `helper =` があると、git は Branvil の資格情報を、それより前に設定された
   helper（OS のキーチェーン、Git Credential Manager）に尋ねません。この行より後に
   設定した helper にはトークンの保存が頼まれてしまうので、最後に置いてください。Windows では
   インストーラが CLI を `PATH` に入れるので、helper は `!guildbotics git-credential` です。
3. これで `git fetch origin` と `git push origin <branch>` が、何も聞かれずに通ります。

最初に求められたとき、GuildBotics は接続を使って、そのマシンのオーナーのトークンを発行します。
トークンのラベルは `git:<デバイス名> (<デバイス ID の末尾 8 文字>)` で、そのマシンの OS の
キーチェーンにだけ保存します。URL、git の設定、シェルの履歴、ワークスペースのファイルには書きません。
Branvil の **設定 → メンバー → オーナー → トークン** で取り消せます。Branvil がそのトークンを
受け付けなくなると、次に求められたときに代わりのトークンを発行し、そのマシンのほかの `git:` の
トークンを取り消します。

このトークンはオーナーのトークンそのもので、GuildBotics がすでに持っている接続と同じだけの権限が
あります。取り消しても、GuildBotics が接続している間は次に求められたときに新しく発行します。

helper がトークンを渡せないとき（Branvil が応答しない、接続のトークンがない、キーチェーンを
使えない）は、git はそこで止まり、パスワードを聞かずに理由を表示します。GuildBotics が remote の
Branvil にそもそも接続していないときは、helper がそのことを表示し、git はユーザー名とパスワードの
入力に切り替わります。入力せずに接続してください。メンバーの git はこの helper を使いません。
GuildBotics はメンバーの git にエージェントのトークンを渡し、設定された helper をすべて外します。

## メンバーの動き

チケットのワークフローと pull request の巡回は、GitHub と同じように動きます。

- チケットの担当者は 1 人まで（そのチケットに取り組むエージェント）です。別のメンバーに割り当てると
  担当者が入れ替わるので、2 人のメンバーが同じチケットを取り上げることはありません。
- チケットは必ずワークスペースのボードのいずれかのレーンにあります。メンバーが作成したチケットは
  **新規** に入り、`member ticket create --no-add-to-board` は拒否されます。
- ワークスペースのボードの **着手可** レーンでメンバーに割り当てられたチケットを取り上げ、
  **作業中** に移します。**作業中** のチケットは、メンバーが最後に発言してから誰かが発言
  するか、未回答のメンションがあれば、もう一度取り上げます。リンクされた pull request が開いていても
  マージ済みでもチケット上の会話を拾い、開いた既存の pull request を再利用します。
  メンバー自身の失敗・レート制限の状態が最後なら、先にチケットを保留します。
  未回答の会話がなければ、開いた pull request は PR の巡回で追い、マージ済みならチケットを
  **完了** に移します。PR が無く、割り当て以降に発言がなければ作業を再開します。
- メンバーの pull request は、メンバーが答えていない発言があると取り上げます。メンバーが
  レビューする pull request は、レビューを依頼されたとき、前回のレビューのあとに新しい
  コミットが来たとき、答えていない発言があるときに取り上げます。依頼によらない自動の
  再レビューは、レビューした head が 3 つに達したあと、そのメンバーがまだレビューしていない
  head が積まれた時点で止まります。host の ticket selector が候補を選ぶ時点で PR を Draft にし、
  Activity に記録して、PR とレビュワーの `notify_work_stops: true` のチャットチャンネルに告知します。
  レビュー済み head のまま発言・thread 返信に応える run は上限後も起動します（force push で
  戻った head や、回数を数え直す前にレビューした head も含みます）。明示的なレビュー依頼は
  上限の対象外です。続けるには Ready for review に戻してください。どのレビュワーも回数を数え直します。
- メンバーは実行の中で CI を待ちません。終わっていないチェックだけが残ると、pull request に
  待ちの通知を残して実行を終え、チェックが終わるか新しいコミットが積まれると巡回が再開します。
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
- 上流の GitHub の Dependabot アラートは Branvil 経由で読めます
  （`member repo-info read --resource dependency_alerts`、`repository/security_alerts`）。
  Branvil は GitHub の読み取りに使うアカウントで読むので、そのアカウントに
  **Dependabot alerts: Read-only** と対象リポジトリへのアクセスが必要です（GitHub App では、追加した
  権限をインストール先で承認します）。上流の無いリポジトリにはアラートが無く、読み取りは未対応として
  拒否されます。

### チケット作成時の状態

作成時の対話指定・担当者・レーン・親・依存先は1回のリクエストで送り、
サーバーが同じトランザクションで確定する。巡回が設定途中のチケットを拾うことはない。
対話チケットとその子孫は巡回の対象外となる。レーンを省略した場合、親か依存先が
あれば `ready`、どちらもなければ `new` になる。
