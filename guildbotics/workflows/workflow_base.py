from abc import ABC, abstractmethod

from guildbotics.runtime import Context


class WorkflowBase(ABC):
    """
    Abstract base class for workflows.
    """

    def __init__(self, context: Context):
        """
        Initialize the workflow with a context, person, and roles.
        Args:
            context (WorkflowContext): The context of the workflow.
            task (Task): The task associated with the workflow.
        """
        self.context = context

    @abstractmethod
    async def run(self):
        """
        Asynchronous method to run the workflow.
        This method should be implemented by subclasses to define the workflow's behavior.
        """
        pass
