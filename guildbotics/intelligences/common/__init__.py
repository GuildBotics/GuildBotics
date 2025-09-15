from typing import ClassVar, Literal

from pydantic import BaseModel, Field

from guildbotics.entities import Task
from guildbotics.utils.i18n_tool import t


class DecisionResponse(BaseModel):
    """Generic response model for selection or classification tasks.

    Attributes:
        label (str): The selected or classified label.
        reason (str): Explanation for the decision.
        confidence (float): Confidence score between 0 and 1.
    """

    label: str = Field(..., description="The selected or classified label.")
    reason: str = Field(..., description="Explanation for the decision.")
    confidence: float = Field(..., description="Confidence score between 0 and 1.")


class Labels:
    """A class to represent a set of labels for classification or selection tasks.
    It can be initialized with a list of labels or a dictionary mapping labels to their descriptions.
    """

    def __init__(self, labels: list[str] | dict[str, str], indent: int = 2):
        """
        Initialize the Labels object.
        Args:
            labels (list[str] | dict[str, str]): A list of labels or a dictionary mapping labels to descriptions.
            indent (int): The number of spaces to indent each label in the string representation.
        """
        self.labels = labels
        self.indent = indent

    def __str__(self):
        if isinstance(self.labels, dict):
            return "\n" + "\n".join(
                f"{' ' * self.indent}- {label}: {description}"
                for label, description in self.labels.items()
            )
        else:
            return "\n" + "\n".join(
                f"{' ' * self.indent}- {label}" for label in self.labels
            )

    def keys(self) -> list[str]:
        """Return all labels (like dict.keys())."""
        if isinstance(self.labels, dict):
            return list(self.labels.keys())
        return list(self.labels)

    def items(self) -> list[tuple[str, str | None]]:
        """Return all label-description pairs (like dict.items())."""
        if isinstance(self.labels, dict):
            return list(self.labels.items())
        return [(label, label) for label in self.labels]

    # New dict-like methods
    def __len__(self) -> int:
        """Return the number of labels."""
        return len(self.keys())

    def __iter__(self):
        """Iterate over labels."""
        yield from self.keys()

    def __contains__(self, key: str) -> bool:
        """Check if a label exists."""
        return key in self.keys()

    def values(self) -> list[str | None]:
        """Return list of descriptions (None if labels is a list)."""
        if isinstance(self.labels, dict):
            return list(self.labels.values())
        return [None] * len(self.labels)

    def get(self, label: str, default=None) -> str | None:
        """Get description for a label, or default if not found."""
        if isinstance(self.labels, dict):
            return self.labels.get(label, default)
        return default

    def __getitem__(self, label: str) -> str | None:
        """Enable dict-style access to descriptions."""
        return self.get(label)


class DecisionResponseList(BaseModel):
    """A list of DecisionResponse objects.

    This model is used to represent multiple decision responses, typically for classification or selection tasks.
    """

    responses: list[DecisionResponse] = Field(
        ..., description="List of decision responses."
    )

    def __str__(self):
        return "\n".join(str(response) for response in self.responses)

    def to_labels(self, indent: int = 2) -> Labels:
        """Convert the list of DecisionResponse objects to a Labels object."""
        if not self.responses:
            return Labels([])

        labels = {response.label: response.reason for response in self.responses}
        return Labels(labels, indent=indent)

    def get_roles(self) -> list[str]:
        """Extract unique labels from the responses."""
        return list({response.label for response in self.responses})


class MissingInfoResponse(BaseModel):
    """Full result of 7W2H analysis including ‘analysis required?’ flag."""

    analysis_required: bool = Field(
        ...,
        description=(
            "Whether 7W2H analysis is needed for this message. "
            "False means the conversation is chit-chat / FYI only, "
            "or none of the 7W2H items are relevant."
        ),
    )
    reason: str | None = Field(
        None,
        description=(
            "If analysis_required is False, a short explanation "
            "(max 2 sentences) why the analysis is unnecessary."
        ),
    )
    missing: list[DecisionResponse] = Field(
        default_factory=list,
        description="Items absent or ambiguous (only if analysis_required=True).",
    )
    provided: list[DecisionResponse] = Field(
        default_factory=list,
        description="Items clearly present (only if analysis_required=True).",
    )
    irrelevant: list[DecisionResponse] = Field(
        default_factory=list,
        description="Items judged not relevant to the current context.",
    )


class IssueBranchLevel3(BaseModel):
    """
    A recursive node of the MECE issue tree.
    Attributes:
        label (str): Branch name (noun phrase).
        status (Literal["provided", "missing"]): Coverage judgement for this branch.
        reason (str): ≤ 2-sentence explanation for the judgement.
        confidence (float): Confidence score between 0 and 1.
    """

    label: str = Field(..., description="Branch name (noun phrase).")
    status: Literal["provided", "missing"] = Field(
        ..., description="Coverage judgement for this branch."
    )
    reason: str = Field(..., description="≤ 2-sentence explanation for the judgement.")
    confidence: float = Field(..., description="Confidence 0–1.")


class IssueBranchLevel2(BaseModel):
    """
    A recursive node of the MECE issue tree.
    Attributes:
        label (str): Branch name (noun phrase).
        status (Literal["provided", "missing"]): Coverage judgement for this branch.
        reason (str): ≤ 2-sentence explanation for the judgement.
        confidence (float): Confidence score between 0 and 1.
        sub (Optional[List[IssueBranchLevel3]]): Child branches, max depth 3.
    """

    label: str = Field(..., description="Branch name (noun phrase).")
    status: Literal["provided", "missing"] = Field(
        ..., description="Coverage judgement for this branch."
    )
    reason: str = Field(..., description="≤ 2-sentence explanation for the judgement.")
    confidence: float = Field(..., description="Confidence 0–1.")
    sub: list["IssueBranchLevel3"] | None = Field(
        None, description="Optional child branches."
    )


class IssueBranchLevel1(BaseModel):
    """
    A recursive node of the MECE issue tree.
    Attributes:
        label (str): Branch name (noun phrase).
        status (Literal["provided", "missing"]): Coverage judgement for this branch.
        reason (str): ≤ 2-sentence explanation for the judgement.
        confidence (float): Confidence score between 0 and 1.
        sub (Optional[List[IssueBranchLevel2]]): Child branches, max depth 3.
    """

    label: str = Field(..., description="Branch name (noun phrase).")
    status: Literal["provided", "missing"] = Field(
        ..., description="Coverage judgement for this branch."
    )
    reason: str = Field(..., description="≤ 2-sentence explanation for the judgement.")
    confidence: float = Field(..., description="Confidence 0–1.")
    sub: list[IssueBranchLevel2] | None = Field(None, description="child branches.")


class IssueTreeResponse(BaseModel):
    """Hierarchical coverage result returned in a single call.

    Attributes:
        branches (List[IssueBranch]): Top-level branches with recursive sub-branches. Each branch includes:
            - label (str): Issue-tree branch name.
            - status (str): Coverage judgement, either "provided" or "missing".
            - reason (str): Justification for the assigned status.
            - confidence (float): Confidence score between 0 and 1.
            - sub (Optional[List[IssueBranch]]): Child branches (max depth 3).
    """

    branches: list[IssueBranchLevel1] = Field(
        default_factory=list,
        description=(
            "Top-level branches with recursive sub-branches. "
            'Each branch includes: label (str), status ("provided" | "missing"), '
            "reason (str), confidence (float 0–1), and optional sub (list of child branches)."
        ),
    )

    def get_missing_branches(self) -> Labels:
        """Get a Labels object containing all branches and sub-branches that are missing."""
        if not self.branches:
            return Labels({})

        missing_labels = {}
        for branch in self.branches:
            if branch.status == "missing":
                missing_labels[branch.label] = branch.reason
            if branch.sub:
                for sub_branch in branch.sub:
                    if sub_branch.status == "missing":
                        sub_label = f"{branch.label} > {sub_branch.label}"
                        missing_labels[sub_label] = sub_branch.reason
                    if sub_branch.sub:
                        for sub_sub_branch in sub_branch.sub:
                            if sub_sub_branch.status == "missing":
                                sub_sub_label = f"{branch.label} > {sub_branch.label} > {sub_sub_branch.label}"
                                missing_labels[sub_sub_label] = sub_sub_branch.reason
        return Labels(missing_labels)

    def __str__(self):
        return "\n".join(
            f"{branch.label} ({branch.status}): {branch.reason} (confidence: {branch.confidence})"
            for branch in self.branches
        )


class NextTaskItem(BaseModel):
    """
    Represents a single next task identified by the identify/next_tasks intelligence.

    Attributes:
        title (str): The title or brief description of the task.
        description (str): A detailed description of the task.
        role (str): The role that should be involved in this task.
        priority (int): Priority level of the task. Smaller values indicate higher priority.
        inputs (list[str]): Information required to start this task.
        output (str): Single expected output or result of the task.
        mode (str): The work style for producing the output.
    """

    title: str = Field(..., description="Title or brief description of the task.")
    description: str = Field(..., description="A detailed description of the task.")
    role: str = Field(..., description="The role that should be involved in this task.")
    priority: int = Field(
        ...,
        description="Priority level of the task. Smaller values indicate higher priority.",
    )
    inputs: list[str] = Field(
        default_factory=list, description="Information required to start this task."
    )
    output: str = Field(
        description="Single expected output or result of the task.",
    )
    mode: str = Field(
        description=("The work style for producing the output."),
    )

    def to_task(self) -> Task:
        description = self.description
        if self.inputs:
            description += "\n\n**Inputs:**\n" + "\n".join(
                f"- {input_}" for input_ in self.inputs
            )
        if self.output:
            description += f"\n\n**Output:**\n- {self.output}"
        return Task(
            title=self.title,
            description=description,
            role=self.role,
            priority=self.priority,
            mode=self.mode,
        )


class NextTasksResponse(BaseModel):
    """
    Response model for the identify/next_tasks intelligence.

    Attributes:
        tasks (list[NextTaskItem]): A list of next tasks with their explanations.
    """

    tasks: list[NextTaskItem] = Field(..., description="List of identified next tasks.")

    def to_labels(self, indent: int = 2) -> Labels:
        """Convert the list of NextTaskItem objects to a Labels object."""
        if not self.tasks:
            return Labels([])

        labels = {task.title: task.description for task in self.tasks}
        return Labels(labels, indent=indent)


class FileInfoResponse(BaseModel):
    file_name: str = Field(
        ...,
        description=(
            "The base filename only, excluding any directory path. "
            "The filename must contain only valid alphanumeric characters and symbols allowed in filenames, "
            "and must be named in English."
        ),
    )
    file_type: str = Field(..., description="The mime type of the file.")
    text_content: str = Field(..., description="The text content of the file.")
    title: str = Field(
        ...,
        description=(
            "The title of the content (e.g., document title, article title, etc.). "
            "The title must be in the same language as text_content."
        ),
    )


class AgentResponse(BaseModel):
    """Response from the Agent after executing a requested task.

    Attributes:
        status (Literal["done", "asking"]): 'done' if the task is complete, 'asking' if more information is needed from the user.
        message (str): If status is 'done', a summary of the completed task. If 'asking', the question to the user.
        skip_ticket_comment (bool): Whether to skip posting a ticket comment by the caller.
    """

    DONE: ClassVar[Literal["done"]] = "done"
    ASKING: ClassVar[Literal["asking"]] = "asking"

    status: Literal["done", "asking"] = Field(
        ...,
        description=(
            "Status of the response. 'done' means the agent has completed its task, "
            "'asking' means it needs more information from the user to proceed."
        ),
    )
    message: str = Field(
        ...,
        description=(
            "If status is 'done', this contains a summary of the completed task. "
            "If status is 'asking', this contains the question for the user."
        ),
    )
    skip_ticket_comment: bool = Field(
        default=False,
        description=(
            "Whether to skip posting an ticket comment by the caller. "
            "When True, additional comments will not be posted, "
            "as comments have already been submitted to the code hosting service."
        ),
    )


class MessageResponse(BaseModel):
    """Response model for a message in the chat channel.

    Attributes:
        content (str): The content of the message.
        author (str): The author of the message.
        author_type (str): The type of the author (User or Assistant).
    """

    content: str = Field(..., description="The content of the message.")
    author: str = Field(..., description="The author of the message.")
    author_type: str = Field(
        ..., description="The type of the author (User or Assistant)."
    )


class ArtifactProcessEvaluation(BaseModel):
    review_comment_count: int = Field(
        ...,
        description="Number of feedback/review comments received.",
    )
    review_cycle_count: int = Field(
        ...,
        description="Number of update cycles (commits or iterations) until completion/close.",
    )
    request_changes_count: int = Field(
        ...,
        description="Number of comments explicitly requesting changes.",
    )
    outcome_score: float = Field(
        ...,
        description=(
            "Outcome score where 1.0 means positive outcome (merged/approved/resolved), "
            "0.0 means negative outcome (closed/rejected/unresolved)."
        ),
    )
    review_sentiment_score: float = Field(
        ...,
        description=(
            "Average sentiment polarity score of feedback comments, "
            "between -1.0 (very negative) and +1.0 (very positive)."
        ),
    )
    overall_score: float = Field(
        ...,
        description="Overall performance score of the artifact process, between 0 and 1.",
    )
    reason: str = Field(
        ..., description="Explanation for how the score was calculated."
    )
    context: str = Field(
        ...,
        description="Key metrics and observations supporting the evaluation.",
    )


class RootCauseItem(BaseModel):
    perspective: str = Field(
        ...,
        description="Perspective category for the analysis (e.g., agent behavior, user behavior, system design, etc.)",
    )
    problem: str = Field(
        ...,
        description="Description of the specific problem or symptom observed in the PR process",
    )
    root_cause: str = Field(
        ...,
        description="Underlying cause of the problem, based on evidence from the evaluation and comments",
    )
    severity: float = Field(
        ...,
        description=(
            "Severity score of the problem, between 0 (minor) and 1 (critical), "
            "indicating how significantly it impacts the PR process"
        ),
    )
    severity_reason: str = Field(
        ...,
        description=(
            "Explanation of why this problem is considered to have the given severity score"
        ),
    )

    def __lt__(self, other: "RootCauseItem"):
        """Compare two items based on their severity score."""
        return self.severity > other.severity


class RootCauseAnalysis(BaseModel):
    items: list[RootCauseItem] = Field(
        ...,
        description="Structured list of identified problems and their root causes, grouped by perspective",
    )

    def __str__(self):
        item_dict = {}
        items = sorted(self.items)
        for item in items:
            texts = item_dict.setdefault(item.perspective, [])
            texts.append(item)
        text = ""
        for perspective, problems in item_dict.items():
            text += f"### {perspective}\n"
            for item in problems:
                text += f"- **{item.problem}**\n"
                text += f"  - {item.root_cause}\n"
                text += (
                    f"  - Severity: **{item.severity}** ({item.severity_reason})**\n"
                )
        return text


class ImprovementSuggestion(BaseModel):
    perspective: str = Field(
        ...,
        description="Perspective category for the suggestion (e.g., agent prompts, ticket template, review process)",
    )
    proposal: str = Field(..., description="Concrete improvement proposal text")
    rationale: str = Field(
        ..., description="Explanation of how this proposal addresses the root cause"
    )
    implementation: str = Field(
        ...,
        description="Detailed steps, code snippets, or configuration to implement the proposal",
    )
    impact_score: float = Field(
        ...,
        description=(
            "Score indicating how effectively this proposal addresses the root cause, "
            "between 0 (low impact) and 1 (high impact)"
        ),
    )
    impact_reason: str = Field(
        ...,
        description=(
            "Explanation of why this proposal is expected to have the given impact score"
        ),
    )

    def __lt__(self, other: "ImprovementSuggestion"):
        """Compare two suggestions based on their impact score."""
        return self.impact_score > other.impact_score

    def to_task(self) -> Task:
        """Convert the suggestion to a Task object."""
        description = t(
            "intelligences.common.ImprovementSuggestion_description",
            perspective=self.perspective,
            rationale=self.rationale,
            implementation=self.implementation,
        )
        return Task(
            title=self.proposal,
            description=description,
            status=Task.RETROSPECTIVE,
        )


class ImprovementRecommendations(BaseModel):
    suggestions: list[ImprovementSuggestion] = Field(
        ...,
        description="List of structured improvement suggestions across various perspectives",
    )

    def __str__(self):
        return "\n".join(str(suggestion) for suggestion in self.suggestions)
