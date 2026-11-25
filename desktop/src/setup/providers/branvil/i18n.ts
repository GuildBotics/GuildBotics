// The provider's diagnostics, merged into the app's translations.
export const branvil = {
  en: {
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
