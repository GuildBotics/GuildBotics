---
name: ask
brain: agent
template_engine: jinja2
description: 現在の作業ツリーでの単発作業をメンバーに依頼する。
inputs:
  message: required
---

これは委任された単発作業です（guildbotics_execution_mode=delegated）。入力として渡された依頼にはこの封筒を適用し、対話スキルの Definition of Done と workflow の完了契約は適用しません。

<instructions>
1. 最初に `guildbotics member context --person {{ context.person.person_id }}` を読みます。member 操作は、その capabilities を正とします。
2. 現在の作業ディレクトリで作業します。ここには依頼元の作業ツリーが、未コミットの変更も含めて入っています。clone、`member git prepare`、ブランチ変更は行いません。grant で書き込みが許されていない場所では、この作業ディレクトリは写しです。`.git` がある場合は読み取り専用なので、git で状態や差分は見られますが、commit はできません。通常ファイルへの変更は、作業が成功して終わったときに依頼元の作業ツリーへ書き戻されます。作成したシンボリックリンクは書き戻されません。
3. 依頼された作業だけを行います。レビューなら読み取りのみ、修正依頼なら依頼された編集と検証を行います。PR 作成と外部へのコメントは依頼に含まれる場合だけ行い、標準作業手順を理由に範囲を広げません。
4. commit と push は行いません。変更を公開するのは依頼元です。変更したファイルを結果に挙げます。
5. 結果、確認内容、進められない理由を標準出力（stdout）のテキストで返し、依頼元のメンバーが伝えられるようにします。workflow の `complete` / `noop` コマンドや、さらに別のメンバーへの委任は実行しません。
</instructions>
