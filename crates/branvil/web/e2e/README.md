# Branvil のブラウザ検証

ルートで `npm ci` を実行し、Chromium を導入してから
`npm run e2e --workspace branvil-web` を実行する。画面と Rust のバイナリを
ビルドし、loopback の一時サービスを起動する。既存 Hub のデータは使わない。
Node.js と Rust の版はリポジトリの指定に従う。

通常の journey はセッションの開始と復帰、チケットの作成・レーン変更と再読込、
別クライアントの変更のイベント反映に絞る。レビュー、設定、ドラッグや担当者変更の
分岐は Vitest の component テストで検証する。E2E は `test` や push CI に含めず、
専用の `Branvil E2E` workflow（PR・手動・定期）で実行する。

既定のポートは 7889。変更するときは `BRANVIL_E2E_BASE_URL` で別の
`http://127.0.0.1:<port>` を指定する。サービスのデータとプロセスは終了時に削除する。

fixture による表示検証は通常 journey から分ける。別途ビルドした画面の preview を
起動し、その URL を `BRANVIL_E2E_BASE_URL` に指定して
`npm run e2e:visual --workspace branvil-web` を実行する。
性能調査も同様に `e2e:perf` で明示して実行する。

レビュー用の画像は一時資料として扱い、ソースへコミットしない。
Branvil の表示検証は Playwright の test-results に画像を出力する。
desktop の撮影は `GUILDBOTICS_SCREENSHOT_PHASE=before` または `after` を指定し、
`visual.spec.ts` を実行する。既定の保存先は
`~/Documents/GuildBotics/screenshots/desktop/`（必要なら
`GUILDBOTICS_SCREENSHOT_DIR` で上書きする）。
#118 では画像レビュー済みの確認結果を PR 本文に残す。
