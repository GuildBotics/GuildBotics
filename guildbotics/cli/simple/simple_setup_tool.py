import asyncio
import os
import re
import traceback
from pathlib import Path
from typing import cast
from urllib.parse import quote

import click
import i18n  # type: ignore
import questionary
import requests  # type: ignore
from pydantic import BaseModel, Field

from guildbotics.cli.setup_tool import SetupTool
from guildbotics.cli.simple.simple_brain_factory import SimpleBrainFactory
from guildbotics.cli.simple.simple_integration_factory import SimpleIntegrationFactory
from guildbotics.cli.simple.simple_loader_factory import SimpleLoaderFactory
from guildbotics.entities.message import Message
from guildbotics.entities.task import Task
from guildbotics.entities.team import Person
from guildbotics.integrations.github.github_ticket_manager import GitHubTicketManager
from guildbotics.integrations.github.github_utils import GitHubAppAuth
from guildbotics.intelligences.functions import talk_as
from guildbotics.modes.comment_mode import CommentMode
from guildbotics.runtime import Context
from guildbotics.utils.fileio import (
    CONFIG_PATH,
    get_template_path,
    load_yaml_file,
    save_yaml_file,
)
from guildbotics.utils.i18n_tool import get_system_default_language, set_language, t

BASE_DIR = Path(__file__).parent
GITHUB_URL = "https://github.com/"
TEMPLATE_PATH = BASE_DIR / "templates"


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
            if config.config_dir.exists():
                if not questionary.confirm(
                    t("cli.directory_confirm"), default=False
                ).ask():
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

        # Step 5. CLI Agent Selection
        cli_agent_map = {
            "OpenAI Codex CLI": "codex",
            "Gemini CLI": "gemini",
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

        # Step 9. Configuration File Creation
        project_config_template = (TEMPLATE_PATH / "project.yml").read_text()
        project_config = project_config_template.format(
            language=config.language,
            repository_name=config.repository_name,
            owner=config.owner,
            project_id=config.project_id,
            project_url=config.github_project_url,
            repo_base_url=config.repo_base_url,
        )

        project_config_file = config.config_dir / "team/project.yml"
        print(f"Create: {project_config_file}")
        project_config_file.parent.mkdir(parents=True, exist_ok=True)
        project_config_file.write_text(project_config)

        model_mapping_template = get_template_path() / "intelligences/model_mapping.yml"
        model_mapping: dict = cast(dict, load_yaml_file(model_mapping_template))
        model_mapping["default"] = model_mapping[config.llm_api_type]
        model_mapping_file = config.config_dir / "intelligences/model_mapping.yml"
        print(f"Create: {model_mapping_file}")
        model_mapping_file.parent.mkdir(parents=True, exist_ok=True)
        save_yaml_file(model_mapping_file, model_mapping)

        cli_mapping_template = (
            get_template_path() / "intelligences/cli_agent_mapping.yml"
        )
        cli_mapping: dict = cast(dict, load_yaml_file(cli_mapping_template))
        cli_mapping["default"] = cli_mapping[config.cli_agent]
        cli_mapping_file = config.config_dir / "intelligences/cli_agent_mapping.yml"
        print(f"Create: {cli_mapping_file}")
        save_yaml_file(cli_mapping_file, cli_mapping)

        cli_agent_config_src_dir = get_template_path() / "intelligences/cli_agents"
        cli_agent_config_dst_dir = config.config_dir / "intelligences/cli_agents"
        cli_agent_config_dst_dir.mkdir(parents=True, exist_ok=True)
        for src_file in cli_agent_config_src_dir.glob("*.yml"):
            dst_file = cli_agent_config_dst_dir / src_file.name
            print(f"Create: {dst_file}")
            dst_file.write_text(src_file.read_text())

        if self.is_env_file_operation_required(config):
            env_file_template = (TEMPLATE_PATH / ".env.example").read_text()
            env_file = env_file_template.format(
                google_api_key=f"GOOGLE_API_KEY={config.google_api_key}",
                openai_api_key=f"OPENAI_API_KEY={config.openai_api_key}",
            )

            if config.env_file_option == "overwrite":
                print(f"Create: {env_file_path}")
                env_file_path.write_text(env_file)
            elif config.env_file_option == "append":
                print(f"Append: {env_file_path}")
                env_file_path.write_text(f"{env_file_path.read_text()}\n\n{env_file}")

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
        config.roles = [f"{role_map[label]}:" for label in config.roles_label]

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

        # Step 7. Create Person Configuration
        person_config_template = (TEMPLATE_PATH / "person.yml").read_text()
        person_config = person_config_template.format(
            github_username=config.github_username,
            person_id=config.person_id,
            person_name=config.person_name,
            person_type=config.person_type,
            is_active=str(config.is_active).lower(),
            git_email=config.git_email,
            roles=f"  {'\n  '.join(config.roles)}",
            speaking_style=config.speaking_style,
            relationships="",
        )

        # Step 8. Set Environment Variables
        sanitized_id = config.person_id.replace("-", "_").upper()
        env_vars = []
        if config.person_type == GitHubAppAuth.GITHUB_APPS:
            installation_id = int(
                questionary.text(
                    t("cli.input_github_installation_id_prompt"),
                    validate=lambda text: text.isdigit()
                    or t("cli.input_github_installation_id_prompt"),
                ).ask()
            )
            env_vars.append(f"{sanitized_id}_GITHUB_INSTALLATION_ID={installation_id}")

            app_id = int(
                questionary.text(
                    t("cli.input_github_app_id_prompt"),
                    validate=lambda text: text.isdigit()
                    or t("cli.input_github_app_id_prompt"),
                ).ask()
            )
            env_vars.append(f"{sanitized_id}_GITHUB_APP_ID={app_id}")

            private_key_path = questionary.text(
                t("cli.input_github_private_key_path_prompt"),
                validate=lambda text: len(text) > 0
                and Path(text).exists()
                or t("cli.input_github_private_key_path_prompt"),
            ).ask()

            env_vars.append(
                f"{sanitized_id}_GITHUB_PRIVATE_KEY_PATH={private_key_path}"
            )

        if config.person_type in [
            GitHubAppAuth.MACHINE_USER,
            GitHubAppAuth.PROXY_AGENT,
        ]:
            access_token = questionary.text(
                t("cli.input_github_access_token_prompt"),
                validate=lambda text: len(text) > 0
                or t("cli.input_github_access_token_prompt"),
            ).ask()
            env_vars.append(f"{sanitized_id}_GITHUB_ACCESS_TOKEN={access_token}")

        # Step 9. Create Person Configuration File
        person_config_file = config_dir / f"team/members/{config.person_id}/person.yml"
        print(f"Create: {person_config_file}")
        person_config_file.parent.mkdir(parents=True, exist_ok=True)
        person_config_file.write_text(person_config)

        # Step 10. Update Environment Variables
        add_secret = False
        env_file_path = Path.cwd() / ".env"
        if env_file_path.exists():
            add_secret = questionary.confirm(
                t("cli.secret_info_prompt"), default=True
            ).ask()
        if add_secret:
            print(f"Append: {env_file_path}")
            env_file_path.write_text(
                f"{env_file_path.read_text()}\n\n# {config.person_id}\n"
                + "\n".join(env_vars)
            )
        else:
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
                    comment_mode = CommentMode(c)
                    res = await comment_mode.run(messages)

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
        choices = [sentinel_none] + existing

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
        url_parts = url.split("/")
        if (
            len(url_parts) < 8
            or not url.startswith(GITHUB_URL)
            or url_parts[3] != "organizations"
            or url_parts[5] != "settings"
            or url_parts[6] != "apps"
            or url_parts[7] == ""
        ):
            return t("cli.error_invalid_github_apps_url")

        config.github_username = url_parts[7]
        return True

    def parse_github_username(
        self, name: str, config: PersonConfig, is_github_apps: bool = False
    ) -> str | bool:
        error_message = t("cli.error_invalid_github_username")
        if name == "":
            return error_message

        github_username = ""
        if is_github_apps:
            config.person_id = name
            config.github_username = f"{name}[bot]"
            github_username = f"{quote(config.github_username)}"
        else:
            config.person_id = name.split("@")[0]
            config.github_username = config.person_id
            github_username = config.person_id

        try:
            response = requests.get(f"https://api.github.com/users/{github_username}")
            if response.status_code != 200:
                return error_message
            data = response.json()
            config.github_user_id = data.get("id", 0)
            config.git_email = f"{config.github_user_id}+{config.github_username}@users.noreply.github.com"
        except Exception:
            return error_message

        return True

    def parse_github_project_url(self, url: str, config: ProjectConfig) -> str | bool:
        url_parts = url.split("/")
        if (
            len(url_parts) < 7
            or not url.startswith(GITHUB_URL)
            or url_parts[3] not in ["orgs", "users"]
            or url_parts[5] != "projects"
            or url_parts[6] == ""
        ):
            return t("cli.error_invalid_github_project_url")

        config.project_type = url_parts[3]
        config.owner = url_parts[4]
        config.project_id = url_parts[6].split("?")[0]
        config.github_project_url = f"{GITHUB_URL}{config.project_type}/{config.owner}/projects/{config.project_id}"
        return True

    def parse_github_repository_url(
        self, url: str, config: ProjectConfig
    ) -> str | bool:
        url_parts = url.split("/")
        if len(url_parts) < 5 or not url.startswith(GITHUB_URL) or url_parts[4] == "":
            return t("cli.error_invalid_github_repository_url")
        if url_parts[3] != config.owner:
            return t("cli.error_inconsistent_github_url")
        config.repository_name = url_parts[4]
        config.github_repository_url = (
            f"{GITHUB_URL}{config.owner}/{config.repository_name}"
        )
        return True
