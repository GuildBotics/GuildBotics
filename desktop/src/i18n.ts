import i18n from "i18next";
import { initReactI18next } from "react-i18next";

export type AppLanguage = "en" | "ja";

const STORAGE_KEY = "guildbotics.appLanguage";

const resources = {
  en: {
    translation: {
      app: {
        localWorkspace: "Local workspace",
        nav: {
          overview: "Overview",
          commands: "Commands",
          setup: "Setup",
        },
        language: {
          label: "Display language",
          english: "English",
          japanese: "Japanese",
        },
      },
      setup: {
        eyebrow: "Setup",
        title: "First setup",
        configuredTitle: "Settings",
        saveInitial: "Create initial settings",
        saveNow: "Save now",
        saveErrorTitle: "Save failed",
        initialCreated: {
          title: "Initial settings created",
          body: "The workspace settings were created. This screen is now in settings mode.",
        },
        saveMode: {
          manual:
            "First-time setup: click Create to write settings files. .env is appended when it already exists, otherwise created.",
          auto: "Changes are saved automatically.",
        },
        status: {
          readyTitle: "Required setup complete",
          readyMessage:
            "GuildBotics can run. Optional integrations such as GitHub can be added later.",
          progressTitle: "Required setup: {{done}} of {{total}} items configured",
          progressMessage:
            "Fill the missing items to complete the minimum setup required to run GuildBotics. GitHub integration can be added later.",
          inputProgressTitle: "Input progress: {{done}} of {{total}} sections completed",
          inputProgressMessage:
            "Complete the required inputs. When all items are completed, you can create the initial settings.",
          back: "Back",
          next: "Next",
        },
        nav: {
          project: "Project",
          intelligence: "LLM / CLI agent",
          github: "GitHub",
          members: "Members",
        },
        project: {
          title: "Project",
          subtitle:
            "Choose your working directory and basic project settings such as language and save location.",
          agentLanguage: "Agent default language",
          agentLanguageDescription:
            "Used for command and role definitions, and for LLM output instructions.",
          configLocation: "Settings file location",
          homeConfig: "Shared home config",
          workspaceConfig: "Inside workspace",
          description: "Project description",
          workspace: "Working directory",
          workspaceDescription:
            "This folder is used as the base location for your project settings.",
          choose: "Choose",
        },
        intelligence: {
          title: "LLM / CLI agent",
          subtitle:
            "Set default AI provider and CLI tool for your team.",
          teamDefault: "Team default",
          defaultProvider: "Default LLM provider",
          providerDescription: "Choose one provider used by default.",
          defaultCliAgent: "Default CLI agent",
          keyPlaceholder: "Saved to .env after input",
          keyConfiguredPlaceholder: "Configured",
          keyConfiguredDescription:
            "The saved value is hidden. Leave this field empty to keep the current key.",
          apiKeyConfigured: "API key set",
          apiKeyMissing: "API key missing",
          detected: "Detected",
          notDetected: "Not detected",
          cliHint:
            "CLI agents are not bundled. Only detected tools on PATH can be selected.",
          advanced: "Advanced settings",
          createBeforeAdvanced:
            "Create the initial settings before editing detailed AI behavior.",
          loadingAdvanced: "Loading advanced settings...",
          loadAdvancedError: "Failed to load advanced settings",
          saveAdvancedError: "Failed to save advanced settings",
          teamAdvancedDescription:
            "Configure team-wide model, CLI agent, and feature assignment defaults.",
          memberOverrideDescription:
            "Override only the default LLM provider and CLI agent for this member when needed.",
          inheritTeamDefaults: "Use team defaults",
          inheritingTitle: "Using team defaults",
          inheritingBody:
            "This member does not have individual intelligence files. Team settings are used.",
          memberDefaultProvider: "Default LLM provider",
          memberDefaultProviderDescription:
            "Overrides the default entry in this member's model_mapping.yml.",
          memberDefaultCliAgent: "Default CLI agent",
          memberDefaultCliAgentDescription:
            "Overrides the default entry in this member's cli_agent_mapping.yml.",
          modelMapping: "Model slots",
          modelDefinitions: "Model definitions",
          cliMapping: "CLI agent slots",
          brainMapping: "Feature assignments",
          cliDefinitions: "CLI agent definitions",
          slot: "Slot",
          model: "Model",
          path: "Path",
          modelClass: "Model class",
          modelId: "Model ID",
          cliAgent: "CLI agent",
          feature: "Feature",
          engine: "Engine",
          target: "Target",
          envJson: "Environment variables (JSON)",
          envJsonError: "Enter a JSON object.",
          script: "Script",
        },
        members: {
          title: "Members",
          subtitle:
            "Manage team members and their roles.",
          activeCountLabel: "Active members",
          activeCountValue: "{{count}} configured",
          requiredTitle: "At least one active member is required",
          requiredBody:
            "GuildBotics cannot run without an active member. Add at least one member before finishing setup.",
          memberActive: "Active",
          memberInactive: "Inactive",
          addTitle: "Add member",
          editTitle: "Edit member",
          type: "Member type",
          githubMode: "GitHub integration for this member",
          githubModeHint:
            "Required only when this member works with GitHub tickets, issues, or pull requests.",
          typeOptions: {
            none: "Do not link GitHub",
            machine_user: "Machine Account (Machine User)",
            github_apps: "GitHub Apps",
            proxy_agent: "Proxy Agent (use your own account as an AI agent)",
            human: "Human",
          },
          tabs: {
            basic: "Basic",
            intelligence: "LLM / CLI agent",
            github: "GitHub",
            slack: "Slack",
            diagnostics: "Diagnostics",
          },
          identity: "GitHub identity",
          identityHint: "GitHub username",
          githubAppsUrl: "GitHub Apps URL for resolution",
          githubAppsUrlHint:
            "Enter the app settings URL, then click Resolve. This URL is not saved.",
          githubResolvedIdentity: "GitHub identity",
          githubUsernameHint: "Enter the GitHub username, then click Resolve.",
          resolve: "Resolve",
          personId: "Member ID",
          personName: "Display name",
          githubUsername: "GitHub username",
          gitEmail: "Git email",
          roles: "Roles",
          rolesPlaceholder: "Select one or more roles",
          rolesEmpty: "No roles found",
          rolesLoadError: "Failed to load role options",
          speakingStyle: "Speaking style",
          clearDefaults: "Clear default values",
          applyDefaultTooltip: "Set default value",
          speakingStyleOptions: {
            friendly: "Friendly",
            professional: "Professional",
            machine: "Machine",
          },
          speakingStyleDescriptions: {
            professional:
              "Calm and logical, with restrained emotional expression. Emphasizes conveying necessary information clearly and concisely while maintaining a professional yet approachable tone.",
            friendly:
              "Energetic and friendly tone with expressive emotion. Approaches like a friend while remaining respectful and considerate.",
            machine:
              "Prioritizes mechanical and precise expression, excluding emotional elements. Aims to provide necessary information quickly and efficiently.",
          },
          characterArchetype: "Character archetype",
          characterArchetypeHint:
            "Short identifier for this member's persona and role in conversations.",
          characterTraits: "Personality traits",
          characterInterests: "Interests",
          characterJoinWhen: "Join when",
          characterAvoidWhen: "Avoid when",
          characterContributionStyle: "How to contribute",
          characterListHint: "One item per line.",
          relationships: "Relationship with other members",
          activeSwitch: "Active member",
          installationId: "GitHub Installation ID",
          appId: "GitHub App ID",
          privateKeyPath: "GitHub private key path",
          accessToken: "GitHub access token",
          accessTokenPlaceholder: "ghp_... or github_pat_...",
          githubAuthNotRequired: "GitHub auth is not required for human members.",
          githubDisabledMemberHint:
            "GitHub is not linked for this member. You can still use local commands, LLM / CLI settings, and Slack settings.",
          slackBotToken: "Slack Bot token",
          slackBotTokenPlaceholder: "xoxb-...",
          slackAppToken: "Slack App token",
          slackAppTokenPlaceholder: "xapp-...",
          slackChannels: "Slack channels to join",
          slackChannelsHint:
            "Comma-separated channel names or IDs. Examples: general, random, C0123456789.",
          addButton: "Add member",
          saveButton: "Save changes",
          newButton: "Add new member",
          editButton: "Edit",
          cancelButton: "Cancel",
          deleteButton: "Delete",
          tabHasError: "This tab has incomplete or invalid fields.",
          deleteConfirmTitle: "Delete member?",
          deleteConfirmBody:
            "This will delete {{name}}'s member settings and related secrets from .env. This action cannot be undone.",
          editingBadge: "Editing: {{id}}",
          loadError: "Failed to load member details",
          resolveError: "Failed to resolve member identity",
          addError: "Failed to add member",
          updateError: "Failed to update member",
          deleteError: "Failed to delete member",
          saveBeforeIntelligence:
            "Save this member before configuring individual LLM / CLI settings.",
          diagnostics: {
            title: "Member diagnostics",
            description:
              "Runs a read-only scenario check for this member. GitHub and Slack data are not updated.",
            run: "Validate this member",
            notRun: "Run diagnostics after saving changes.",
            failed: "Diagnostics failed",
            ok: "Member settings validated",
            okDescription: "{{count}} checks passed.",
            saveFirstTitle: "Save this member first",
            saveFirstBody:
              "Diagnostics run against saved settings. Add or save this member before validating it.",
          },
        },
        github: {
          title: "GitHub",
          subtitle:
            "Choose whether this project uses GitHub Projects, Issues, and repositories.",
          decision: "GitHub integration",
          decisionPlaceholder: "Choose whether to use GitHub",
          disabled: "Do not use GitHub",
          enabled: "Use GitHub",
          disabledHint:
            "GitHub-dependent routines stay disabled until you configure this integration.",
          projectUrl: "GitHub Project URL",
          repositoryUrl: "GitHub Repository URL",
        },
        autosave: {
          idle: "Autosave",
          saving: "Saving",
          saved: "Saved",
          error: "Save failed",
        },
        validation: {
          workspaceRequired: "Working directory is required.",
          descriptionRequired: "Project description is required.",
          githubDecisionRequired: "Choose whether to use GitHub.",
          githubProjectRequired: "GitHub project URL is required.",
          githubProjectInvalid:
            "Enter a GitHub Project URL such as https://github.com/orgs/<org>/projects/<number> or https://github.com/users/<user>/projects/<number>.",
          githubRepositoryRequired: "GitHub repository URL is required.",
          githubRepositoryInvalid:
            "Enter a GitHub Repository URL such as https://github.com/<owner>/<repository>.",
          githubRepositoryOwnerMismatch:
            "The repository owner must match the GitHub Project owner.",
          memberIdRequired: "Member ID is required.",
          memberIdInvalid:
            "Use only lowercase letters, digits, underscores, or hyphens.",
          memberIdDuplicate: "This member ID is already in use.",
          memberNameRequired: "Display name is required.",
          memberRolesRequired: "Select at least one role.",
          memberSpeakingStyleRequired: "Speaking style is required.",
          memberCharacterArchetypeRequired: "Character archetype is required.",
          memberCharacterTraitsRequired:
            "Enter at least one personality trait.",
          memberCharacterInterestsRequired: "Enter at least one interest.",
          memberCharacterJoinWhenRequired: "Join-when guidance is required.",
          memberCharacterAvoidWhenRequired: "Avoid-when guidance is required.",
          memberCharacterContributionRequired:
            "Contribution style is required.",
          memberGithubIdentityRequired:
            "Enter the GitHub identity, then click Resolve.",
          memberGithubAppsUrlRequired:
            "Enter the GitHub Apps URL, then click Resolve.",
          memberGithubIdentityNotFound:
            "Could not resolve this GitHub identity. Check the username, email, or GitHub Apps URL.",
          memberGithubIdentityResolveFailed:
            "Failed to resolve this GitHub identity. Check your network connection and try again.",
          memberGithubAppsUrlInvalid:
            "Enter a GitHub Apps settings URL such as https://github.com/organizations/<org>/settings/apps/<app>.",
          memberGithubUsernameRequired:
            "Resolve the GitHub identity or enter the GitHub username.",
          memberGithubUsernameInvalid:
            "Enter a valid GitHub username. Email addresses are not supported here.",
          memberGitEmailRequired:
            "Resolve the GitHub identity or enter the Git email address.",
          memberGitEmailInvalid: "Enter a valid email address.",
          githubInstallationIdRequired: "GitHub Installation ID is required.",
          githubInstallationIdInvalid: "GitHub Installation ID must be digits only.",
          githubAppIdRequired: "GitHub App ID is required.",
          githubAppIdInvalid: "GitHub App ID must be digits only.",
          githubPrivateKeyPathRequired: "GitHub private key path is required.",
          githubAccessTokenRequired: "GitHub access token is required.",
          githubAccessTokenInvalid:
            "Enter a GitHub access token starting with ghp_, gho_, ghu_, ghs_, ghr_, or github_pat_.",
          slackBotTokenRequired:
            "Slack Bot token is required when Slack channels are configured.",
          slackBotTokenInvalid: "Enter a Slack Bot token starting with xoxb-.",
          slackAppTokenRequired:
            "Slack App token is required when Slack channels are configured.",
          slackAppTokenInvalid: "Enter a Slack App token starting with xapp-.",
          slackChannelsInvalid:
            "Enter Slack channel names or IDs separated by commas. Use lowercase channel names such as general or Slack IDs such as C0123456789.",
        },
      },
      overview: {
        eyebrow: "Desktop Preview",
        title: "Runtime Control",
        refresh: "Refresh",
        configuration: "Configuration",
        verify: "Run diagnostics",
        scenarioDiagnostics: {
          run: "Validate settings",
          notRun:
            "Runs a read-only scenario check across LLM, CLI agent, GitHub, Slack, and Git. It does not update GitHub or Slack data.",
          running: "Validating settings...",
          failed: "Validation failed",
          ok: "Settings validated",
          okDescription:
            "{{count}} checks passed. GitHub and Slack checks are read-only.",
          target: "Target",
        },
        diagnosticSections: {
          config: "Config",
          members: "Members",
          llm: "LLM",
          cli_agent: "CLI agent",
          github: "GitHub",
          slack: "Slack",
          git: "Git",
        },
        diagnosticSuccess: {
          config_load: {
            title: "Configuration was loaded",
            description: "The saved project and member settings were read successfully.",
          },
          active_members: {
            title: "Member settings are available",
            description: "The target member can be checked.",
          },
          llm_live_call: {
            title: "LLM check passed",
            description: "The selected LLM provider accepted a minimal request.",
          },
          cli_agent_executable: {
            title: "CLI agent executable was found",
            description:
              "The selected CLI agent command was found on PATH. Runtime behavior is checked separately.",
          },
          cli_agent_brain: {
            title: "CLI agent check passed",
            description:
              "The configured CLI agent ran through CliAgentBrain and returned a response to a minimal read-only request.",
          },
          github_not_configured: {
            title: "GitHub is not configured",
            description: "GitHub diagnostics were skipped because this project does not use GitHub.",
          },
          github_project_access: {
            title: "GitHub Project was readable",
            description: "Project status options were fetched without updating GitHub.",
          },
          github_repository_access: {
            title: "GitHub repository was readable",
            description: "Repository metadata was fetched without updating GitHub.",
          },
          slack_not_configured: {
            title: "Slack channels are not configured",
            description: "Slack diagnostics were skipped for this member.",
          },
          slack_bot_auth: {
            title: "Slack Bot authentication passed",
            description: "Slack Bot identity was read successfully.",
          },
          slack_channel_history: {
            title: "Slack channel history was readable",
            description: "Channel history was read without posting messages.",
          },
        },
        diagnostics: {
          notRun:
            "Optional lightweight diagnostics. Most setup problems are handled by form validation and save errors.",
          running: "Checking settings...",
          failed: "Diagnostics failed",
          ok: "No obvious configuration issues",
          okDescription: "{{count}} checks passed. This does not validate external LLM API keys.",
          target: "Target",
        },
        diagnosticChecks: {
          config_project_file: {
            title: "Project settings were not found",
            description:
              "Open Setup and create the initial settings for this workspace.",
          },
          env_file: {
            title: ".env was not found",
            description:
              "Secrets are normally read from .env. This may be fine if environment variables are provided another way.",
          },
          team_load: {
            title: "Team settings could not be loaded",
            description:
              "Project or member settings may be incomplete or invalid.",
          },
          active_members: {
            title: "No active member is configured",
            description:
              "Add at least one active member before running GuildBotics.",
          },
          llm_api_key: {
            title: "LLM API key is missing",
            description:
              "The selected provider key was not found. This check only confirms presence.",
          },
          cli_agent_mapping: {
            title: "Default CLI agent could not be determined",
            description:
              "Review the LLM / CLI agent settings.",
          },
          cli_agent_executable: {
            title: "Default CLI agent was not found",
            description:
              "Install the selected CLI agent or choose another detected CLI agent.",
          },
          cli_agent_brain: {
            title: "CLI agent check failed",
            description:
              "The configured CliAgentBrain could not complete a minimal read-only request. See details below.",
          },
          github_credential: {
            title: "Member GitHub credential is missing",
            description:
              "Configure the member's GitHub credentials before using GitHub workflows.",
          },
          llm_live_call: {
            title: "LLM check failed",
            description:
              "The selected LLM provider did not accept the minimal validation request.",
          },
          github_project_access: {
            title: "GitHub project access failed",
            description:
              "The GitHub Project could not be read with this member's credentials.",
          },
          github_repository_access: {
            title: "GitHub repository access failed",
            description:
              "The GitHub repository metadata could not be read with this member's credentials.",
          },
          github_access: {
            title: "GitHub read-only check failed",
            description:
              "Project or repository access failed. Check GitHub credentials and repository settings.",
          },
          slack_app_token: {
            title: "Slack App token is missing",
            description:
              "Socket Mode runtime requires a Slack App token for this member.",
          },
          slack_bot_auth: {
            title: "Slack bot authentication failed",
            description:
              "Slack bot authentication failed. Check the member's Slack Bot token.",
          },
          slack_channel: {
            title: "Slack channel could not be resolved",
            description:
              "The configured Slack channel name or ID could not be resolved.",
          },
          slack_channel_history: {
            title: "Slack channel history could not be read",
            description:
              "Slack channel history could not be read. Check channel access and token scopes.",
          },
          slack_access: {
            title: "Slack read-only check failed",
            description:
              "Slack read-only access failed. Check Slack tokens and channel settings.",
          },
        },
        config: "Config",
        ready: "Ready",
        missing: "Missing",
        env: ".env",
        found: "Found",
        notFound: "Not found",
        github: "GitHub",
        enabled: "Enabled",
        disabled: "Disabled",
        activeMembers: "Active members",
        scheduler: "Scheduler",
        running: "Running",
        stopped: "Stopped",
        routine: "Routine command",
        requiresGithub: "GitHub required",
        startGuardTitle: "This routine requires GitHub integration",
        startGuardBody:
          "Enable GitHub integration in Setup before starting this routine.",
        startError: "Scheduler start failed",
        runtimeFeed: "Runtime stream",
        events: "Events",
        logs: "Logs",
        start: "Start",
        stop: "Stop",
      },
      commands: {
        eyebrow: "Commands",
        title: "Run Command",
        run: "Run",
        member: "Member",
        memberPlaceholder: "Use default selection",
        command: "Command",
        message: "Message",
        events: "Events",
        logs: "Logs",
        githubRequiredTitle: "GitHub integration is required",
        githubRequiredBody:
          "ticket_driven_workflow cannot run until GitHub integration is configured.",
      },
    },
  },
  ja: {
    translation: {
      app: {
        localWorkspace: "Local workspace",
        nav: {
          overview: "Overview",
          commands: "Commands",
          setup: "Setup",
        },
        language: {
          label: "表示言語",
          english: "English",
          japanese: "日本語",
        },
      },
      setup: {
        eyebrow: "Setup",
        title: "はじめに",
        configuredTitle: "設定",
        saveInitial: "初期設定を作成",
        saveNow: "今すぐ保存",
        saveErrorTitle: "保存に失敗しました",
        initialCreated: {
          title: "初期設定を作成しました",
          body: "作業ディレクトリの設定ファイルを作成しました。この画面は設定済み状態に切り替わりました。",
        },
        saveMode: {
          manual:
            "初回は「初期設定を作成」を押して設定ファイルを作成します。.env は既存があれば追記、なければ新規作成します。",
          auto: "変更は自動で保存されます。",
        },
        status: {
          readyTitle: "必須設定 完了",
          readyMessage:
            "実行できます。GitHub などの連携は必要になったタイミングで追加できます。",
          progressTitle: "必須設定: {{total}}項目中 {{done}}項目 設定済み",
          progressMessage:
            "不足項目を埋めると、GuildBoticsの実行に必要な初期設定が完了します。GitHub連携は後から追加できます。",
          inputProgressTitle: "入力進捗: {{total}}セクション中 {{done}}セクション完了",
          inputProgressMessage:
            "必須項目を入力してください。すべて入力すると初期設定を作成できます。",
          back: "戻る",
          next: "次へ",
        },
        nav: {
          project: "プロジェクト",
          intelligence: "LLM・CLIエージェント",
          github: "GitHub",
          members: "メンバー",
        },
        project: {
          title: "プロジェクト",
          subtitle:
            "作業ディレクトリと言語・保存先などの基本設定を指定します。",
          agentLanguage: "エージェントの既定言語",
          agentLanguageDescription:
            "コマンド、ロール定義、LLMへの指示で使う既定言語です。",
          configLocation: "設定ファイルの保存先",
          homeConfig: "ホーム共通",
          workspaceConfig: "作業ディレクトリ内",
          description: "プロジェクトの説明",
          workspace: "作業ディレクトリ",
          workspaceDescription: "このフォルダを基点にプロジェクト設定を保存します。",
          choose: "選択",
        },
        intelligence: {
          title: "LLM・CLIエージェント",
          subtitle:
            "チーム全体で使うAIプロバイダとCLIツールの既定値を設定します。",
          teamDefault: "チーム既定",
          defaultProvider: "デフォルトの LLM プロバイダ",
          providerDescription: "利用する1つを選択します。",
          defaultCliAgent: "デフォルトの CLI エージェント",
          keyPlaceholder: "入力後 .env に保存",
          keyConfiguredPlaceholder: "設定済み",
          keyConfiguredDescription:
            "保存済みの値は表示しません。空欄のままなら現在のキーを維持します。",
          apiKeyConfigured: "API key 設定済み",
          apiKeyMissing: "API key 未設定",
          detected: "検出済み",
          notDetected: "未検出",
          cliHint:
            "CLIエージェントは同梱しません。PATHで検出できたものだけ選択できます。",
          advanced: "詳細設定",
          createBeforeAdvanced:
            "詳細なAI挙動を編集するには、先に初期設定を作成してください。",
          loadingAdvanced: "詳細設定を読み込み中...",
          loadAdvancedError: "詳細設定の取得に失敗しました",
          saveAdvancedError: "詳細設定の保存に失敗しました",
          teamAdvancedDescription:
            "チーム全体で使うモデル、CLIエージェント、機能ごとの割り当てを設定します。",
          memberOverrideDescription:
            "必要な場合だけ、このメンバー専用のデフォルト LLM プロバイダと CLI エージェントを上書きします。",
          inheritTeamDefaults: "チーム既定を使う",
          inheritingTitle: "チーム既定を使用中",
          inheritingBody:
            "このメンバー専用の intelligence 設定ファイルは作成せず、チーム設定を使います。",
          memberDefaultProvider: "デフォルトの LLM プロバイダ",
          memberDefaultProviderDescription:
            "このメンバーの model_mapping.yml の default を上書きします。",
          memberDefaultCliAgent: "デフォルトの CLI エージェント",
          memberDefaultCliAgentDescription:
            "このメンバーの cli_agent_mapping.yml の default を上書きします。",
          modelMapping: "モデルスロット",
          modelDefinitions: "モデル定義",
          cliMapping: "CLIエージェントスロット",
          brainMapping: "機能ごとの割り当て",
          cliDefinitions: "CLIエージェント定義",
          slot: "スロット",
          model: "モデル",
          path: "パス",
          modelClass: "モデルクラス",
          modelId: "モデルID",
          cliAgent: "CLIエージェント",
          feature: "機能",
          engine: "エンジン",
          target: "割り当て先",
          envJson: "環境変数（JSON）",
          envJsonError: "JSONオブジェクトを入力してください。",
          script: "スクリプト",
        },
        members: {
          title: "メンバー",
          subtitle:
            "チームメンバーと役割を管理します。",
          activeCountLabel: "有効メンバー",
          activeCountValue: "{{count}}人 設定済み",
          requiredTitle: "有効メンバーを1人以上設定してください",
          requiredBody:
            "有効メンバーが0人の状態ではGuildBoticsは実行できません。セットアップ完了前に1人以上追加してください。",
          memberActive: "有効",
          memberInactive: "無効",
          addTitle: "メンバーを追加",
          editTitle: "メンバーを編集",
          type: "メンバー種別",
          githubMode: "このメンバーの GitHub 連携",
          githubModeHint:
            "GitHub のチケット、Issue、Pull Request を扱うメンバーだけ設定します。",
          typeOptions: {
            none: "GitHub連携なし",
            machine_user: "マシンアカウント（マシンユーザー）",
            github_apps: "GitHub Apps",
            proxy_agent: "代理エージェント（自分自身のアカウントをAIエージェント用に利用する）",
            human: "人間",
          },
          tabs: {
            basic: "基本",
            intelligence: "LLM・CLIエージェント",
            github: "GitHub",
            slack: "Slack",
            diagnostics: "検証",
          },
          identity: "GitHub識別子",
          identityHint: "GitHubユーザー名",
          githubAppsUrl: "解決用 GitHub Apps URL",
          githubAppsUrlHint:
            "GitHub App の設定URLを入力して「解決」を実行します。このURLは保存されません。",
          githubResolvedIdentity: "GitHub識別子",
          githubUsernameHint: "GitHubユーザー名を入力して「解決」を実行します。",
          resolve: "解決",
          personId: "メンバーID",
          personName: "表示名",
          githubUsername: "GitHubユーザー名",
          gitEmail: "Gitメールアドレス",
          roles: "ロール",
          rolesPlaceholder: "1つ以上選択",
          rolesEmpty: "ロールが見つかりません",
          rolesLoadError: "ロール候補の取得に失敗しました",
          speakingStyle: "会話スタイル",
          clearDefaults: "デフォルト値をクリア",
          applyDefaultTooltip: "デフォルト値を設定する",
          speakingStyleOptions: {
            friendly: "フレンドリー",
            professional: "プロフェッショナル",
            machine: "マシン",
          },
          speakingStyleDescriptions: {
            professional:
              "冷静で論理的、感情的な表現は控えめです。必要な情報を明確かつ簡潔に伝えることを重視し、プロフェッショナルな態度を保ちながらも親しみやすい雰囲気を持ちます。",
            friendly:
              "元気で親しみやすい口調で感情豊かに話します。友達感覚で接しつつも相手へのリスペクトや思いやりを忘れません。",
            machine:
              "機械的で正確な表現を重視し、感情的な要素は排除します。必要な情報を迅速かつ効率的に提供することを目指します。",
          },
          characterArchetype: "会話上の立ち位置",
          characterArchetypeHint:
            "このメンバーがどんな視点で会話に入るかを一言で表します。",
          characterTraits: "性格・ふるまい",
          characterInterests: "得意・関心領域",
          characterJoinWhen: "会話に参加する場面",
          characterAvoidWhen: "参加を控える場面",
          characterContributionStyle: "参加時の貢献スタイル",
          characterListHint: "1行に1項目で入力します。",
          relationships: "他のメンバーとの関係性",
          activeSwitch: "有効メンバーにする",
          installationId: "GitHub Installation ID",
          appId: "GitHub App ID",
          privateKeyPath: "GitHub秘密鍵パス",
          accessToken: "GitHubアクセストークン",
          accessTokenPlaceholder: "ghp_... または github_pat_...",
          githubAuthNotRequired: "人間メンバーでは GitHub 認証は不要です。",
          githubDisabledMemberHint:
            "このメンバーは GitHub と連携しません。ローカルコマンド、LLM・CLI設定、Slack設定は利用できます。",
          slackBotToken: "Slack Bot トークン",
          slackBotTokenPlaceholder: "xoxb-...",
          slackAppToken: "Slack App トークン",
          slackAppTokenPlaceholder: "xapp-...",
          slackChannels: "参加するSlackチャンネル",
          slackChannelsHint:
            "チャンネル名またはIDをカンマ区切りで入力します。例: general, random, C0123456789",
          addButton: "メンバーを追加",
          saveButton: "変更を保存",
          newButton: "新しいメンバーを追加",
          editButton: "編集",
          cancelButton: "キャンセル",
          deleteButton: "削除",
          tabHasError: "このタブに未入力または形式エラーの項目があります。",
          deleteConfirmTitle: "メンバーを削除しますか？",
          deleteConfirmBody:
            "{{name}} のメンバー設定と .env 内の関連シークレットを削除します。この操作は元に戻せません。",
          editingBadge: "編集中: {{id}}",
          loadError: "メンバー詳細の取得に失敗しました",
          resolveError: "メンバー識別子の解決に失敗しました",
          addError: "メンバー追加に失敗しました",
          updateError: "メンバー更新に失敗しました",
          deleteError: "メンバー削除に失敗しました",
          saveBeforeIntelligence:
            "メンバー個別の LLM・CLI 設定は、メンバーを保存した後に設定できます。",
          diagnostics: {
            title: "メンバー設定の検証",
            description:
              "このメンバーの設定を読み取り専用で検証します。GitHub や Slack のデータは更新しません。",
            run: "このメンバーを検証",
            notRun: "変更を保存した後に検証を実行してください。",
            failed: "検証に失敗しました",
            ok: "メンバー設定を検証しました",
            okDescription: "{{count}}件のチェックに問題はありません。",
            saveFirstTitle: "先にメンバーを保存してください",
            saveFirstBody:
              "検証は保存済みの設定を対象に実行します。メンバーを追加または保存してから検証してください。",
          },
        },
        github: {
          title: "GitHub",
          subtitle:
            "このプロジェクトで GitHub Projects、Issue、リポジトリを使うかを選択します。",
          decision: "GitHub連携",
          decisionPlaceholder: "GitHubを使うか選択",
          disabled: "GitHubを使わない",
          enabled: "GitHubを使う",
          disabledHint:
            "GitHub が必要な routine は、この連携を設定するまで起動できません。",
          projectUrl: "GitHub Project URL",
          repositoryUrl: "GitHub Repository URL",
        },
        autosave: {
          idle: "自動保存",
          saving: "保存中",
          saved: "保存済み",
          error: "保存失敗",
        },
        validation: {
          workspaceRequired: "作業ディレクトリは必須です。",
          descriptionRequired: "プロジェクトの説明は必須です。",
          githubDecisionRequired: "GitHubを使うか選択してください。",
          githubProjectRequired: "GitHub Project URL が必要です。",
          githubProjectInvalid:
            "GitHub Project URL は https://github.com/orgs/<org>/projects/<number> または https://github.com/users/<user>/projects/<number> の形式で入力してください。",
          githubRepositoryRequired: "GitHub Repository URL が必要です。",
          githubRepositoryInvalid:
            "GitHub Repository URL は https://github.com/<owner>/<repository> の形式で入力してください。",
          githubRepositoryOwnerMismatch:
            "Repository URL の owner は GitHub Project の owner と一致している必要があります。",
          memberIdRequired: "メンバーIDは必須です。",
          memberIdInvalid:
            "メンバーIDは小文字英数字、アンダースコア、ハイフンのみ使えます。",
          memberIdDuplicate: "このメンバーIDはすでに使われています。",
          memberNameRequired: "表示名は必須です。",
          memberRolesRequired: "ロールを1つ以上選択してください。",
          memberSpeakingStyleRequired: "会話スタイルは必須です。",
          memberCharacterArchetypeRequired: "会話上の立ち位置は必須です。",
          memberCharacterTraitsRequired:
            "性格・ふるまいを1つ以上入力してください。",
          memberCharacterInterestsRequired:
            "得意・関心領域を1つ以上入力してください。",
          memberCharacterJoinWhenRequired: "会話に参加する場面は必須です。",
          memberCharacterAvoidWhenRequired: "参加を控える場面は必須です。",
          memberCharacterContributionRequired:
            "参加時の貢献スタイルは必須です。",
          memberGithubIdentityRequired:
            "GitHub識別子を入力し、「解決」を実行してください。",
          memberGithubAppsUrlRequired:
            "GitHub Apps URL を入力し、「解決」を実行してください。",
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
          memberGitEmailRequired:
            "GitHub識別子を解決するか、Gitメールアドレスを入力してください。",
          memberGitEmailInvalid: "有効なメールアドレスを入力してください。",
          githubInstallationIdRequired: "GitHub Installation ID は必須です。",
          githubInstallationIdInvalid: "GitHub Installation ID は数字のみで入力してください。",
          githubAppIdRequired: "GitHub App ID は必須です。",
          githubAppIdInvalid: "GitHub App ID は数字のみで入力してください。",
          githubPrivateKeyPathRequired: "GitHub秘密鍵パスは必須です。",
          githubAccessTokenRequired: "GitHubアクセストークンは必須です。",
          githubAccessTokenInvalid:
            "GitHubアクセストークンは ghp_、gho_、ghu_、ghs_、ghr_、github_pat_ のいずれかで始まる値を入力してください。",
          slackBotTokenRequired:
            "参加するSlackチャンネルを設定する場合、Slack Bot トークンは必須です。",
          slackBotTokenInvalid: "Slack Bot トークンは xoxb- で始まる値を入力してください。",
          slackAppTokenRequired:
            "参加するSlackチャンネルを設定する場合、Slack App トークンは必須です。",
          slackAppTokenInvalid: "Slack App トークンは xapp- で始まる値を入力してください。",
          slackChannelsInvalid:
            "Slackチャンネル名またはIDをカンマ区切りで入力してください。チャンネル名は general のような小文字名、IDは C0123456789 のような形式です。",
        },
      },
      overview: {
        eyebrow: "Desktop Preview",
        title: "Runtime Control",
        refresh: "Refresh",
        configuration: "Configuration",
        verify: "診断を実行",
        scenarioDiagnostics: {
          run: "設定を検証",
          notRun:
            "LLM、CLIエージェント、GitHub、Slack、Git を読み取り専用で検証します。GitHub や Slack のデータは更新しません。",
          running: "設定を検証しています...",
          failed: "検証に失敗しました",
          ok: "設定を検証しました",
          okDescription:
            "{{count}}件のチェックに問題はありません。GitHub / Slack の検証は読み取り専用です。",
          target: "対象",
        },
        diagnosticSections: {
          config: "設定",
          members: "メンバー",
          llm: "LLM",
          cli_agent: "CLIエージェント",
          github: "GitHub",
          slack: "Slack",
          git: "Git",
        },
        diagnosticSuccess: {
          config_load: {
            title: "設定を読み込みました",
            description: "保存済みのプロジェクト設定とメンバー設定を読み込めました。",
          },
          active_members: {
            title: "対象メンバーを検証できます",
            description: "このメンバーの保存済み設定を対象に検証しています。",
          },
          llm_live_call: {
            title: "LLM の検証に成功しました",
            description: "選択中の LLM provider が最小リクエストを受け付けました。",
          },
          cli_agent_executable: {
            title: "CLIエージェント実行ファイルを検出しました",
            description:
              "選択中の CLIエージェントコマンドが PATH 上に見つかりました。実際に応答できるかは別の項目で検証します。",
          },
          cli_agent_brain: {
            title: "CLIエージェントの検証に成功しました",
            description:
              "設定された CLIエージェントが CliAgentBrain 経由で実行され、最小の読み取り専用リクエストに応答しました。",
          },
          github_not_configured: {
            title: "GitHub は未設定です",
            description: "このプロジェクトは GitHub を使わないため、GitHub 検証をスキップしました。",
          },
          github_project_access: {
            title: "GitHub Project を読み取れました",
            description: "GitHub を更新せず、Project の status options を取得できました。",
          },
          github_repository_access: {
            title: "GitHub リポジトリを読み取れました",
            description: "GitHub を更新せず、リポジトリ情報を取得できました。",
          },
          slack_not_configured: {
            title: "Slack チャンネルは未設定です",
            description: "このメンバーには Slack チャンネルがないため、Slack 検証をスキップしました。",
          },
          slack_bot_auth: {
            title: "Slack Bot 認証に成功しました",
            description: "Slack Bot の identity を読み取れました。",
          },
          slack_channel_history: {
            title: "Slack チャンネル履歴を読み取れました",
            description: "メッセージを投稿せず、チャンネル履歴を読み取れました。",
          },
        },
        diagnostics: {
          notRun:
            "補助的な軽量診断です。初期設定の大半の問題はフォーム入力制限と保存エラーで扱います。",
          running: "設定を診断しています...",
          failed: "診断に失敗しました",
          ok: "明らかな設定問題はありません",
          okDescription:
            "{{count}}件のチェックに問題はありません。外部 LLM API key の正当性は検証しません。",
          target: "対象",
        },
        diagnosticChecks: {
          config_project_file: {
            title: "プロジェクト設定が見つかりません",
            description:
              "Setup でこの workspace の初期設定を作成してください。",
          },
          env_file: {
            title: ".env が見つかりません",
            description:
              "通常、シークレットは .env から読み込みます。別の方法で環境変数を渡している場合は問題ありません。",
          },
          team_load: {
            title: "チーム設定を読み込めません",
            description:
              "プロジェクトまたはメンバー設定が未完成、または不正な可能性があります。",
          },
          active_members: {
            title: "有効メンバーがいません",
            description:
              "GuildBotics を実行するには、有効なメンバーを1人以上追加してください。",
          },
          llm_api_key: {
            title: "LLM API key が見つかりません",
            description:
              "選択中の provider key が見つかりません。この診断は存在確認のみです。",
          },
          cli_agent_mapping: {
            title: "既定の CLI エージェントを判定できません",
            description:
              "LLM・CLIエージェント設定を確認してください。",
          },
          cli_agent_executable: {
            title: "既定の CLI エージェントが見つかりません",
            description:
              "選択中の CLI エージェントをインストールするか、検出済みの別エージェントを選択してください。",
          },
          cli_agent_brain: {
            title: "CLIエージェントの検証に失敗しました",
            description:
              "設定された CliAgentBrain が最小の読み取り専用リクエストを完了できませんでした。詳細を確認してください。",
          },
          github_credential: {
            title: "メンバーの GitHub 認証情報が不足しています",
            description:
              "GitHub ワークフローを使う前に、対象メンバーの GitHub 認証情報を設定してください。",
          },
          llm_live_call: {
            title: "LLM の検証に失敗しました",
            description:
              "選択中の LLM provider が最小リクエストを受け付けませんでした。",
          },
          github_project_access: {
            title: "GitHub Project にアクセスできません",
            description:
              "このメンバーの認証情報で GitHub Project を読み取れませんでした。",
          },
          github_repository_access: {
            title: "GitHub リポジトリにアクセスできません",
            description:
              "このメンバーの認証情報で GitHub リポジトリ情報を読み取れませんでした。",
          },
          github_access: {
            title: "GitHub の読み取り検証に失敗しました",
            description:
              "Project または repository の読み取りに失敗しました。GitHub 認証情報とリポジトリ設定を確認してください。",
          },
          slack_app_token: {
            title: "Slack App トークンが不足しています",
            description:
              "Socket Mode runtime には、このメンバーの Slack App トークンが必要です。",
          },
          slack_bot_auth: {
            title: "Slack Bot 認証に失敗しました",
            description:
              "Slack Bot 認証に失敗しました。メンバーの Slack Bot トークンを確認してください。",
          },
          slack_channel: {
            title: "Slack チャンネルを解決できません",
            description:
              "設定された Slack チャンネル名または ID を解決できませんでした。",
          },
          slack_channel_history: {
            title: "Slack チャンネル履歴を読み取れません",
            description:
              "Slack チャンネル履歴を読み取れませんでした。チャンネル権限と token scope を確認してください。",
          },
          slack_access: {
            title: "Slack の読み取り検証に失敗しました",
            description:
              "Slack の読み取りアクセスに失敗しました。Slack トークンとチャンネル設定を確認してください。",
          },
        },
        config: "Config",
        ready: "Ready",
        missing: "Missing",
        env: ".env",
        found: "Found",
        notFound: "Not found",
        github: "GitHub",
        enabled: "有効",
        disabled: "無効",
        activeMembers: "Active members",
        scheduler: "Scheduler",
        running: "Running",
        stopped: "Stopped",
        routine: "ルーチンコマンド",
        requiresGithub: "GitHub必須",
        startGuardTitle: "このルーチンには GitHub 連携が必要です",
        startGuardBody:
          "セットアップ画面で GitHub 連携を有効化してから開始してください。",
        startError: "Scheduler の起動に失敗しました",
        runtimeFeed: "実行ストリーム",
        events: "イベント",
        logs: "ログ",
        start: "Start",
        stop: "Stop",
      },
      commands: {
        eyebrow: "Commands",
        title: "Run Command",
        run: "Run",
        member: "メンバー",
        memberPlaceholder: "デフォルト選択を使う",
        command: "Command",
        message: "Message",
        events: "イベント",
        logs: "ログ",
        githubRequiredTitle: "GitHub 連携が必要です",
        githubRequiredBody:
          "ticket_driven_workflow は GitHub 連携設定が完了するまで実行できません。",
      },
    },
  },
};

export function getInitialAppLanguage(): AppLanguage {
  const stored = normalizeLanguage(localStorage.getItem(STORAGE_KEY));
  if (stored) {
    return stored;
  }
  return normalizeLanguage(navigator.language) ?? "en";
}

export function setAppLanguage(language: AppLanguage) {
  localStorage.setItem(STORAGE_KEY, language);
  return i18n.changeLanguage(language);
}

export function normalizeLanguage(value: string | null | undefined): AppLanguage | null {
  if (!value) {
    return null;
  }
  const normalized = value.toLowerCase();
  if (normalized.startsWith("ja")) {
    return "ja";
  }
  if (normalized.startsWith("en")) {
    return "en";
  }
  return null;
}

i18n.use(initReactI18next).init({
  resources,
  lng: getInitialAppLanguage(),
  fallbackLng: "en",
  interpolation: {
    escapeValue: false,
  },
});

export default i18n;
