---
name: evaluate_interaction_performance
response_class: guildbotics.intelligences.common.ArtifactProcessEvaluation
description: Evaluate agent performance on a single {subject_type} and report metrics and an overall score.
---

Your task is to evaluate the performance of an AI agent based on a single {subject_type}.
Use only the {summary_label}, {feedback_label}, and {outcome_label} sections to assess the agent’s behavior.
Evaluate using the following axes:
- review_comment_count: Number of feedback/review comments received.
- review_cycle_count: Number of update cycles (commits or iterations) until completion/close.
- request_changes_count: Number of comments explicitly requesting changes.
- outcome_score: 1.0 for a successful outcome ("{positive_outcome_value}"), 0.0 for a negative outcome ("{negative_outcome_value}").
- review_sentiment_score: Average sentiment polarity of feedback comments (–1.0 = very negative, +1.0 = very positive).

<instructions>
- Parse the “{summary_label}” section to understand the scope of the {subject_type}.
- Compute review_comment_count from the “{feedback_label}” section.
- Determine review_cycle_count: count of commits or update events on the {subject_type} until completion/close.
- Count request_changes_count: number of comments explicitly requesting changes (set to 0 if not applicable).
- If “{outcome_label}” is present, map its value to a numeric score: 1.0 for “{positive_outcome_value}”, 0.0 for “{negative_outcome_value}”. If this section is absent, infer a reasonable proxy from context or set 0.5 with justification.
- Analyze “{feedback_label}” text:
    1. Apply sentiment analysis to each comment body to obtain a polarity score between –1.0 and +1.0.
    2. Calculate review_sentiment_score as the average polarity across all comments.
- Normalize or weight each metric equally to derive a final `overall_score` between 0 and 1.
- Return a `ArtifactProcessEvaluation` with:
    - `overall_score`: Final aggregated performance score.
    - `reason`: Brief explanation of how the score was reached.
    - `context`: Key metric values and observations supporting the evaluation.
- When describing reason and context, use the project's default language: {language}.
</instructions>
