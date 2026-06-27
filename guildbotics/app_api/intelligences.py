from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, cast

from guildbotics.app_api.cli_agents import resolve_cli_agent_path
from guildbotics.app_api.models import (
    BrainAssignment,
    CliAgentDefinition,
    IntelligenceConfigResponse,
    IntelligenceConfigUpdateRequest,
    ModelDefinition,
    ModelProviderDefault,
)
from guildbotics.editions.simple import simple_brain_factory
from guildbotics.editions.simple.setup_service import CreatedFile
from guildbotics.intelligences.brains import agno_agent, cli_agent
from guildbotics.utils.fileio import get_template_path, load_yaml_file, save_yaml_file

AGNO_BRAIN_CLASS = "guildbotics.intelligences.brains.agno_agent.AgnoAgentDefaultBrain"
CLI_BRAIN_CLASS = "guildbotics.intelligences.brains.cli_agent.CliAgentBrain"
MODEL_PATH_PROVIDER_INDEX = 1
LLM_PROVIDERS = ("openai", "gemini", "anthropic")


class IntelligenceConfigResult:
    def __init__(self, files: list[CreatedFile]) -> None:
        self.files = files


class IntelligenceConfigService:
    def read_config(
        self, *, config_dir: Path, person_id: str | None = None
    ) -> IntelligenceConfigResponse:
        base_dir = self._scope_dir(config_dir, person_id)
        inherited = person_id is not None and not (base_dir / "intelligences").exists()

        model_mapping = self._read_scoped_mapping(
            config_dir, person_id, "model_mapping.yml"
        )
        cli_agent_mapping = self._read_scoped_mapping(
            config_dir, person_id, "cli_agent_mapping.yml"
        )
        brain_mapping = self._read_scoped_yaml(
            config_dir, person_id, "brain_mapping.yml"
        )

        return IntelligenceConfigResponse(
            config_dir=config_dir,
            person_id=person_id,
            inherited=inherited,
            model_mapping=model_mapping,
            models=self._read_models(config_dir, person_id, model_mapping),
            provider_defaults=self._read_provider_defaults(config_dir, person_id),
            cli_agent_mapping=cli_agent_mapping,
            cli_agents=self._read_cli_agents(config_dir, person_id, cli_agent_mapping),
            brain_mapping=self._read_brain_assignments(brain_mapping),
        )

    def update_config(
        self, request: IntelligenceConfigUpdateRequest
    ) -> IntelligenceConfigResult:
        base_dir = self._scope_dir(request.config_dir, request.person_id)
        target_dir = base_dir / "intelligences"
        if request.person_id and request.inherit_team_defaults:
            if target_dir.exists():
                shutil.rmtree(target_dir)
            self._clear_runtime_caches(request.person_id)
            return IntelligenceConfigResult(
                [CreatedFile(path=target_dir, action="delete")]
            )
        if request.person_id:
            return self._update_member_overrides(request, target_dir)

        files: list[CreatedFile] = []
        target_dir.mkdir(parents=True, exist_ok=True)

        model_mapping_file = target_dir / "model_mapping.yml"
        save_yaml_file(model_mapping_file, request.model_mapping)
        files.append(CreatedFile(path=model_mapping_file, action="update"))

        for model in request.models:
            model_file = target_dir / model.path
            model_file.parent.mkdir(parents=True, exist_ok=True)
            model_data = self._read_optional_yaml(model_file)
            if not model_data:
                model_data = self._read_optional_yaml(
                    get_template_path() / "intelligences" / model.path
                )
            model_data["model_class"] = model.model_class
            parameters = model_data.get("parameters", {})
            if not isinstance(parameters, dict):
                parameters = {}
            parameters["id"] = model.model_id
            model_data["parameters"] = parameters
            save_yaml_file(model_file, model_data)
            files.append(CreatedFile(path=model_file, action="update"))

        cli_mapping_file = target_dir / "cli_agent_mapping.yml"
        save_yaml_file(cli_mapping_file, request.cli_agent_mapping)
        files.append(CreatedFile(path=cli_mapping_file, action="update"))

        cli_agents_dir = target_dir / "cli_agents"
        cli_agents_dir.mkdir(parents=True, exist_ok=True)
        for agent in request.cli_agents:
            agent_file = cli_agents_dir / agent.path
            # Preserve the existing/template script when the request does not
            # carry one. The editor only loads the mapped agent's script, so a
            # newly selected agent would otherwise overwrite a real script with
            # an empty one. Mirrors how model files are merged above.
            agent_data = self._read_optional_yaml(agent_file)
            if not agent_data:
                agent_data = self._read_optional_yaml(
                    get_template_path() / "intelligences/cli_agents" / agent.path
                )
            agent_data["env"] = agent.env
            if agent.script:
                agent_data["script"] = agent.script
            agent_data.setdefault("script", "")
            save_yaml_file(agent_file, agent_data)
            files.append(CreatedFile(path=agent_file, action="update"))

        brain_mapping_file = target_dir / "brain_mapping.yml"
        save_yaml_file(
            brain_mapping_file,
            {
                assignment.name: self._to_brain_config(assignment)
                for assignment in request.brain_mapping
            },
        )
        files.append(CreatedFile(path=brain_mapping_file, action="update"))

        self._clear_runtime_caches(request.person_id)
        return IntelligenceConfigResult(files)

    def _update_member_overrides(
        self, request: IntelligenceConfigUpdateRequest, target_dir: Path
    ) -> IntelligenceConfigResult:
        if target_dir.exists():
            shutil.rmtree(target_dir)
        target_dir.mkdir(parents=True, exist_ok=True)

        files: list[CreatedFile] = []
        model_mapping_file = target_dir / "model_mapping.yml"
        save_yaml_file(model_mapping_file, request.model_mapping)
        files.append(CreatedFile(path=model_mapping_file, action="update"))

        cli_mapping_file = target_dir / "cli_agent_mapping.yml"
        save_yaml_file(cli_mapping_file, request.cli_agent_mapping)
        files.append(CreatedFile(path=cli_mapping_file, action="update"))

        self._clear_runtime_caches(request.person_id)
        return IntelligenceConfigResult(files)

    def _scope_dir(self, config_dir: Path, person_id: str | None) -> Path:
        if person_id:
            return config_dir / "team/members" / person_id
        return config_dir

    def _read_scoped_mapping(
        self, config_dir: Path, person_id: str | None, file_name: str
    ) -> dict[str, str]:
        data = self._read_scoped_yaml(config_dir, person_id, file_name)
        return {str(key): str(value) for key, value in data.items()}

    def _read_scoped_yaml(
        self, config_dir: Path, person_id: str | None, relative_path: str
    ) -> dict[str, Any]:
        if person_id:
            member_file = (
                config_dir
                / "team/members"
                / person_id
                / "intelligences"
                / relative_path
            )
            data = self._read_optional_yaml(member_file)
            if data:
                return data
        team_file = config_dir / "intelligences" / relative_path
        data = self._read_optional_yaml(team_file)
        if data:
            return data
        return self._read_optional_yaml(
            get_template_path() / "intelligences" / relative_path
        )

    def _read_models(
        self, config_dir: Path, person_id: str | None, model_mapping: dict[str, str]
    ) -> list[ModelDefinition]:
        models: list[ModelDefinition] = []
        seen: set[str] = set()
        for model_path in model_mapping.values():
            if not model_path.startswith("models/") or model_path in seen:
                continue
            seen.add(model_path)
            data = self._read_scoped_yaml(config_dir, person_id, model_path)
            parameters = data.get("parameters", {}) if isinstance(data, dict) else {}
            if not isinstance(parameters, dict):
                parameters = {}
            models.append(
                ModelDefinition(
                    path=model_path,
                    provider=self._provider_from_model_path(model_path),
                    model_class=str(data.get("model_class", "")) if data else "",
                    model_id=str(parameters.get("id", "")),
                )
            )
        return models

    def _read_provider_defaults(
        self, config_dir: Path, person_id: str | None
    ) -> list[ModelProviderDefault]:
        # The default model for each provider lives in
        # ``models/<provider>/default.yml`` (config override or shipped template).
        # Exposing it lets the desktop editor seed a slot when its provider
        # changes without duplicating model ids/classes in the frontend.
        defaults: list[ModelProviderDefault] = []
        for provider in LLM_PROVIDERS:
            data = self._read_scoped_yaml(
                config_dir, person_id, f"models/{provider}/default.yml"
            )
            parameters = data.get("parameters", {}) if isinstance(data, dict) else {}
            if not isinstance(parameters, dict):
                parameters = {}
            defaults.append(
                ModelProviderDefault(
                    provider=provider,
                    model_class=str(data.get("model_class", "")) if data else "",
                    model_id=str(parameters.get("id", "")),
                )
            )
        return defaults

    def _read_cli_agents(
        self,
        config_dir: Path,
        person_id: str | None,
        cli_agent_mapping: dict[str, str],
    ) -> list[CliAgentDefinition]:
        agents: list[CliAgentDefinition] = []
        seen: set[str] = set()
        for agent_path in cli_agent_mapping.values():
            if not agent_path.endswith(".yml") or agent_path in seen:
                continue
            seen.add(agent_path)
            data = self._read_scoped_yaml(
                config_dir, person_id, f"cli_agents/{agent_path}"
            )
            env = data.get("env", {}) if isinstance(data, dict) else {}
            if not isinstance(env, dict):
                env = {}
            name = agent_path.removesuffix(".yml")
            executable = name.removesuffix("-cli")
            detected_path = resolve_cli_agent_path(executable)
            agents.append(
                CliAgentDefinition(
                    path=agent_path,
                    name=name,
                    env=env,
                    script=str(data.get("script", "")) if data else "",
                    detected=bool(detected_path),
                    detected_path=detected_path,
                )
            )
        return agents

    def _read_brain_assignments(
        self, brain_mapping: dict[str, Any]
    ) -> list[BrainAssignment]:
        assignments: list[BrainAssignment] = []
        for name, value in brain_mapping.items():
            if not isinstance(value, dict):
                continue
            brain_class = str(value.get("class", ""))
            args = value.get("args", {})
            if not isinstance(args, dict):
                args = {}
            if brain_class == CLI_BRAIN_CLASS:
                assignments.append(
                    BrainAssignment(
                        name=str(name),
                        brain_class=brain_class,
                        engine="cli",
                        target=str(args.get("cli_agent", "default")),
                    )
                )
            else:
                assignments.append(
                    BrainAssignment(
                        name=str(name),
                        brain_class=brain_class or AGNO_BRAIN_CLASS,
                        engine="llm",
                        target=str(args.get("model", "default")),
                    )
                )
        return assignments

    def _to_brain_config(self, assignment: BrainAssignment) -> dict[str, Any]:
        if assignment.engine == "cli":
            return {
                "class": CLI_BRAIN_CLASS,
                "args": {"cli_agent": assignment.target},
            }
        return {
            "class": AGNO_BRAIN_CLASS,
            "args": {"model": assignment.target},
        }

    def _read_optional_yaml(self, file_path: Path) -> dict[str, Any]:
        if not file_path.exists():
            return {}
        data = load_yaml_file(file_path)
        if isinstance(data, dict):
            return cast(dict[str, Any], data)
        return {}

    def _provider_from_model_path(self, model_path: str) -> str:
        parts = model_path.split("/")
        if len(parts) > MODEL_PATH_PROVIDER_INDEX:
            return parts[MODEL_PATH_PROVIDER_INDEX]
        return ""

    def _clear_runtime_caches(self, person_id: str | None) -> None:
        if person_id:
            simple_brain_factory.person_brain_mapping.pop(person_id, None)
            agno_agent.person_model_mapping.pop(person_id, None)
            cli_agent.person_cli_agent_mapping.pop(person_id, None)
            return
        simple_brain_factory.person_brain_mapping.clear()
        agno_agent.person_model_mapping.clear()
        cli_agent.person_cli_agent_mapping.clear()
