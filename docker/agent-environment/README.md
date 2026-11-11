# ベースイメージの変更

`Dockerfile` を変更したら、リポジトリ直下の
`scripts/build-agent-environment-image.sh` で各 CPU 用の archive を作り、
ワークスペースへ digest を宣言します。別 CPU のデバイスには対応する archive を転送します。

マージの前に、人が Hub ホストで PR head から実行ファイルをビルドし、
Hub の CPU 用 archive を `guildbotics environment image load` で読み込んでから
運用ワークスペースの `guildbotics environment build` を実行します。
[Hub の事前ビルド手順](../../docs/hub-server.md#レシピ変更のマージ前にスナップショットを用意する)
に従い、稼働中の checkout は変更せず、snapshot が ready であることを確かめてからマージします。
