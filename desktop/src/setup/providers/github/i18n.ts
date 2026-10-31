// GitHub's texts: its setup panels and its checks, merged into the app's
// translations when the provider registry loads.
export const github = {
  en: {
    setup: {
      members: {
        githubAccountType: "GitHub account type",
        githubAccountTypeHint:
          "Required only when this member works with GitHub tickets, issues, or pull requests.",
        githubAccountTypeOptions: {
          none: "Do not link GitHub",
          machine_user: "Machine Account (Machine User)",
          github_apps: "GitHub Apps",
          human: "Human",
        },
        githubAppsUrl: "GitHub Apps URL for resolution",
        githubAppsUrlHint: "Enter the app settings URL, then click Resolve. This URL is not saved.",
        githubResolvedIdentity: "GitHub identity",
        githubUsernameHint: "Enter the GitHub username, then click Resolve.",
        resolve: "Resolve",
        githubUsername: "GitHub username",
        gitEmail: "Git email",
        installationId: "GitHub Installation ID",
        appId: "GitHub App ID",
        privateKey: "GitHub private key file",
        privateKeyPlaceholder: "Choose the .pem file GitHub downloaded",
        agentFieldSyncFailedTitle: "Agent field update failed",
        agentFieldSyncFailedBody:
          "The member was saved, but the project's Agent field could not be updated. Update it from Setup → Integrations.",
        githubAppsSetupMode: {
          label: "GitHub App setup method",
          create: "Register a new app",
          existing: "Use a registered app",
        },
        githubAppRegistration: {
          hint: "Create the GitHub App from here. Approve the creation and installation in the browser, and the fields below are filled in automatically. The app is created under your personal account, or under an organization when you enter its name.",
          appName: "App name",
          organization: "Organization",
          register: "Register on GitHub",
          pending: "In the browser, click “Create GitHub App”…",
          reopen: "Reopen the registration page",
          converted:
            "Created app “{{slug}}”. Choose the repositories to install it on in the browser…",
          openInstall: "Open the install page",
          installed:
            "Registered, waiting for save. Saving the member stores the app's private key in the keychain.",
          saved: "Saved. The app's private key is stored in the keychain.",
          saveFailed:
            "The member was not saved. Until the registration expires, you can save again with it.",
          installCheckError: "Failed to check the installation status: {{message}}",
          errors: {
            invalidAppName: "App name must be 1-34 characters.",
            expired: "The registration has expired. Register the app on GitHub again.",
          },
        },
        accessToken: "GitHub access token",
        accessTokenPlaceholder: "ghp_... or github_pat_...",
        githubAuthNotRequired: "GitHub auth is not required for human members.",
        githubDisabledMemberHint:
          "GitHub is not linked for this member. You can still use local commands, LLM / CLI settings, and Slack settings.",
      },
      github: {
        owner: "Owner",
        ownerHint: "The organization or user whose repositories the members write to.",
        ownerRequired: "Enter the owner of the repositories.",
        projectUrl: "GitHub Project URL",
        projectUrlHint:
          "URL of the GitHub Project used as the task board (https://github.com/orgs/<org>/projects/<number>).",
        laneMapping: "Lane mapping",
        laneMappingHint:
          "Map workflow lanes to your GitHub Project status options. Defaults work with a standard Todo / In Progress / Done board.",
        laneMappingManualHint:
          "Status options could not be read automatically; enter the status names used by your GitHub Project (defaults assume Todo / In Progress / Done).",
        laneReady: "Ready lane",
        laneWorking: "Working lane",
        laneWorkingHint:
          "Tickets are moved here when work starts. If this lane does not exist on the board, tickets are simply not moved.",
        laneDone: "Done lane",
        agentField: "Agent field (for agent assignment)",
        agentFieldHint:
          "Non-human members are assigned tickets through the Project's Agent single-select field. Create the field and register members here so they can be assigned.",
        agentFieldLoading: "Reading the Agent field state...",
        agentFieldUnavailableTitle: "Agent field state could not be read",
        agentFieldUnavailableHint:
          "This needs a saved active member with GitHub credentials and a reachable Project. Save members first, then reopen this section.",
        agentFieldStatus: "Agent field:",
        agentFieldExists: "Exists",
        agentFieldMissing: "Not created",
        agentFieldRegistered: "Registered members",
        agentFieldNoMembers: "No members are registered as options yet.",
        agentFieldMissingMembers: "Not yet registered",
        agentFieldCreate: "Create Agent field",
        agentFieldAddMembers: "Add members ({{count}})",
        agentFieldUpToDate: "Up to date",
        agentFieldError:
          "Could not update the Agent field. Check member credentials and the Project URL.",
      },
      validation: {
        githubProjectRequired: "GitHub project URL is required.",
        laneReadyDoneSame: "Ready and done lanes must be different.",
        githubProjectInvalid:
          "Enter a GitHub Project URL such as https://github.com/orgs/<org>/projects/<number> or https://github.com/users/<user>/projects/<number>.",
        memberGithubAppsUrlRequired: "Enter the GitHub Apps URL, then click Resolve.",
        memberGithubIdentityNotFound:
          "Could not resolve this GitHub identity. Check the username, email, or GitHub Apps URL.",
        memberGithubIdentityResolveFailed:
          "Failed to resolve this GitHub identity. Check your network connection and try again.",
        memberGithubAppsUrlInvalid:
          "Enter a GitHub Apps settings URL such as https://github.com/organizations/<org>/settings/apps/<app>.",
        memberGithubUsernameRequired: "Resolve the GitHub identity or enter the GitHub username.",
        memberGithubUsernameInvalid:
          "Enter a valid GitHub username. Email addresses are not supported here.",
        memberGitEmailRequired: "Resolve the GitHub identity or enter the Git email address.",
        memberGitEmailInvalid: "Enter a valid email address.",
        githubInstallationIdRequired: "GitHub Installation ID is required.",
        githubInstallationIdInvalid: "GitHub Installation ID must be digits only.",
        githubAppIdRequired: "GitHub App ID is required.",
        githubAppIdInvalid: "GitHub App ID must be digits only.",
        githubPrivateKeyRequired: "Choose the GitHub private key file.",
        githubAccessTokenRequired: "GitHub access token is required.",
        githubAccessTokenInvalid:
          "Enter a GitHub access token starting with ghp_, gho_, ghu_, ghs_, ghr_, or github_pat_.",
      },
    },
    overview: {
      diagnosticSuccess: {
        github_code_hosting_access: {
          title: "GitHub accepted the credential",
          description: "The member's GitHub credential was checked without updating GitHub.",
        },
        github_project_access: {
          title: "GitHub Project was readable",
          description: "Project status options were fetched without updating GitHub.",
        },
        github_lane_mapping: {
          title: "Workflow lanes are valid",
          description: "The ready and done lanes exist in the GitHub Project.",
        },
        github_agent_assignment: {
          title: "Member can be assigned tickets",
          description:
            "The member resolves to a GitHub user account or has a matching Agent field option.",
        },
      },
      diagnosticChecks: {
        github_credential: {
          title: "Member GitHub credential is missing",
          description: "Configure the member's GitHub credentials before using GitHub workflows.",
        },
        github_project_access: {
          title: "GitHub project access failed",
          description: "The GitHub Project could not be read with this member's credentials.",
        },
        github_lane_missing: {
          title: "Workflow lanes are missing",
          description:
            "The configured ready or done lane was not found among the GitHub Project status options.",
        },
        github_working_lane_missing: {
          title: "Working lane was not found",
          description:
            "The configured working lane is not a GitHub Project status; tickets will not be moved to a working lane on start.",
        },
        github_member_not_assignable: {
          title: "Member cannot be assigned tickets",
          description:
            "The member's GitHub username could not be resolved to a user account; check the member's GitHub username.",
        },
        github_agent_field_required: {
          title: "Member cannot be assigned tickets",
          description:
            "The member is not a resolvable GitHub user and has no Agent field option; set the Agent field for this member.",
        },
        github_access: {
          title: "GitHub read-only check failed",
          description:
            "Project or repository access failed. Check GitHub credentials and repository settings.",
        },
      },
    },
  },
  ja: {
    setup: {
      members: {
        githubAccountType: "GitHub アカウント種別",
        githubAccountTypeHint:
          "GitHub のチケット、Issue、Pull Request を扱うメンバーだけ設定します。",
        githubAccountTypeOptions: {
          none: "GitHub連携なし",
          machine_user: "マシンアカウント（マシンユーザー）",
          github_apps: "GitHub Apps",
          human: "人間",
        },
        githubAppsUrl: "解決用 GitHub Apps URL",
        githubAppsUrlHint:
          "GitHub App の設定URLを入力して「解決」を実行します。このURLは保存されません。",
        githubResolvedIdentity: "GitHub識別子",
        githubUsernameHint: "GitHubユーザー名を入力して「解決」を実行します。",
        resolve: "解決",
        githubUsername: "GitHubユーザー名",
        gitEmail: "Gitメールアドレス",
        installationId: "GitHub Installation ID",
        appId: "GitHub App ID",
        privateKey: "GitHub秘密鍵ファイル",
        privateKeyPlaceholder: "GitHub からダウンロードした .pem ファイルを選択",
        agentFieldSyncFailedTitle: "Agent フィールドの更新に失敗しました",
        agentFieldSyncFailedBody:
          "メンバーは保存されましたが、プロジェクトの Agent フィールドを更新できませんでした。設定 → 連携 から更新してください。",
        githubAppsSetupMode: {
          label: "GitHub App の設定方法",
          create: "新規に App を登録",
          existing: "登録済みの App を使う",
        },
        githubAppRegistration: {
          hint: "ここから GitHub 上に App を作成できます。ブラウザで作成とインストールを承認すると、以下の項目が自動入力されます。App は個人アカウント配下に作成されます。組織配下に作成する場合は Organization に組織名を入力してください。",
          appName: "App 名",
          organization: "Organization",
          register: "GitHub に登録",
          pending: "ブラウザで「Create GitHub App」をクリックしてください…",
          reopen: "登録ページを再度開く",
          converted:
            "App「{{slug}}」を作成しました。ブラウザでインストール先リポジトリを選択してください…",
          openInstall: "インストール画面を開く",
          installed:
            "登録済み・保存待ちです。メンバーを保存すると、App の秘密鍵をキーチェーンに保存します。",
          saved: "保存しました。App の秘密鍵はキーチェーンに保存済みです。",
          saveFailed:
            "メンバーを保存できませんでした。登録の有効期限内は、この登録のまま再度保存できます。",
          installCheckError: "インストール状態の確認に失敗しました: {{message}}",
          errors: {
            invalidAppName: "App 名は 1〜34 文字で入力してください。",
            expired: "登録の有効期限が切れました。もう一度 GitHub に登録してください。",
          },
        },
        accessToken: "GitHubアクセストークン",
        accessTokenPlaceholder: "ghp_... または github_pat_...",
        githubAuthNotRequired: "人間メンバーでは GitHub 認証は不要です。",
        githubDisabledMemberHint:
          "このメンバーは GitHub と連携しません。ローカルコマンド、LLM・CLI設定、Slack設定は利用できます。",
      },
      github: {
        owner: "オーナー",
        ownerHint: "メンバーが書き込むリポジトリの organization またはユーザーです。",
        ownerRequired: "リポジトリのオーナーを入力してください。",
        projectUrl: "GitHub Project URL",
        projectUrlHint:
          "タスクボードとして使う GitHub Project の URL（https://github.com/orgs/<組織>/projects/<番号> 形式）。",
        laneMapping: "レーンマッピング",
        laneMappingHint:
          "ワークフローのレーンを GitHub Project の status option に対応付けます。標準的な Todo / In Progress / Done のボードなら既定値のままで動作します。",
        laneMappingManualHint:
          "status options を自動取得できませんでした。GitHub Project で使っている status 名を入力してください（既定は Todo / In Progress / Done）。",
        laneReady: "着手可能レーン",
        laneWorking: "作業中レーン",
        laneWorkingHint:
          "着手時にチケットをここへ移動します。このレーンがボード上に存在しない場合は移動を行いません。",
        laneDone: "完了レーン",
        agentField: "Agent (エージェント割り当て用) フィールド設定",
        agentFieldHint:
          "非人間メンバーは Project の Agent 単一選択フィールド経由でチケットを割り当てます。ここでフィールドを作成し、メンバーを選択肢として登録してください。",
        agentFieldLoading: "Agent フィールドの状態を取得しています...",
        agentFieldUnavailableTitle: "Agent フィールドの状態を取得できません",
        agentFieldUnavailableHint:
          "GitHub 認証情報を持つ有効メンバーの保存と、到達可能な Project が必要です。先にメンバーを保存してから、このセクションを開き直してください。",
        agentFieldStatus: "Agent フィールド:",
        agentFieldExists: "あり",
        agentFieldMissing: "未作成",
        agentFieldRegistered: "登録済みメンバー",
        agentFieldNoMembers: "選択肢として登録されたメンバーはまだありません。",
        agentFieldMissingMembers: "未登録",
        agentFieldCreate: "Agent フィールドを作成",
        agentFieldAddMembers: "メンバーを追加（{{count}}）",
        agentFieldUpToDate: "設定済み",
        agentFieldError:
          "Agent フィールドを更新できませんでした。メンバーの認証情報と Project URL を確認してください。",
      },
      validation: {
        githubProjectRequired: "GitHub Project URL が必要です。",
        laneReadyDoneSame: "着手可能レーンと完了レーンは異なる必要があります。",
        githubProjectInvalid:
          "GitHub Project URL は https://github.com/orgs/<org>/projects/<number> または https://github.com/users/<user>/projects/<number> の形式で入力してください。",
        memberGithubAppsUrlRequired: "GitHub Apps URL を入力し、「解決」を実行してください。",
        memberGithubIdentityNotFound:
          "このGitHub識別子は解決できませんでした。ユーザー名、メール、GitHub Apps URL を確認してください。",
        memberGithubIdentityResolveFailed:
          "GitHub識別子の解決に失敗しました。ネットワーク接続を確認して再実行してください。",
        memberGithubAppsUrlInvalid:
          "GitHub Apps URL は https://github.com/organizations/<org>/settings/apps/<app> の形式で入力してください。",
        memberGithubUsernameRequired:
          "GitHub識別子を解決するか、GitHubユーザー名を入力してください。",
        memberGithubUsernameInvalid:
          "有効なGitHubユーザー名を入力してください。この欄ではメールアドレスは使えません。",
        memberGitEmailRequired: "GitHub識別子を解決するか、Gitメールアドレスを入力してください。",
        memberGitEmailInvalid: "有効なメールアドレスを入力してください。",
        githubInstallationIdRequired: "GitHub Installation ID は必須です。",
        githubInstallationIdInvalid: "GitHub Installation ID は数字のみで入力してください。",
        githubAppIdRequired: "GitHub App ID は必須です。",
        githubAppIdInvalid: "GitHub App ID は数字のみで入力してください。",
        githubPrivateKeyRequired: "GitHub秘密鍵ファイルを選択してください。",
        githubAccessTokenRequired: "GitHubアクセストークンは必須です。",
        githubAccessTokenInvalid:
          "GitHubアクセストークンは ghp_、gho_、ghu_、ghs_、ghr_、github_pat_ のいずれかで始まる値を入力してください。",
      },
    },
    overview: {
      diagnosticSuccess: {
        github_code_hosting_access: {
          title: "GitHub が資格情報を受け付けました",
          description: "GitHub を更新せずに、メンバーの GitHub 資格情報を確かめました。",
        },
        github_project_access: {
          title: "GitHub Project を読み取れました",
          description: "GitHub を更新せず、Project の status options を取得できました。",
        },
        github_lane_mapping: {
          title: "ワークフローのレーンが有効です",
          description: "着手可能レーンと完了レーンが GitHub Project に存在します。",
        },
        github_agent_assignment: {
          title: "メンバーにチケットを割り当て可能です",
          description:
            "GitHub ユーザーアカウントとして解決できるか、対応する Agent field option が存在します。",
        },
      },
      diagnosticChecks: {
        github_credential: {
          title: "メンバーの GitHub 認証情報が不足しています",
          description:
            "GitHub ワークフローを使う前に、対象メンバーの GitHub 認証情報を設定してください。",
        },
        github_project_access: {
          title: "GitHub Project にアクセスできません",
          description: "このメンバーの認証情報で GitHub Project を読み取れませんでした。",
        },
        github_lane_missing: {
          title: "ワークフローのレーンが不足しています",
          description:
            "設定した着手可能レーンまたは完了レーンが GitHub Project の status options に見つかりません。",
        },
        github_working_lane_missing: {
          title: "作業中レーンが見つかりません",
          description:
            "設定した作業中レーンが GitHub Project の status に存在しないため、着手時にチケットを移動しません。",
        },
        github_member_not_assignable: {
          title: "メンバーにチケットを割り当てられません",
          description:
            "メンバーの GitHub ユーザー名を GitHub ユーザーアカウントとして解決できませんでした。GitHub ユーザー名を確認してください。",
        },
        github_agent_field_required: {
          title: "メンバーにチケットを割り当てられません",
          description:
            "GitHub ユーザーとして解決できず、Agent field option もありません。このメンバーの Agent field を設定してください。",
        },
        github_access: {
          title: "GitHub の読み取り検証に失敗しました",
          description:
            "Project または repository の読み取りに失敗しました。GitHub 認証情報とリポジトリ設定を確認してください。",
        },
      },
    },
  },
};
