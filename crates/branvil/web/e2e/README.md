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

#118 の変更前後の画像は [screenshots](../../../../docs/screenshots/shared-api-118) に保存。
desktop は隔離した実 backend、Branvil は表示検証の固定 fixture を使用する。
desktop の画像は `GUILDBOTICS_SCREENSHOT_PHASE=before` または `after` と
`GUILDBOTICS_SCREENSHOT_DIR=../docs/screenshots/shared-api-118/desktop` を指定し、
desktop の `visual.spec.ts` で再生成できる。
