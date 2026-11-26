const en = {
  "markdown.diagram": "Diagram",
  "markdown.diagramSource": "Show source",
  "markdown.diagramFullSize": "Show full size",
  "markdown.diagramError": "Could not draw the diagram",
  "common.close": "Close",
  "attach.choose": "Attach images",
  "attach.placeholder": "Uploading {{name}}…",
  "attach.uploading": "Uploading images…",
  "hub.versionLabel": '{{sha}} ("{{subject}}")',
  "hub.preparingVersions":
    "Preparing an automatic Hub update from {{current}} to {{target}}. New runs will stop being accepted soon.",
  "hub.updateVersions":
    "The Hub is updating automatically and is not accepting new runs. Patrol, schedules and chat reception are stopped; manual runs and environment builds are refused. After {{runs}} running tasks and {{ci_jobs}} CI jobs finish, the Hub will switch from {{current}} to {{target}}. Services will resume afterward if they were running when the stop was accepted.",
  "hub.title": "Hub maintenance",
  "hub.version": "Hub version",
  "hub.pendingReleases": "Releases not applied yet: {{names}}.",
  "hub.noPendingReleases": "No release is waiting to be applied.",
  "hub.releasesPage": "Releases",
  "hub.separator": ", ",
  "hub.dismiss": "Dismiss until the phase changes",
  "hub.preparing": "Preparing an automatic Hub update. New runs will stop being accepted soon.",
  "hub.update":
    "The Hub is updating automatically and is not accepting new runs. Patrol, schedules and chat reception are stopped; manual runs and environment builds are refused. After {{runs}} running tasks and {{ci_jobs}} CI jobs finish, the Hub will switch versions. Services will resume afterward if they were running when the stop was accepted.",
  "hub.restart":
    "The Hub is restarting and is not accepting new runs. Patrol, schedules and chat reception are stopped; manual runs and environment builds are refused. It will restart after {{runs}} running tasks and {{ci_jobs}} CI jobs finish.",
  "hub.stop":
    "The Hub is stopping and is not accepting new runs. Patrol, schedules and chat reception are stopped; manual runs and environment builds are refused. It will stop after {{runs}} running tasks and {{ci_jobs}} CI jobs finish.",
  "common.retry": "Retry",
  "app.language.label": "Display language",
};

const ja = {
  "markdown.diagram": "図",
  "markdown.diagramSource": "ソースを表示",
  "markdown.diagramFullSize": "原寸で表示",
  "markdown.diagramError": "図を描画できませんでした",
  "common.close": "閉じる",
  "attach.choose": "画像を添付",
  "attach.placeholder": "{{name}} をアップロード中…",
  "attach.uploading": "画像をアップロード中…",
  "hub.versionLabel": "{{sha}}（{{subject}}）",
  "hub.preparingVersions":
    "Hub を {{current}} から {{target}} に自動更新する準備をしています。まもなく新しい実行の受け付けを止めます。",
  "hub.updateVersions":
    "Hub の自動更新のため、新しい実行の受け付けを止めています。巡回・定期実行・チャットの受信は止まり、手動の実行や実行環境のビルドは断ります。実行中の作業 {{runs}} 件と CI のジョブ {{ci_jobs}} 件が終わると、{{current}} から {{target}} に切り替わります。止めた時点でサービスが動いていたら、切り替えのあとに再開します。",
  "hub.title": "Hub の作業",
  "hub.version": "Hub の版",
  "hub.pendingReleases": "適用待ちのリリース：{{names}}",
  "hub.noPendingReleases": "適用待ちのリリースはありません。",
  "hub.releasesPage": "リリースの画面",
  "hub.separator": "、",
  "hub.dismiss": "段階が変わるまで閉じる",
  "hub.preparing": "Hub を自動更新する準備をしています。まもなく新しい実行の受け付けを止めます。",
  "hub.update":
    "Hub の自動更新のため、新しい実行の受け付けを止めています。巡回・定期実行・チャットの受信は止まり、手動の実行や実行環境のビルドは断ります。実行中の作業 {{runs}} 件と CI のジョブ {{ci_jobs}} 件が終わると、版を切り替えます。止めた時点でサービスが動いていたら、切り替えのあとに再開します。",
  "hub.restart":
    "Hub の再起動のため、新しい実行の受け付けを止めています。巡回・定期実行・チャットの受信は止まり、手動の実行や実行環境のビルドは断ります。実行中の作業 {{runs}} 件と CI のジョブ {{ci_jobs}} 件が終わると、再起動します。",
  "hub.stop":
    "Hub の停止のため、新しい実行の受け付けを止めています。巡回・定期実行・チャットの受信は止まり、手動の実行や実行環境のビルドは断ります。実行中の作業 {{runs}} 件と CI のジョブ {{ci_jobs}} 件が終わると、停止します。",
  "common.retry": "再試行",
  "app.language.label": "表示言語",
} satisfies Record<keyof typeof en, string>;

export const shared = { en, ja };
