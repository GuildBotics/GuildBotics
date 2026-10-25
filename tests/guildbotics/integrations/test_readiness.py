"""Completion follows the host's requirements while retaining measured lag."""

import pytest

from guildbotics.integrations.readiness import Freshness, readiness_of
from guildbotics.runtime.resources import WorkTarget


@pytest.mark.parametrize("requires_up_to_date", [False, True])
@pytest.mark.parametrize("behind_by", [0, 2])
def test_lag_blocks_completion_only_when_the_host_requires_it(
    requires_up_to_date, behind_by
):
    result = readiness_of(
        target=WorkTarget(
            kind="pull_request",
            url="https://code.example/pr/1",
            title="Fix",
            label="PR-1",
        ),
        head_sha="head",
        current_head_sha="head",
        checks=[
            {
                "name": "ci",
                "status": "completed",
                "conclusion": "success",
                "details_url": "",
            }
        ],
        checks_expected=False,
        freshness=Freshness("base", behind_by, "base", requires_up_to_date),
        failed_logs=None,
    )

    blocked = requires_up_to_date and behind_by > 0
    assert result.behind_by == behind_by
    assert result.out_of_date is (behind_by > 0)
    assert result.readiness == ("blocked" if blocked else "ready")
    assert [b.code for b in result.completion_blockers] == (
        ["base_out_of_date"] if blocked else []
    )
