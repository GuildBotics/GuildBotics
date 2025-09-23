import pathlib
import sys

# Add project root to sys.path for test execution
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[4]))

import logging
from unittest.mock import Mock

import pytest

from guildbotics.intelligences.brains.brain_stub import BrainStub
from guildbotics.intelligences.common import (
    AgentResponse,
    DecisionResponse,
    DecisionResponseList,
    FileInfoResponse,
    IssueTreeResponse,
    MessageResponse,
    MissingInfoResponse,
    NextTasksResponse,
)


@pytest.fixture
def mock_logger():
    return Mock(spec=logging.Logger)


@pytest.mark.asyncio
async def test_brain_stub_no_response_class(mock_logger):
    brain_stub = BrainStub(
        person_id="test_person", name="test_brain", logger=mock_logger
    )
    message = "Hello, world!"
    response = await brain_stub.run(message)
    assert response == f"Stubbed response for: {message}"
    mock_logger.info.assert_called_with(f"BrainStub received message: {message}")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response_cls, expected_message_field",
    [
        (DecisionResponse, "label"),
        (MissingInfoResponse, "analysis_required"),
        (FileInfoResponse, "file_name"),
        (AgentResponse, "status"),
        (MessageResponse, "content"),
    ],
)
async def test_brain_stub_with_simple_response_class(
    mock_logger, response_cls, expected_message_field
):
    brain_stub = BrainStub(
        person_id="test_person",
        name="test_brain",
        logger=mock_logger,
        response_class=response_cls,
    )
    message = "Test message"
    response = await brain_stub.run(message)

    assert isinstance(response, response_cls)
    if expected_message_field == "label":
        assert response.label == f"Stubbed response for: {message}"
    elif expected_message_field == "analysis_required":
        assert response.analysis_required is False
        assert response.reason == f"Stubbed response for: {message}"
    elif expected_message_field == "file_name":
        assert response.file_name == f"Stubbed response for: {message}"
        assert response.file_type == "text/plain"
        assert response.text_content == f"Stubbed response for: {message}"
        assert response.title == f"Stubbed response for: {message}"
    elif expected_message_field == "status":
        assert response.status == "done"
        assert response.message == f"Stubbed response for: {message}"
    elif expected_message_field == "content":
        assert response.content == f"Stubbed response for: {message}"
        assert response.author == "BrainStub"
        assert response.author_type == "Assistant"
    mock_logger.info.assert_called_with(f"BrainStub received message: {message}")


@pytest.mark.asyncio
async def test_brain_stub_with_decision_response_list(mock_logger):
    brain_stub = BrainStub(
        person_id="test_person",
        name="test_brain",
        logger=mock_logger,
        template_engine="default",
        response_class=DecisionResponseList,
    )
    message = "Test message for list"
    response = await brain_stub.run(message)

    assert isinstance(response, DecisionResponseList)
    assert len(response.responses) == 1
    assert response.responses[0].label == f"Stubbed response for: {message}"
    mock_logger.info.assert_called_with(f"BrainStub received message: {message}")


@pytest.mark.asyncio
async def test_brain_stub_with_issue_tree_response(mock_logger):
    brain_stub = BrainStub(
        person_id="test_person",
        name="test_brain",
        logger=mock_logger,
        template_engine="default",
        response_class=IssueTreeResponse,
    )
    message = "Test message for issue tree"
    response = await brain_stub.run(message)

    assert isinstance(response, IssueTreeResponse)
    assert len(response.branches) == 1
    assert response.branches[0].label == f"Stubbed response for: {message}"
    assert response.branches[0].status == "provided"
    mock_logger.info.assert_called_with(f"BrainStub received message: {message}")


@pytest.mark.asyncio
async def test_brain_stub_with_next_tasks_response(mock_logger):
    brain_stub = BrainStub(
        person_id="test_person",
        name="test_brain",
        logger=mock_logger,
        template_engine="default",
        response_class=NextTasksResponse,
    )
    message = "Test message for next tasks"
    response = await brain_stub.run(message)

    assert isinstance(response, NextTasksResponse)
    assert len(response.tasks) == 1
    assert response.tasks[0].title == f"Stubbed response for: {message}"
    assert response.tasks[0].description == f"Stubbed response for: {message}"
    mock_logger.info.assert_called_with(f"BrainStub received message: {message}")
