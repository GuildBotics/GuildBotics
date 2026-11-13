# ベースイメージの変更

`Dockerfile` を変更したら、リポジトリ直下の
`scripts/build-agent-environment-image.sh` で各 CPU 用の archive を作り、
ワークスペースへ digest を宣言します。別 CPU のデバイスには対応する archive を転送します。

マージの前に、人が Hub ホストで PR head から実行ファイルをビルドし、
Hub の CPU 用 archive を `guildbotics environment image load` で読み込んでから
運用ワークスペースの `guildbotics environment build` を実行します。
[Hub の事前ビルド手順](../../docs/hub-server.md#レシピ変更のマージ前にスナップショットを用意する)
に従い、稼働中の checkout は変更せず、snapshot が ready であることを確かめてからマージします。

## Windows クライアントのクロスビルド

Linux 上で Windows クライアントをビルドするため、Rust の target
`x86_64-pc-windows-gnu` と、mingw-w64 の C コンパイラー・リンカーを導入します。
`CARGO_TARGET_X86_64_PC_WINDOWS_GNU_LINKER` に `x86_64-w64-mingw32-gcc` を設定し、
`cargo build --target x86_64-pc-windows-gnu` のリンクに使います。
Dockerfile の `RUN` で target の導入とコンパイラー・リンカーの起動を確認します。

イメージの実ビルドと Hub への反映は #211 で行います。
この変更では CI ジョブを追加しません。Windows 向け clippy は #214、
同じ commit のクライアントを Hub が配布するための成果物のジョブは #215 で扱います。

## cargo の実行前処理

イメージの `cargo` は、作業ディレクトリから親へたどり、最初に見つけた
`.cargo/before-cargo.sh` に同じ引数を渡してから `$REAL_CARGO` を実行します。
スクリプトが無い場合や失敗した場合も cargo は実行します。

このリポジトリのスクリプトは `rustc -vV` とルート manifest の profile が変わると
全成果物を消し、ブランチ名または `Cargo.lock` の hash が変わると
`cargo clean --workspace` と残った自前 target の片付けを行います。
第三者依存は後者では残します。印は target 内に置き、旧形式の印は使いません。
`--manifest-path`、`--config`、`--target-dir` は片付けにも引き継ぎます。
フックの探索は cwd を基準とするため、リポジトリ外から `--manifest-path` を
渡しても、探索経路にフックが無ければ片付けません。

この変更のイメージ作り直しと Hub への反映は #209 で行います。
