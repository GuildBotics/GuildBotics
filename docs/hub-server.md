# macOS Hubの起動と更新

Hubのチェックアウトで、オーナーがmacOSのデスクトップにログインしてから実行します。Git、リポジトリの `rust-toolchain.toml` が指定するRust、Node.js 24、npmを `PATH` に入れておきます。コードホスト（リポジトリ、チケット、プルリクエスト、CI）のデータは `BRANVIL_DATA_DIR` の絶対パスに置き、初回の起動時に作られます。既存のHubを切り替える場合は、これまでの方法でGuildBoticsとBranvilを停止し、両方の旧LaunchAgentの登録を解除してから、既存のBranvilデータを指定します。

## 署名の識別を用意する

Hubは、ビルドした実行ファイルにオーナーのApple Developmentの証明書で署名し、LaunchAgentはそのコピーを固定のパス（`builds/current`）から起動します。macOSはキーチェーンの許可を証明書のTeam IDに、「書類」フォルダの許可を実行ファイルのパスと署名に結び付けるので、同じ識別で署名した更新では確認が出ません。

1. Xcodeの設定の「Apple Accounts」でApple IDを追加します。無料のApple ID（Personal Team）で足ります。
2. Apple IDのチームを選び、「Manage Certificates…」の「＋」から「Apple Development」を作ります。
3. 次を実行し、「Valid identities only」の下の `Apple Development: …` の行にある40桁の16進数（SHA-1）を控えます。

   ```sh
   security find-identity -v -p codesigning
   ```

   証明書が「Valid identities only」に出ない場合は、Appleの中間証明書（[Apple Worldwide Developer Relations Certification Authority G3](https://www.apple.com/certificateauthority/AppleWWDRCAG3.cer)）をダウンロードし、ダブルクリックしてログインキーチェーンへ追加します。

`codesign` が初めてこの証明書の鍵を使うとき、「codesignがキーチェーンに含まれるキーへアクセスしようとしています」と確認が出ます。ログインのパスワードを入力して「常に許可」を選びます。

Apple Developmentの証明書の有効期限は1年です。期限が切れると `update` は署名の前に止まります。Xcodeで同じApple IDの新しい証明書を作り、そのSHA-1を `~/Library/Application Support/GuildBotics/Hub/signing-identity` に書き込みます。次に新しいコミットへ `update` するときから、新しい証明書で署名します。署名の要件は証明書の名前とTeam IDで決まるので、同じApple IDの証明書であれば確認は出ません。別のチームの証明書に替えると確認が出るため、`update` は止まります。チームを替えるときは `uninstall` してから、新しい識別で `install` し直します。

## インストール

Hubのチェックアウトは、開発用のチェックアウトとは別にcloneします。linked worktreeにすると、gitのデータ、設定、hookを開発用のチェックアウトと共有し、開発側での削除、設定やhookの変更、worktreeの整理が自動更新に及びます。

`install` と後述の `auto-install` は、開発用のvenvなどを有効にしていないシェル（VS Codeのターミナルではなくターミナル.appなど）で実行します。そのシェルの `PATH` がLaunchAgentに記録されます。

```sh
export BRANVIL_DATA_DIR="/absolute/path/to/code-host-data"
export GUILDBOTICS_HUB_SIGNING_IDENTITY="<控えたSHA-1>"
scripts/hub-server.sh install
scripts/hub-server.sh status
```

`install` はチェックアウトのコミット済みの `HEAD` から両方の画面とRustの実行ファイルを作ります。LaunchAgent `local.guildbotics` がこの実行ファイルの `server start` を直接起動し、1つのプロセスが1つのポート（既定7878）で GuildBotics と Branvil を配ります。ログイン時と異常終了後にも起動します。正常に停止したあとは、次回のログインか `scripts/hub-server.sh restart` で起動します。

`install` は署名の識別を `signing-identity` に記録し、`update` は同じ識別で署名します。識別がキーチェーンに見つからない場合、署名に失敗した場合、署名にTeam IDがない場合（自己署名の証明書など）は、ビルドを切り替えずに止まります。登録時の `PATH` と `BRANVIL_DATA_DIR` をLaunchAgentが使います。GuildBoticsのデータは既定で `~/.guildbotics`、ポートは7878です。画面は `http://127.0.0.1:7878/-/guildbotics/` で配り、Branvil を設定済みなら `/` は Branvil の画面、未設定なら GuildBotics の画面へ案内します。更新後は `guildbotics open` でブラウザに入り直してください。変更する場合は登録前に絶対パスの `GUILDBOTICS_HOME` または `GUILDBOTICS_SERVER_PORT` を設定します。同じ設定でCLIを実行してください。起動確認後に `guildbotics install` がCLIとスキルを配置します。サーバーのログは `~/Library/Logs/guildbotics.log` で確認できます。

インストール後の最初の起動時、macOSの確認が出たらオーナーが回答してください。スクリプトは最大20分間、起動を待ちます。同じ識別で署名した更新では、これらの確認は出ません。

- **キーチェーンへのアクセス**：GuildBoticsのSecretを暗号化・復号する端末共通の鍵（サービス名 `GuildBotics`、アカウント名 `agent-environment-credentials`）への許可です。「常に許可」を選ぶと、同じTeam IDで署名したビルドには再び確認が出ません。`guildbotics connect` が保存する `guildbotics-server` の項目はCLIの接続用トークンで、この暗号化の鍵とは別です。Branvilの秘密はデータディレクトリにあり、Branvil用の鍵の確認がもう1回出るわけではありません。
- **「書類」フォルダへのアクセス**：macOSのプライバシー保護（TCC）の確認です。「書類」の下にワークスペースや明示的な許可先を置いた場合に必要です。キーチェーンとは別に許可してください。

```sh
scripts/hub-server.sh restart    # GuildBoticsの作業とBranvilのCIを待ってから再起動
scripts/hub-server.sh uninstall  # 停止してLaunchAgentの登録を解除
```

`uninstall` はサーバーのデータ、ビルド、バックアップを残します。巡回やチャット受信を起動時に開始するかどうかは、GuildBoticsのサービス設定で指定します。

### ビルドとバックアップを別のディスクに置く

更新のたびに大きく書き換わるビルドのソース、Cargoの `target`、バックアップ、予行の写しは、`GUILDBOTICS_HUB_STORAGE` の絶対パスの下の `Hub`（Hubの写しは `Hub-<名前>`）へ移せます。指定しなければ、これまでどおり `~/Library/Application Support/GuildBotics/Hub` を使います。LaunchAgentが起動する `builds/current`、更新途中のコピー、署名の識別、ロックは、macOSの許可を保つために元の場所に残ります。動いているビルドはこの置き場のソースを読むので、ログイン時にディスクがマウントされている必要があります。リンクを通るパスは指定しないでください。

値は `install` のときにLaunchAgentへ記録し、`update` はその値を使います。変えるときは登録を解除してから、新しい値でインストールし直します。

```sh
scripts/hub-server.sh uninstall
export BRANVIL_DATA_DIR="/absolute/path/to/code-host-data"
export GUILDBOTICS_HUB_SIGNING_IDENTITY="<控えたSHA-1>"
export GUILDBOTICS_HUB_STORAGE="/Volumes/<ディスク名>/guildbotics-hub"
scripts/hub-server.sh install
```

`GUILDBOTICS_HOME` などを変えていた場合は、それも同じ値を設定します。元の場所の `builds/<commit>/`、`target`、`backups/` は自動では消しません。新しい置き場で動くことを確かめてから整理してください。

## CI のキャッシュ容量

キャッシュはランナーごとに保存します。既定は 16 GiB です。Branvil の設定 → CI で、共通の上限またはランナー別の上限を MiB 単位で指定できます。以前に保存した設定値は、そのランナーの上限として引き続き使われます。

GuildBotics の CI では、ランナーが受け持つ各ジョブの最新世代と、そこからマージ待ちになる保留イメージが収まるように設定します。各イメージの `.branvil-data` にある実データ量の合計を見積もり、ジョブ実行中のディスクと Hub のバックアップ用の空きも確認します。例えば最新世代の合計が 22 GiB、同時に残る保留が 8 GiB なら、32 GiB（32768 MiB）を目安にします。[キャッシュの容量と追い出し順](../crates/branvil/docs/ci.md#dependency-cache)

## 手動更新

Hubのチェックアウトで、オーナーがBranvilのmainの表示にある確認済みのコミットを確認してから実行します。保存形式を上げる場合は、稼働中の版から1段ずつ進めてください。

```sh
git fetch origin main
git merge --ff-only <確認済みのコミットのSHA>
scripts/hub-server.sh update
```

更新は Branvil の main で確認済みのコミットを対象とし、同じコミットの
`client-windows` と `client-linux` の成果物を照合してから切り替えます。
事前にリポジトリ設定 → CI のマージ後専用ジョブへ `client-linux` を追加します。
arm64 でこのジョブを引き取るランナーでは、x86_64 GNU のクロスツールチェーンを
含む `guildbotics/agent-environment:dev` をロードします。

1. `update` は更新するコミットから画面と実行ファイルを作り、インストール時に記録した識別で署名します。現在のサーバーは稼働を続けます。
2. 現行ビルドの `server hub-data backup` で、稼働中のサーバーからGuildBoticsの記録とコードホストのデータを退避します。新ビルドの `server hub-data restore` でその退避を別のディレクトリへ復元し、写しに `server hub-data upgrade`（検査を含む）を実行します。
3. 検査が通ると、GuildBoticsの実行中の作業と後処理を待ちます。その間もBranvilはCIジョブをランナーへ渡し続けます。作業と後処理が終わってから、新しいCIジョブの配布を止め、実行中のCI（`ci.stop_wait_secs` の上限まで）を待って、統合サーバーが停止します。新ビルドで実データをアップグレードして検査し、LaunchAgentの起動先を切り替えます。
4. 新ビルドを起動し、起動を確認できると、新ビルドからCLIとスキルを導入します。

手動更新でも停止時のサービス再開の意図を保存します。自動更新を登録している場合は、その次の回が記録に従ってサービスを再開します。

状態は `~/Library/Application Support/GuildBotics/Hub` にあります（[別のディスクに置く](#ビルドとバックアップを別のディスクに置く)場合、`builds/<commit>/`、`target`、`backups/` はその置き場にあります）。`builds/<commit>/` はその版の署名した実行ファイルとソース、`builds/current` はLaunchAgentが起動する現行ビルドのコピーで、どのビルドのコピーかはこの実行ファイル自身が答えます。`builds/update-from` は切り替える前に動いていたコピー、`builds/update-to` は更新候補のコピーです。復旧が終わるまで保持します。`update` は、切り替えの前に、現行ビルド・更新候補・配置済みのCLIのビルドを除き、どのプロセスも使っていない `builds/<commit>/` を削除します。GuildBoticsのプロセスは自分のビルドのソースを読むので、終わるまでそのソースを使用中にします（OSのファイルロック）。使用中のビルドは、のちの更新で削除します。`signing-identity` は署名の識別です。`backups/` に直近3世代の完了したバックアップを保持します。途中で止まった退避は次の退避時に削除します。バックアップに含まれるデータと含まれない資格情報は[Hubのデータの退避・復元・検査](#hubのデータの退避復元検査)を参照してください。

## 失敗した更新から戻す

更新が失敗すると、実行したターミナルに理由が出ます。Hubの `last-failure` には直近の失敗の時刻、段階、リビジョンが残ります。サーバーの起動エラーは `~/Library/Logs/guildbotics.log` にあります。写しでの検査までに失敗した場合は、現行ビルドで稼働を続けます。

新ビルドが起動する前に切り替えが失敗すると、前のビルドへ起動先を戻します。前のビルドで実データを検査できれば、そのまま再起動します。データの形式が変わって前のビルドで検査できない場合は、データをそのまま残し、LaunchAgentを disable して停止します。次のログインでも起動しません。復旧時の bootstrap は enable してから起動します。サーバーが一度でも応答した場合、バックアップ後の書き込みが含まれる可能性があります。更新時の出力に出たDBのコピーとバックアップを確認し、どのデータを戻すかはオーナーが決めます。[コードホストのアップグレードと復元](../crates/branvil/docs/backup-restore.md#upgrading)に従って元の形式へ戻してから、次のコマンドを実行してください。

```sh
scripts/hub-server.sh recover
```

更新の途中でマシンを再起動した場合も、`builds/update-from` が残っていれば `recover` を実行します。現在のビルドが応答していればそのまま使い、応答していなければ保存した更新候補で実データを検査して起動します。それが失敗した場合だけ前のビルドの検査と起動をやり直します。強制終了でHubの `operation` ディレクトリが残った場合は、先に更新やビルドのプロセスが終わっていることを確かめてから、そのディレクトリを取り除きます。サーバーが停止していて `builds/update-from` がなければ、`restart` で現在のビルドを起動できます。

起動確認後にCLIやスキルの配置が失敗した場合、起動済みのサーバーは稼働を続けます。チェックアウトの `HEAD` が現在のビルドと同じなら、`scripts/hub-server.sh update` で配置だけを再試行できます。ビルドを変えずに配置を再試行するには、登録時と同じ環境設定で次を実行します。

```sh
"$HOME/Library/Application Support/GuildBotics/Hub/builds/current" install
```

通常停止が作業とCIを待っている間も、画面とCLIは使えます。待機を中断する必要がある場合は、`scripts/hub-server.sh status` でジョブを確認し、そのプロセスにSIGTERMを送ります。

## 署名を入れる前のHubを移す

`hub-update.json` の `manual_update_version` 1 の手作業です。署名の識別を記録していないHubでは、`update` が切り替えの前に止まります。次のコマンドは、Hubのチェックアウトの同じターミナルで続けて実行します。

1. [署名の識別を用意する](#署名の識別を用意する)の手順で識別を用意します。
2. 署名を入れた変更のコミットを求め、その直前のコミットまで、退避と予行を通すいつもの手動更新を行います。チェックアウトがすでに先へ進んでいれば、`git reset --keep` がそこへ戻します。

   ```sh
   git fetch origin main
   change=$(git log --first-parent --reverse --format=%H -G'"manual_update_version":1' FETCH_HEAD -- hub-update.json | head -n 1)
   git reset --keep "$change^"
   scripts/hub-server.sh update
   ```

   この `update` が途中で止まった場合は、チェックアウトを動かさずに `scripts/hub-server.sh recover` を済ませてから、手順 3 へ進みます。

3. 署名を入れた変更のコミットへ進め、LaunchAgentの登録を解除してから、識別を指定してインストールし直します。`install` はこのシェルの `PATH` をLaunchAgentに記録します。`GUILDBOTICS_HOME` や `GUILDBOTICS_SERVER_PORT` を変えていた場合は、それも同じ値を設定します。最初の起動で出るキーチェーンと「書類」フォルダの確認には、[インストール](#インストール)のとおりに答えます。

   ```sh
   git merge --ff-only "$change"
   scripts/hub-server.sh uninstall
   export BRANVIL_DATA_DIR="/absolute/path/to/branvil-data"
   export GUILDBOTICS_HUB_SIGNING_IDENTITY="<控えたSHA-1>"
   scripts/hub-server.sh install
   ```

4. 残りのコミットへ、いつもの手動更新で進めます。

   ```sh
   git merge --ff-only FETCH_HEAD
   scripts/hub-server.sh update
   ```

## 自動更新用の内部コマンド

以下はシェルの更新ループ向けの隠しコマンドです。成功時の標準出力は1行の JSON、実行エラーは終了コード1です。状態の判定結果は終了コード0で返します。実データを変更せず、通知だけは外部の webhook へ送信します。

- `guildbotics service status`: `server_running`、サービスの `running`、`state` を返します。サーバー停止中はそれぞれ `false`、`false`、`stopped`。サーバーのロックが指すポートへ接続し、オーナーの証明を検証してから状態を読みます。認証できない場合は停止扱いにせずエラーにします。
- `guildbotics server hub-verified --repo GuildBotics/GuildBotics --since <full-sha>`: `since_on_main` と `verified` を返します。mainのfirst-parent履歴をカーソルでたどり、指定コミットの直前までの確認済みのSHAを新しい順に返します。指定コミットが最後まで見つからなければ `since_on_main` は `false` です。
- `--repo` の代わりに `--checkout <path>` を渡すと、そのチェックアウトの origin からリポジトリを取得します。origin はコードホストの clone URL（`{base}/git/{repo}.git`）の形で、このマシンのサーバーの URL（`http://127.0.0.1:<GUILDBOTICS_SERVER_PORT>`）と一致する HTTP(S) URL に限ります。認証情報や query を含まないものに限ります。接続先はこのマシンのサーバーに固定し、プロキシやリダイレクトを使いません。トークンはサーバーのオーナーのトークンファイル（`GUILDBOTICS_HOME/data/server/<port>/owner.token`）から直接読み、サーバーがそのダイジェストを持つことを確かめてから送ります。引数や環境変数では受け取りません。
- `guildbotics server hub-notify`: 標準入力の文を identity の store の webhook へ送り、`sent` を返します。サーバー停止中にも使えます。未設定なら `false`、送信成功なら `true`、送信失敗はエラーです。URL は出力しません。入力する文には秘密を含めないでください。キーチェーンのアクセス権は実行ファイルの署名に従います。
- `guildbotics server hub-migration`: 実行中のバイナリに埋め込まれた `manual_update_version` と `instructions`（手順書の相対パス、不要なら `null`）を返します。ルートの `hub-update.json` が宣言です。宣言のないソースからのビルドでは番号0となります。このコマンド自体のない旧バイナリも更新ループで番号0として扱います。候補の番号が現在の番号より大きければ手作業が必要です。

## ポートを 7878 にまとめる版への切り替え

この版の `manual_update_version` は 3。自動更新は手作業が必要として止まる。
実機への適用は #177 で行う。既存のチケット・PR・Git remote の 7878 の URL は変えない。
入口リンクとブラウザセッションは GuildBotics を正として共用する。更新後は
`guildbotics open`（Branvil へは `guildbotics open --branvil` または
`guildbotics branvil open`）の新しい入口リンクで一度入り直す。
古い Branvil セッションは統合サーバーで使えない。単体の `guildbotics branvil serve` は従来どおり。

### Hub

1. 稼働中の作業が終わるのを待ち、保存した設定で `sh scripts/hub-server.sh update` を実行する。
   LaunchAgent に `GUILDBOTICS_SERVER_PORT=8765` を設定していた場合は、その指定を外すか
   7878 に変更して `sh scripts/hub-server.sh install` で登録し直す。CLI 側も同じ値にする。
2. `guildbotics open` で `http://127.0.0.1:7878/-/guildbotics/` に入り直す。
   8765 のセッションは移せない。言語などブラウザの origin ごとの設定も選び直す。
3. `connections/branvil.yml` の `base_url` が共通の URL と一致することを確認する。
   既定の 7878 なら変更不要。独自ポートなら GuildBotics の接続設定も同じ URL にする。
   統合サーバーと `guildbotics branvil` のクライアントは `GUILDBOTICS_SERVER_PORT` を使い、
   Branvil の `listen` は単体の `guildbotics branvil serve` だけで使う。
4. 新しい接続を確認後、古い `~/.guildbotics/data/server/8765/` を削除する。
   キーチェーンアクセスで service `guildbotics-server`、account `127.0.0.1:8765` の
   古い同意トークンを削除する。別の項目や新しい 7878 の記録は削除しない。

### Hub 以外のマシン

1. 既存の転送サービスを、完全なポート一覧として 7878 だけを指定して入れ直す。
   POSIX は `sh scripts/hub-forward.sh install --hub my-hub --port 7878`。
   Windows は `powershell -ExecutionPolicy Bypass -File scripts\hub-forward.ps1 -Action install -Hub my-hub -Port "7878"`。
   `my-hub` は実際の SSH 設定の別名に置き換える。
2. ブラウザで `guildbotics open` の新しい入口を開く。`guildbotics connect` を使っていた
   マシンでは同じコマンドで同意を取り直す。古い 8765 の同意トークンと記録を上記と同様に削除する。
3. GuildBotics と Branvil の画面、チケット URL、Git fetch / push を確認する。
   8765 の転送では GuildBotics の画面を開けない。

### 開発用データ

`scripts/dev.sh` は両方を 8866（変更時は `GUILDBOTICS_SERVER_PORT`）で起動する。
既存の開発用 Branvil のデータはそのまま使う。`branvil.toml` の 7879 の `listen` や
`base_url` は統合サーバーでは使わない。開発ワークスペースの Branvil 接続設定も共通 URL に変更する。


## 確認済みのコミットを追う自動更新

Hubのチェックアウトで、上記と同じRust、Node.js 24、npm、GitをPATHに入れ、
オーナーのデスクトップセッションから、サーバーを `install` したあとに登録します。

```sh
scripts/hub-server.sh auto-install
# 停止して登録を解除（サーバーの登録には影響しない）
scripts/hub-server.sh auto-uninstall
```

別のLaunchAgent `local.guildbotics.update` がログイン時に、署名した現行ビルド `builds/current` の
隠しコマンド `server hub-auto-update` を起動し、起動直後と5分ごとにoriginのmainをfetchします。
常駐するのはGuildBoticsの実行ファイルだけで、ログイン項目やアクセスの確認にもその署名で表示されます。
チェックアウトの場所は登録時に引数として記録します。登録時のPATHは更新のビルドのために保存し、サーバーのデータ、identity root、ポートは
`local.guildbotics.plist` から読みます。チェックアウトは自動更新専用にしてください。
HEADより先にある確認済みのコミットのうち、保存形式の版が稼働中の版+1以下で最も新しいものを選びます。
手動更新版の番号が現行以下、独自ベースイメージが
読み込み済みのときだけfast-forwardして通常の `update` を実行します。
独自イメージとスナップショットはCLIで選択中のワークスペースについて確認します。
保存形式の版を1段ずつたどり、次の回でさらに先の確認済みのコミットへ進みます。候補がなければ待ちます。
本番がmainからどれだけ遅れているかは、BranvilのPR一覧の見出しのmainの状態（「確認済みから n 本進んでいる」、失敗時は待っている修復チケット）で確認します。

更新がサーバーを止めた時点でサービスが動いていた場合だけ、更新後やロールバック後に
`guildbotics service start` で再開します。ビルド中に開始したサービスは再開し、ビルド中に止めたサービス（作業の終わりを待っている途中を含む）は再開しません。途中で停止した場合も、再開の意図を保存しておき、
人が復旧した後の次の回で再開します。新ビルドのスナップショットがmissing・stale・failedなら
`guildbotics environment build` が必要なことを通知します。building は通知せず、
その後の自動更新の確認で failed になった場合は通知します。新しいスナップショットが ready になると、
次の確認で `environment prune` を自動で再実行し、古いスナップショットと参照されなくなったイメージの版を片付けます。
この確認は新しい更新候補がないときも行い、待ちの記録は再起動後も引き継ぎます。現行ビルドが入れ替わると
ループは終了し、launchdが新しいビルドで起動し直します。

自動更新の登録し直し・解除も `Hub/operation` を取得します。更新中なら操作を拒否し、更新の子プロセスを止めません。解除できなかった場合は更新が終わってから再実行します。

更新中は、まずGuildBoticsの実行中の作業と後処理の完了を待ち、その間もCIジョブをランナーへ渡し続けます。作業と後処理が終わってから新しいCIジョブの配布を止め、実行中のCIの完了を待ちます。その間も画面とCLIは使えますが、新しい実行・サービスの開始・実行環境のビルド・利用枠の確認・ワークスペースの切り替えは断ります。画面とCLIが使えないのは、新しいビルドへ切り替える間だけです。
1時間経っても更新が終わらなければ通知しますが、待機は打ち切りません。
`--force` は使いません。

通知は `server hub-notify` を使います。webhook未設定時も、コミット・段階・理由・
次にすることが `~/Library/Logs/guildbotics-update.log` に残ります。
コミット由来の通知は同じコミット・同じ段階で一度だけです。操作ロック・復旧待ち・通信障害・サーバー停止は、その状態の発生ごとに通知し、解消後の再発も通知します。操作ロックは同じロックが1時間続いた場合に通知し、短い手動操作では通知しません。送信エラーは未送信の文を保持し、導入済み・試行済みのコミットでも次の回で再試行します。
`Hub/auto-update.json` に試行したコミット、結果、通知を保存します。サービス再開の意図は、停止する現行ビルドが `Hub/service-before` に保存し、再開処理が成功したら削除します。更新スクリプトは止めるビルドより新しいので、保存の依頼は古いビルドが無視できる環境変数 `GUILDBOTICS_HUB_SERVICE_STATE` で渡し、知らないビルドは保存しません。
`Hub/last-failure` は判定に使いません。

止まった場合はログの段階に応じて対処します。

- 手動更新版：通知の `docs/` の手順を読み、手動更新を済ませます。以後は自動で続行します。
- 独自イメージ未読込：`guildbotics environment image load <archive.tar>` で読み込みます。
- チェックアウトの変更・分岐：未コミットの変更を解消し、origin/mainへfast-forwardできる状態に戻します。
- `builds/update-from` が残る：上の復旧手順で `recover` を実行します。自動では復旧を始めません。
- `operation` が残る：ほかの操作中なら待ちます。古いロックかをPIDだけで決めず、更新とビルドの
  プロセスが終わったことを確認してから取り除きます。自動削除はしません。
- サーバー無応答：サーバーログを確認し、必要なら `restart` または `recover` を実行します。
- 更新失敗：同じコミットでは再試行しません。手で対処するか、より新しい、保存形式の条件を満たす確認済みのコミットで自動に再試行します。
  更新途中でループが終了し、結果を記録できなかった場合も同じ扱いです。

診断用に `scripts/hub-server.sh auto-once` で1回だけ実行できます。
ループの間隔は起動時の `GUILDBOTICS_HUB_UPDATE_INTERVAL`（秒、既定300）、
長時間通知は `GUILDBOTICS_HUB_WAIT_NOTICE`（秒、既定3600）で指定できます。

## 自動更新をGuildBoticsの実行ファイルへ移す版への切り替え

この版の `manual_update_version` は 4。それまでの自動更新（Node.jsで動く `scripts/hub-auto-update.mjs`）は、
この版を見ると手作業が必要として通知し、更新しません。そのスクリプトはこの版で無くなるので、
先に止めてからチェックアウトを進めます。Hubのチェックアウトの同じターミナルで続けて実行します。

```sh
scripts/hub-server.sh auto-uninstall
git fetch origin main
git merge --ff-only FETCH_HEAD
scripts/hub-server.sh update
scripts/hub-server.sh auto-install
```

ログイン項目の `node` が無くなり、自動更新は署名した実行ファイルとして動きます。

## 作業停止の通知を webhook に切り替える

`hub-update.json` の `manual_update_version` 10 の手作業です。メンバーのチケットや PR の作業停止は、Branvil の通知用 webhook に届くようになります。webhook が未設定の場合、作業停止の通知は届きません。

1. オーナーとして Branvil の **設定 → 通知** を開き、設定状況と送り先のホストを確認します。Slack で受け取る場合は、Slack 側で送り先を確認し、どのメンバーも監視していないチャンネルを使います。未設定の場合や送り先を変える場合は、受け取り先の Incoming Webhook の URL を入力して保存します。
2. [手動更新](#手動更新)の手順で、版 10 の確認済みコミットへ更新します。
3. **設定 → 通知** を開き直し、「設定済み」と送り先のホストを確認します。次に作業が人の対応を待って止まったとき、送り先にメンバー名と対象へのリンクが届くことを確認します。送信の失敗は同じ画面に表示されます。

## Hubの写しで更新を確かめる

失敗する更新のような、本番で起こせない経路は、Hubと並べて動かす写しで確かめます。
`GUILDBOTICS_HUB_INSTANCE`（英小文字・数字・ハイフン）に名前を付けると、`scripts/hub-server.sh` の
すべての操作が、その名前のLaunchAgent `local.guildbotics.<名前>`（自動更新は `local.guildbotics.<名前>.update`）、
状態 `~/Library/Application Support/GuildBotics/Hub-<名前>`、ログ `~/Library/Logs/guildbotics-<名前>.log`
（自動更新は `guildbotics-<名前>-update.log`）を使います。自動更新のLaunchAgentにもこの名前を記録します。
実行ファイルの署名はHubと同じ識別子なので、キーチェーンの確認は出ません。

名前に `update` は使えません。写しの通知とスクリプトの案内は、この名前を含めて表示します。案内のCLIのコマンドは、本番と同じく、そのHubの `builds/current` にそのHubの設定を付けた形です。

写し専用のチェックアウトで、`install` の前に、`GUILDBOTICS_HOME`、`BRANVIL_DATA_DIR`、`GUILDBOTICS_SERVER_PORT` に
Hubと別のものを設定し、`GUILDBOTICS_IDENTITY_ROOT` は外します（写しは `GUILDBOTICS_HOME` の下のidentityを使います）。
以後の操作でも同じ `GUILDBOTICS_HUB_INSTANCE` を設定します。写しの `install` と `update` も、AI CLIのユーザースキルを
写しの版で書き換えます。
片付けは `auto-uninstall`、`uninstall` のあと、状態とデータのディレクトリを削除し、
Hubの `builds/current install` でスキルを戻します。

```sh
export GUILDBOTICS_HUB_INSTANCE=copy
export GUILDBOTICS_HOME="/absolute/path/to/copy-home"
export BRANVIL_DATA_DIR="/absolute/path/to/copy-code-host-data"
export GUILDBOTICS_SERVER_PORT=7979
export GUILDBOTICS_HUB_SIGNING_IDENTITY="<控えたSHA-1>"
scripts/hub-server.sh install
```

## レシピ変更のマージ前にスナップショットを用意する

AI CLI・Node.js・Python 依存・ベースイメージを変える PR は、マージの前に
人が Hub ホストで次を実施します。動いているサーバーの checkout は変更せず、
PR head を別の checkout に用意し、そこで `cargo build --release --locked -p guildbotics`
を実行します。生成した実行ファイルはその checkout のレシピを使います。
Hub の LaunchAgent と同じ OS ユーザー・`GUILDBOTICS_HOME`、運用ワークスペースを使います。

```sh
# PR head の checkout で実行する。パスは実際の運用先に置き換える。
# 独自ベースイメージを宣言している場合は、先に Hub の CPU 用 archive を読み込む。
./target/release/guildbotics environment image load /absolute/path/to/arm64-image.tar
./target/release/guildbotics environment --workspace /absolute/path/to/workspace build
./target/release/guildbotics environment --workspace /absolute/path/to/workspace status --format json
```

既定のイメージなら `image load` は不要です。独自イメージは宣言した digest と
一致するものを読み込み、`snapshot.state` が `ready` であることを確認してから
マージします。ビルドと稼働中の upkeep はほかのレシピを削除しないので、
切り替えまでは新旧両方のディスク容量が必要です。例外は宣言したイメージのビルドで、宣言はどの版にも
効くため、終わったときにほかのイメージから作ったレシピを削除します（同じイメージのレシピは残します）。
ビルド後の掃除では、タグ、動いている microVM、登録済みの各ワークスペースのスナップショットが参照するイメージの版を残します。
タグを付け替えて使わなくなった版は、その版を指すdigest参照ごと削除します。古いタグそのものを消すときは、[手動でのイメージの削除](native_agent_runtime.ja.md#古いイメージのタグを削除する)を参照してください。スナップショットのイメージが索引で守られていないとき
（別のワークスペースに同じ名前で別のイメージのスナップショットがあるときなど）は、何も削除しません。

`scripts/hub-server.sh update` / `recover` は、新版の認証付き起動確認後に
更新途中の記録を消し、CLI・スキルを更新してから `guildbotics environment prune` で選択中ワークスペースの
現在のレシピ以外のスナップショットを削除し、続けてビルド後と同じ規則で使われていないイメージの版を削除します。ロールバックでは実行しません。
現在のスナップショットが未完成、ビルド中、イメージの読み込み中、または削除に失敗した場合は、
再実行の案内を出して更新を成功として終えます。CLI・スキルの更新失敗は引き続き
エラーとして報告し、その場合も削除を試みます。起動済みの新版は維持します。必要なビルドを終えてから
同じ Hub 設定・ワークスペースで `guildbotics environment prune` を再実行してください。
自動更新で片付けを見送った場合は、更新処理と同じ `Hub/operation` を取得し、
更新途中の記録がなく、記録した新版が現在も稼働していて ready のときだけ再実行します。
ロールバックや別の版への切り替えでその版が稼働していなければ待ちを破棄します。
通常の upkeep は片付けを開始せず、同じイメージのレシピを削除しないので、その後に次の更新を事前ビルドしても保持されます。

## Hubのデータの退避・復元・検査

`guildbotics server hub-data` が、GuildBotics の記録とコードホストのデータを1つのコマンドで扱います。

```sh
guildbotics server hub-data backup --to <空のディレクトリ>            # サーバーの稼働中
guildbotics server hub-data restore --from <退避> --data-dir <空のディレクトリ>
guildbotics server hub-data check [--data-dir <復元先>]               # サーバーの停止中
guildbotics server hub-data upgrade [--data-dir <復元先>]             # 検査を含む
guildbotics server hub-data reindex [--data-dir <復元先>]
```

`backup` は、稼働中のサーバーにコードホストのデータの退避を `code-host/` へ書かせ、続けて GuildBotics の記録を写します。サーバーへの要求には、このマシンのサーバーのオーナーのトークンを使います。最後に `manifest.json` を書き、その有無で退避の完了を表します。

GuildBotics の記録は、このマシンの `data/workspaces.json` に登録した全ワークスペース（`gb_workspace::selection::registered`）と identity root の `.guildbotics/config`・`state`・`local` です。写しの番号は identity root が 0 で、ワークスペースが続きます。manifest の版は 2 で、ほかの版の退避は扱いません。identity root は `GUILDBOTICS_IDENTITY_ROOT`、未指定なら `GUILDBOTICS_HOME/identity` です。共有書き込みと同じ identity → workspace のロックを取り、通常ファイルをコピーします。リンクと特殊ファイルは拒否し、退避先は所有者だけが開けます。

再作成可能な `local/clones`、`local/cache`、`local/agent_environment/snapshots`、`local/msb` と実行中だけ使う `local/run` は除外します。マシンの `data/msb` にある runtime は対象の外です。秘密の暗号化ファイルは復号せず退避します。OS キーチェーンの鍵は含まれないため、この退避だけでは別のマシンへ秘密を移せません。コードホストの退避に含まれるデータと含まれない資格情報は[コードホストのバックアップと復元](../crates/branvil/docs/backup-restore.md)を参照してください。

更新は `backups/hub-update-<時刻>.<乱数>/` に退避し、直近3世代を残します。不完全な退避は次の退避時に削除します。更新前の退避には、その後に完了した仕事の書き込みは含まれません。

`restore` は退避の検査値を確かめ、新しいディレクトリへ GuildBotics の記録の写しとコードホストのデータ（`code-host/`）を置いて検査します。直前の版のコードホストのデータは、置くときに変換します。復元先は、そのまま `check` と `upgrade` の `--data-dir` に渡せる退避の形です。本物へ戻す手順は自動化していません。停止中に manifest の root と番号付きの写しを照合し、戻す対象をオーナーが決めてください。

`check`、`upgrade`、`reindex` はサーバーの停止中に使います。コードホストのデータを開いているサーバーがあれば断ります。`--data-dir` を省略すると、このマシンの記録と `BRANVIL_DATA_DIR`（未指定なら `GUILDBOTICS_HOME/data/code-host`）を対象にします。まだデータのないコードホストは対象にしません。`upgrade` で変換したコードホストのデータは、`check` が通るまでサーバーが開きません。

`upgrade --data-dir <復元先>` と `check --data-dir <復元先>` は番号付きの写しと `code-host/` だけを扱い、manifest の元のパスへはアクセスしません。GuildBotics の記録はJSON・JSONL・YAML の構文と記録の schema_version を検査します。JSONL は製品の読み取りと同様に壊れた行とオブジェクト以外を読み飛ばし、読めた全記録の版を検査します。検査は退避した元の行を書き換えません。版1〜4は項目追加だけなので変換しません。版4から版5への変換は、identity root から組み込みのコードホストへの接続設定 `config/connections/branvil.yml`、秘密の `BRANVIL_ADMIN_TOKEN` と各メンバーの `BRANVIL_TOKEN`・`BRANVIL_TOKEN_ID`、各メンバーの identity の `account_info.branvil_username` を削除し、変換したときは identity root の `state/schema.json` に `schema_version: 5` を記録します。版4はこれらを identity root だけに置くので、ワークスペースは検査だけを行います。旧ビルドの検査はこの記録で停止します。変換は対象のファイルと秘密をすべて読んでから変更するので、秘密を復号できない場合は何も変更せずに失敗します。秘密の復号にはこのマシンの OS キーチェーンの鍵を使います。繰り返し実行しても、変換済みの root は変わりません。互換性を壊す版では直前の1版からの変換と検査を追加します。それ以前の形式を並行サポートしません。`--data-dir` を省略すると登録済みの本物を対象にします。更新ではサーバー停止後に本物へ `upgrade`（検査を含む）を行い、その成功後だけ起動します。`recover` も候補の変換と検査を再実行します。

旧ビルドへ戻す場合も検査が必要です。検査に通らない場合はデータを残して停止し、退避の親ディレクトリを表示します。オーナーは退避後の書き込みを確認し、戻す対象を決めてください。自動では実データを退避で上書きしません。

## CLI の写しと結果の取り直しへの更新（手動更新版 5）

実行中の `guildbotics run` が終わってから `scripts/hub-server.sh update` を実行してください。
利用者が編集したスキルは `guildbotics install` で置き直されないため、`skills/guildbotics/SKILL.md` の
`run ask` の説明を手で取り込んでください。読み書き可の許可先やメンバー clone 内の cwd も
写しで実行します。そこでの commit、ignore されるファイルの生成は、書き戻し後に対話の側で行ってください。

cwd はリポジトリのルートにしてください。下位フォルダだけを写すと、許可先や clone の親にある本物の `.git` が見えるため、実行中に commit すると本物の履歴を変更します。


## 受け渡しフォルダを廃止する更新（版6）

更新前に、`~/Documents/GuildBotics` に残したいファイルを別の場所へ移し、自作コマンドの固定パスを実行の作業ディレクトリへ書き換えます。GuildBotics はこの既存フォルダを削除しません。更新後は共通許可を与えず、画面で cwd が空欄の手動実行と巡回・チャット・定期実行は、端末状態の `run/files/<run_id>/work` を使います。成果物は実行終了後にコマンド画面・診断画面からダウンロードします（7日間保持、通常ファイルのみ、1ファイル64 MBまで）。アップロードも7日間保持し、要求した実行にだけ読み取り専用で渡します。期限切れは次の利用時に掃除します。

**macOS のシステム設定 → プライバシーとセキュリティ → ファイルとフォルダで、以前 `python3.15` に与えた「書類フォルダ」の許可を外してください。** 同じ uv 管理の Python を使う別のプログラムにも影響する許可です。「書類」の下に明示的な許可先やワークスペースを置く場合だけ、その用途の許可を残します。Hub 以外のクライアントでの手作業はありません。

## コードホストのコマンドを guildbotics にまとめる更新（手動更新版 7）

この版で、コードホストのコマンドは `guildbotics runner`、`guildbotics server hub-data`、`guildbotics server git-hook` になります。コードホストは自前のセッションと owner のトークンファイルを持たず、GuildBotics のオーナーの認証だけを使います。保存形式は 37 になり、変換で `secrets/owner.token`、ブラウザのセッションの表、設定ファイルの `listen`・`base_url`・`limits.session_hours` を削除します。

稼働中の旧ビルドには新しい `server hub-data backup` がないため、`update` が退避を取れません。自動更新は手作業が必要として止まります。旧ビルドで退避を新しい形に取り、それを `update` に渡します。`update` はその退避で新ビルドのリハーサルを行い、成功してから旧ビルドを止めます。Hubのチェックアウトの同じターミナルで続けて実行します。

```sh
hub="$HOME/Library/Application Support/GuildBotics/Hub"
plist="$HOME/Library/LaunchAgents/local.guildbotics.plist"
for key in GUILDBOTICS_HOME BRANVIL_DATA_DIR GUILDBOTICS_SERVER_PORT GUILDBOTICS_IDENTITY_ROOT; do
    if value=$(plutil -extract "EnvironmentVariables.$key" raw -o - "$plist" 2>/dev/null); then
        export "$key=$value"
    else
        unset "$key"
    fi
done
unset BRANVIL_TOKEN BRANVIL_URL
scripts/hub-server.sh auto-uninstall
backup="$hub/backups/hub-update-before-7"
mkdir "$backup"
"$hub/builds/current" server hub-data backup --to "$backup"
"$hub/builds/current" branvil backup --data-dir "$BRANVIL_DATA_DIR" --to "$backup/code-host"
git fetch origin main
git merge --ff-only FETCH_HEAD
GUILDBOTICS_HUB_MANUAL_BACKUP="$backup" scripts/hub-server.sh update
scripts/hub-server.sh auto-install
```

新ビルドが起動できずに旧ビルドへ戻った場合、旧ビルドの検査はコードホストの形式を確かめないため、旧ビルドは変換済みのデータを開けずに停止します。ログで原因を確かめてから `recover` で新ビルドを起動し直すか、退避 `$backup` から戻してください。

更新後は `guildbotics open` で画面に入り直してください。開発用のチェックアウトで動かすサーバーも、同じ変換を `guildbotics server hub-data upgrade` で行ってから起動します。更新を確かめたら、以前の形の退避 `backups/branvil-update-*` と `backups/guildbotics-update-*` は削除できます。Hub以外のマシンでの手作業はありません。

## 別のマシンから member CLI と Git を使う

Hub への OpenSSH のポート転送を用意し、`guildbotics connect` で同意を取得します。
CLI は手元の稼働中サーバー（`app-api.lock`）があればオーナートークンで接続し、
無ければ `127.0.0.1:<GUILDBOTICS_SERVER_PORT>`（既定 7878）へ接続します。
同意トークンは OS キーチェーンのサービス `guildbotics-server`、
アカウント `127.0.0.1:<port>` に保存されます。送信前に `/local-api/proof` で
接続先を確かめます。資格情報が無い、または失効している場合は `guildbotics connect`
を案内して停止します。

クライアントにワークスペースの写しは不要です。member CLI は Hub が選択中の
ワークスペース ID を `/health` で取得するため、クライアントでは `--workspace` を
付けず、選択は Hub で行います。伏せ字に必要なキー名は `/member/redaction-keys` から
取得し、値はクライアント自身の環境変数から読みます。この API は秘密の値を返しません。
`git-credential` は端末のキャッシュが使えないときに同意トークンで新しい Git トークンを
要求し、要求元の端末 ID と表示名を Hub へ渡します。

同意トークンで使えるのは `/health`、`/member/run`・`/member/plan`・`/member/git`、
`/member/redaction-keys`、`/git-credentials` と既存の `/session/links` です。
設定・秘密・隔離環境・サービス・画面用の API は従来の認証を維持します。
`guildbotics run` のクライアント対応は別の変更で扱います。

### 画面で見る更新の段階と版

GuildBotics と組み込み Branvil は、すべてのページの上部に Hub の更新・再起動・停止の段階を表示します。帯は段階が変わるまで閉じられます。

- 準備中（ビルド・退避・リハーサル）は通常どおり使えます。まもなく新しい実行の受け付けを止めることを知らせます。
- 受け付けの停止中は巡回・定期実行・チャットの受信が止まります。手動実行・コマンド作成・確認・診断・実行環境のビルド・利用枠の確認・ワークスペースの切り替えは断ります。新しい CI ジョブは始めず、画面と CLI の閲覧、実行中の作業と CI ジョブは続きます。帯には残る作業とジョブの件数を表示します。
- 切り替え中は画面と CLI も接続できなくなり、起動後に接続し直します。自動更新では、停止を受け付けた時点でサービスが動いていた場合に再開します。手動の再起動・停止の帯にはサービス再開の案内を出しません。

今の版は GuildBotics のサービス画面に常時表示し、帯には今の版と受け取った更新先の版を表示します。SHA の先頭 8 桁と題名を示し、ワークスペースのコードホストがリモートを解決できればコミットへのリンクにします。開発用起動などビルド情報がない場合は今の版を表示しません。Branvil 単体では従来の CI 停止の案内を使います。

更新スクリプトは現行ビルドの `server stop` に新しい引数を渡さず、`GUILDBOTICS_HUB_STOP_REASON` をシェルから継承しない形で設定します（`update` / `restart`、指定しなければ `stop`）。`server hub-phase` の準備・解除の通知は表示用で、古いビルドが受け付けない場合もログに残して更新を続けます。最初の更新で帯が出なくても、対応版の次の更新から表示します。

## CPU 別の CI イメージへの更新（手作業の版 8）

Hub とランナーを新しい版に更新してから、Hub のリポジトリで
`scripts/build-agent-environment-image.sh` を実行し直す。
arm64 と amd64 のアーカイブが Hub のデータに保存され、各ランナーが
自分の CPU のイメージを取得する。既存のアーカイブを使う場合は、それぞれを
`guildbotics environment image load-ci <archive>` で登録する。
Hub の既存キャッシュだけでは、別 CPU のアーカイブは配布されない。

ランナーは取得・読み込みのあと、保有するタグ・CPU・digest を通知する。
レジストリのイメージは各ランナーで `guildbotics runner pull <tag>` を実行する。

## HubのCIイメージを共有ストアへ移す（手作業の版9）

新しいHubは、CIと隔離コマンドのイメージを `<GuildBoticsのデータディレクトリ>/msb` に置きます。
データディレクトリの既定値は `~/.guildbotics/data` です。

1. 更新前に、CI設定画面のイメージ一覧で、使っているタグと設定digestを控えます。配布済みのイメージは、Hubのコードホストデータにある `ci/images/catalog.json` を見て、該当するCPUとタグの `<id>.tar` を取り出します。手元に元のアーカイブがある場合も、そのファイルを使えます。

   元のアーカイブもレジストリから取得できる同じ版もない場合は、旧CIストアからタグを一つずつ書き出します。再書き出しでは設定digestが変わるため、取り込んだあとに新しいdigestで使うことを確認します。次の例のパスとタグは、旧CI設定画面のランタイムとイメージに合わせて置き換えます。

   ```sh
   MSB_HOME="$HOME/.branvil/msb-home" "$HOME/.guildbotics/data/msb/bin/msb" save \
     --output /absolute/path/to/ci-image.tar guildbotics/agent-environment:dev
   ```

2. [手動更新](#手動更新)の手順でHubを更新します。旧ストアと書き出したアーカイブは、動作確認が済むまで保管します。次の手順で読み込むまでにイメージが見つからず `startup_failure` になったジョブは、手順4のあとに再実行します。
3. 新しいHubで、必要なタグのアーカイブを読み込みます。次のコマンドはHubのストアに読み込み、同じCPUの別マシンのランナーにも配ります。

   ```sh
   guildbotics environment image load --ci /absolute/path/to/ci-image.tar
   ```

   レジストリから同じ版を取得できるタグは、Hubで `guildbotics runner pull <tag>` を実行する方法もあります。

4. CI設定画面で、読み込んだタグと設定digestを手順1の記録と照合します。再書き出ししたタグの新しいdigestを隔離環境の宣言にも使う場合は、該当するCPUのdigestを更新してスナップショットをビルドします。同じタグを使うCIジョブをHubで実行し、隔離コマンドも起動できることを確認します。
5. 元に戻す必要がなくなったら、旧ストアと作業用に書き出したアーカイブを削除できます。削除できるのは旧ストア専用のディレクトリです。パスを `realpath` で確認し、新しいストアやHubの運用データを配下に含む場合はディレクトリごと保管します。旧ストアが専用ディレクトリでパスが既定値なら、`rm -ri "$HOME/.branvil/msb-home"` で削除する内容を確認しながら進めます。コードホストの配布用アーカイブはランナーが取得に使うため保管します。

CI設定の `[ci.microsandbox]` で旧ストアを指定していた場合は、その設定値を手順1の書き出しに使います。更新後のHubは共有ストアを使います。
