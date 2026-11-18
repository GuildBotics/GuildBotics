# Branvil 連携ガイド

Branvil は自分で動かすコードホスト兼ボードです。ワークスペースにまとめた Smart HTTP の Git
リポジトリ、レビューと CI 付きの pull request、ワークスペースのボードに載る各リポジトリの
チケットを持ちます。GuildBotics は `branvil`
provider を通じて、GitHub の代わりにコードホスト（`services.code_hosting_service`）と
チケット（`services.ticket_service`）として使います。チャットはプロジェクトが選んだもの
（Slack など）のままです。

このガイドは、GuildBotics が Branvil に求めるものを説明します。サーバーがこれを動かします。
ランナーとデータの保守は `guildbotics runner` と `guildbotics server hub-data` です
（[CLI リファレンス](cli_reference.md)を参照してください）。

チケットのメンション判定には、Branvil が本文とコメントに返す `mentions` を使います。
名前は Branvil のメンバーとして本文の保存時に解決されます。
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
│ guildbotics server start     │            │ OpenSSH (OS user service)    │
│   http://127.0.0.1:7878      │◀── SSH ────│   http://127.0.0.1:7878      │
│ GuildBoticsとBranvil         │            │                              │
└──────────────────────────────┘            └──────────────────────────────┘
```

- Branvil は Hub（GuildBotics が動くマシン）で 1 つだけ動かし、loopback でだけ待ち受けます。
- すべてのマシンが同じベース URL（既定 `http://127.0.0.1:7878`）で Branvil に届きます。
  Hub のマシンでは Branvil そのものです。ほかのマシンでは、下記の
  [SSH 転送サービス](#hub-以外のマシンで-ssh-転送を設定する)を一度設定します。OpenSSH が
  ログイン時に起動し、1 本の接続で loopback のポートを転送します。GuildBotics や Branvil の
  クライアントとは独立して動き、そのマシンのブラウザ、git、Branvil の CLI がこれを使います。
- どのマシンでも同じベース URL であることで、記録した URL がどこでも有効になります。チケットは
  `{base}/{repository}/-/tickets/{n}`、pull request は `{base}/{repository}/-/pulls/{n}` です。
  リポジトリはチケット（`#1`）を pull request（`!1`）とは別に番号付けします。

GuildBotics のサーバーは、このサービスを同じプロセスの中で直接呼び出します。サーバーの停止中など
組み込みのサービスが動いていないときは、チケットと pull request の操作はすべて失敗し、
**設定 → 検証** はワークスペースを読み取れないと報告します。

この失敗は `ServiceUnavailable` として記録します。
自動巡回と定期実行はその回の作業を見送り、ワークフローの失敗コメントを残さず、開始済みの実行は
`deferred` と記録します。Activity では情報として表示し、ワーカーの連続エラー数は増減させません。
チャットなどの他の作業は続き、次の巡回でもう一度確かめ、動いていれば作業を再開します。
見送った定期実行の予定は実行待ちのまま保ち、次のスケジューラの周期で再試行します。
対話で実行したコマンドは従来どおりエラーを返します。それ以外の失敗は、見送りとして自動再試行せず、
これまでどおり失敗として扱います。

## Branvil を準備する

Hubで[GuildBoticsのインストール手順](../README.ja.md#インストール)に従って、次を行います。

1. サーバーを起動します。初回の起動で、コードホストのデータ（`BRANVIL_DATA_DIR`、
   既定は `~/.guildbotics/data/code-host`）と、画面を使ってマージを行う人のアカウントを作ります。
   ほかのマシンでは下記のSSH転送を設定します。
2. メンバーが作業するリポジトリ（`acme/demo`。チケットに付けられるラベルも設定する）を
   Branvil の GUI で作る。ワークスペース（`acme`）はリポジトリをまとめ、ボードを 1 つ持ちます。
   チケットはリポジトリが持ち、マージで閉じられるのは pull request と同じリポジトリの
   チケットだけです。ワークスペースは GuildBotics からも作れます（下記）。

メンバーは画面で作りません。各 AI メンバーは GuildBotics が作ります（[メンバー](#メンバー)）。

## Windows クライアントの導入

先に古い GuildBotics Desktop アプリをアンインストールしてください。人のチェックアウトと SSH 転送は残します。

1. 下記の手順に従い `scripts/hub-forward.ps1` で SSH 転送を設定します。
2. Hub で `guildbotics open --print` を実行し、入口リンクを Windows のブラウザで開きます。
3. 設定の「このマシン」にある Windows クライアントから `guildbotics.exe` をダウンロードします。画面のセッションで Hub の現在の版を取得できます。
4. PowerShell で `./guildbotics.exe install` を実行します。`~/.guildbotics/bin` に managed CLI を置き、PATH を設定し、AI ツールごとのスキルの状態を表示します。人の git の credential helper は、このマシンの managed CLI を転送先の Hub の origin に設定します。PATH 反映後は新しい端末を開き、先に見つかる別の CLI の警告があれば解消してください。
5. クライアント専用ビルドは、登録先の実在する `.guildbotics`、identity root、キーチェーンの項目を一覧にします。`DELETE` と入力した場合だけ消し、Enter または EOF なら残します。一般ファイルとチェックアウト、人の git のトークン（`GuildBotics/branvil-git`）、同意トークン（`guildbotics-server`）、端末 ID、managed CLI、SSH 転送は残します。
6. `guildbotics connect` を実行し、ブラウザでこのマシンを許可します。スキルの確認・置き直しには `guildbotics install` を使います。編集したスキルは残します。以後の CLI 呼び出しで Hub と同じ版に自動更新されます。

## Hub 以外のマシンで SSH 転送を設定する

Hub 以外の各マシンで、チェックアウトに同梱されたスクリプトを実行します。必要なのは
OpenSSH と OS のサービス管理だけで、Python や GuildBotics の CLI は使いません。
Hub 自体は、利用者の SSH 鍵で接続できれば追加設定はありません。
[ブラウザ](../README.ja.md#ブラウザから使う)と Branvil は共通のポート 7878 を使うので、
7878 だけを転送してください。

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
sh scripts/hub-forward.sh install --hub my-hub --port 7878
sh scripts/hub-forward.sh status
# 更新は、必要なすべてのポートを指定して install をもう一度実行する。
sh scripts/hub-forward.sh uninstall
```

Linux のユーザーサービスには、ログインシェルの `SSH_AUTH_SOCK` は渡りません。
`--agent-socket` の指定がなければ agent を使わないため、パスフレーズのない鍵が必要です。
agent を使う場合は、その**固定の絶対パス**を指定します。

```sh
sh scripts/hub-forward.sh install --hub my-hub --port 7878 --agent-socket "$SSH_AUTH_SOCK"
```

スクリプトがサービスの環境変数 `SSH_AUTH_SOCK` に設定します。ログインのたびに socket の
パスが変わる agent では、新しいパスで登録し直すか、固定パスを設定してください。
macOS では、OS が launchd に渡す agent を使います。

Windows（タスクスケジューラー、Windows PowerShell 5.1。管理者権限は不要）:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\hub-forward.ps1 -Action install -Hub my-hub -Port "7878"
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
統合サーバーは POSIX で `--port 7878`、PowerShell では `-Port "7878"` と指定します。

転送はすべて `127.0.0.1` だけで待ち受け、Hub の `127.0.0.1` の同じポートにつなぎます。
切断は SSH の keepalive で検出して張り直します。Linux と Windows は終了から 15 秒あけ、
macOS は `ThrottleInterval` により起動が 15 秒に 1 回を超えないよう抑制します。status は OS の
プロセスやタスクと終了結果の確認であり、Branvil 自体の疎通確認ではありません。Windows では
タスクが実行中でも SSH の再試行待ちの場合があります。Branvil も確かめるには、ブラウザで
`http://127.0.0.1:7878` を開いてください。

同じポートの転送サービスを複数動かさないでください。スクリプト自体はアプリの名前や既定ポートを
持ちません。

複数の GuildBotics のワークスペースで 1 つの Branvil を使えます。メンバーはすべてのワークスペースで
共通です。メンバーが作業するコードホストのワークスペースは、GuildBotics のワークスペースごとに選びます。

サーバーのすべての画面で、入口とブラウザセッションを共用します。
`guildbotics open` と `guildbotics open --branvil` は `/-/guildbotics/-/enter` の
入口リンクを表示し、入った後の画面だけが異なります。

## コードホストとチケットを設定する

Hub の画面の **設定 → 連携** から:

1. コードホストとチケットに **Branvil** を選ぶ。どちらも Hub の同じサービスを使います。
2. メンバーが作業する **ワークスペース** を選ぶか、その場で作る。メンバーはその
   リポジトリにだけ書き込み、そのボードからチケットを取ります。読み取りは制限しません。

接続の手順はありません。サーバーはオーナーとして、また各 AI メンバーとして、同じプロセスの中の
サービスを直接呼び出します。そのためのトークンを保存せず、メンバー・コマンド・隔離環境にも渡しません。

サイドメニューの **設定** の下の項目から、同じブラウザセッションのままコードホストの画面に移れます。
サーバーがコードホストを組み込んで動いているときに表示されます。

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

と書きます。

**設定 → 検証** の **設定を検証** で、選んだワークスペースがあることを確かめられます。

## メンバー

AI メンバーを追加したあとに、コードホスト側で行う作業はありません。

- AI メンバーは、メンバーの ID を名前、表示名を表示名として自動で作られます。作られるのは、
  サーバーの起動、ワークスペースの切り替え、メンバーの保存のときと、どのメンバーの操作よりも前です。
  そのため、まだ一度も動いていないメンバーにも、ほかのメンバーやオーナーがチケットを割り当てられます。名前がメンバーの ID と同じエージェントがすでにあれば、
  それを使います。同じ名前のメンバーがエージェントでなければ、操作を拒否します。
- 人間のメンバーはオーナー（サーバーの初回の起動で作られた人）です。
- メンバーが git で push・fetch する実行では、サーバーがそのメンバーのトークン（ラベル
  `guildbotics-git`）を発行し、メンバーの ID とともに git に渡します。実行が終わってサービスを
  手放すときに取り消します。トークンはどのストアにも保存しません。
- メンバーをワークスペースから外しても、削除しても、コードホスト側のメンバーは残ります。
  履歴がその名前を指すためです。

## 自分のチェックアウトで Branvil を使う

人のメンバー（あなた）は Branvil のオーナーです。Hub では、自分の
チェックアウトの `origin` を Branvil にできます。Branvil の git はメンバーの名前とトークンを
求めます。GuildBotics があなたの名前とトークンを git に渡すので、git がユーザー名やパスワードを
聞いてくることはありません:

1. **設定 → 連携** のコードホストかチケットで、ワークスペースの選択の上に
   **このマシンのあなたの git** に追加する行が出ます。コピーします。
2. `git config --global --edit` でグローバルな git の設定を開き、**最後に**貼り付けます:

   ```ini
   [credential "http://127.0.0.1:7878"]
   	helper =
   	helper = "!'/Users/you/.guildbotics/bin/guildbotics' git-credential"
   ```

   空の `helper =` があると、git は Branvil の資格情報を、それより前に設定された
   helper（OS のキーチェーン、Git Credential Manager）に尋ねません。この行より後に
   設定した helper にはトークンの保存が頼まれてしまうので、最後に置いてください。
3. これで `git fetch origin` と `git push origin <branch>` が、何も聞かれずに通ります。

最初に求められたとき、helper は起動中の GuildBotics サーバーに、その端末用のオーナーのトークンの発行を頼みます。
有効な手元のトークンは取得元の接続先で直接検証して再利用し、
GuildBotics API には問い合わせません。新しいトークンの発行にはサーバーの起動が必要です。
トークンのラベルは `git:<デバイス名> (<デバイス ID の末尾 8 文字>)` で、そのマシンの OS の
キーチェーンにだけ保存します。URL、git の設定、シェルの履歴、ワークスペースのファイルには書きません。
Branvil の **設定 → メンバー → オーナー → トークン** で取り消せます。Branvil がそのトークンを
受け付けなくなると、次に求められたときに代わりのトークンを発行し、そのマシンのほかの `git:` の
トークンを取り消します。

このトークンはオーナーのトークンそのもので、オーナーと同じ権限があります。取り消しても、
次に求められたときにサーバーが新しく発行します。

helper がトークンを渡せないとき（サーバーや組み込みのサービスが動いていない、キーチェーンを使えない）は、
git はそこで止まり、パスワードを聞かずに理由を表示します。remote がこのマシンの GuildBotics の
サービスでないときは、helper がそのことを表示し、git はユーザー名とパスワードの入力に切り替わります。
メンバーの git はこの helper を使いません。GuildBotics はメンバーの git に実行ごとのトークン
（[メンバー](#メンバー)）を渡し、設定された helper をすべて外します。

## メンバーの動き

チケットのワークフローとpull requestの巡回は、Branvilのボードと会話を確認します。

- チケットの担当者は 1 人まで（そのチケットに取り組むエージェント）です。別のメンバーに割り当てると
  担当者が入れ替わるので、2 人のメンバーが同じチケットを取り上げることはありません。
- チケットは必ずワークスペースのボードのいずれかのレーンにあります。メンバーが作成したチケットは
  **新規** に入ります。
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
  GitHub にpull requestを開きます。対応するGitHubアカウントはBranvilのメンバー設定で
  登録します。[上流の設定](../crates/branvil/README.md#upstream-github)
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
