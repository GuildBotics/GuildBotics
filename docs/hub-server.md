# macOS Hubの起動と更新

Hubのチェックアウトで、オーナーがmacOSのデスクトップにログインしてから実行します。Git、リポジトリの `rust-toolchain.toml` が指定するRust、Node.js 24、npmを `PATH` に入れておきます。Branvilを初めて使う場合は、ビルド済みの `guildbotics branvil init --data-dir <絶対パス>` でデータディレクトリを用意します。既存のHubを切り替える場合は、これまでの方法でGuildBoticsとBranvilを停止し、両方の旧LaunchAgentの登録を解除してから、既存のBranvilデータを指定します。

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

```sh
export BRANVIL_DATA_DIR="/absolute/path/to/branvil-data"
export GUILDBOTICS_HUB_SIGNING_IDENTITY="<控えたSHA-1>"
scripts/hub-server.sh install
scripts/hub-server.sh status
```

`install` はチェックアウトのコミット済みの `HEAD` から両方の画面とRustの実行ファイルを作ります。LaunchAgent `local.guildbotics` がこの実行ファイルの `server start` を直接起動し、1つのプロセスが1つのポート（既定7878）で GuildBotics と Branvil を配ります。ログイン時と異常終了後にも起動します。正常に停止したあとは、次回のログインか `scripts/hub-server.sh restart` で起動します。

`install` は署名の識別を `signing-identity` に記録し、`update` は同じ識別で署名します。識別がキーチェーンに見つからない場合、署名に失敗した場合、署名にTeam IDがない場合（自己署名の証明書など）は、ビルドを切り替えずに止まります。登録時の `PATH` と `BRANVIL_DATA_DIR` をLaunchAgentが使います。GuildBoticsのデータは既定で `~/.guildbotics`、ポートは7878です。画面は `http://127.0.0.1:7878/-/guildbotics/` で配り、Branvil を設定済みなら `/` は Branvil の画面、未設定なら GuildBotics の画面へ案内します。更新後は `guildbotics open` でブラウザに入り直してください。変更する場合は登録前に絶対パスの `GUILDBOTICS_HOME` または `GUILDBOTICS_SERVER_PORT` を設定します。同じ設定でCLIを実行してください。起動確認後に `guildbotics install` がCLIとスキルを配置します。サーバーのログは `~/Library/Logs/guildbotics.log` で確認できます。

インストール後の最初の起動時、macOSの確認が出たらオーナーが回答してください。スクリプトは最大20分間、起動を待ちます。同じ識別で署名した更新では、これらの確認は出ません。

- **キーチェーンへのアクセス**：GuildBoticsのSecretを暗号化・復号する端末共通の鍵（サービス名 `GuildBotics`、アカウント名 `agent-environment-credentials`）への許可です。「常に許可」を選ぶと、同じTeam IDで署名したビルドには再び確認が出ません。`guildbotics connect` が保存する `guildbotics-server` の項目はCLIの接続用トークンで、この暗号化の鍵とは別です。Branvilの秘密はデータディレクトリにあり、Branvil用の鍵の確認がもう1回出るわけではありません。
- **「書類」フォルダへのアクセス**：macOSのプライバシー保護（TCC）の確認です。サーバーが、メンバーとの受け渡し先 `~/Documents/GuildBotics` や「書類」の下にあるワークスペースなどへアクセスするために必要です。キーチェーンとは別に許可してください。

```sh
scripts/hub-server.sh restart    # GuildBoticsの作業とBranvilのCIを待ってから再起動
scripts/hub-server.sh uninstall  # 停止してLaunchAgentの登録を解除
```

`uninstall` はサーバーのデータ、ビルド、バックアップを残します。巡回やチャット受信を起動時に開始するかどうかは、GuildBoticsのサービス設定で指定します。

## 手動更新

Hubのチェックアウトで、オーナーが更新するコミットを確認してから実行します。

```sh
git fetch origin main
git merge --ff-only FETCH_HEAD
scripts/hub-server.sh update
```

1. `update` は更新するコミットから画面と実行ファイルを作り、インストール時に記録した識別で署名します。現在のサーバーは稼働を続けます。
2. 現行ビルドでBranvilのバックアップを取り、別のディレクトリへ復元します。新ビルドでその写しに `guildbotics branvil admin upgrade` と `guildbotics branvil admin check` を実行します。GuildBotics も退避し、退避の写しに新ビルドの `server hub-data upgrade` と `check` を実行します。
3. 検査が通ると、GuildBoticsの実行中の作業と、Branvilが実行中のCI（`ci.stop_wait_secs` の上限まで）を並べて待ち、統合サーバーが停止します。新ビルドで実データをアップグレードして検査し、LaunchAgentの起動先を切り替えます。
4. 新ビルドを起動し、起動を確認できると、新ビルドからCLIとスキルを導入します。

状態は `~/Library/Application Support/GuildBotics/Hub` にあります。`builds/<commit>/` はその版の署名した実行ファイルとソース、`builds/current` はLaunchAgentが起動する現行ビルドのコピーで、どのビルドのコピーかはこの実行ファイル自身が答えます。`builds/update-from` は切り替える前に動いていたコピー、`builds/update-to` は更新候補のコピーです。復旧が終わるまで保持します。`update` は、切り替えの前に、現行ビルド・更新候補・配置済みのCLIのビルドを除き、どのプロセスも使っていない `builds/<commit>/` を削除します。GuildBoticsのプロセスは自分のビルドのソースを読むので、終わるまでそのソースを使用中にします（OSのファイルロック）。使用中のビルドは、のちの更新で削除します。`signing-identity` は署名の識別です。`backups/` に直近3世代の完了したバックアップを保持します。途中で止まった退避は次の退避時に削除します。バックアップに含まれるデータと含まれない資格情報は[Branvilのバックアップと復元](../crates/branvil/docs/backup-restore.md)を参照してください。

## 失敗した更新から戻す

更新が失敗すると、実行したターミナルに理由が出ます。Hubの `last-failure` には直近の失敗の時刻、段階、リビジョンが残ります。サーバーの起動エラーは `~/Library/Logs/guildbotics.log` にあります。写しでの検査までに失敗した場合は、現行ビルドで稼働を続けます。

新ビルドが起動する前に切り替えが失敗すると、前のビルドへ起動先を戻します。前のビルドで実データを検査できれば、そのまま再起動します。データの形式が変わって前のビルドで検査できない場合は、データをそのまま残し、LaunchAgentを disable して停止します。次のログインでも起動しません。復旧時の bootstrap は enable してから起動します。サーバーが一度でも応答した場合、バックアップ後の書き込みが含まれる可能性があります。更新時の出力に出たDBのコピーとバックアップを確認し、どのデータを戻すかはオーナーが決めます。[Branvilのアップグレードと復元](../crates/branvil/docs/backup-restore.md#upgrading)に従って元の形式へ戻してから、次のコマンドを実行してください。

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
- `guildbotics server hub-checks --repo GuildBotics/GuildBotics --sha <full-sha>`: `run_registered` と `state` を返します。`state` は `unregistered`（run 未登録）、`pending`（必須チェック未完了・欠落）、`failure`（必須チェック失敗）、`success`（必須チェック成功）。必須チェックのない登録済み run は `success`。不正な pipeline は失敗です。必須チェックは `success` またはサービスによるパス判定の `skipped` だけを成功とします。
- `--repo` の代わりに `--checkout <path>` を渡すと、そのチェックアウトの origin からリポジトリを取得します。origin は Branvil の clone URL（`{base}/git/{repo}.git`）の形で、設定済み base URL と一致する HTTP(S) URL に限ります。認証情報や query を含まないものに限ります。接続先は設定済み Hub に固定し、プロキシやリダイレクトを使いません。トークンは `BRANVIL_DATA_DIR` の `secrets/owner.token` から直接読み、引数や環境変数では受け取りません。
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


## main を見張る自動更新

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
必須チェックがすべて成功し、手動更新版の番号が現行以下、独自ベースイメージが
読み込み済みのときだけfast-forwardして通常の `update` を実行します。
独自イメージとスナップショットはCLIで選択中のワークスペースについて確認します。
チェック失敗は通知し、次の回で再確認します。未登録・未完了のチェックは次の回を待ちます。

更新前にGuildBoticsのサービスが動いていた場合だけ、更新後やロールバック後に
`guildbotics service start` で再開します。途中で停止した場合も、再開の意図を保存しておき、
人が復旧した後の次の回で再開します。新ビルドのスナップショットがmissing・stale・failedなら
`guildbotics environment build` が必要なことを通知します。現行ビルドが入れ替わると
ループは終了し、launchdが新しいビルドで起動し直します。

自動更新の登録し直し・解除も `Hub/operation` を取得します。更新中なら操作を拒否し、更新の子プロセスを止めません。解除できなかった場合は更新が終わってから再実行します。

更新中は実行中の作業とCIの完了を待ちます。その間も画面とCLIは使えますが、新しい実行・サービスの開始・実行環境のビルド・利用枠の確認・ワークスペースの切り替えは断り、新しいCIジョブも始めません。画面とCLIが使えないのは、新しいビルドへ切り替える間だけです。
1時間経っても更新が終わらなければ通知しますが、待機は打ち切りません。
`--force` は使いません。

通知は `server hub-notify` を使います。webhook未設定時も、コミット・段階・理由・
次にすることが `~/Library/Logs/guildbotics-update.log` に残ります。
コミット由来の通知は同じコミット・同じ段階で一度だけです。操作ロック・復旧待ち・通信障害・サーバー停止は、その状態の発生ごとに通知し、解消後の再発も通知します。操作ロックは同じロックが1時間続いた場合に通知し、短い手動操作では通知しません。送信エラーは未送信の文を保持し、導入済み・試行済みのコミットでも次の回で再試行します。
`Hub/auto-update.json` に試行したコミット、結果、通知、サービス再開の意図を保存し、
`Hub/last-failure` は判定に使いません。

止まった場合はログの段階に応じて対処します。

- 手動更新版：通知の `docs/` の手順を読み、手動更新を済ませます。以後は自動で続行します。
- 独自イメージ未読込：`guildbotics environment image load <archive.tar>` で読み込みます。
- チェックアウトの変更・分岐：未コミットの変更を解消し、origin/mainへfast-forwardできる状態に戻します。
- `builds/update-from` が残る：上の復旧手順で `recover` を実行します。自動では復旧を始めません。
- `operation` が残る：ほかの操作中なら待ちます。古いロックかをPIDだけで決めず、更新とビルドの
  プロセスが終わったことを確認してから取り除きます。自動削除はしません。
- サーバー無応答：サーバーログを確認し、必要なら `restart` または `recover` を実行します。
- 更新失敗：同じコミットでは再試行しません。手で対処するか、新しいコミットを待ちます。
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
export BRANVIL_DATA_DIR="/absolute/path/to/copy-branvil-data"
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
切り替えまでは新旧両方のディスク容量が必要です。

`scripts/hub-server.sh update` / `recover` は、新版の認証付き起動確認後に
更新途中の記録を消し、CLI・スキルを更新してから `guildbotics environment prune` で選択中ワークスペースの
現在のレシピ以外を削除します。ロールバックでは実行しません。
現在のスナップショットが未完成、ビルド中、または削除に失敗した場合は、
再実行の案内を出して更新を成功として終えます。CLI・スキルの更新失敗は引き続き
エラーとして報告し、その場合も削除を試みます。起動済みの新版は維持します。必要なビルドを終えてから
同じ Hub 設定・ワークスペースで `guildbotics environment prune` を再実行してください。
通常の upkeep は削除しないので、その後に次の更新を事前ビルドしても保持されます。

## GuildBotics のデータの退避と検査

更新候補の隠しコマンド `guildbotics server hub-data backup --to <空のディレクトリ>` が、このマシンの `data/workspaces.json` に登録した全ワークスペース（`gb_workspace::selection::registered`）と identity root の `.guildbotics/config`・`state`・`local` を退避します。identity root は `GUILDBOTICS_IDENTITY_ROOT`、未指定なら `GUILDBOTICS_HOME/identity` です。共有書き込みと同じ identity → workspace のロックを取り、通常ファイルをコピーします。リンクと特殊ファイルは拒否し、退避先は所有者だけが開けます。

再作成可能な `local/clones`、`local/cache`、`local/agent_environment/snapshots`、`local/msb` と実行中だけ使う `local/run` は除外します。マシンの `data/msb` にある runtime は対象の外です。秘密の暗号化ファイルは復号せず退避します。OS キーチェーンの鍵は含まれないため、この退避だけでは別のマシンへ秘密を移せません。

`backups/guildbotics-update-<時刻>.<乱数>/` の `manifest.json` に元の root と完了を記録し、`0/.guildbotics`、`1/.guildbotics`…にデータを置きます。Branvil とは別に直近3世代を残し、不完全な退避は次の退避時に削除します。更新前の退避には、その後に完了した仕事の書き込みは含まれません。

`server hub-data upgrade --data-dir <退避の写し>` と `check --data-dir <退避の写し>` は番号付きの写しだけを扱い、manifest の元のパスへはアクセスしません。JSON・JSONL・YAML の構文と記録の schema_version を検査します。JSONL は製品の読み取りと同様に壊れた行とオブジェクト以外を読み飛ばし、読めた全記録の版を検査します。退避した元の行は書き換えません。版1〜4は項目追加だけなので変換しません。将来、互換性を壊す版では直前の1版からの変換と検査を追加します。それ以前の形式を並行サポートしません。`--data-dir` を省略すると登録済みの本物を対象にします。更新ではサーバー停止後に本物へ `upgrade`、`check` を行い、その成功後だけ起動します。`recover` も候補の変換と検査を再実行します。

旧ビルドへ戻す場合も両サービスの検査が必要です。GuildBotics の検査に通らない場合はデータを残して停止し、退避の親ディレクトリを表示します。この内部コマンドがまだない旧ビルドへの復旧も、安全のため停止します。オーナーは退避後の書き込みを確認し、停止中に manifest の root と番号付きの写しを照合して、戻す対象を決めてください。自動では実データを退避で上書きしません。

## CLI の写しと結果の取り直しへの更新（手動更新版 5）

実行中の `guildbotics run` が終わってから `scripts/hub-server.sh update` を実行してください。
利用者が編集したスキルは `guildbotics install` で置き直されないため、`skills/guildbotics/SKILL.md` の
`run ask` の説明を手で取り込んでください。読み書き可の許可先やメンバー clone 内の cwd も
写しで実行します。そこでの commit、ignore されるファイルの生成は、書き戻し後に対話の側で行ってください。

cwd はリポジトリのルートにしてください。下位フォルダだけを写すと、許可先や clone の親にある本物の `.git` が見えるため、実行中に commit すると本物の履歴を変更します。
