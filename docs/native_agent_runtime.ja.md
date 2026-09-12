# Codex・Claude Code・Grok Build・GitHub Copilot・Antigravityのセッション連携

GuildBoticsでCodex、Claude Code、Grok Build、GitHub Copilot、Antigravityを利用する場合は、
Slackスレッドやチケットにセッションを対応付け、前回の続きから作業を再開できます。Codexとの連携には
[Codex App Server](https://developers.openai.com/codex/app-server)を使用し、Claude Codeとの
連携には公式の`stream-json`入出力と`--resume <session-id>`を使用します。Grok BuildとGitHub
Copilotとの連携には、それぞれ`grok agent stdio`と`copilot --acp`が提供する
[Agent Client Protocol](https://agentclientprotocol.com/protocol/v1/initialization)
（ACP）v1を使用します。ACPを使う2つのAI CLIツールは共通のACPクライアントを共有し、
AI CLIツールごとの実装は起動コマンド、認証、セッション設定、独自拡張の通知だけです。

Antigravityだけは「1プロセスで複数ターンを実行する」形になりません。`agy`には常駐サーバ
モードがなく、プログラムから使える口は`agy --print --output-format stream-json`の一発実行
だけです。したがって1ターンが1プロセスであり、セッションの同一性は生きているプロセスでは
なく`--conversation <id>`が担保します。それ以外（厳密な再開、イベントの逐次配信、
トークン使用量、構造化されたエラー分類）は他のネイティブ連携と同じです。

GuildBoticsが実行できるAI CLIツールはこの5つだけです。新しいツールへ対応するには、
本リポジトリにネイティブアダプタを実装します。ワークスペースにYAMLを置いて未対応のツールを
追加する経路はありません。

## 設定

`intelligences/cli_agent_mapping.yml`では、CodexまたはClaude Codeを次のように直接指定します。

```yaml
default: codex
codex: codex
claude: claude
grok: grok
copilot: copilot
antigravity: antigravity
```

各AI CLIツールは`intelligences/cli_agents/<tool>/`配下の定義ファイルも読み込みます。
このファイルが持つのは`parameters:`と`effort:`のオーバーレイ（プロバイダ非依存の`low` / `high`を
AI CLIツールごとの設定へ翻訳するためのもの。書式は[カスタムコマンドガイド](custom_command_guide.ja.md)を参照）と、
後述の`network:`ブロックです。同梱の既定ファイルはこのほかに、設定エディタの型付き編集用の宣言である
`effort_fields:`を持ちます。

## エージェント隔離環境のアクセス許可

AI CLIツールのターンはすべてエージェント隔離環境の中で実行します。この端末でビルドした
snapshotからターンごとにGuildBoticsが起動するmicroVMで、ターンが終わると破棄します。
ターンが到達できる範囲は以下のアクセス契約で、それを強制するのは隔離環境です。プロバイダの
CLIは中で動き、それ以外を見ないので、OSやプロバイダによらず同じ形で強制されます。
プロバイダごとの変換や「どのプロバイダが何を強制できるか」の表はありません。この端末で
隔離環境が用意できない場合（runtimeが無い、snapshotが無い、ログインしていない）は、より広い
権限へ置き換えずにエージェントを起動しません。

隔離環境のruntimeは[microsandbox](https://microsandbox.dev/)（libkrun系のmicroVM）で、
GuildBoticsが同梱し、最初に必要になったときに`~/.guildbotics/data/msb`へ配置してSDKに
そのパスを指させます（`MSB_HOME` / `MSB_PATH`）。ダウンロードはせず、GuildBotics以外が
どのruntimeを動かすかを決めることはありません。必要なハードウェア仮想化はmacOSでApple
Silicon、Windows 11でWindowsハイパーバイザー プラットフォーム、LinuxでKVMです。Windowsでは
runtimeがlistenするsocketに対してWindows Defender Firewallが確認を出すため、固定パスに対する
規則を昇格の確認1回で作成します。Windowsのhostパスは環境の中では`C:\work`→`/c/work`の
ように、ドライブ文字を最上位ディレクトリにした形で見えます（作業ディレクトリもこの規約で
bindします）。

環境の中身（ベースイメージ、GuildBoticsが版を固定して導入するプロバイダCLI、
`config/intelligences/agent_environment.yml`で宣言した追加パッケージ）はsnapshotとして端末ごとに
ビルドし、宣言と一致しなければ再ビルドします。Desktopの **LLM・AI CLIツール** の
「エージェント隔離環境」カードがruntime、snapshotの状態（ビルドボタンつき）、DNSリゾルバ、
ツールごとのログインを示し、CLIでは`guildbotics environment status` / `build` / `login`が
同じ状態と操作です。サービス稼働中は宣言の変更（他端末からの同期で届いたものを含む）を
自動で再ビルドし、ビルド中と環境が使えない間はticket patrolとchat dispatchを失敗ではなく
見送りにします。turnが起動できない理由（runtime無し、宣言不正、snapshot未ビルド、
未ログイン）は、同じ文言で画面上部の状態異常にも出ます。

macOS では、**システム設定 → プライバシーとセキュリティ → ファイルとフォルダ**で、GuildBotics を起動しているアプリに書類フォルダへのアクセスを一度許可してください。開発中（`tauri dev`）は、起動に使ったターミナルや Visual Studio Code が対象です。環境の状態表示と turn の開始前にディレクトリへのアクセスを確認し、許可がなければ CLI と Desktop に同じ拒否理由を表示します。

- **作業ディレクトリ**: ターンの`cwd`（チケット作業ならメンバーのclone、内部処理なら
  `<workspace>/.guildbotics/local/work/...`）は、hostと同じパスに読み書きでbindします。
  ワークスペースの`.guildbotics/config`や`state`は含みません
- **作業ディレクトリの外**は2つあり、どちらもhostと同じパスに、hostのホームディレクトリと同じ
  パスのホームの下でbindします。**documents**: 作業で読み書きするホームディレクトリ配下の
  ディレクトリ（`read` / `read_write`、相対パスのみ）。無ければターン開始時に作成し、
  `intelligences/cli_agent_filesystem_grants.yml`で共有します。このファイルとは別に、
  `Documents/GuildBotics`（受け渡しフォルダ）は常に読み書きで許可されます: Desktopから渡した
  ファイル（貼り付け画像、隔離環境から届かないファイルのコピー）はその`tmp/`に置かれてアプリの
  終了時に消え、作業ディレクトリを指定しないDesktopからの実行はここで動き、エージェントが作った
  成果物は指定が無ければこの下に出ます。Desktopが入力欄に入れるパスは隔離環境の綴り
  （Windowsでは`C:\...`ではなく`/c/...`）で、エージェントはそれをそのまま開けます。**この端末の設定**: 追加パス
  （絶対パス可、存在が必要）と、開いている場所の一部を閉じる`deny`。
  `local/cli_agent_filesystem_grants.yml`に置き、同期しません。認証情報のディレクトリ
  （`~/.ssh`、プロバイダ自身のディレクトリ）とワークスペース自身の`.guildbotics`は同梱のdenyで
  常に閉じます。それ以外のhostのもの
  （PATH、他のclone、キーチェーン）は中に存在しません。エージェントの道具は隔離環境自身のもので、
  `config/intelligences/agent_environment.yml`に宣言します
  （[`guildbotics environment`](cli_reference.md#guildbotics-environment)を参照）。

  ```yaml
  # config/intelligences/cli_agent_filesystem_grants.yml（共有）
  documents:
    - path: Documents/shared-documents
      access: read
    - path: Projects/generated-assets
      access: read_write
  ```

  ```yaml
  # local/cli_agent_filesystem_grants.yml（この端末だけ）
  paths:
    - path: .cache/uv
      access: read_write
  deny:
    - Documents/shared-documents/private
  ```

- **ネットワーク**: 選択したAI CLIツール定義（`cli_agents/<tool>/<slot>.yml`）の`network:`
  ブロックで、シェルコマンドとその子プロセス、ツール組み込みのweb検索・URL取得のどちらで
  到達するかによらず1つの規則です（隔離環境のgatewayは両者を区別できません）。`mode`は
  `deny` / `allowlist` / `unrestricted`のいずれか（`off`はYAMLの真偽値として読まれるため
  使いません）、`allowed_domains`は`allowlist`でのみ使い、`allow_local_network`はlocalhostと
  LANも開きます。同梱の既定は閉じています。`network:`を省いたスロットはツールの`default.yml`
  からブロック全体を継承し、書いたスロットは全体を書きます。プロバイダ自身のAPIドメインと
  localhostのmember brokerはモードによらず常に到達でき、設定ではなくGuildBoticsが決めます。

  ```yaml
  network:
    mode: allowlist
    allowed_domains: [registry.npmjs.org]
    allow_local_network: false
  ```

これらはDesktopの **LLM・AI CLIツール → 詳細設定** から編集できます。「ワークスペース共通の
ディレクトリ」カードがdocumentsを、「この端末のディレクトリ」カードがここで足した追加パスと
禁止を持ち、どちらも入力または選択したパスを保存前に判定します（存在有無、認証情報を含む
場所の警告）。各メンバーのスロットはターン開始時と同じ解決処理で判定され、この端末で
起動できないメンバーはメンバー一覧に理由付きで示され、設定を直すまで画面上部に状態異常が出ます。

隔離環境の中では各プロバイダ自身のsandboxも有効のままですが、環境より狭めることはしません。
microVMの中にあるものはすべて許可済みなので、内側のsandboxが足すのは、エージェントのコマンドから
プロバイダ自身の認証情報を隠すことと、プロバイダ設定の変更をターンをまたいで残さないことです。
Codexは環境のmountをそのまま写したpermission profileで動きます: guest全体を読め、環境がbindした
各ディレクトリはmountされたとおりに書き込み可または読み取り専用（`read_write`のgrantはCodexの
コマンドからも読み書きできる）、作業ディレクトリ（`.git`を含む）と一時ディレクトリは書き込み可、
ネットワークは有効、`~/.codex`だけを隠します。profileで`/`を書き込み可能にすると、Codex 0.153では
`/dev/null`へ書けなくなるため、`/`は指定しません。Codexは常に
非対話の`never` approval policyで動き、予期しない確認要求は拒否します。Claude Codeは
`bypassPermissions`と`sandbox.enabled=false`で動きます（microVMの中ではrootなので、Claude Codeが
rootでの`bypassPermissions`を拒否しないよう`IS_SANDBOX=1`を渡します）。Grok Buildは`--sandbox off`と
`--always-approve`（LinuxのprofileはLandlockを要し、環境のkernelには無いため。Grokは強制できないprofileでは起動を拒否する）、GitHub Copilotは`--no-remote-export`と`allow_all: on`（読み取り専用ターンでは
`off`にして全要求を拒否）、Antigravityは`--dangerously-skip-permissions`で起動し、設定から
フラグは注入されません。各プロバイダの内側sandboxがmicroVMのkernelで動くかはプロバイダを
provisionするたびに実機で確認し、Codexは同梱のbubblewrapで動くことを確認済みです（imageに
bubblewrapを入れると同梱のものより優先され、Codexのhelperを起動できないため、imageには入れません）。

ターンをまたいで残るのはプロバイダの永続状態（認証情報とセッション）だけで、この端末の
store（`~/.guildbotics/data/agent_environment/<provider>/`）からbindして全メンバーで共有します。
プロバイダの設定やskillはsnapshot側のもので、ターンごとに元へ戻ります。読み取り専用ターンは
member brokerが強制します（person leaseを持たず、書き込み系のmemberコマンドをすべて拒否
します）。そのターンでプロバイダが自身のファイル操作ツールで何をしたかは承認イベントに
記録されますが、境界ではありません。

有効になったpolicyと承認の判断は、プロバイダ非依存の診断イベントとして記録します。型の誤り、
廃止したキー、未知の値は検証で失敗し、有効な境界が黙って変わることはありません。

## 認証

AI CLIツールは隔離環境の中で動くため、hostにインストールする必要はなく、hostでのログインも
turnには使われません。ログインは環境の中で行います。turnを実行する端末ごとに、ターミナルで
`guildbotics environment login <tool>`（`codex` / `claude` / `grok` / `copilot` / `antigravity`）を実行すると、そのツール自身の
ログインコマンドが環境の中で起動し、device code方式でブラウザ承認を案内します。結果は
端末のstore（`~/.guildbotics/data/agent_environment/<tool>/`）に保存され、その端末の全メンバー・
全ワークスペースで共有し、turnごとに認証情報とセッションだけを環境へbindします。
GuildBoticsのセッション情報や診断記録には複製されません。Desktopはログイン状態と
実行すべきコマンドを示し、Desktop自身がログインの対話を行うことはありません。

loginは環境の中の端末（TTY）で動かします。認証情報をファイルに保存する前に確認するツールは端末でしか
確認しないためで、Copilotは環境にキーチェーンが無いことを検出し、state root配下への平文保存を1回
確認します。Grok Buildはdevice code方式のloginです。Antigravityにはloginコマンドが無く、保存済み
loginの無いprint mode実行がGoogleのサインインURLを表示して認可コードを標準入力から受け取ります
（60秒以内）。Grok Build自身のsandbox profileはLinuxでLandlockを要し、環境のkernelには無いため、
環境の中では`--sandbox off`で動きます（境界は環境です）。

設定カードには、認証情報が保存済みでもログインコマンドとコピー操作が表示されます。
macOS / Linuxでは管理CLIの絶対パス、WindowsではPATH上の`guildbotics`を使います。
カードを開いている間、状態は10秒ごとに自動更新されます。**状態を更新** ですぐに読み直すこともできます。
**認証情報保存済み** はファイルの存在だけを表し、有効性の確認ではありません。
turnの構造化された認証失敗は、端末・ツールごとにプロバイダのマウント領域の外へ保持し、
`status.py`を通じてカードとalertへ反映します。認証情報が残るログイン完了、または別メンバーを
含む後続の正常実行で解除します。それ以外のエラーは前回の認証結果を変えません。
過去の認証失敗は案内として扱い、turnの起動を拒否しないため、再試行できます。
GuildBoticsによる認証probeやトークン更新は行いません。

Grok Buildでは、ACPの`initialize`が提示した認証方式のうち、保存済みログインを使う
`cached_token`だけを選択します。APIキー方式は使用しません。APIキーは環境変数でしか
プロセスへ届かず、後述のとおりAI CLIツールの環境からは認証情報名の変数を取り除くためです。
ブラウザを開く`grok.com`の対話認証は、headless実行
中に自動で開始しません。保存済みの認証がない場合は認証エラーとして停止し、`grok login`
（または`grok login --device-auth`）の実行を案内します。診断記録に残すのは選択した認証方式の
識別子だけで、`~/.grok/auth.json`の内容は読み取りません。

GitHub Copilotが提示する認証方式は`copilot-login`の1つだけで、その付随情報には「端末で
`copilot login`を実行する」と記載されています。GuildBoticsはこの方式でACPの`authenticate`を
呼び、保存済みログインの有無だけを確認します。保存済みの認証情報で認証できた場合は即座に応答が返ります。
対話的なログイン操作をGuildBotics側から開始することはありません。`authenticate`が拒否された
場合、認証方式が提示されない場合、応答が返らない場合（利用者のいない環境で端末ログインを待って
いる状態）は、いずれも認証エラーとして停止し、`copilot login`の実行を案内します。診断記録に
残すのは認証方式の識別子だけで、Copilotの認証情報保存先の内容は読み取りません。

GitHub、Git、SSHへの書き込みに使う認証情報は、これらのAI CLIツールのプロセスへ渡しません。
親プロセスの環境変数のうち、名前に`TOKEN`、`SECRET`、`PASSWORD`、`PRIVATE_KEY`、`API_KEY`を
含むものはすべて取り除きます。除外する名前を列挙するのではなくパターンで判定するのは、列挙は
「追加を忘れた秘密」だけを残す形になるためです。さらに、ワークスペースのSecretStoreに保存された
キーは名前に関係なく取り除きます。`guildbotics secrets set`は任意のキー名を受け付けるため
（例: `DATABASE_URL`）、「秘密ストアに保存された」という出所そのものを判定根拠にします。
名前パターンは、シェルからexportされたGuildBotics管理外の認証情報を拾う保険として機能します。
これにより、メンバー自身の
`{PERSON_ID}_GITHUB_ACCESS_TOKEN` / `_SLACK_BOT_TOKEN` / `_SLACK_APP_TOKEN`と、
LLMプロバイダのAPIキー（`OPENAI_API_KEY`など）も渡りません。いずれもGuildBoticsのプロセス内で
消費するものであり、member CLIは自分でOSキーチェーンから読み込むため、この除去の影響を
受けません。認証情報を渡す代わりに呼び出させる`git`/`ssh`のhelperとsocket
（`GIT_ASKPASS`、`SSH_ASKPASS`、`SSH_AUTH_SOCK`）も取り除きます。
親プロセスのworkspace root、run識別子、execution delegationも同様に取り除きます。有効な
delegationは単なる識別ラベルではなくそのまま使えるgrantであるため、継承させると、providerのプロセスがmember CLIを
直接呼び出し、自身のtransportが敷いている境界を迂回できてしまいます。この情報を正当に運ぶ
brokerだけが、実行contextと保持中のleaseから明示的に再注入します。

Codex、Claude Code、Grok Build、GitHub Copilot、Antigravityには、`127.0.0.1`にbindした
adapter専用のHTTP MCP endpointと、推測困難なbearer grantを渡します。ACPを使うGrok Buildと
GitHub Copilotはsessionの`mcpServers`、CodexとClaude Codeはprocess単位のMCP設定を使います。
Codexは生のtokenを専用環境変数から`bearer_token_env_var`で読み、必要な
`Authorization: Bearer` prefixをCodex自身が付けます。Antigravityは`--add-dir`で追加した専用の
補助workspaceにある`.agents/mcp_config.json`を読みます。process cwdとprimary workspaceは、
memberの実作業ディレクトリのまま維持します。
唯一の`guildbotics_member` toolが受け取るのは、固定された
`guildbotics member` entrypointのtoken化済み引数だけです。実行ファイルやshellを選ぶこと、
workspaceを上書きすること、別personとして動くことはできません。endpointはproviderのsandbox外に
あるGuildBotics processで動作し、turn実行中だけ利用でき、毎turn更新する第2のgrantも要求し、
adapterとともに停止します。各provider processへmember execution leaseやdelegation identityを渡しません。

brokerはmember CLIを別のtrusted processとして起動するため、OS KeychainなどのSecretStore backendを
そのまま利用できます。有効期間の短いleaseは、そのCLI processだけへ渡します。CLIの
`--workspace`には常に選択中のGuildBotics workspace rootを指定し、child processのcwdはmemberの
隔離作業ディレクトリのまま維持します。workspace data rootはこれらと独立して上書きできます。
read-only turnではdelegationを渡さないため、既存のmember CLI guardが書き込み可能なcommandを
すべて拒否します。全native adapterがこの同じmember capability境界を使用します。

## Slackスレッド・チケットとセッションの対応付け

セッションの対応付けには、`person + adapter + work kind + stable work identity`からなる
会話識別子を使用します。

- チケット: issueまたはpull requestの正規URL。同じ作業の完了条件が満たされず再試行するときだけ、
  同じセッションから再開します。作業完了後に同じチケットから新しい依頼を受けた場合は、新しい
  セッションを開始します。
- Slack: `slack:<bot-user-id>:<channel-id>:<thread-root-ts>`。同じSlackスレッドへの追加依頼は、
  保存済みのセッションから再開します。処理済み位置を示すcursorは、応答が正常に完了した後だけ
  更新します。
- 手動実行: 呼び出し元が作業を識別する値を明示します。

### Slackスレッドの文脈を渡す方法

チャットワークフローは、最新のイベントと、最大件数を設けたSlackスレッドの履歴を別々に
実行基盤へ渡します。実行基盤は、AI CLIツールがセッションを引き継げる範囲に応じて、
実際に送る内容を次のように選びます。

- Codex、Claude Code、Grok Build、GitHub Copilot、Antigravityの既存セッションを引き継ぐ場合は、セッション内に
  保持されている文脈へ最新のイベントだけを追加します。安全に新しいセッションへ切り替えられるよう、
  ワークフロー側でもSlackスレッドの履歴を更新しますが、引き継ぎ中のセッションへその履歴を重ねて
  送りません。
- これらのAI CLIツールで新しいセッションを開始するときや、セッションを切り替えたときは、
  最新のイベントより前のSlackスレッドの履歴と最新のイベントを一度だけ送ります。

Slack APIからスレッドの履歴を安全に取得できない場合は、新しいセッションを開始するとき、
またはセッションを切り替えたときに限り、AI CLIツール自身にSlackスレッドを確認させます。
この動作を内部では`inspect_required` fallbackと呼びます。

正常なセッションを引き継ぐ場合は、保存済みのセッションと最新の
イベントだけを使用します。そのため、`inspect_required` fallbackを理由に、それまでの会話履歴を
重複して送ることはありません。

Slackイベントの処理済み位置を示すcursorは、AI CLIツールからの応答が正常に完了した後にだけ
更新します。応答が失敗した場合はcursorを進めないため、未処理のイベントが失われることは
ありません。完了条件を満たさず同じcursorから再試行する場合は、新しい依頼ではなく、直前の
作業の続きとして扱います。

セッションとの対応付けは、
`<workspace-data-root>/agent-runtime/conversations/<person>/<adapter>/`へ安全に保存します。
保存内容には、AI CLIツールのセッションIDとturn ID、cursor、使用量、セッション文脈量、
セッションの状態、世代、切り替え理由が含まれます。ACPには標準のturn IDがないため、ACPを使う
AI CLIツールではJSON-RPCのリクエストIDをturn IDとして保存せず、空のままにします。AI CLIツールの認証情報と、プロトコルから受信した未加工データは
保存しません。

GuildBoticsは、AI CLIツール側の「最新のセッション」や暗黙の会話継続には依存せず、保存した
セッションIDを明示して再開します。セッションが存在しない場合や、正常に再開できない状態の場合、
`resume`は失敗します。再開方法が`auto`の場合は新しいセッションを開始し、文脈を再構築します。
キャンセル、不正または不完全なストリーム、プロセスの失敗、AI CLIツール側での文脈圧縮、
有効期間・turn数・使用量の上限、モデルの変更が発生した場合も、新しいセッションへ切り替えます。
Codexの`contextCompaction`とClaude Codeの`compact_boundary`は、GuildBotics内では同じ種類の
イベントとして記録します。文脈圧縮が完了したturn自体は成功として扱い、次の依頼で新しい
セッションを開始してSlackスレッドの履歴を再構築します。

Grok BuildにはACP標準の文脈圧縮通知がないため、xAI独自拡張の`auto_compact_started`などの
通知を`context_compaction`として正規化します。0.2.114ではACP標準の`usage_update`が送られて
こないため、そこから得るセッション文脈量（`used` / `size`）による90%到達時の`context_limit`
切り替えは、この版では作動しません。`usage_update`を送る版に備えて処理自体は実装しており、
その場合は`used`の減少も文脈圧縮の検出手段として併用します。

0.2.114でトークン使用量が届く経路は、xAI独自拡張の`turn_completed`だけです。ここに含まれる
`inputTokens` / `outputTokens` / `cachedReadTokens` / `reasoningTokens` / `totalTokens`を
共通のトークン項目へ正規化するため、有効期間・turn数・使用量の上限による切り替えは通常どおり
機能します。`costUsdTicks`、`modelCalls`、`apiDurationMs`はトークン数ではないので、使用量とは
合算せず診断記録の詳細情報として保持します。

xAI独自拡張は`_x.ai/session_notification`と`_x.ai/session/update`の2経路で届くため、どちらも
同じように処理します。これら以外の`_x.ai/*`は画面表示用の状態通知であり、ターンごとに1件へ
集約します。0.2.114で内容を確認済みの経路（`_x.ai/queue/changed`、`_x.ai/sessions/changed`、
`_x.ai/settings/update`、`_x.ai/announcements/update`など）は、送信したプロンプト本文や
ワークスペースのパスを含むため、件数だけを記録します。未知の経路が現れた場合は、経路名・件数に
加えてpayloadの第一階層のフィールド名だけを残します。診断記録の伏せ字処理はマッピングのキーに
対して働くため、payloadを文字列化して保存すると秘密情報がそのまま残ります。値は保存しません。

ツール実行の種別は、ACPの`kind`が`execute`のものをコマンド、`edit` / `delete` / `move`の
ものをファイル変更として記録します。`locations`は変更したファイルだけでなく読み取っただけの
ファイルも含むため、種別の判定には使わず、関係するパスの記録にとどめます。`tool_call_update`は
`toolCallId`以外を省略できるので、開始時に宣言された種別を`toolCallId`ごとに保持し、種別を
含まない完了通知にも同じ種別を適用します。

推論（reasoning）の途中経過は`agent_thought_chunk`として届きますが、回答本文には含めません。
応答として組み立てるのは`agent_message_chunk`だけです。

ACPの`session/prompt`はターンが終了した時点で応答が返るため、この要求だけはリクエスト単位の
締め切りを設けません。ターン全体の実行時間は上位のターンタイムアウトで制限し、超過した場合は
`session/cancel`を送ってプロセスグループごと停止します。initializeやsession/loadなど、即座に
応答が返る要求にはリクエスト単位の締め切りを維持します。

実行中のプロセスがすでに開いているセッションは、再度読み込みません。会話はプロセス内に残って
いるため再送すべき履歴がなく、Copilotはこの場合`already loaded`エラーを返します。再読み込みを
行うのは、保存済みのセッションIDしか手掛かりがない再起動後のプロセスだけです。どちらの場合も
そのターンの設定は改めて適用します。

ACPを使うAI CLIツールの正確な再開には、`initialize`が提示した機能に応じてACPの`session/resume`
または`session/load`を使用します。Grok Build 0.2.114とGitHub Copilot CLI 1.0.77はどちらも
`sessionCapabilities.resume`を提示しないため、`session/load`を使用します。`session/load`はセッション全体の履歴を再送してから応答を返すため、
その応答を境界として、再送された履歴を現在のturnのイベント、Slackへの投稿、通常の実行記録から
除外します。再送された件数だけを診断記録に残します。履歴は標準の`session/update`だけでなく
xAI独自拡張の経路でも再送され、前回turnの`turn_completed`（トークン使用量）が含まれます。
これらも履歴として数えるだけで解釈しないため、前回のトークン使用量が今回のturnの使用量として
二重に計上されることはありません。

保存したセッションとの対応付けは、次のコマンドで明示的にリセットできます。

```bash
guildbotics member agent conversation reset \
  --person aiko --adapter codex --work-kind ticket \
  --work-identity https://github.com/GuildBotics/GuildBotics/issues/300
```

Slackの場合は、前述の`slack:<bot-user-id>:<channel-id>:<thread-root-ts>`形式の識別子を
`--work-identity`に渡します。

## 並行実行と停止

OSのadvisory lockを使った実行権の管理により、スケジューラー、チャット、手動実行のAPIやCLI、
別のGuildBoticsプロセスをまたいでも、同じメンバーのAI CLIツールが同時に実行されないように
します。異なるメンバーの作業は並行して実行できます。AI CLIツールから呼び出された
`guildbotics member ...`コマンドは、メンバー、実行権、委任情報、実行ID、実行中のプロセスID、
保持中のロックがすべて一致した場合にだけ受け付けます。

これらのAI CLIツールのプロセスは、独立したプロセスグループとして起動します。キャンセル、
サービスの停止、通信エラー、実行コンテキストの終了時には、グループ全体を停止して終了を確認します。
そのため、GuildBoticsの停止後にAI CLIツールのプロセスだけが背後で動き続けることはありません。

## 利用制限・認証エラーと診断記録

認証切れと利用制限（rate limit）は、AI CLIツールが出力する構造化データを使って判定します。
Claude Codeでは`rate_limit_event`を使用し、そのepoch形式`resetsAt`を正確な`retry_after_at`
として採用します（`system/api_retry`のリトライ遅延より優先）。Codexではアカウント情報や
rate limitに関するRPCデータを使用します。標準エラー出力に表示される、人間向けの
エラーメッセージには依存しません。

Antigravityでは、終端の`result`イベントで判定します。`status`が`SUCCESS`以外なら異常とみなし、
同じイベントの`error`フィールドで種別を決めます。`agy` 1.1.10は利用制限と認証エラーをコード
ではなく文章で報告するため、この1フィールドだけをアダプタ内の限定的な正規表現と照合します。
文章に含まれる復帰時刻（`Resets in 1h23m`）は、他のツールと共通の正規化処理へ渡します。
利用制限ではセッションを切り替えず、認証・プロトコル・プロセスの失敗では切り替えます。

再開可能な時刻を取得できた場合は、その時刻まで対象チケットの選択と保留中のチャット処理を
延期します。この待機によって、同じプロセス内で行う完了条件未達時の再試行回数を消費することは
ありません。

診断記録には`agent_runtime.*`、`workflow.rate_limited`、`credential.failed`、`credential.verified`
というイベント名を使用します。メンバー、実行、会話識別子、セッションの世代、AI CLIツールのセッションIDとturn ID、
cursor、実行権を記録し、同じ作業に属するイベントを対応付けます。機密情報を含む可能性がある項目は
伏せ字にし、長い文章は上限を設けて切り詰めます。記録はDesktopの診断画面、または
`<workspace-data-root>/run/diagnostics.jsonl`から確認できます。

`unsupported_version`が記録された場合は、使用しているAI CLIツールを更新してください。
Claude Codeでは`--input-format`、`--output-format`、`stream-json`、`--resume`、`--mcp-config`、
`--strict-mcp-config`への対応を確認します。2.1.246より前はstrict modeでもproject MCP serverの
承認待ちが起こりうるため、workspaceに`.mcp.json`がある場合は2.1.246以降を必須とします。
CodexではApp Serverの初期化処理を通して、必要な機能に対応しているか確認します。
ACPを使うAI CLIツールでは、`initialize`が返すプロトコル版数が1であることと、正確な再開に必要な
`loadSession`または`sessionCapabilities.resume`のいずれかが提示されることを確認します。
ACPを使うadapterではさらに、trusted member capability transportに必要なHTTP MCP対応も確認します。
バージョン文字列では判定しないため、これらのcapabilityを提示する新しい版はそのまま利用できます。
Antigravityでは`agy --help`（標準エラー出力へ表示し、終了コード0で終わります）を読み取り、
`--print`、`--output-format`、`--conversation`、`--model`、`--effort`、`--add-dir`への対応を
確認します。動作確認済みの基準バージョンは、Grok Build 0.2.118、GitHub Copilot CLI 1.0.77です。
Antigravity 1.1.11が必要なflagを公開することは確認済みですが、追加した補助workspaceからMCP設定を
読み込めることは、trusted member transportの対応版と宣言する前の実機確認項目として残します。

Grok Buildの利用制限は、ACPまたはxAI独自拡張が構造化データを返した場合にだけ`rate_limited`
として分類します。標準エラー出力や応答本文の解析は行いません。xAIのretry-state通知は
そのturn全体の構造化データとして扱います。Grok Buildが`is_rate_limited`を通知した後に
コードだけのRPCエラーでturnを終えた場合、その失敗は`rate_limited`として分類され、
エージェントの再試行ではなくworkflowのrate-limit退避へ進みます。Codexの
`account/rateLimits/read`に相当する利用量取得手段はACP経由では公開されていない
（`x.ai/session/usage`は`Method not found`、0.2.114で確認）ため、Grok Buildでは
週間・5時間枠の利用量メーターを提供しません。Activity Historyでは「使用量情報なし」を通常の
状態として扱い、利用率0%のような値は生成しません。

GitHub Copilot CLI 1.0.77は、ACP経由でトークン使用量をまったく報告しません。標準の
`usage_update`も、独自拡張の通知も届きません。そのためGitHub Copilotでは使用量が空のままとなり、
有効期間・turn数・使用量・`context_limit`による切り替えはこの版では作動しません。標準の
`usage_update`を処理する実装はあるため、これを送る版では変更なしで機能します。GitHub Copilotの
利用制限も、RPCエラーの構造化データ（週間上限を示す`user_weekly_rate_limited`など）からのみ
`rate_limited`として分類し、標準エラー出力や応答本文は解析しません。分類できないエラーは
プロトコルエラーとして扱い、セッションを切り替えて回復します。

Antigravityはターンごとのトークン使用量（`input_tokens` / `output_tokens` /
`thinking_tokens` / `cache_read_tokens` / `total_tokens`）を報告するため、共通のトークン項目へ
正規化して扱います。一方でセッション文脈量の絶対値は報告しないため、文脈使用率による切り替えは
作動せず、有効期間・turn数・トークン累計の上限だけが機能します。これはGrok Buildと同じ状況です。
