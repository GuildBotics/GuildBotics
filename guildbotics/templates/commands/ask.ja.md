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
2. 現在の作業ディレクトリを、未コミットの変更も含めてそのまま使います。clone、`member git prepare`、ブランチ変更は行いません。
3. 依頼された作業だけを行います。レビューなら読み取りのみ、修正依頼なら依頼された編集と検証を行います。コミット、push、PR 作成、外部へのコメントは依頼に含まれる場合だけ行い、標準作業手順を理由に範囲を広げません。
4. git への公開を依頼された場合は、member git コマンドに `--workspace-mode current` を指定します。
5. 結果、確認内容、進められない理由を標準出力（stdout）のテキストで返し、依頼元のメンバーが伝えられるようにします。workflow の `complete` / `noop` コマンドや、さらに別のメンバーへの委任は実行しません。
</instructions>
