// Branvil's texts: its setup panels and its checks, merged into the app's
// translations when the provider registry loads.
export const branvil = {
  en: {
    setup: {
      branvil: {
        nav: {
          label: "Branvil",
        },
        workspace: "Workspace",
        workspaceHint:
          "The Branvil workspace members work in: they write only to its repositories and take tickets from its board.",
        workspaceRequired: "Choose the workspace members work in.",
        workspaceInvalid: "Use one name of letters, digits, '.', '_' and '-', such as acme.",
        newWorkspace: "New workspace name",
        createWorkspace: "Create",
        git: {
          title: "Your own git on this machine",
          hint: "To use Branvil as the origin of your own checkouts, add these lines at the end of your global git configuration (git config --global --edit opens it). Git then gets this machine's Branvil token from GuildBotics instead of asking for a name and a password.",
          copy: "Copy",
          copied: "Copied",
        },
      },
    },
    overview: {
      diagnosticSuccess: {
        branvil_workspace: {
          title: "The Branvil workspace was read",
          description: "The workspace {{target}} was read.",
        },
      },
      diagnosticChecks: {
        branvil_workspace: {
          title: "The Branvil workspace could not be read",
          description: "Check that the workspace {{target}} exists on Branvil.",
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
        workspace: "ワークスペース",
        workspaceHint:
          "メンバーが作業する Branvil のワークスペースです。そのリポジトリにだけ書き込み、そのボードからチケットを取ります。",
        workspaceRequired: "メンバーが作業するワークスペースを選んでください。",
        workspaceInvalid: "acme のように、英数字と '.'、'_'、'-' の名前を 1 つ入力してください。",
        newWorkspace: "新しいワークスペースの名前",
        createWorkspace: "作成",
        git: {
          title: "このマシンのあなたの git",
          hint: "自分のチェックアウトで Branvil を origin にするときは、グローバルな git の設定（git config --global --edit で開けます）の最後にこの行を追加してください。git はユーザー名とパスワードを聞く代わりに、このマシンの Branvil のトークンを GuildBotics から受け取ります。",
          copy: "コピー",
          copied: "コピーしました",
        },
      },
    },
    overview: {
      diagnosticSuccess: {
        branvil_workspace: {
          title: "Branvil のワークスペースを読み取りました",
          description: "ワークスペース {{target}} を読み取りました。",
        },
      },
      diagnosticChecks: {
        branvil_workspace: {
          title: "Branvil のワークスペースを読み取れませんでした",
          description: "ワークスペース {{target}} が Branvil にあるか確認してください。",
        },
      },
    },
  },
};
