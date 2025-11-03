from guildbotics.runtime.context import Context
from guildbotics.utils.fileio import get_workspace_path
from guildbotics.utils.git_tool import GitTool


async def get_git_tool(context: Context) -> GitTool:
    workspace_path = get_workspace_path(context.person.person_id)
    code_hosting_service = context.get_code_hosting_service(context.task.repository)
    git_user = context.person.account_info.get("git_user", "Default User")
    git_email = context.person.account_info.get("git_email", "default@example.com")

    return GitTool(
        workspace_path,
        await code_hosting_service.get_repository_url(),
        context.logger,
        git_user,
        git_email,
        await code_hosting_service.get_default_branch(),
    )


def get_branch_name(context: Context) -> str:
    return f"ticket/{context.task.id}"


async def checkout(context: Context, branch_name: str | None = None) -> GitTool:
    if branch_name is None:
        branch_name = get_branch_name(context)
    git_tool = await get_git_tool(context)
    git_tool.checkout_branch(branch_name)
    return git_tool
