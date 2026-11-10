# macOS Hubの起動と更新

Hubのチェックアウトで、オーナーがmacOSのデスクトップにログインしてから実行します。Git、リポジトリの `rust-toolchain.toml` が指定するRust、Node.js 24、npmを `PATH` に入れておきます。Branvilを初めて使う場合は、ビルド済みの `guildbotics branvil init --data-dir <絶対パス>` でデータディレクトリを用意します。既存のHubを切り替える場合は、これまでの方法でGuildBoticsとBranvilを停止し、両方の旧LaunchAgentの登録を解除してから、既存のBranvilデータを指定します。

## インストール

```sh
export BRANVIL_DATA_DIR="/absolute/path/to/branvil-data"
scripts/hub-server.sh install
scripts/hub-server.sh status
```

`install` はチェックアウトのコミット済みの `HEAD` から両方の画面とRustの実行ファイルを作ります。LaunchAgent `local.guildbotics` がこの実行ファイルの `server start` を直接起動し、1つのプロセスで両方のポートを待ち受けます。ログイン時と異常終了後にも起動します。正常に停止したあとは、次回のログインか `scripts/hub-server.sh restart` で起動します。

登録時の `PATH` と `BRANVIL_DATA_DIR` をLaunchAgentが使います。GuildBoticsのデータは既定で `~/.guildbotics`、ポートは8765です。画面は `http://127.0.0.1:8765/-/guildbotics/` で配り、`/` はこの場所へ案内します。更新後は `guildbotics open` でブラウザに入り直してください。変更する場合は登録前に絶対パスの `GUILDBOTICS_HOME` または `GUILDBOTICS_SERVER_PORT` を設定します。同じ設定でCLIを実行してください。起動確認後に `guildbotics install` がCLIとスキルを配置します。サーバーのログは `~/Library/Logs/guildbotics.log` で確認できます。

新しいビルドの最初の起動時（インストール・更新とも）、macOSの確認が出たらオーナーが回答してください。スクリプトは最大20分間、起動を待ちます。

- **キーチェーンへのアクセス**：GuildBoticsのSecretを暗号化・復号する端末共通の鍵（サービス名 `GuildBotics`、アカウント名 `agent-environment-credentials`）への許可です。#108 の写しでは、更新のたびに1回でした。回数は保存済みの鍵やアクセス許可の状態によるため、2回出ることを前提に待つ必要はありません。`guildbotics connect` が保存する `guildbotics-server` の項目はCLIの接続用トークンで、この暗号化の鍵とは別です。Branvilの秘密はデータディレクトリにあり、Branvil用の鍵の確認がもう1回出るわけではありません。
- **「書類」フォルダへのアクセス**：macOSのプライバシー保護（TCC）の確認です。サーバーが、メンバーとの受け渡し先 `~/Documents/GuildBotics` や「書類」の下にあるワークスペースなどへアクセスするために必要です。#108 の写しでは、初回起動でも更新でも1回出ました。新しいビルドで再度表示されることがあるので、キーチェーンとは別に許可してください。

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

1. `update` は更新するコミットから画面と実行ファイルを作ります。現在のサーバーは稼働を続けます。
2. 現行ビルドでBranvilのバックアップを取り、別のディレクトリへ復元します。新ビルドでその写しに `guildbotics branvil admin upgrade` と `guildbotics branvil admin check` を実行します。
3. 検査が通ると、Branvilを稼働させたままGuildBoticsの実行中の作業を待ちます。次にBranvilが実行中のCIを `ci.stop_wait_secs` の上限まで待ち、統合サーバーが停止します。新ビルドで実データをアップグレードして検査し、LaunchAgentの起動先を切り替えます。
4. 新ビルドの起動時には、インストール時と同じくキーチェーンと「書類」フォルダへのアクセスの確認に回答してください（対象と回数は上記を参照）。起動を確認できると、新ビルドからCLIとスキルを導入します。

状態は `~/Library/Application Support/GuildBotics/Hub` にあります。`builds/<commit>/` はその版の実行ファイルとソース、`builds/current` はLaunchAgentが起動する実行ファイルへのリンクです。`backups/` に直近3世代の完了したバックアップを保持します。バックアップに含まれるデータと含まれない資格情報は[Branvilのバックアップと復元](../crates/branvil/docs/backup-restore.md)を参照してください。

## 失敗した更新から戻す

更新が失敗すると、実行したターミナルに理由が出ます。Hubの `last-failure` には直近の失敗の時刻、段階、リビジョンが残ります。サーバーの起動エラーは `~/Library/Logs/guildbotics.log` にあります。写しでの検査までに失敗した場合は、現行ビルドで稼働を続けます。

新ビルドが起動する前に切り替えが失敗すると、前のビルドへ起動先を戻します。前のビルドで実データを検査できれば、そのまま再起動します。データの形式が変わって前のビルドで検査できない場合は、データをそのまま残して停止します。サーバーが一度でも応答した場合、バックアップ後の書き込みが含まれる可能性があります。更新時の出力に出たDBのコピーとバックアップを確認し、どのデータを戻すかはオーナーが決めます。[Branvilのアップグレードと復元](../crates/branvil/docs/backup-restore.md#upgrading)に従って元の形式へ戻してから、次のコマンドを実行してください。

```sh
scripts/hub-server.sh recover
```

更新の途中でマシンを再起動した場合も、`builds/update-from` が残っていれば `recover` を実行します。現在のビルドが応答していればそのまま使い、応答していなければ前のビルドの検査と起動をやり直します。強制終了でHubの `operation` ディレクトリが残った場合は、先に更新やビルドのプロセスが終わっていることを確かめてから、そのディレクトリを取り除きます。サーバーが停止していて `builds/update-from` がなければ、`restart` で現在のビルドを起動できます。

起動確認後にCLIやスキルの配置が失敗した場合、起動済みのサーバーは稼働を続けます。チェックアウトの `HEAD` が現在のビルドと同じなら、`scripts/hub-server.sh update` で配置だけを再試行できます。ビルドを変えずに配置を再試行するには、登録時と同じ環境設定で次を実行します。

```sh
"$HOME/Library/Application Support/GuildBotics/Hub/builds/current" install
```

通常停止がBranvilのCIを待っている段階では、GuildBoticsのHTTPポートは閉じています。待機を中断する必要がある場合は、`scripts/hub-server.sh status` でジョブを確認し、そのプロセスにSIGTERMを送ります。

## 自動更新用の内部コマンド

以下はシェルの更新ループ向けの隠しコマンドです。成功時の標準出力は1行の JSON、実行エラーは終了コード1です。状態の判定結果は終了コード0で返します。実データを変更せず、通知だけは外部の webhook へ送信します。

- `guildbotics service status`: `server_running`、サービスの `running`、`state` を返します。サーバー停止中はそれぞれ `false`、`false`、`stopped`。サーバーのロックが指すポートへ接続し、オーナーの証明を検証してから状態を読みます。認証できない場合は停止扱いにせずエラーにします。
- `guildbotics server hub-checks --repo GuildBotics/GuildBotics --sha <full-sha>`: `run_registered` と `state` を返します。`state` は `unregistered`（run 未登録）、`pending`（必須チェック未完了・欠落）、`failure`（必須チェック失敗）、`success`（必須チェック成功）。必須チェックのない登録済み run は `success`。不正な pipeline は失敗です。必須チェックは `success` またはサービスによるパス判定の `skipped` だけを成功とします。
- `--repo` の代わりに `--checkout <path>` を渡すと、そのチェックアウトの origin からリポジトリを取得します。origin は Branvil の clone URL（`{base}/git/{repo}.git`）の形で、設定済み base URL と一致する HTTP(S) URL に限ります。認証情報や query を含まないものに限ります。接続先は設定済み Hub に固定し、プロキシやリダイレクトを使いません。トークンは `BRANVIL_DATA_DIR` の `secrets/owner.token` から直接読み、引数や環境変数では受け取りません。
- `guildbotics server hub-notify`: 標準入力の文を identity の store の webhook へ送り、`sent` を返します。サーバー停止中にも使えます。未設定なら `false`、送信成功なら `true`、送信失敗はエラーです。URL は出力しません。入力する文には秘密を含めないでください。キーチェーンのアクセス権は実行ファイルの署名に従います。
- `guildbotics server hub-migration`: 実行中のバイナリに埋め込まれた `manual_update_version` と `instructions`（手順書の相対パス、不要なら `null`）を返します。ルートの `hub-update.json` が宣言です。宣言のないソースからのビルドでは番号0となります。このコマンド自体のない旧バイナリも更新ループで番号0として扱います。候補の番号が現在の番号より大きければ手作業が必要です。
