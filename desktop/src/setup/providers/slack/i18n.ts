// Slack's texts: its setup panels and its checks, merged into the app's
// translations when the provider registry loads.
export const slack = {
  en: {
    setup: {
      members: {
        slackAppsSetupMode: {
          label: "Slack App setup method",
          create: "Register a new app",
          existing: "Use a registered app",
        },
        slackAppRegistration: {
          hint: "Create the Slack App from here. The browser opens with the scopes, Socket Mode and event subscriptions already configured, so all you do is pick a workspace and copy the two tokens back.",
          appName: "App name",
          register: "Create the app on Slack",
          reopen: "Reopen the creation page",
          openAppList: "Open the Slack app list",
          signedOutHint:
            "If Slack asks you to sign in, sign in first and then press the button again — Slack drops the prepared settings on the sign-in redirect.",
          stepsTitle: "Then, in the browser:",
          steps: {
            create: "Pick the workspace and click “Create”.",
            reinstall:
              "Open “OAuth & Permissions” and press “Reinstall to Workspace”, then approve. Slack issues the app's first bot token without the manifest's scopes, so this step is required — not optional.",
            copyBot: "On that same page, copy the Bot User OAuth Token (xoxb-…).",
            copyAppToken:
              "Open “Basic Information” → “App-Level Tokens” and copy the token (xapp-…).",
            paste: "Paste both tokens into the fields below and verify them.",
          },
          errors: {
            invalidAppName: "App name must be 1-35 characters.",
          },
        },
        slackTokenVerify: {
          button: "Verify app",
          labels: {
            bot: "Bot token",
            botStored: "Saved bot token",
            appToken: "App token",
            appTokenStored: "Saved app token",
            scopes: "Bot permissions",
          },
          ok: {
            bot: "OK — {{displayName}} ({{userId}}) in {{workspace}}",
            botNoWorkspace: "OK — {{displayName}} ({{userId}})",
            appToken: "OK — Socket Mode connected.",
            scopes: "OK — the token can read channels.",
            channel: "OK — the bot can read this channel.",
          },
          channelNotJoined: "the bot has not joined this channel. Run /invite @{{botName}} in it.",
          channelNotFound:
            "no such channel in this workspace. Check the name, or use the channel ID.",
          scopeMissing:
            "the token carries none of the scopes this read needs ({{needed}}). Scopes reach a token only when the app is installed, so ones added later need a reinstall: open the app's “OAuth & Permissions”, press “Reinstall to Workspace”, and copy the bot token again.",
          scopeMissingUnnamed:
            "the token is missing a required scope. Reinstall the app from “OAuth & Permissions” and copy the bot token again.",
          errors: {
            missing: "not entered.",
            wrong_token_type: "wrong token type. Check that xoxb- and xapp- are not swapped.",
            unreachable: "could not reach Slack. Check your network connection.",
            invalid_auth: "rejected by Slack (invalid_auth). The token is invalid or revoked.",
            unknown: "verification failed ({{code}}).",
          },
        },
        slackUserId: "Slack User ID",
        slackUserIdHint:
          "Enter the Slack member ID that starts with U. Open the user's Slack profile and choose Copy member ID from the more menu.",
        slackBotToken: "Slack Bot token",
        slackBotTokenPlaceholder: "xoxb-...",
        slackAppToken: "Slack App token",
        slackAppTokenPlaceholder: "xapp-...",
      },
      validation: {
        slackUserIdRequired: "Slack User ID is required for human members.",
        slackUserIdInvalid: "Enter a Slack User ID starting with U.",
        slackBotTokenRequired: "Slack Bot token is required when Slack channels are configured.",
        slackBotTokenInvalid: "Enter a Slack Bot token starting with xoxb-.",
        slackAppTokenRequired: "Slack App token is required when Slack channels are configured.",
        slackAppTokenInvalid: "Enter a Slack App token starting with xapp-.",
      },
    },
    overview: {
      diagnosticSuccess: {
        slack_bot_auth: {
          title: "Slack Bot authentication passed",
          description: "Slack Bot identity was read successfully.",
        },
        slack_channel_history: {
          title: "Slack channel history was readable",
          description: "Channel history was read without posting messages.",
        },
      },
      diagnosticChecks: {
        slack_app_token: {
          title: "Slack App token is missing",
          description: "Socket Mode runtime requires a Slack App token for this member.",
        },
        slack_bot_auth: {
          title: "Slack bot authentication failed",
          description: "Slack bot authentication failed. Check the member's Slack Bot token.",
        },
        slack_channel: {
          title: "Slack channel could not be resolved",
          description: "The configured Slack channel name or ID could not be resolved.",
        },
        slack_channel_history: {
          title: "Slack channel history could not be read",
          description:
            "Slack channel history could not be read. Check channel access and token scopes.",
        },
        slack_channel_not_joined: {
          title: "Slack bot has not joined #{{channel}}",
          description:
            "The bot is not a member of #{{channel}}, so it cannot read its history. Run /invite @{{bot_name}} in that channel.",
        },
        slack_access: {
          title: "Slack read-only check failed",
          description: "Slack read-only access failed. Check Slack tokens and channel settings.",
        },
      },
    },
  },
  ja: {
    setup: {
      members: {
        slackAppsSetupMode: {
          label: "Slack App の設定方法",
          create: "新規に App を登録",
          existing: "登録済みの App を使う",
        },
        slackAppRegistration: {
          hint: "ここから Slack 上に App を作成できます。scope・Socket Mode・イベント購読を設定済みの状態でブラウザが開くので、ワークスペースを選んで 2 つのトークンをコピーするだけです。",
          appName: "App 名",
          register: "Slack で App を作成",
          reopen: "作成ページを再度開く",
          openAppList: "Slack の App 一覧を開く",
          signedOutHint:
            "Slack からサインインを求められた場合は、サインインしてからもう一度ボタンを押してください（サインインの遷移で設定内容が失われます）。",
          stepsTitle: "続けて、ブラウザ側で以下を行います:",
          steps: {
            create: "ワークスペースを選び「Create」をクリックします。",
            reinstall:
              "「OAuth & Permissions」を開き「Reinstall to Workspace」を実行して承認します。作成直後に発行される Bot トークンには manifest の scope が付与されないため、この手順は必須です。",
            copyBot: "同じ画面の Bot User OAuth Token（xoxb-…）をコピーします。",
            copyAppToken:
              "「Basic Information」→「App-Level Tokens」でトークン（xapp-…）をコピーします。",
            paste: "2 つのトークンを下の入力欄に貼り付けて検証します。",
          },
          errors: {
            invalidAppName: "App 名は 1〜35 文字で入力してください。",
          },
        },
        slackTokenVerify: {
          button: "App を検証",
          labels: {
            bot: "Bot トークン",
            botStored: "保存済みの Bot トークン",
            appToken: "App トークン",
            appTokenStored: "保存済みの App トークン",
            scopes: "Bot の権限",
          },
          ok: {
            bot: "OK — {{displayName}}（{{userId}}）/ {{workspace}}",
            botNoWorkspace: "OK — {{displayName}}（{{userId}}）",
            appToken: "OK — Socket Mode に接続できました。",
            scopes: "OK — チャンネルを読み取れます。",
            channel: "OK — Bot が読み取れます。",
          },
          channelNotJoined:
            "Bot が参加していません。このチャンネルで /invite @{{botName}} を実行してください。",
          channelNotFound:
            "このワークスペースに見つかりません。名前を確認するか、チャンネル ID を指定してください。",
          scopeMissing:
            "この読み取りに必要な scope（{{needed}}）がトークンに付与されていません。scope はインストール時にトークンへ付与されるため、後から追加した分は再インストールが必要です。App の「OAuth & Permissions」で「Reinstall to Workspace」を実行し、Bot トークンをコピーし直してください。",
          scopeMissingUnnamed:
            "トークンに必要な scope が不足しています。App の「OAuth & Permissions」から再インストールし、Bot トークンをコピーし直してください。",
          errors: {
            missing: "入力されていません。",
            wrong_token_type:
              "トークンの種類が違います。xoxb- と xapp- が入れ替わっていないか確認してください。",
            unreachable: "Slack に接続できませんでした。ネットワーク接続を確認してください。",
            invalid_auth:
              "Slack に拒否されました（invalid_auth）。トークンが無効か失効しています。",
            unknown: "検証に失敗しました（{{code}}）。",
          },
        },
        slackUserId: "Slack User ID",
        slackUserIdHint:
          "U で始まる Slack member ID を入力してください。Slack で対象ユーザーのプロフィールを開き、その他メニューから Copy member ID を選択して取得できます。",
        slackBotToken: "Slack Bot トークン",
        slackBotTokenPlaceholder: "xoxb-...",
        slackAppToken: "Slack App トークン",
        slackAppTokenPlaceholder: "xapp-...",
      },
      validation: {
        slackUserIdRequired: "人間メンバーには Slack User ID が必要です。",
        slackUserIdInvalid: "Slack User ID は U で始まる値を入力してください。",
        slackBotTokenRequired:
          "参加するSlackチャンネルを設定する場合、Slack Bot トークンは必須です。",
        slackBotTokenInvalid: "Slack Bot トークンは xoxb- で始まる値を入力してください。",
        slackAppTokenRequired:
          "参加するSlackチャンネルを設定する場合、Slack App トークンは必須です。",
        slackAppTokenInvalid: "Slack App トークンは xapp- で始まる値を入力してください。",
      },
    },
    overview: {
      diagnosticSuccess: {
        slack_bot_auth: {
          title: "Slack Bot 認証に成功しました",
          description: "Slack Bot の identity を読み取れました。",
        },
        slack_channel_history: {
          title: "Slack チャンネル履歴を読み取れました",
          description: "メッセージを投稿せず、チャンネル履歴を読み取れました。",
        },
      },
      diagnosticChecks: {
        slack_app_token: {
          title: "Slack App トークンが不足しています",
          description: "Socket Mode runtime には、このメンバーの Slack App トークンが必要です。",
        },
        slack_bot_auth: {
          title: "Slack Bot 認証に失敗しました",
          description:
            "Slack Bot 認証に失敗しました。メンバーの Slack Bot トークンを確認してください。",
        },
        slack_channel: {
          title: "Slack チャンネルを解決できません",
          description: "設定された Slack チャンネル名または ID を解決できませんでした。",
        },
        slack_channel_history: {
          title: "Slack チャンネル履歴を読み取れません",
          description:
            "Slack チャンネル履歴を読み取れませんでした。チャンネル権限と token scope を確認してください。",
        },
        slack_channel_not_joined: {
          title: "Bot が #{{channel}} に参加していません",
          description:
            "Bot が #{{channel}} のメンバーではないため、履歴を読み取れません。このチャンネルで /invite @{{bot_name}} を実行して招待してください。",
        },
        slack_access: {
          title: "Slack の読み取り検証に失敗しました",
          description:
            "Slack の読み取りアクセスに失敗しました。Slack トークンとチャンネル設定を確認してください。",
        },
      },
    },
  },
};
