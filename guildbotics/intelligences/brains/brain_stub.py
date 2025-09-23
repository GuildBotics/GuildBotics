from logging import Logger
from typing import Type

from pydantic import BaseModel

from guildbotics.intelligences.brains.brain import Brain
from guildbotics.intelligences.common import (
    DecisionResponse,
    IssueBranchLevel1,
    NextTaskItem,
)


class BrainStub(Brain):

    def __init__(
        self,
        person_id: str,
        name: str,
        logger: Logger,
        description: str = "",
        template_engine: str = "default",
        response_class: Type[BaseModel] | None = None,
    ):
        super().__init__(
            person_id, name, logger, description, template_engine, response_class
        )
        self.logger.info(f"BrainStub initialized: {self.name}")

    async def run(self, message: str, **kwargs):
        self.logger.info(f"BrainStub received message: {message}")
        # For E2E testing, we can simply return the message or a predefined response
        if self.response_class:
            if self.response_class.__name__ == "DecisionResponse":
                return self.response_class(
                    label=f"Stubbed response for: {message}",
                    reason="Stubbed reason",
                    confidence=1.0,
                )
            elif self.response_class.__name__ == "DecisionResponseList":
                return self.response_class(
                    responses=[
                        DecisionResponse(
                            label=f"Stubbed response for: {message}",
                            reason="Stubbed reason",
                            confidence=1.0,
                        )
                    ]
                )
            elif self.response_class.__name__ == "MissingInfoResponse":
                return self.response_class(
                    analysis_required=False, reason=f"Stubbed response for: {message}"
                )
            elif self.response_class.__name__ == "IssueTreeResponse":
                return self.response_class(
                    branches=[
                        IssueBranchLevel1(
                            label=f"Stubbed response for: {message}",
                            status="provided",
                            reason="Stubbed reason",
                            confidence=1.0,
                            sub=None,
                        )
                    ]
                )
            elif self.response_class.__name__ == "NextTasksResponse":
                return self.response_class(
                    tasks=[
                        NextTaskItem(
                            title=f"Stubbed response for: {message}",
                            description=f"Stubbed response for: {message}",
                            role="default",
                            priority=1,
                            output="default",
                            mode="default",
                        )
                    ]
                )
            elif self.response_class.__name__ == "FileInfoResponse":
                return self.response_class(
                    file_name=f"Stubbed response for: {message}",
                    file_type="text/plain",
                    text_content=f"Stubbed response for: {message}",
                    title=f"Stubbed response for: {message}",
                )
            elif self.response_class.__name__ == "AgentResponse":
                return self.response_class(
                    status="done", message=f"Stubbed response for: {message}"
                )
            elif self.response_class.__name__ == "MessageResponse":
                return self.response_class(
                    content=f"Stubbed response for: {message}",
                    author="BrainStub",
                    author_type="Assistant",
                )
        return f"Stubbed response for: {message}"
