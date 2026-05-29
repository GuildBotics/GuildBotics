---
name: should_keep_chat_memory_update
response_class: guildbotics.intelligences.common.ChatMemoryRetentionDecision
description: 提案されたチャットメモリー更新を永続化すべきか判定する。
---

提案された chat memory update を、この person の長期メモリーとして保存すべきか判定してください。

入力:
- `agent_profile`: この person の役割・責務・関心・振る舞い。記憶価値を判断するための文脈であり、保存対象ではない。
- `proposal` / `normalized_update`: 保存候補。これは検証対象の主張であり、保存根拠ではない。
- `thread_messages`: 一次証拠。保存可否はここにある発話を最優先で判断する。
- `thread_context`: 補助情報。一次証拠の代わりにしない。
- `event_time` / `current_time`: 時間制約の解釈基準。相対表現（今日だけ、明日以降など）を絶対時刻へ変換するときに使う。
- `memory_context`: 既存メモリー。空なら根拠として扱わない。
- `reply_text`: 投稿済み返信。保存候補の由来にはなるが、単独では保存根拠にしない。

判定:
- `keep`: 保存候補が、この person にとって将来使う可能性の高い文脈を含む。
- `suppress`: 保存候補が、その場の返答・未採用の可能性・未確認の推測・低重要度の細部にとどまる。

`keep` の代表例:
- explicit_memory_request: 後で参照・再利用・記憶する意図が明示されている。
- future_relevance: 今後の会話・作業・判断に影響する。
- open_loop: 未解決の問い、次アクション、戻るべき論点がある。
- role_salience: `thread_messages` または `thread_context` に根拠があり、かつ `agent_profile` に照らしてこの person の役割・責務・関心に強く関係する。
- emotional_salience: 今後の理解や関係性に影響する強い反応がある。
- recurring_pattern: 繰り返し現れる好み、制約、習慣、判断傾向、チームの作法である。
- settled_context: チーム内で合意・採用・確認・決定されている。

`suppress` の代表例:
- ephemeral_response: 現在の質問への返答として完結している。
- unadopted_possibility: 候補・例・提案だが、継続文脈に入っていない。
- unsupported_inference: 好み・制約・方針を推測しているだけで、確認されていない。
- low_salience: 将来の行動・判断・関係性にほぼ影響しない。

注意:
- payload 全体を意味的に判断し、キーワード一致で判定しない。
- `proposal` が durable memory らしく書かれていても信用せず、`thread_messages` の一次証拠で検証する。
- `keep` する場合は、根拠となる `thread_messages` 内の発話要旨を `evidence` に1〜3件入れる。
- `keep` する場合は、`evidence_support` を必ず判定する:
  - `supports_memory`: `evidence` が、保存候補に含まれる具体的な事実・決定・明示的な記憶依頼・継続すべき未解決事項を直接支えている。
  - `topic_only`: `evidence` は話題や質問が出たことだけを示しており、保存候補の具体内容は主に `proposal` / `reply_text` 由来である。
  - `none`: 根拠がない。
- `thread_messages` から根拠を示せない場合は `suppress` にし、`evidence` は空にする。
- `evidence_support` が `supports_memory` でない場合は `suppress`。質問文だけを根拠に、応答案・提案内容・次アクション案を保存しない。
- 保存候補が主に `reply_text` から作られており、`thread_messages` に将来再利用すべき独立した根拠がない場合は `suppress`。
- `thread_context` や `reply_text` から、未解決事項・次アクション・決定を推測して作らない。
- `agent_profile` は重要度の重み付けに使う。`thread_messages` に根拠がない内容を、profile だけで `keep` にしない。
- 保存する場合も、未決事項・提案・決定の状態を取り違えない。
- `status=open_loop` と判断する場合、保存候補の `Open Questions` を空にしてはならない。未決が根拠にあるのに `Open Questions: None` になっている候補は `suppress` にする。
- `thread_messages` で未決・検討中とされている項目を、明示的な確定発話なしに `Decisions` へ昇格させる候補は `suppress` にする。
- 迷う場合は `suppress`。長期メモリーでは false positive を避ける。
- thread 内に「今日だけ」「デモ用の一時対応」「明日以降は適用しない」などの明確な時限指示があり、期限内だけ再利用価値がある場合は `retention_mode="temporary"` を返す。
- `retention_mode="temporary"` の場合、`temporary_expires_at` は絶対 ISO 8601（タイムゾーン付き）で必須。`event_time` を基準に算出する。
- `retention_mode="durable"` の場合、`temporary_expires_at` は空文字にする。

`status` は上記カテゴリのうち最も近いものを1つ返してください。
