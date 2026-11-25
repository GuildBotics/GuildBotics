// The provider's diagnostics, merged into the app's translations.
export const branvil = {
  en: {
    overview: {
      diagnosticSuccess: {
        branvil_workspace: {
          title: "The workspace was read",
          description: "The workspace {{target}} was read.",
        },
      },
      diagnosticChecks: {
        branvil_workspace: {
          title: "The workspace could not be read",
          description: "Check that the workspace {{target}} exists in GuildBotics.",
        },
      },
    },
  },
  ja: {
    overview: {
      diagnosticSuccess: {
        branvil_workspace: {
          title: "ワークスペースを読み取りました",
          description: "ワークスペース {{target}} を読み取りました。",
        },
      },
      diagnosticChecks: {
        branvil_workspace: {
          title: "ワークスペースを読み取れませんでした",
          description: "ワークスペース {{target}} が GuildBotics にあるか確認してください。",
        },
      },
    },
  },
};
