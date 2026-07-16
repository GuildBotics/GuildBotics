# Codex・Claude Codeのセッション連携

GuildBoticsでCodexまたはClaude Codeを利用する場合は、Slackスレッドやチケットに
セッションを対応付け、前回の続きから作業を再開できます。Codexとの連携には
[Codex App Server](https://developers.openai.com/codex/app-server)を使用し、Claude Codeとの
連携には公式の`stream-json`入出力と`--resume <session-id>`を使用します。

AntigravityやGitHub Copilotなど、Codex・Claude Code以外のAI CLIツールは、
`intelligences/cli_agents/`にあるYAML形式の設定に従ってスクリプト経由で実行します。
これらのツールに会話履歴を渡す方法については、
[Slackスレッドの文脈を渡す方法](#slackスレッドの文脈を渡す方法)を参照してください。

## 設定

`intelligences/cli_agent_mapping.yml`では、CodexまたはClaude Codeを次のように直接指定します。

```yaml
default: codex
codex: codex
claude: claude
```

CodexとClaude Codeの実行には、`intelligences/cli_agents/`配下のスクリプト設定を
使用しません。ユーザーが変更できる実行権限は、
`intelligences/native_agent_policy.yml`で指定するCodexのファイルアクセス範囲だけです。

```yaml
codex:
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

Claude Codeは、従来の`--dangerously-skip-permissions`と同じく、操作ごとの確認を省略する
`bypassPermissions`で常に実行します。Bash sandboxはチケット作業やチャットからの依頼に必要な
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
（`codex login`または`claude auth login`）を行います。ログイン情報は各ツール自身の
認証情報保存先にだけ保持され、GuildBoticsのセッション情報や診断記録には複製されません。

GitHub、Git、SSHへの書き込みに使う認証情報は、CodexとClaude Codeのプロセスへ渡しません。
AI CLIツールは、GuildBoticsが検証した`guildbotics member ...`コマンドを通してのみ、
メンバーとして書き込み操作を行います。そのコマンドから呼び出される子コマンドが引き継ぐのは、
有効期間の短い実行委任用の識別情報であり、AI CLIツールの認証トークンではありません。

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

- CodexまたはClaude Codeの既存セッションを引き継ぐ場合は、セッション内に保持されている文脈へ
  最新のイベントだけを追加します。安全に新しいセッションへ切り替えられるよう、ワークフロー側でも
  Slackスレッドの履歴を更新しますが、引き継ぎ中のセッションへその履歴を重ねて送りません。
- CodexまたはClaude Codeで新しいセッションを開始するときや、セッションを切り替えたときは、
  最新のイベントより前のSlackスレッドの履歴と最新のイベントを一度だけ送ります。
- GitHub Copilotなど、呼び出しのたびに新しい会話として実行されるAI CLIツールには、最大件数を
  設けたSlackスレッドの履歴と最新のイベントを毎回送ります。
- Antigravityのように、同じ依頼の再試行中に限って会話を引き継げるAI CLIツールでは、保存済みの
  会話IDを使って前回の続きから再開します。この場合は続行指示だけを送り、同じイベントや会話履歴を
  重ねて送りません。この動作を利用する設定では、`conversation_scope: dispatch`を指定します。

Slack APIからスレッドの履歴を安全に取得できない場合は、新しいセッションを開始するとき、
セッションを切り替えたとき、または会話を引き継がないAI CLIツールを実行するときに限り、
AI CLIツール自身にSlackスレッドを確認させます。この動作を内部では`inspect_required`
fallbackと呼びます。

CodexまたはClaude Codeの正常なセッションを引き継ぐ場合は、保存済みのセッションと最新の
イベントだけを使用します。そのため、`inspect_required` fallbackを理由に、それまでの会話履歴を
重複して送ることはありません。

Slackイベントの処理済み位置を示すcursorは、AI CLIツールからの応答が正常に完了した後にだけ
更新します。応答が失敗した場合はcursorを進めないため、未処理のイベントが失われることは
ありません。完了条件を満たさず同じcursorから再試行する場合は、新しい依頼ではなく、直前の
作業の続きとして扱います。

セッションとの対応付けは、
`<workspace-data-root>/agent-runtime/conversations/<person>/<adapter>/`へ安全に保存します。
保存内容には、AI CLIツールのセッションIDとturn ID、cursor、使用量、セッションの状態、世代、
切り替え理由が含まれます。AI CLIツールの認証情報と、プロトコルから受信した未加工データは
保存しません。

GuildBoticsは、AI CLIツール側の「最新のセッション」や暗黙の会話継続には依存せず、保存した
セッションIDを明示して再開します。セッションが存在しない場合や、正常に再開できない状態の場合、
`resume`は失敗します。再開方法が`auto`の場合は新しいセッションを開始し、文脈を再構築します。
キャンセル、不正または不完全なストリーム、プロセスの失敗、AI CLIツール側での文脈圧縮、
有効期間・turn数・使用量の上限、モデルの変更が発生した場合も、新しいセッションへ切り替えます。
Codexの`contextCompaction`とClaude Codeの`compact_boundary`は、GuildBotics内では同じ種類の
イベントとして記録します。文脈圧縮が完了したturn自体は成功として扱い、次の依頼で新しい
セッションを開始してSlackスレッドの履歴を再構築します。

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

CodexとClaude Codeのプロセスは、独立したプロセスグループとして起動します。キャンセル、
サービスの停止、通信エラー、実行コンテキストの終了時には、グループ全体を停止して終了を確認します。
そのため、GuildBoticsの停止後にAI CLIツールのプロセスだけが背後で動き続けることはありません。

## 利用制限・認証エラーと診断記録

認証切れと利用制限（rate limit）は、AI CLIツールが出力する構造化データを使って判定します。
Claude Codeでは`system/api_retry`、Codexではアカウント情報やrate limitに関するRPCデータを
使用します。標準エラー出力に表示される、人間向けのエラーメッセージには依存しません。

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
