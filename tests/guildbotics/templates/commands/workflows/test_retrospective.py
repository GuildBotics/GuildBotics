import pytest

from guildbotics.intelligences.common import (
    ArtifactProcessEvaluation,
    ImprovementRecommendations,
    RootCauseAnalysis,
    RootCauseItem,
)
from guildbotics.templates.commands.workflows import retrospective


@pytest.mark.asyncio
async def test_evaluate_interaction_performance_and_root_cause(
    monkeypatch, fake_context, stub_brain
):
    ctx = fake_context

    # Stub translation helper to emit deterministic strings pulled from kwargs.
    monkeypatch.setattr(
        retrospective,
        "t",
        lambda key, **kw: kw.get("subject_type")
        or kw.get("reason")
        or kw.get("context")
        or key,
    )

    eval_model = ArtifactProcessEvaluation(
        review_comment_count=1,
        review_cycle_count=2,
        request_changes_count=3,
        outcome_score=1.0,
        review_sentiment_score=0.5,
        overall_score=0.8,
        reason="why",
        context="ctx",
    )

    async def fake_get_content_eval(context, name, message, **kwargs):
        return eval_model

    monkeypatch.setattr(retrospective, "get_content", fake_get_content_eval)
    summary = await retrospective.evaluate_interaction_performance(ctx, "text")
    # Our t stub returns either reason/context/subject_type/key; ensure reason leaked
    assert "why" in summary or "ctx" in summary

    # analyze_root_cause default path
    rca = RootCauseAnalysis(
        items=[
            RootCauseItem(
                perspective="Process",
                problem="p",
                root_cause="rc",
                severity=0.9,
                severity_reason="sr",
            )
        ]
    )

    async def fake_get_content_rca(context, name, message, **kwargs):
        return rca

    monkeypatch.setattr(retrospective, "get_content", fake_get_content_rca)
    assert await retrospective.analyze_root_cause(ctx, "txt", "ev") is rca


@pytest.mark.asyncio
async def test_propose_process_improvements(monkeypatch, fake_context, stub_brain):
    ctx = fake_context
    rec = ImprovementRecommendations(suggestions=[])

    async def fake_get_content(context, name, message, **kwargs):
        return rec

    monkeypatch.setattr(retrospective, "get_content", fake_get_content)
    assert (
        await retrospective.propose_process_improvements(
            ctx, RootCauseAnalysis(items=[])
        )
        is rec
    )
