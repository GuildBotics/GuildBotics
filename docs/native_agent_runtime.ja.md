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
このファイルが持つのは`parameters:`と`effort:`のオーバーレイで、
プロバイダ非依存の`low` / `high`をAI CLIツールごとの設定へ翻訳するためのものです
（書式は[カスタムコマンドガイド](custom_command_guide.ja.md)を参照）。
設定として書くのはこの2つだけです。同梱の既定ファイルはこのほかに、
設定エディタの型付き編集用の宣言である `effort_fields:` を持ちます。

ユーザーが変更できる実行時の**境界**は、
`intelligences/native_agent_policy.yml`でAI CLIツールごとに指定するファイルアクセス範囲だけです。

```yaml
codex:
  filesystem_access: workspace

grok:
  filesystem_access: workspace

copilot:
  filesystem_access: workspace
```

新しいワークスペースを作成すると、このファイルがパッケージのテンプレートからコピーされます。
このファイルがない既存のワークスペースでは、設定を保存するまでパッケージの既定値を使用します。
チーム共通の設定は、Desktopの **LLM・AI CLIツール → 詳細設定** で変更できます。
メンバーごとの設定では、チーム設定を継承するか、個別の値を保存できます。個別設定は
`team/members/<person_id>/intelligences/native_agent_policy.yml`に保存されます。

画面を利用できない環境ではYAMLを直接編集できます。`filesystem_access`には、既定値の
`workspace`または`host`を指定できます。`workspace`ではファイルアクセスをワークスペース内に
制限し、`host`ではファイルアクセスの制限を設けません。どちらの場合もネットワークアクセスは
有効です。Codexには操作の確認を求めない`never`を常に指定し、Codexから予期しない確認要求が
届いた場合は拒否します。ネットワークアクセスと確認方法はユーザー設定として公開しません。

Grok Buildでは、`workspace`が`--sandbox workspace`、`host`が`--sandbox off`に対応します。
起動時のコマンドは`grok --no-auto-update --sandbox <profile> agent --always-approve stdio`で
固定し、任意のCLIオプションを設定から注入することはできません。`--always-approve`は必ず
sandboxと併用し、ACPの`session/request_permission`で予期しない確認要求が届いた場合は拒否して
診断記録へ残します。拒否の際は、要求に含まれる`options`から`reject_once`（無ければ
`reject_always`）のoption IDを選んで返します。option IDは要求ごとにGrokが決める識別子であり、
種別名をIDとして送り返しません。拒否用のoptionが提示されない場合は、許可用のoptionへ
読み替えず`cancelled`を返します。headless実行中にCLIが自動更新されないよう`--no-auto-update`を常に渡し、
ユーザーの`config.toml`は書き換えません。

GitHub Copilotでは、`workspace`がCopilot自身の既定動作（作業ディレクトリとシステムの一時
ディレクトリにファイルアクセスを制限）に対応し、`host`ではその検証を無効にする
`--allow-all-paths`を追加します。ただし読み取り専用のターンでは、メンバーが`host`を設定して
いても作業ディレクトリ内に制限したまま実行します。読み取り専用のターンはログ・チケット・チャット
など信頼できない記録を読み取る前提であり、許可されたパス内の読み取りには確認要求が発生せず、
読み取った内容はそのターンの応答から外部へ出ます。書き込みを拒否するだけではこの経路を塞げない
ため、アクセス範囲自体を狭めます。起動時のコマンドは
`copilot --acp --no-auto-update --no-remote-export [--allow-all-paths]`で固定し、任意のCLI
オプションを設定から注入することはできません。`--no-remote-export`は、メンバーのセッションが
GitHubのWebやモバイルへ書き出されたり、そこから操作されたりすることを防ぎます。セッションには
ワークスペースの内容が含まれ、指示はGuildBoticsからのみ受け取るべきだからです。

Copilotの承認方針は起動オプションではなくセッション設定項目のため、ターンごとに指定します。
通常のターンは`allow_all: on`で実行し、確認要求は発生しません。読み取り専用のターンは
`allow_all: off`で実行するため、ファイル書き込み・シェル実行・URL取得のたびにCopilotが確認を
求め、GuildBoticsはそのすべてを拒否して診断記録へ残します。許可されたパス内の読み取りは確認なしで
実行できるため、読み取り専用のターンは通常どおり調査を行えます。拒否の方法はGrok Buildと同じで、
要求に含まれる`reject_once`（無ければ`reject_always`）のoption IDを返し、拒否用のoptionが
提示されない場合は`cancelled`を返します。

モデルと推論の深さ（reasoning effort）もセッション設定項目であり、セッションの作成または再開後に
`session/set_config_option`で適用します。Copilotは未知の設定項目IDに対してエラーではなく空の
応答を返すため、Copilotが返す設定項目一覧を読み取り、実際に適用された値を診断記録の設定イベントと
して残します。要求した値をそのまま記録することはありません。適用されなかった項目は`rejected`として
記録し、警告を出力します。これらの設定は実行中のセッションへいつでも適用できるため、効きの強さや
モデルを変更してもセッションを切り替えません。

Antigravityのモデルと効きの強さは毎ターンのコマンドラインで渡り、再開した会話でも
`--model`の変更が反映されるため、設定を変えてもセッションを切り替えません。`--model`と
`--effort`は併用できません。`agy models`が提示するモデルIDは、いずれも効きの強さをID自体に
含む（`gemini-3.6-flash-low`）か、`--effort`自体を受け付けない（`claude-sonnet-4-6`）ため、
両方を渡すと`agy`がターンを拒否します。両方を設定したスロットではモデルを採用して効きの強さを
落とし、その事実を設定イベントへ記録します。`agy models`に無いモデルは警告のうえ落とし、
カタログを取得できない場合は検証を省略してそのまま実行します。`agy`がモデル名を報告するのは
コマンドラインで明示した場合だけなので、`--model`を渡さないターンではアカウント既定の
モデル名を推測せず、空のモデル名を記録します。

Antigravityは`native_agent_policy.yml`の対象外です。`agy --sandbox`が制限するのはターミナル
実行だけで、`agy`自身のファイル書き込みツールは作業ディレクトリの外へ到達できます。
`filesystem_access`として公開すると、実際には守られない範囲を約束することになるためです。

Antigravityは毎ターン`--dangerously-skip-permissions`を
指定し、あわせて`--add-dir <cwd>`を渡します。後者は省略できません。これが無いと`agy`は
`run_command`を含むすべてのツールを、メンバーのワークスペースではなく`agy`自身の作業用
ディレクトリに対して解決します。

**Antigravityでは、読み取り専用のターンをプロバイダ側で担保できません。** `agy` 1.1.10が
提供する3つの手段はいずれも成立しませんでした。`--mode plan`は
`--dangerously-skip-permissions`と併用しても書き込みが通り、`--sandbox`はシェル実行しか
制限せず`agy`自身のファイルツールには及ばず、権限スキップを外すとheadlessモードがコマンドを
すべて自動拒否して応答が空になるため、読み取り専用のターンが調査そのものを行えなくなります。
そのため読み取り専用のターンも通常のターンと同じ引数で実行し、承認イベントに必ず
`read_only_enforced: false`を記録して、担保していないことを診断記録から確認できるようにします。
他の層の防御はそのまま効きます。読み取り専用のターンはperson leaseを取得しないため、
書き込み系の`guildbotics member`コマンドは`validate_delegation`で失敗し、後述の認証情報の分離に
より直接の`git push`や`gh`も認証できません。塞げていないのはワークスペース内のローカル
ファイル書き換えと任意のシェル実行です。`agy`が本物の読み取り専用モードを備えた時点で見直します。

Claude Codeは、操作ごとの確認を省略する`bypassPermissions`で常に実行します。Bash sandboxはチケット作業やチャットからの依頼に必要な
幅広いコマンドと互換性がないため、`sandbox.enabled=false`も明示します。ただし、これらより
優先されるClaude Codeの管理ポリシーがある場合は、その設定に従います。Claude Codeの
確認方法とsandboxはワークスペース設定に保存せず、Desktopにも設定項目を表示しません。

実際に適用した設定と操作ごとの承認判断は、AI CLIツールに依存しない共通形式の診断記録へ
保存します。Codexで`host`を選択した場合と、Claude Codeを`bypassPermissions`で実行する
場合は、ワークスペース外のファイルも変更できます。認証情報の分離を維持し、ワークスペース外の
アクセスを許容できる環境で使用してください。不正な型、廃止された設定項目、未知の値が指定された
場合は、別の権限へ暗黙に置き換えず、設定エラーとして停止します。

## 認証

GuildBoticsを起動する前に、使用するAI CLIツールをインストールしてください。その後、
GuildBoticsのサービスを実行するOSユーザーと同じユーザーで、各ツールの標準的なログイン操作
（`codex login`、`claude auth login`、`grok login`、`copilot login`、または`agy`の
初回起動時のログイン）を行います。ログイン情報は各ツール自身の
認証情報保存先にだけ保持され、GuildBoticsのセッション情報や診断記録には複製されません。

Grok Buildでは、ACPの`initialize`が提示した認証方式のうち、保存済みログインを使う
`cached_token`だけを選択します。`XAI_API_KEY`が設定されていて、かつCLIが`xai.api_key`を
提示した場合に限りAPIキー方式を使用します。ブラウザを開く`grok.com`の対話認証は、headless実行
中に自動で開始しません。保存済みの認証がない場合は認証エラーとして停止し、`grok login`
（または`grok login --device-auth`）の実行を案内します。診断記録に残すのは選択した認証方式の
識別子だけで、`~/.grok/auth.json`の内容は読み取りません。

GitHub Copilotが提示する認証方式は`copilot-login`の1つだけで、その付随情報には「端末で
`copilot login`を実行する」と記載されています。GuildBoticsはこの方式でACPの`authenticate`を
呼び、保存済みログインの有無だけを確認します。ログイン済みの環境では即座に応答が返ります。
対話的なログイン操作をGuildBotics側から開始することはありません。`authenticate`が拒否された
場合、認証方式が提示されない場合、応答が返らない場合（利用者のいない環境で端末ログインを待って
いる状態）は、いずれも認証エラーとして停止し、`copilot login`の実行を案内します。診断記録に
残すのは認証方式の識別子だけで、Copilotの認証情報保存先の内容は読み取りません。

GitHub、Git、SSHへの書き込みに使う認証情報は、これらのAI CLIツールのプロセスへ渡しません。
Grokには、`127.0.0.1`にbindしたadapter専用のHTTP MCP endpointと、推測困難なbearer grantを
渡します。唯一の`guildbotics_member` toolが受け取るのは、固定された
`guildbotics member` entrypointのtoken化済み引数だけです。実行ファイルやshellを選ぶこと、
workspaceを上書きすること、別personとして動くことはできません。endpointはGrokのsandbox外にある
GuildBotics processで動作し、turn実行中だけ利用でき、毎turn更新する第2のgrantも要求し、
adapterとともに停止します。Grok processへmember execution leaseやdelegation identityを
渡すことはありません。

brokerはmember CLIを別のtrusted processとして起動するため、OS KeychainなどのSecretStore backendを
そのまま利用できます。有効期間の短いleaseは、そのCLI processだけへ渡します。CLIの
`--workspace`には常に選択中のGuildBotics workspace rootを指定し、child processのcwdはmemberの
隔離作業ディレクトリのまま維持します。workspace data rootはこれらと独立して上書きできます。
read-only turnではdelegationを渡さないため、既存のmember CLI guardが書き込み可能なcommandを
すべて拒否します。その他のnative adapterも、それぞれのnative command経路から同じ検証済み
member capability境界を使用します。

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
Claude Codeでは`system/api_retry`、Codexではアカウント情報やrate limitに関するRPCデータを
使用します。標準エラー出力に表示される、人間向けのエラーメッセージには依存しません。

Antigravityでは、終端の`result`イベントで判定します。`status`が`SUCCESS`以外なら異常とみなし、
同じイベントの`error`フィールドで種別を決めます。`agy` 1.1.10は利用制限と認証エラーをコード
ではなく文章で報告するため、この1フィールドだけをアダプタ内の限定的な正規表現と照合します。
文章に含まれる復帰時刻（`Resets in 1h23m`）は、他のツールと共通の正規化処理へ渡します。
利用制限ではセッションを切り替えず、認証・プロトコル・プロセスの失敗では切り替えます。

再開可能な時刻を取得できた場合は、その時刻まで対象チケットの選択と保留中のチャット処理を
延期します。この待機によって、同じプロセス内で行う完了条件未達時の再試行回数を消費することは
ありません。

診断記録には`agent_runtime.*`、`workflow.rate_limited`、`credential.failed`というイベント名を
使用します。メンバー、実行、会話識別子、セッションの世代、AI CLIツールのセッションIDとturn ID、
cursor、実行権を記録し、同じ作業に属するイベントを対応付けます。機密情報を含む可能性がある項目は
伏せ字にし、長い文章は上限を設けて切り詰めます。記録はDesktopの診断画面、または
`<workspace-data-root>/run/diagnostics.jsonl`から確認できます。

`unsupported_version`が記録された場合は、使用しているAI CLIツールを更新してください。
Claude Codeでは`--input-format`、`--output-format`、`stream-json`、`--resume`への対応を確認します。
CodexではApp Serverの初期化処理を通して、必要な機能に対応しているか確認します。
ACPを使うAI CLIツールでは、`initialize`が返すプロトコル版数が1であることと、正確な再開に必要な
`loadSession`または`sessionCapabilities.resume`のいずれかが提示されることを確認します。
Grokではさらに、trusted member capability transportに必要なHTTP MCP対応も確認します。
バージョン文字列では判定しないため、これらのcapabilityを提示する新しい版はそのまま利用できます。
Antigravityでは`agy --help`（標準エラー出力へ表示し、終了コード0で終わります）を読み取り、
`--print`、`--output-format`、`--conversation`、`--model`、`--effort`、`--add-dir`への対応を
確認します。動作確認済みの基準バージョンは、Grok Build 0.2.118、GitHub Copilot CLI 1.0.77、
Antigravity 1.1.10です。

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
