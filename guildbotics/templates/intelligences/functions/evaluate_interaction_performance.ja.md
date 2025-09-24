---
name: evaluate_interaction_performance
response_class: guildbotics.intelligences.common.ArtifactProcessEvaluation
description: 単一の{subject_type}に基づき指標と総合スコアでエージェントのパフォーマンスを評価します。
---

あなたのタスクは、単一の{subject_type}に基づいて成果物作成プロセスのパフォーマンスを評価することです。
エージェントの行動を評価するために、{summary_label}、{feedback_label}、{outcome_label} の各セクションのみを使用してください。
以下の軸を使用して評価します。
- review_comment_count：受け取ったフィードバック/レビューコメントの数。
- review_cycle_count：完了/クローズまでの更新サイクル（コミットまたは反復）の数。
- request_changes_count：明示的に変更を要求するコメントの数。
- outcome_score：良い結果（「{positive_outcome_value}」）は1.0、悪い結果（「{negative_outcome_value}」）は0.0。
- review_sentiment_score：フィードバックコメントの平均感情極性（–1.0 = 非常に否定的、+1.0 = 非常に肯定的）。

<instructions>
- 「{summary_label}」セクションを解析して、{subject_type}の概要を把握します。
- 「{feedback_label}」セクションから review_comment_count を算出します。
- review_cycle_count を算出します。{subject_type} が完了/クローズに至るまでのコミットまたは更新イベントの回数です。
- request_changes_count を数えます。明示的に変更を要求するコメントの数です（該当しない場合は 0 ）。
- 「{outcome_label}」が存在する場合、その値を数値にマッピングします。「{positive_outcome_value}」は 1.0、「{negative_outcome_value}」は 0.0。
    このセクションが無い場合は、コンテキストから妥当な代替を推定するか、根拠を添えて 0.5 を設定します。
- 「{feedback_label}」テキストを分析します。
    1. 各コメント本文に感情分析を適用して、–1.0 から +1.0 の間の極性スコアを取得します。
    2. すべてのコメントにわたる平均極性として review_sentiment_score を計算します。
- 各メトリックを均等に正規化または重み付けして、0〜1 の最終的な `overall_score` を導出します。
- 以下を含む `ArtifactProcessEvaluation` を返します。
    - `overall_score`：最終的な集計パフォーマンススコア。
    - `reason`：スコアがどのように算出されたかの簡単な説明。
    - `context`：評価を裏付ける主要なメトリック値と観察結果。
- 理由とコンテキストの説明には、プロジェクトのデフォルト言語 {language} を使用してください。
</instructions>
