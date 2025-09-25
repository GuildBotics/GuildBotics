from pathlib import Path

import httpx

from guildbotics.entities.message import Message
from guildbotics.entities.task import Task
from guildbotics.entities.team import Service
from guildbotics.integrations.code_hosting_service import PullRequest, ReviewComments
from guildbotics.intelligences.common import AgentResponse
from guildbotics.intelligences.functions import (
    analyze_root_cause,
    edit_files,
    evaluate_interaction_performance,
    identify_pr_comment_action,
    messages_to_simple_dicts,
    preprocess,
    propose_process_improvements,
    talk_as,
    write_commit_message,
    write_pull_request_description,
)
from guildbotics.modes.mode_base import ModeBase
from guildbotics.runtime.context import Context
from guildbotics.utils.i18n_tool import t


class EditMode(ModeBase):
    """
    Mode for handling Git operations, including creating and updating code or documents in a Git repository.
    This mode integrates with Git and code hosting services to manage pull requests and code changes.
    """

    def __init__(self, context: Context):
        """
        Initialize the EditMode.
        Args:
            workflow (Workflow): The workflow instance associated with this mode.
        """
        super().__init__(context)

    async def run(self, messages: list[Message]) -> AgentResponse:
        inputs = messages_to_simple_dicts(messages)

        # Retrospective handling is a self-contained flow; delegate and return early
        if self.context.task.status == Task.RETROSPECTIVE:
            return await self._handle_retrospective(messages, inputs)

        git_tool = await self.checkout()

        pull_request_url = self.find_pr_url_from_task_comments(self.context.task, False)
        is_reviewing = self.context.task.status == Task.IN_REVIEW and bool(
            pull_request_url
        )
        if is_reviewing:
            (
                comments,
                changed,
                is_asking,
                message,
                conversation_history,
            ) = await self._handle_review_flow(
                inputs, messages, pull_request_url, git_tool
            )
            if changed and message:
                context_location = t("modes.edit_mode.pull_request_context_location")
                comments.reply = await talk_as(
                    self.context, message, context_location, conversation_history
                )
        else:
            # Run the coding agent script to generate code changes
            response = await self.edit_files(self.context, inputs, git_tool.repo_path)

            # If the response is asking for more information, return it.
            if response.status == AgentResponse.ASKING:
                response.message = await talk_as(
                    self.context,
                    response.message,
                    t("modes.edit_mode.ticket_comment_context_location"),
                    messages,
                )
                return response

        # generate commit message based on changes
        diff = git_tool.get_diff()
        if diff:
            commit_message = await write_commit_message(
                self.context,
                task_title=self.context.task.title,
                changes=diff,
            )
        else:
            commit_message = f"{self.context.task.title}"

        # Add all changes to the staging area, commit them and push to the remote repository.
        commit_sha = git_tool.commit_changes(commit_message)

        if is_reviewing:
            # Respond to comments in the pull request
            if commit_sha:
                comments.reply = f"{comments.reply}\n{commit_sha}"
            await self.code_hosting_service.respond_to_comments(
                html_url=pull_request_url, comments=comments
            )
            skip_ticket_comment = (
                len(messages) > 0 and messages[-1].author_type == Message.ASSISTANT
            )
            return AgentResponse(
                status=AgentResponse.DONE,
                message=comments.reply or pull_request_url,
                skip_ticket_comment=skip_ticket_comment,
            )
        else:
            if commit_sha:
                # Write the pull request description
                ticket_url = await self.context.get_ticket_manager().get_ticket_url(
                    self.context.task
                )
                pr_template = self.read_pull_request_template()
                pr_description = await write_pull_request_description(
                    self.context, diff, commit_message, ticket_url, pr_template
                )

                # Create a pull request in the code hosting service.
                pull_request_url = await self.code_hosting_service.create_pull_request(
                    branch_name=self.branch_name,
                    title=self.context.task.title,
                    description=pr_description,
                    ticket_url=ticket_url,
                )

                return await self.get_done_response(
                    title=self.context.task.title,
                    url=pull_request_url,
                    messages=messages,
                    topic=response.message,
                )
            return AgentResponse(
                status=AgentResponse.DONE,
                message=response.message,
                skip_ticket_comment=False,
            )

    async def _handle_retrospective(
        self, messages: list[Message], inputs: list[dict]
    ) -> AgentResponse:
        pull_request_url = self.find_pr_url_from_task_comments(self.context.task, True)
        pr = await self.code_hosting_service.get_pull_request(pull_request_url)
        pr_text = self.pr_to_text(pr)
        evaluation = await evaluate_interaction_performance(self.context, pr_text)
        root_cause = await analyze_root_cause(self.context, pr_text, evaluation)
        proposal = await propose_process_improvements(self.context, root_cause)
        ticket_manager = self.context.get_ticket_manager()
        tasks = []
        suggestions = sorted(proposal.suggestions)
        if len(suggestions) > 5:
            suggestions = suggestions[:5]
        for suggestion in suggestions:
            tasks.append(suggestion.to_task())
        await ticket_manager.create_tickets(tasks)

        evaluation_and_root_cause = t(
            "modes.edit_mode.evaluation_and_root_cause",
            evaluation=evaluation,
            root_cause=str(root_cause),
        )
        evaluation_messages = [
            Message(
                content=evaluation_and_root_cause,
                author="Evaluation System",
                author_type=Message.USER,
                timestamp="",
            ),
        ]

        result = await talk_as(
            self.context,
            t("modes.edit_mode.evaluation_topic"),
            context_location=t("modes.edit_mode.evaluation_context_location"),
            conversation_history=evaluation_messages,
        )
        return AgentResponse(
            status=AgentResponse.ASKING,
            message=evaluation_and_root_cause + "\n\n---\n\n" + result,
        )

    async def _handle_review_flow(
        self,
        inputs: list[dict],
        messages: list[Message],
        pull_request_url: str,
        git_tool,
    ) -> tuple[ReviewComments, bool, bool, str, list[Message]]:
        """Process review-time edits and acknowledgements.

        Returns a tuple of (comments, changed, is_asking, overall_message, conversation_history).
        """
        context_location = t("modes.edit_mode.pull_request_context_location")
        comments = await self.code_hosting_service.get_pull_request_comments(
            pull_request_url
        )

        is_asking = False
        inputs.extend(comments.to_simple_dicts())

        conversation_history: list[Message] = []
        conversation_history.extend(messages)
        if comments.body:
            conversation_history.append(
                Message(
                    content=comments.body,
                    author="user",
                    author_type=Message.USER,
                    timestamp="",
                )
            )

        message = t("modes.edit_mode.default_message")
        changed = False

        if len(comments.inline_comment_threads) == 0:
            # If there are no inline comments, first decide if edits are needed.
            last_reviewer_comment = None
            for rc in reversed(comments.review_comments):
                if not rc.is_reviewee:
                    last_reviewer_comment = rc
                    break

            acknowledged = await self._acknowledge_comment(
                pull_request_url,
                (
                    getattr(last_reviewer_comment, "comment_id", None)
                    if last_reviewer_comment
                    else None
                ),
                last_reviewer_comment.body if last_reviewer_comment else None,
            )

            if acknowledged:
                # No edits needed; acknowledged by reaction. Avoid redundant reply.
                message = ""
            else:
                # Run the coding agent script to perform edits
                response = await self.edit_files(
                    self.context, inputs, git_tool.repo_path
                )
                if response.message:
                    message = response.message
                is_asking = response.status == AgentResponse.ASKING
                # Mark as changed only when not asking for more info
                if not is_asking:
                    changed = True
                if is_asking and not response.message:
                    message = t("modes.edit_mode.default_question")
        else:
            for thread in comments.inline_comment_threads:
                review_comment = inputs.copy()
                review_comment.append(thread.to_dict())
                for comment in thread.comments:
                    conversation_history.append(
                        Message(
                            content=comment.body,
                            author=comment.author,
                            author_type=(
                                Message.ASSISTANT
                                if comment.is_reviewee
                                else Message.USER
                            ),
                            timestamp="",
                        )
                    )
                # Check only the last comment in the thread
                last_comment = thread.comments[-1] if thread.comments else None

                # If the last comment is by the reviewee (ourselves), skip this thread
                if not last_comment or last_comment.is_reviewee:
                    continue

                # Decide action and, if ACK, react and skip editing
                if await self._acknowledge_comment(
                    pull_request_url,
                    getattr(last_comment, "comment_id", None),
                    last_comment.body,
                    is_inline=True,
                ):
                    continue

                response = await self.edit_files(
                    self.context, review_comment, git_tool.repo_path
                )
                if response.status == AgentResponse.ASKING:
                    is_asking = True
                else:
                    changed = True

                thread.add_reply(
                    await talk_as(
                        self.context,
                        response.message,
                        context_location,
                        conversation_history,
                    )
                )

        return comments, changed, is_asking, message, conversation_history

    def pr_to_text(self, pr: PullRequest) -> str:
        message = t(
            "modes.edit_mode.pull_request_text",
            title=pr.title,
            description=pr.description,
            review_comments=str(pr.review_comments),
        )
        for i, thread in enumerate(pr.review_comments.inline_comment_threads):
            message = message + t(
                "modes.edit_mode.pull_request_inline_comment_thread",
                thread_number=i + 1,
                thread_text=str(thread),
            )
        message = message + t(
            "modes.edit_mode.pull_request_merge_outcome",
            merge_outcome="merged" if pr.is_merged else "closed",
        )
        return message

    def read_pull_request_template(self) -> str:
        """Read the pull/merge request template from the repository.

        Tries several common template locations for GitHub and GitLab.
        If no template file is found, returns a generic default template.

        Returns:
            str: The content of the first template file found, or a default template.
        """
        # Relative paths to check for PR/MR templates
        template_paths = [
            ".github/pull_request_template.md",
            ".github/PULL_REQUEST_TEMPLATE.md",
            ".github/pull_request_template.txt",
            ".gitlab/merge_request_templates/Default.md",
        ]

        default_template_text = t("modes.edit_mode.default_pr_template")
        base = Path(self.workspace_path)
        for rel in template_paths:
            tpl = base / rel
            if tpl.is_file():
                try:
                    return tpl.read_text(encoding="utf-8")
                except Exception as e:
                    # Fallback to next if read fails
                    self.context.logger.warning(f"Could not read template {tpl}: {e}")

        return default_template_text

    def find_pr_url_from_task_comments(self, task: Task, strict: bool) -> str:
        """Find the pull request URL from task comments.

        Args:
            task (Task): The task containing comments.
            strict (bool): If True, applies stricter matching criteria when searching for the pull request URL;
                if False, uses a more lenient search.

        Returns:
            str: The pull request URL if found, otherwise an empty string.
        """
        return task.find_output_title_and_url_from_comments(strict)[1]

    @classmethod
    def get_dependent_services(cls) -> list[Service]:
        """
        Get the list of services that this mode depends on.
        Returns:
            list[Service]: A list of service instances that this mode depends on.
        """
        return [Service.CODE_HOSTING_SERVICE]

    @classmethod
    def get_use_case_description(cls) -> str:
        """
        Get the use case description of the mode.
        Returns:
            str: A brief description of the mode's use case.
        """
        return t("modes.edit_mode.use_case_description")

    async def _acknowledge_comment(
        self,
        pull_request_url: str,
        comment_id: int | None,
        comment_body: str | None,
        *,
        is_inline: bool = False,
    ) -> bool:
        """Decide action for a PR comment and acknowledge if appropriate.

        - Runs `identify_pr_comment_action` on `comment_body` when provided.
        - If the action is `ack`, adds a thumbs-up reaction to the target comment
          using the appropriate API (inline vs issue) and returns True.
        - Otherwise, returns False without side effects.

        Exceptions from reaction API are swallowed with a warning log.

        Returns:
            bool: True if acknowledged (reaction added or attempted), False otherwise.
        """
        action = "edit"
        if comment_body:
            action = await identify_pr_comment_action(self.context, comment_body)

        if action != "ack":
            return False

        if not comment_id:
            # No concrete comment to react to, treat as not acknowledged in effect
            return True

        try:
            await self.code_hosting_service.add_reaction_to_comment(
                pull_request_url, comment_id, "+1", is_inline=is_inline
            )
        except (ValueError, TypeError, httpx.HTTPError) as e:
            self.context.logger.warning(
                f"Failed to add reaction to comment {comment_id}: {e}"
            )
        return True

    async def edit_files(
        self, context: Context, input: list[dict], cwd: Path
    ) -> AgentResponse:
        """
        Edit files in the given directory based on the input instructions.

        Args:
            context (Context): The runtime context.
            input (list[dict]): The input instructions for editing files.
            cwd (Path): The current working directory where files are located.

        Returns:
            AgentResponse: The response from the agent after attempting to edit files.
        """
        for k, v in input[-1].items():
            v = preprocess(context, v)
            input[-1][k] = v
            break

        return await edit_files(context, input, cwd)
