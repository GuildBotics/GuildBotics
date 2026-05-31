import asyncio
import os
import re
import traceback
from pathlib import Path
from typing import cast

import click
import i18n  # type: ignore
import questionary
from pydantic import BaseModel, Field

from guildbotics.cli.setup_tool import SetupTool
from guildbotics.cli.simple.setup_service import (
    PersonSetupInput,
    ProjectSetupInput,
    SetupServiceError,
    SimplePersonSetupService,
    SimpleProjectSetupService,
)
from guildbotics.cli.simple.simple_brain_factory import SimpleBrainFactory
from guildbotics.cli.simple.simple_integration_factory import SimpleIntegrationFactory
from guildbotics.cli.simple.simple_loader_factory import SimpleLoaderFactory
from guildbotics.entities.message import Message
from guildbotics.entities.task import Task
from guildbotics.entities.team import Person
from guildbotics.integrations.github.github_ticket_manager import GitHubTicketManager
from guildbotics.integrations.github.github_utils import GitHubAppAuth
from guildbotics.intelligences.functions import talk_as
from guildbotics.runtime import Context
from guildbotics.templates.commands.workflows.modes import comment_mode
from guildbotics.templates.commands.workflows.modes.util import checkout
from guildbotics.utils.fileio import CONFIG_PATH
from guildbotics.utils.i18n_tool import get_system_default_language, set_language, t

BASE_DIR = Path(__file__).parent


i18n.load_path.append(BASE_DIR / "locales")


class ProjectConfig(BaseModel):
    language: str = Field(default="")
    language_label: str = Field(default="")
    config_dir: Path = Field(default=Path())
    config_dir_label: str = Field(default="")
    env_file_option: str = Field(default="")
    env_file_option_label: str = Field(default="")
    llm_api_type: str = Field(default="")
    llm_api_type_label: str = Field(default="")
    google_api_key: str = Field(default="")
    openai_api_key: str = Field(default="")
    anthropic_api_key: str = Field(default="")
    cli_agent: str = Field(default="")
    cli_agent_label: str = Field(default="")
    github_project_url: str = Field(default="")
    github_repository_url: str = Field(default="")
    project_type: str = Field(default="")
    owner: str = Field(default="")
    project_id: str = Field(default="")
    repository_name: str = Field(default="")
    repo_access_label: str = Field(default="")
    repo_base_url: str = Field(default="https://github.com")


class PersonConfig(BaseModel):
    person_type: str = Field(default="")
    person_type_label: str = Field(default="")

    person_id: str = Field(default="")
    person_name: str = Field(default="")
    is_active: bool = Field(default=True)
    github_username: str = Field(default="")
    github_user_id: int = Field(default=0)
    git_email: str = Field(default="")

    roles: list[str] = Field(default_factory=list)
    roles_label: str = Field(default="")

    relationships: str = Field(default="")
    speaking_style: str = Field(default="")


class SimpleSetupTool(SetupTool):
    def get_context(self, message: str = "") -> Context:
        return Context.get_default(
            SimpleLoaderFactory(),
            SimpleIntegrationFactory(),
            SimpleBrainFactory(),
            message=message,
        )

    def init_project(self) -> None:
        """Initialize a new project."""
        config = ProjectConfig()

        default_language = get_system_default_language()
        set_language(default_language)

        # Step 1. Language Selection
        lang_map = {
            "English (en)": "en",
            "日本語 (ja)": "ja",
        }

        config.language_label = questionary.select(
            t("cli.language_prompt"), choices=list(lang_map.keys())
        ).ask()
        config.language = lang_map[config.language_label]
        set_language(config.language)

        # Step 2. Directory Selection
        special_config_dir = os.getenv("GUILDBOTICS_CONFIG_DIR")
        if special_config_dir:
            config.config_dir = Path(special_config_dir)
        else:
            home_dir = Path.home() / CONFIG_PATH
            current_dir = Path.cwd() / CONFIG_PATH

            dir_map = {
                t("cli.target_directory_home", path=str(home_dir)): home_dir,
                t("cli.target_directory_current", path=str(current_dir)): current_dir,
            }
            config.config_dir_label = questionary.select(
                t("cli.target_directory_prompt"), choices=list(dir_map.keys())
            ).ask()
            config.config_dir = dir_map[config.config_dir_label]
            if (
                config.config_dir.exists()
                and not questionary.confirm(
                    t("cli.directory_confirm"), default=False
                ).ask()
            ):
                return

        # Step 3. Environment File Creation
        env_file_option_map = {
            t("cli.env_file_option_skip"): "skip",
            t("cli.env_file_option_append"): "append",
            t("cli.env_file_option_overwrite"): "overwrite",
        }
        env_file_path = Path.cwd() / ".env"

        if (
            not env_file_path.exists()
            and questionary.confirm(t("cli.create_env_file_prompt"), default=True).ask()
        ):
            config.env_file_option_label = t("cli.env_file_option_create")
            config.env_file_option = "overwrite"
        else:
            config.env_file_option_label = questionary.select(
                t("cli.env_file_option_prompt"),
                choices=list(env_file_option_map.keys()),
            ).ask()
            config.env_file_option = env_file_option_map[config.env_file_option_label]

        # Step 4. LLM API Type Selection
        llm_api_type_map = {
            "OpenAI": "openai",
            "Google Gemini": "gemini",
            "Anthropic Claude": "anthropic",
        }

        config.llm_api_type_label = questionary.select(
            t("cli.llm_api_type_prompt"),
            choices=list(llm_api_type_map.keys()),
        ).ask()
        config.llm_api_type = llm_api_type_map[config.llm_api_type_label]

        if self.is_env_file_operation_required(config):
            if config.llm_api_type == "gemini":
                config.google_api_key = questionary.text(
                    t("cli.google_api_key_prompt")
                ).ask()
            if config.llm_api_type == "openai":
                config.openai_api_key = questionary.text(
                    t("cli.openai_api_key_prompt")
                ).ask()
            if config.llm_api_type == "anthropic":
                config.anthropic_api_key = questionary.text(
                    t("cli.anthropic_api_key_prompt")
                ).ask()

        # Step 5. CLI Agent Selection
        cli_agent_map = {
            "OpenAI Codex CLI": "codex",
            "Gemini CLI": "gemini",
            "Claude Code": "claude",
            "GitHub Copilot CLI": "copilot",
        }

        config.cli_agent_label = questionary.select(
            t("cli.cli_agent_type_prompt"),
            choices=list(cli_agent_map.keys()),
        ).ask()

        config.cli_agent = cli_agent_map[config.cli_agent_label]

        # Step 6. GitHub Project and Repository URL Input
        questionary.text(
            t("cli.github_project_url_prompt"),
            validate=lambda text: self.parse_github_project_url(text, config),
        ).ask()

        questionary.text(
            t("cli.github_repository_url_prompt"),
            validate=lambda text: self.parse_github_repository_url(text, config),
        ).ask()

        # Step 7. Repository Access Method (HTTPS / SSH)
        access_map = {
            t("cli.repo_access_https"): "https://github.com",
            t("cli.repo_access_ssh"): "ssh://git@github.com",
        }
        config.repo_access_label = questionary.select(
            t("cli.repo_access_prompt"), choices=list(access_map.keys())
        ).ask()
        config.repo_base_url = access_map[config.repo_access_label]

        # Step 8. Configuration Summary
        click.echo(f"\n{t('cli.config_content')} :")
        click.echo(f"  {t('cli.config_lang')} : {config.language_label}")
        click.echo(f"  {t('cli.config_target_directory')} : {config.config_dir_label}")
        click.echo(f"  {t('cli.config_env_file')} : {config.env_file_option_label}")
        click.echo(f"  {t('cli.config_llm_api')} : {config.llm_api_type_label}")
        if self.is_env_file_operation_required(config):
            if config.llm_api_type == "gemini":
                click.echo(
                    f"  {t('cli.config_google_api_key')} : {config.google_api_key}"
                )
            if config.llm_api_type == "openai":
                click.echo(
                    f"  {t('cli.config_openai_api_key')} : {config.openai_api_key}"
                )
            if config.llm_api_type == "anthropic":
                click.echo(
                    f"  {t('cli.config_anthropic_api_key')} : {config.anthropic_api_key}"
                )
        click.echo(f"  {t('cli.config_cli_agent')} : {config.cli_agent_label}")
        click.echo(
            f"  {t('cli.config_github_project_url')} : {config.github_project_url}"
        )
        click.echo(
            f"  {t('cli.config_github_repository_url')} : {config.github_repository_url}\n"
        )
        click.echo(f"  {t('cli.config_repo_access')} : {config.repo_access_label}")

        if not questionary.confirm(t("cli.config_confirm"), default=True).ask():
            click.echo(t("cli.config_cancel"))
            return

        setup_result = SimpleProjectSetupService().write_project(
            ProjectSetupInput(
                config_dir=config.config_dir,
                env_file_path=env_file_path,
                env_file_option=config.env_file_option,
                language=config.language,
                repository_name=config.repository_name,
                owner=config.owner,
                project_id=config.project_id,
                github_project_url=config.github_project_url,
                repo_base_url=config.repo_base_url,
                llm_api_type=config.llm_api_type,
                cli_agent=config.cli_agent,
                google_api_key=config.google_api_key,
                openai_api_key=config.openai_api_key,
                anthropic_api_key=config.anthropic_api_key,
            )
        )
        for created_file in setup_result.files:
            print(f"{created_file.action.capitalize()}: {created_file.path}")

    def get_config_dir(self) -> Path | None:
        special_config_dir = os.getenv("GUILDBOTICS_CONFIG_DIR")
        if special_config_dir:
            config_dir = Path(special_config_dir)
        else:
            home_dir = Path.home() / CONFIG_PATH
            current_dir = Path.cwd() / CONFIG_PATH
            config_dir = current_dir if current_dir.exists() else home_dir

        if not config_dir.exists():
            questionary.print(t("cli.error_config_dir_not_found", path=config_dir))
            return None

        project_config_file = self.get_project_config_file(config_dir)
        if not project_config_file.exists():
            questionary.print(
                t("cli.error_project_yml_not_found", path=project_config_file)
            )
            return None

        return config_dir

    def get_project_config_file(self, config_dir):
        return config_dir / "team" / "project.yml"

    def add_member(self) -> None:
        config_dir = self.get_config_dir()
        if not config_dir:
            return

        self.get_context()
        config = PersonConfig()

        # Step 1. Select Person Type
        person_type_map = {
            t("cli.person_type_human"): GitHubAppAuth.HUMAN,
            t("cli.person_type_machine_user"): GitHubAppAuth.MACHINE_USER,
            t("cli.person_type_github_apps"): GitHubAppAuth.GITHUB_APPS,
            t("cli.person_type_proxy_agent"): GitHubAppAuth.PROXY_AGENT,
        }

        config.person_type_label = questionary.select(
            t("cli.person_type_prompt"), choices=list(person_type_map.keys())
        ).ask()
        config.person_type = person_type_map[config.person_type_label]

        config.is_active = config.person_type in [
            GitHubAppAuth.GITHUB_APPS,
            GitHubAppAuth.MACHINE_USER,
            GitHubAppAuth.PROXY_AGENT,
        ]

        # Step 2. Select GitHub Username
        if config.person_type in [
            GitHubAppAuth.HUMAN,
            GitHubAppAuth.MACHINE_USER,
            GitHubAppAuth.PROXY_AGENT,
        ]:
            while True:
                user_name = questionary.text(t("cli.github_username_prompt")).ask()
                result = self.parse_github_username(user_name, config)
                if type(result) is str:
                    questionary.print(result)
                elif result is True:
                    break
        else:
            while True:
                questionary.text(
                    t("cli.github_apps_url_prompt"),
                    validate=lambda text: self.parse_github_apps_url(text, config),
                ).ask()
                result = self.parse_github_username(
                    config.github_username, config, is_github_apps=True
                )
                if type(result) is str:
                    questionary.print(result)
                elif result is True:
                    break

        # Step 3. Input Person ID
        if config.person_type == GitHubAppAuth.PROXY_AGENT:
            config.person_id = ""

        config.person_id = questionary.text(
            t("cli.input_person_id_prompt"),
            default=config.person_id,
            validate=lambda text: (
                t("cli.input_person_id_prompt")
                if len(text) == 0 or not re.match(r"^[a-z0-9_-]+$", text)
                else True
            ),
        ).ask()

        # Step 4. Input Person Name
        config.person_name = questionary.text(
            t("cli.person_name_prompt"),
            validate=lambda text: len(text) > 0 or t("cli.person_name_prompt"),
        ).ask()

        # Step 5. Select Person Roles
        role_map = {
            t("cli.product_owner"): "product_owner",
            t("cli.project_manager"): "project_manager",
            t("cli.architect"): "architect",
            t("cli.infrastructure_engineer"): "infrastructure_engineer",
            t("cli.designer"): "designer",
            t("cli.content_creator"): "content_creator",
            t("cli.data_analyst"): "data_analyst",
            t("cli.sales_marketing"): "sales_marketing",
            t("cli.customer_support"): "customer_support",
        }
        config.roles_label = questionary.checkbox(
            t("cli.roles_prompt"),
            choices=list(role_map.keys()),
        ).ask()
        config.roles = [role_map[label] for label in config.roles_label]

        # Step 6. Select Speaking Style
        if config.person_type in [
            GitHubAppAuth.GITHUB_APPS,
            GitHubAppAuth.MACHINE_USER,
            GitHubAppAuth.PROXY_AGENT,
        ]:
            speaking_style_map = {
                t("cli.speaking_style_friendly"): "friendly",
                t("cli.speaking_style_professional"): "professional",
                t("cli.speaking_style_machine"): "machine",
            }
            speaking_style_label = questionary.select(
                t("cli.speaking_style_prompt"), choices=list(speaking_style_map.keys())
            ).ask()
            speaking_style = speaking_style_map[speaking_style_label]
            config.speaking_style = t(
                f"cli.speaking_style_{speaking_style}_description"
            )

        # Step 7. Set Environment Variables
        github_installation_id = None
        github_app_id = None
        github_private_key_path = None
        github_access_token = ""
        if config.person_type == GitHubAppAuth.GITHUB_APPS:
            github_installation_id = int(
                questionary.text(
                    t("cli.input_github_installation_id_prompt"),
                    validate=lambda text: (
                        text.isdigit() or t("cli.input_github_installation_id_prompt")
                    ),
                ).ask()
            )

            github_app_id = int(
                questionary.text(
                    t("cli.input_github_app_id_prompt"),
                    validate=lambda text: (
                        text.isdigit() or t("cli.input_github_app_id_prompt")
                    ),
                ).ask()
            )

            github_private_key_path = Path(
                questionary.text(
                    t("cli.input_github_private_key_path_prompt"),
                    validate=lambda text: (
                        (len(text) > 0 and Path(text).exists())
                        or t("cli.input_github_private_key_path_prompt")
                    ),
                ).ask()
            )

        if config.person_type in [
            GitHubAppAuth.MACHINE_USER,
            GitHubAppAuth.PROXY_AGENT,
        ]:
            github_access_token = questionary.text(
                t("cli.input_github_access_token_prompt"),
                validate=lambda text: (
                    len(text) > 0 or t("cli.input_github_access_token_prompt")
                ),
            ).ask()

        # Step 8. Create Person Configuration File
        add_secret = False
        env_file_path = Path.cwd() / ".env"
        if env_file_path.exists():
            add_secret = questionary.confirm(
                t("cli.secret_info_prompt"), default=True
            ).ask()

        person_input = PersonSetupInput(
            config_dir=config_dir,
            env_file_path=env_file_path,
            append_env_file=add_secret,
            person_type=config.person_type,
            person_id=config.person_id,
            person_name=config.person_name,
            is_active=config.is_active,
            github_username=config.github_username,
            git_email=config.git_email,
            roles=config.roles,
            speaking_style=config.speaking_style,
            github_installation_id=github_installation_id,
            github_app_id=github_app_id,
            github_private_key_path=github_private_key_path,
            github_access_token=github_access_token,
        )
        person_service = SimplePersonSetupService()
        env_vars = person_service.build_environment_variables(person_input)
        setup_result = person_service.write_person(person_input)
        for created_file in setup_result.files:
            print(f"{created_file.action.capitalize()}: {created_file.path}")

        # Step 9. Show Environment Variables
        if not add_secret and env_vars:
            print()
            print(t("cli.environment_variable_prompt"))
            for env_var in env_vars:
                print(f"  {env_var}")

    def verify_environment(self) -> None:
        """Verify the project environment."""

        # Step 1. Check config directory
        config_dir = self.get_config_dir()
        if not config_dir:
            return

        context = self.get_context()

        # Step 2. Check for active members
        has_active_member = False
        for member in context.team.members:
            if member.is_active:
                has_active_member = True
                break

        if not has_active_member:
            print(t("cli.error_no_active_member"))
            return

        # Step 3. Verify each active member's configuration
        success = asyncio.run(self.verify_members(context))
        if not success:
            return

        # Step 4. Ensure custom fields are set up
        if questionary.confirm(t("cli.confirm_add_custom_fields"), default=True).ask():
            self.ensure_custom_fields(context)
        else:
            print(t("cli.message_add_custom_fields"))

        # Step 5. Map statuses
        self.map_statuses(context, config_dir)

    async def verify_members(self, context: Context) -> bool:
        has_error = False
        for member in context.team.members:
            if member.is_active:
                try:
                    c = context.clone_for(member)
                    # Check GitHub Settings
                    ticket_manager = cast(GitHubTicketManager, c.get_ticket_manager())
                    await ticket_manager.get_statuses()
                    c.task = Task(
                        id="ticket/introduce_yourself",
                        title="Introduce Yourself",
                        description="Please introduce yourself.",
                    )
                    messages = [
                        Message(
                            content=t("cli.introduction_prompt"),
                            author="User",
                            author_type=Message.USER,
                            timestamp="",
                        )
                    ]

                    # Check LLM API Settings
                    name = await talk_as(
                        c, f"My name is {c.person.name}", "chat room", messages
                    )

                    # Check Git and CLI Agent Settings
                    git_tool = await checkout(c)
                    res = await comment_mode.main(c, messages, git_tool)

                    print()
                    print("----- API Agent Output -----")
                    print(name)
                    print()
                    print("----- CLI Agent Output -----")
                    print(res.message)
                    print()

                except Exception as e:
                    has_error = True
                    member.is_active = False
                    traceback.print_exc()
                    print()
                    print(
                        t(
                            "cli.error_insufficient_config",
                            person=member.person_id,
                            details=str(e),
                        )
                    )
        return not has_error

    def get_active_member(self, context: Context) -> Person | None:
        for member in context.team.members:
            if member.is_active:
                return member
        return None

    def get_ticket_manager(self, context: Context) -> GitHubTicketManager | None:
        member = self.get_active_member(context)
        if not member:
            return None

        c = context.clone_for(member)
        return cast(GitHubTicketManager, c.get_ticket_manager())

    def ensure_custom_fields(self, context: Context):
        ticket_manager = self.get_ticket_manager(context)
        if ticket_manager is None:
            return

        asyncio.run(ticket_manager.ensure_custom_fields())

    def map_statuses(self, context: Context, config_dir: Path):
        ticket_manager = self.get_ticket_manager(context)
        if ticket_manager is None:
            return

        default_status_map = ticket_manager.default_status_map
        default_statuses = set(default_status_map.values())
        current_status = set(ticket_manager.status_map.keys())
        github_statuses = asyncio.run(ticket_manager.get_statuses())

        needs_status_setup = (
            set(github_statuses) != current_status
            and default_statuses == current_status
        )

        if not needs_status_setup:
            return

        standard_statuses = list(default_status_map.values())
        status_map = self.interactive_map(standard_statuses, github_statuses)
        project_config_file = self.get_project_config_file(config_dir)
        project_config = project_config_file.read_text()
        for key, value in status_map.items():
            project_config = project_config.replace(key, value)

        print(f"Update: {project_config_file}")
        project_config_file.write_text(project_config)
        print(
            t(
                "cli.update_project_config_file_message",
                path=project_config_file,
            )
        )

    def interactive_map(
        self, standard: list[str], existing: list[str]
    ) -> dict[str, str]:
        sentinel_none = t("cli.sentinel_none")
        mapping: dict[str, str] = {}
        choices = [sentinel_none, *existing]

        for std in standard:
            answer = questionary.select(
                message=t("cli.select_status_prompt", status=std),
                choices=choices,
                default=sentinel_none,
            ).ask()
            if answer is None:
                raise KeyboardInterrupt
            if answer != sentinel_none:
                mapping[std] = answer
                choices.remove(answer)
        return mapping

    def is_env_file_operation_required(self, config: ProjectConfig) -> bool:
        return config.env_file_option != "skip"

    def parse_github_apps_url(self, url: str, config: PersonConfig) -> str | bool:
        try:
            config.github_username = SimplePersonSetupService().parse_github_apps_url(
                url
            )
        except SetupServiceError:
            return t("cli.error_invalid_github_apps_url")
        return True

    def parse_github_username(
        self, name: str, config: PersonConfig, is_github_apps: bool = False
    ) -> str | bool:
        try:
            reference = SimplePersonSetupService().resolve_github_user(
                name, is_github_apps=is_github_apps
            )
        except SetupServiceError:
            return t("cli.error_invalid_github_username")

        config.person_id = reference.person_id
        config.github_username = reference.github_username
        config.github_user_id = reference.github_user_id
        config.git_email = reference.git_email
        return True

    def parse_github_project_url(self, url: str, config: ProjectConfig) -> str | bool:
        try:
            reference = SimpleProjectSetupService().parse_github_project_url(url)
        except SetupServiceError:
            return t("cli.error_invalid_github_project_url")

        config.project_type = reference.project_type
        config.owner = reference.owner
        config.project_id = reference.project_id
        config.github_project_url = reference.url
        return True

    def parse_github_repository_url(
        self, url: str, config: ProjectConfig
    ) -> str | bool:
        try:
            reference = SimpleProjectSetupService().parse_github_repository_url(
                url, owner=config.owner
            )
        except SetupServiceError as exc:
            if exc.code == "inconsistent_github_url":
                return t("cli.error_inconsistent_github_url")
            return t("cli.error_invalid_github_repository_url")

        config.repository_name = reference.repository_name
        config.github_repository_url = reference.url
        return True

    def get_default_routines(self) -> list[str]:
        return ["workflows/ticket_driven_workflow"]
