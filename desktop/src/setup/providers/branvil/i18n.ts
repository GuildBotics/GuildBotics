// Branvil's texts: its setup panels and its checks, merged into the app's
// translations when the provider registry loads.
export const branvil = {
  en: {
    setup: {
      branvil: {
        nav: {
          label: "Branvil",
        },
        baseUrl: "Connection",
        workspace: "Workspace",
        workspaceHint:
          "The Branvil workspace members work in: they write only to its repositories and take tickets from its board.",
        workspaceConnect: "Connect to Branvil to choose one of its workspaces or create one.",
        workspaceRequired: "Choose the workspace members work in.",
        workspaceInvalid: "Use one name of letters, digits, '.', '_' and '-', such as acme.",
        newWorkspace: "New workspace name",
        createWorkspace: "Create",
        connection: {
          connect: "Connect to Branvil",
          reconnect: "Connect again",
          connected: "Connected to Branvil as its owner {{owner}}.",
          open: "Open Branvil",
          pending: "Answer Branvil's consent screen in the browser…",
          hint: "Open the shared entry link on this machine, then allow GuildBotics to manage Branvil. The same browser session opens both screens.",
        },
        git: {
          title: "Your own git on this machine",
          hint: "To use Branvil as the origin of your own checkouts, add these lines at the end of your global git configuration (git config --global --edit opens it). Git then gets this machine's Branvil token from GuildBotics instead of asking for a name and a password.",
          copy: "Copy",
          copied: "Copied",
        },
        member: {
          label: "Branvil account",
          new: "Create a new one ({{name}})",
          existing: "Use an account on Branvil",
          choose: "Account",
          chooseRequired: "Choose the account.",
          stored: "Branvil account: {{name}}",
          tokenSet: "Its token is issued by GuildBotics and kept in the OS secret store.",
          tokenMissing: "Its token is issued when the member is saved.",
          reissue: "Issue a new token when saved",
          owner: "Branvil account: {{name}} (owner)",
          ownerPending:
            "A human member uses Branvil's owner account (the owner is Branvil's only human). It is linked when saved.",
        },
      },
    },
    overview: {
      diagnosticSuccess: {
        branvil_reachable: {
          title: "Branvil answered",
          description: "Branvil answered at {{target}}.",
        },
        branvil_access: {
          title: "Branvil accepted the member's token",
          description: "The token is the member's own.",
        },
        branvil_workspace: {
          title: "The Branvil workspace was read",
          description: "The workspace {{target}} was read.",
        },
        branvil_owner: {
          title: "The human member is Branvil's owner",
          description: "GuildBotics' connection to Branvil has the owner's rights.",
        },
      },
      diagnosticChecks: {
        branvil_reachable: {
          title: "Branvil is not reachable",
          description: "Branvil did not answer at {{target}}. Check that Branvil is running.",
        },
        branvil_access: {
          title: "Branvil refused the member's token",
          description: "Reissue the member's Branvil token in the member's Branvil settings.",
        },
        branvil_workspace: {
          title: "The Branvil workspace could not be read",
          description: "Check that the workspace {{target}} exists on Branvil.",
        },
        branvil_credential: {
          title: "A Branvil setting is missing",
          description: "{{target}} is not configured.",
        },
        branvil_owner: {
          title: "GuildBotics is not connected to Branvil as its owner",
          description:
            "Connect to Branvil under Setup → Integrations, then save this member again. The reason is below.",
        },
      },
    },
  },
  ja: {
    setup: {
      branvil: {
        nav: {
          label: "Branvil",
        },
        baseUrl: "接続先",
        workspace: "ワークスペース",
        workspaceHint:
          "メンバーが作業する Branvil のワークスペースです。そのリポジトリにだけ書き込み、そのボードからチケットを取ります。",
        workspaceConnect: "Branvil に接続すると、ワークスペースを選んだり作成したりできます。",
        workspaceRequired: "メンバーが作業するワークスペースを選んでください。",
        workspaceInvalid: "acme のように、英数字と '.'、'_'、'-' の名前を 1 つ入力してください。",
        newWorkspace: "新しいワークスペースの名前",
        createWorkspace: "作成",
        connection: {
          connect: "Branvil に接続",
          reconnect: "接続し直す",
          connected: "Branvil にオーナー {{owner}} として接続しています。",
          open: "Branvil を開く",
          pending: "ブラウザで Branvil の同意画面に答えてください…",
          hint: "このマシンのブラウザで共通の入口リンクを開き、GuildBotics に Branvil の管理を許可してください。同じセッションで両方の画面を使えます。",
        },
        git: {
          title: "このマシンのあなたの git",
          hint: "自分のチェックアウトで Branvil を origin にするときは、グローバルな git の設定（git config --global --edit で開けます）の最後にこの行を追加してください。git はユーザー名とパスワードを聞く代わりに、このマシンの Branvil のトークンを GuildBotics から受け取ります。",
          copy: "コピー",
          copied: "コピーしました",
        },
        member: {
          label: "Branvil のアカウント",
          new: "新しく作る（{{name}}）",
          existing: "Branvil にあるアカウントを使う",
          choose: "アカウント",
          chooseRequired: "アカウントを選んでください。",
          stored: "Branvil のアカウント: {{name}}",
          tokenSet: "トークンは GuildBotics が発行し、OS の秘密ストアに保存しています。",
          tokenMissing: "トークンはメンバーを保存したときに発行します。",
          reissue: "保存するときにトークンを再発行する",
          owner: "Branvil のアカウント: {{name}}（オーナー）",
          ownerPending:
            "人間のメンバーは、Branvil のオーナーのアカウントを使います（Branvil を使う人間はオーナー 1 人だけです）。保存すると対応付けます。",
        },
      },
    },
    overview: {
      diagnosticSuccess: {
        branvil_reachable: {
          title: "Branvil が応答しました",
          description: "{{target}} の Branvil が応答しました。",
        },
        branvil_access: {
          title: "Branvil がメンバーのトークンを受け付けました",
          description: "トークンはメンバー本人のものです。",
        },
        branvil_workspace: {
          title: "Branvil のワークスペースを読み取りました",
          description: "ワークスペース {{target}} を読み取りました。",
        },
        branvil_owner: {
          title: "人間のメンバーは Branvil のオーナーです",
          description: "GuildBotics の Branvil への接続はオーナーの権限を持っています。",
        },
      },
      diagnosticChecks: {
        branvil_reachable: {
          title: "Branvil に届きません",
          description:
            "{{target}} の Branvil が応答しませんでした。Branvil が動いていることを確認してください。",
        },
        branvil_access: {
          title: "Branvil がメンバーのトークンを拒否しました",
          description: "メンバーの Branvil の設定でトークンを再発行してください。",
        },
        branvil_workspace: {
          title: "Branvil のワークスペースを読み取れませんでした",
          description: "ワークスペース {{target}} が Branvil にあるか確認してください。",
        },
        branvil_credential: {
          title: "Branvil の設定が足りません",
          description: "{{target}} が設定されていません。",
        },
        branvil_owner: {
          title: "GuildBotics が Branvil にオーナーとして接続していません",
          description:
            "設定 → 連携で Branvil に接続してから、このメンバーをもう一度保存してください。理由は下にあります。",
        },
      },
    },
  },
};
