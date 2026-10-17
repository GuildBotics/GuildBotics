// Branvil's texts: its setup panels and its checks, merged into the app's
// translations when the provider registry loads.
export const branvil = {
  en: {
    setup: {
      branvil: {
        baseUrl: "Base URL",
        baseUrlHint:
          "Where Branvil is reached, the same on every machine (leave empty for http://127.0.0.1:7878). On a machine other than the Hub, Branvil's tunnel forwards it.",
        baseUrlInvalid: "Enter an http(s) origin such as http://127.0.0.1:7878.",
        owner: "Namespace",
        ownerHint:
          "The namespace members write in, such as acme: they write only to the repositories directly in it.",
        ownerRequired: "Enter the namespace members write in.",
        ownerInvalid: "Enter names joined by /, such as acme or acme/team.",
        board: "Board",
        boardHint:
          "The board tickets are taken from, such as acme/board. Without one, no ticket is a member's work.",
        boardInvalid: "Enter a board path such as acme/board.",
        username: "Branvil username",
        usernameHint: "The member's name on Branvil.",
        usernameRequired: "Enter the member's Branvil username.",
        usernameInvalid: "Use letters, digits, '.', '_' and '-' only.",
        token: "Branvil token",
        tokenHint: "A token Branvil issued for this member. It is also the member's git password.",
        tokenPlaceholder: "bvt_…",
        tokenRequired: "Enter the member's Branvil token.",
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
        branvil_board: {
          title: "The Branvil board was read",
          description: "The board {{target}} was read.",
        },
        human_branvil_user: {
          title: "The Branvil username is configured",
          description: "Tickets can be assigned to this member by name.",
        },
      },
      diagnosticChecks: {
        branvil_reachable: {
          title: "Branvil is not reachable",
          description:
            "Branvil did not answer at {{target}}. On a machine other than the Hub, check that the Branvil tunnel is running.",
        },
        branvil_access: {
          title: "Branvil refused the member's token",
          description: "Check the member's Branvil token and username.",
        },
        branvil_board: {
          title: "The Branvil board could not be read",
          description: "Check the board {{target}} and the member's permission on it.",
        },
        branvil_board_missing: {
          title: "No Branvil board is configured",
          description: "Without a board, no ticket is selected as a member's work.",
        },
        branvil_credential: {
          title: "A Branvil setting is missing",
          description: "{{target}} is not configured.",
        },
        human_branvil_user: {
          title: "The Branvil username is not configured",
          description: "Enter the Branvil username of this human member.",
        },
      },
    },
  },
  ja: {
    setup: {
      branvil: {
        baseUrl: "ベース URL",
        baseUrlHint:
          "Branvil に届く URL です。すべてのマシンで同じにします（空欄なら http://127.0.0.1:7878）。Hub 以外のマシンでは Branvil のトンネルが転送します。",
        baseUrlInvalid: "http://127.0.0.1:7878 のような http(s) のオリジンを入力してください。",
        owner: "名前空間",
        ownerHint:
          "メンバーが書き込む名前空間（acme など）です。その直下のリポジトリにだけ書き込みます。",
        ownerRequired: "メンバーが書き込む名前空間を入力してください。",
        ownerInvalid: "acme や acme/team のように、名前を / でつないで入力してください。",
        board: "ボード",
        boardHint:
          "チケットを取るボード（acme/board など）です。無ければ、どのチケットもメンバーの仕事になりません。",
        boardInvalid: "acme/board のようなボードのパスを入力してください。",
        username: "Branvil ユーザー名",
        usernameHint: "Branvil でのメンバーの名前です。",
        usernameRequired: "メンバーの Branvil ユーザー名を入力してください。",
        usernameInvalid: "英数字と '.'、'_'、'-' だけを使ってください。",
        token: "Branvil トークン",
        tokenHint:
          "Branvil がこのメンバーに発行したトークンです。メンバーの git のパスワードにもなります。",
        tokenPlaceholder: "bvt_…",
        tokenRequired: "メンバーの Branvil トークンを入力してください。",
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
        branvil_board: {
          title: "Branvil のボードを読み取りました",
          description: "ボード {{target}} を読み取りました。",
        },
        human_branvil_user: {
          title: "Branvil ユーザー名が設定されています",
          description: "このメンバーにチケットを名前で割り当てられます。",
        },
      },
      diagnosticChecks: {
        branvil_reachable: {
          title: "Branvil に届きません",
          description:
            "{{target}} の Branvil が応答しませんでした。Hub 以外のマシンでは、Branvil のトンネルが動いているか確認してください。",
        },
        branvil_access: {
          title: "Branvil がメンバーのトークンを拒否しました",
          description: "メンバーの Branvil トークンとユーザー名を確認してください。",
        },
        branvil_board: {
          title: "Branvil のボードを読み取れませんでした",
          description: "ボード {{target}} と、それに対するメンバーの権限を確認してください。",
        },
        branvil_board_missing: {
          title: "Branvil のボードが設定されていません",
          description: "ボードが無いと、どのチケットもメンバーの仕事として選ばれません。",
        },
        branvil_credential: {
          title: "Branvil の設定が足りません",
          description: "{{target}} が設定されていません。",
        },
        human_branvil_user: {
          title: "Branvil ユーザー名が設定されていません",
          description: "この人間のメンバーの Branvil ユーザー名を入力してください。",
        },
      },
    },
  },
};
