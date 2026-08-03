from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, cast

from guildbotics.app_api.models import (
    AdapterNativeAgentPolicySettings,
    BrainAssignment,
    CliAgentDefinition,
    EffortFieldSpec,
    IntelligenceConfigResponse,
    IntelligenceConfigUpdateRequest,
    ModelDefinition,
    NativeAgentPolicySettings,
)
from guildbotics.editions.simple import simple_brain_factory
from guildbotics.editions.simple.setup_service import (
    CreatedFile,
    SetupServiceError,
)
from guildbotics.intelligences.agent_runtime.policy import (
    POLICY_ADAPTERS,
    parse_native_agent_policy,
)
from guildbotics.intelligences.brains import agno_agent, cli_agent
from guildbotics.intelligences.cli_agents import (
    cli_agent_default_path,
    cli_agent_name_from_path,
    discover_cli_agents,
    is_native_cli_agent,
    resolve_cli_agent_path,
)
from guildbotics.intelligences.effort import (
    describe_overlay_problems,
    validate_effort_fields,
)
from guildbotics.intelligences.llm_providers import PROVIDER_DEFAULT_FILENAME
from guildbotics.utils.fileio import get_template_path, load_yaml_file, save_yaml_file

AGNO_BRAIN_CLASS = "guildbotics.intelligences.brains.agno_agent.AgnoAgentDefaultBrain"
CLI_BRAIN_CLASS = "guildbotics.intelligences.brains.cli_agent.CliAgentBrain"
MODEL_PATH_PROVIDER_INDEX = 1


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
        brain_mapping = self._read_merged_mapping(
            config_dir, person_id, "brain_mapping.yml"
        )

        # For a member, the team owns every slot/feature name it defines: those
        # can be value-overridden but not deleted/renamed (the merge would revive
        # them). Expose the team-owned names so the editor can lock them.
        inherited_model_slots: list[str] = []
        inherited_cli_slots: list[str] = []
        inherited_brain_features: list[str] = []
        if person_id:
            inherited_model_slots = list(
                self._read_merged_mapping(config_dir, None, "model_mapping.yml")
            )
            inherited_cli_slots = list(
                self._read_merged_mapping(config_dir, None, "cli_agent_mapping.yml")
            )
            inherited_brain_features = list(
                self._read_merged_mapping(config_dir, None, "brain_mapping.yml")
            )

        return IntelligenceConfigResponse(
            config_dir=config_dir,
            person_id=person_id,
            inherited=inherited,
            model_mapping=model_mapping,
            models=self._read_models(config_dir, person_id, model_mapping),
            cli_agent_mapping=cli_agent_mapping,
            cli_agents=self._read_cli_agents(config_dir, person_id, cli_agent_mapping),
            brain_mapping=self._read_brain_assignments(brain_mapping),
            native_agent_policy=self._read_native_agent_policy(config_dir, person_id),
            inherited_model_slots=inherited_model_slots,
            inherited_cli_slots=inherited_cli_slots,
            inherited_brain_features=inherited_brain_features,
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
            self._write_model_def(request.config_dir, None, target_dir, model, files)

        cli_mapping_file = target_dir / "cli_agent_mapping.yml"
        save_yaml_file(
            cli_mapping_file, self._normalize_cli_mapping(request.cli_agent_mapping)
        )
        files.append(CreatedFile(path=cli_mapping_file, action="update"))

        for agent in request.cli_agents:
            self._write_cli_agent_def(
                request.config_dir, None, target_dir, agent, files
            )

        brain_mapping_file = target_dir / "brain_mapping.yml"
        save_yaml_file(
            brain_mapping_file,
            {
                assignment.name: self._to_brain_config(assignment)
                for assignment in request.brain_mapping
            },
        )
        files.append(CreatedFile(path=brain_mapping_file, action="update"))

        self._write_native_agent_policy(target_dir, request.native_agent_policy, files)

        self._clear_runtime_caches(request.person_id)
        return IntelligenceConfigResult(files)

    def _update_member_overrides(
        self, request: IntelligenceConfigUpdateRequest, target_dir: Path
    ) -> IntelligenceConfigResult:
        # A member override stores only what differs from the team defaults, so
        # the runtime's per-key merge (load_person_slot_mapping) keeps inheriting
        # every slot the member does not customize. Files are reconciled in place
        # (never a blind rmtree): a definition is rewritten from the member's own
        # file when present, so fields the editor never surfaces -- a hand-tuned
        # rate_limit or conversation_scope -- survive a save, and definitions that
        # no longer differ from the team are pruned.
        team = self.read_config(config_dir=request.config_dir, person_id=None)
        requested_cli = self._normalize_cli_mapping(request.cli_agent_mapping)

        model_override = {
            slot: path
            for slot, path in request.model_mapping.items()
            if team.model_mapping.get(slot) != path
        }
        cli_override = {
            slot: path
            for slot, path in requested_cli.items()
            if team.cli_agent_mapping.get(slot) != path
        }
        team_brain = {assignment.name: assignment for assignment in team.brain_mapping}
        brain_override = {
            assignment.name: self._to_brain_config(assignment)
            for assignment in request.brain_mapping
            if team_brain.get(assignment.name) != assignment
        }

        files: list[CreatedFile] = []

        self._reconcile_mapping_file(
            target_dir / "model_mapping.yml", model_override, files
        )
        self._reconcile_mapping_file(
            target_dir / "cli_agent_mapping.yml", cli_override, files
        )
        self._reconcile_mapping_file(
            target_dir / "brain_mapping.yml", brain_override, files
        )
        self._reconcile_member_defs(
            request.config_dir,
            target_dir,
            "models",
            [
                model.path
                for model in request.models
                if model.path.startswith("models/")
            ],
            {model.path: model for model in request.models},
            lambda base, model: self._model_def_yaml(
                base,
                model,
                self._described_keys(
                    self._model_effort_descriptors(
                        request.config_dir, request.person_id, model.path
                    ),
                    f"provider '{self._provider_from_model_path(model.path)}'",
                ),
            ),
            files,
        )
        self._reconcile_member_defs(
            request.config_dir,
            target_dir,
            "cli_agents",
            [self._cli_agent_rel_path(agent) for agent in request.cli_agents],
            {self._cli_agent_rel_path(agent): agent for agent in request.cli_agents},
            lambda base, agent: self._cli_def_yaml(
                base,
                agent,
                self._described_keys(
                    self._cli_effort_descriptors(
                        request.config_dir, request.person_id, agent.path
                    ),
                    f"AI CLI tool '{agent.path}'",
                ),
            ),
            files,
        )
        self._reconcile_member_policy(request, team, target_dir, files)

        if target_dir.exists() and not any(target_dir.iterdir()):
            target_dir.rmdir()

        self._clear_runtime_caches(request.person_id)
        return IntelligenceConfigResult(files)

    def _reconcile_mapping_file(
        self, path: Path, data: dict[str, Any], files: list[CreatedFile]
    ) -> None:
        if data:
            path.parent.mkdir(parents=True, exist_ok=True)
            save_yaml_file(path, data)
            files.append(CreatedFile(path=path, action="update"))
        elif path.exists():
            path.unlink()
            files.append(CreatedFile(path=path, action="delete"))

    def _reconcile_member_defs(
        self,
        config_dir: Path,
        target_dir: Path,
        subdir: str,
        paths: list[str],
        definitions: dict[str, Any],
        build: Any,
        files: list[CreatedFile],
    ) -> None:
        """Write member definition files that differ from the inherited ones.

        A member copy is kept only when its resolved content differs from the
        team/template definition; identical copies (and definitions for paths the
        member no longer references) are pruned so the runtime keeps inheriting.
        """
        kept: set[Path] = set()
        for rel_path in paths:
            member_file = target_dir / rel_path
            inherited = self._read_scoped_yaml(config_dir, None, rel_path)
            present = self._read_optional_yaml(member_file)
            desired = build(present if present else inherited, definitions[rel_path])
            if self._comparable_def(desired) == self._comparable_def(inherited):
                continue
            member_file.parent.mkdir(parents=True, exist_ok=True)
            save_yaml_file(member_file, desired)
            kept.add(member_file)
            files.append(CreatedFile(path=member_file, action="update"))
        self._prune_member_dir(target_dir / subdir, kept, files)

    def _comparable_def(self, data: Any) -> Any:
        """Normalize a definition for override comparison.

        Drops empty values (mirroring ``save_yaml_file`` cleaning) plus empty
        containers, so a definition whose only field is an empty ``parameters``
        map is treated as equal to a missing definition and is not written.
        ``effort_fields`` is dropped too: it describes what the provider accepts
        rather than what this scope configured, so its presence in an inherited
        file must not make an otherwise identical definition look customized.
        """
        if isinstance(data, dict):
            result = {}
            for key, value in data.items():
                if key == "effort_fields":
                    continue
                normalized = self._comparable_def(value)
                if normalized not in (None, "", {}, []):
                    result[key] = normalized
            return result
        if isinstance(data, list):
            return [self._comparable_def(item) for item in data]
        return data

    def _prune_member_dir(
        self, directory: Path, kept: set[Path], files: list[CreatedFile]
    ) -> None:
        if not directory.exists():
            return
        for path in sorted(directory.rglob("*.yml")):
            if path not in kept:
                path.unlink()
                files.append(CreatedFile(path=path, action="delete"))
        for sub in sorted(directory.rglob("*"), reverse=True):
            if sub.is_dir() and not any(sub.iterdir()):
                sub.rmdir()
        if not any(directory.iterdir()):
            directory.rmdir()

    def _reconcile_member_policy(
        self,
        request: IntelligenceConfigUpdateRequest,
        team: IntelligenceConfigResponse,
        target_dir: Path,
        files: list[CreatedFile],
    ) -> None:
        policy_file = target_dir / "native_agent_policy.yml"
        # Omitted policy: keep whatever the member already had (nothing to do).
        if request.native_agent_policy is None:
            return
        if request.native_agent_policy != team.native_agent_policy:
            self._write_native_agent_policy(
                target_dir, request.native_agent_policy, files
            )
        elif policy_file.exists():
            policy_file.unlink()
            files.append(CreatedFile(path=policy_file, action="delete"))

    def _normalize_cli_mapping(self, mapping: dict[str, str]) -> dict[str, str]:
        """Pass definition paths through unchanged.

        A slot names a definition by path, exactly as a model slot does, so
        there is no second spelling to normalize. Rewriting anything else into a
        path here would make a stale mapping look valid in the editor while the
        runtime, which does no such rewriting, could not resolve it.
        """
        return dict(mapping)

    def _write_model_def(
        self,
        config_dir: Path,
        person_id: str | None,
        target_dir: Path,
        model: ModelDefinition,
        files: list[CreatedFile],
    ) -> None:
        if not model.path.startswith("models/"):
            return
        provider = self._provider_from_model_path(model.path)
        descriptors = self._model_effort_descriptors(config_dir, person_id, model.path)
        self._reject_unusable_effort(
            model.effort, descriptors, where=f"provider '{provider}'"
        )
        # The baseline speaks the same vocabulary, so a typo there is caught the
        # same way rather than surfacing at run time.
        self._reject_unusable_effort(
            {"parameters": model.parameters},
            descriptors,
            where=f"provider '{provider}'",
        )
        model_file = target_dir / model.path
        model_file.parent.mkdir(parents=True, exist_ok=True)
        # Seed from the inherited definition (member -> team -> template) so
        # fields the editor does not surface are preserved on save.
        base = self._read_scoped_yaml(config_dir, person_id, model.path)
        save_yaml_file(
            model_file,
            self._model_def_yaml(
                base, model, self._described_keys(descriptors, f"provider '{provider}'")
            ),
        )
        files.append(CreatedFile(path=model_file, action="update"))

    def _write_cli_agent_def(
        self,
        config_dir: Path,
        person_id: str | None,
        target_dir: Path,
        agent: CliAgentDefinition,
        files: list[CreatedFile],
    ) -> None:
        rel_path = self._cli_agent_rel_path(agent)
        descriptors = self._cli_effort_descriptors(config_dir, person_id, rel_path)
        self._reject_unusable_effort(
            agent.effort, descriptors, where=f"AI CLI tool '{agent.path}'"
        )
        # The baseline speaks the same vocabulary, so a typo there is caught the
        # same way rather than surfacing at run time.
        self._reject_unusable_effort(
            {"parameters": agent.parameters},
            descriptors,
            where=f"AI CLI tool '{agent.path}'",
        )
        agent_file = target_dir / rel_path
        # A native tool's file is written even for an empty mapping, as an
        # explicit `effort: {}`. Deleting it instead would fall through to the
        # packaged template, whose mapping would silently take effect again --
        # so clearing the field in the editor would not clear anything.
        agent_file.parent.mkdir(parents=True, exist_ok=True)
        base = self._read_scoped_yaml(config_dir, person_id, rel_path)
        save_yaml_file(
            agent_file,
            self._cli_def_yaml(
                base,
                agent,
                self._described_keys(descriptors, f"AI CLI tool '{agent.path}'"),
            ),
        )
        files.append(CreatedFile(path=agent_file, action="update"))

    def _model_def_yaml(
        self, base: dict[str, Any], model: ModelDefinition, described: set[str]
    ) -> dict[str, Any]:
        data = dict(base)
        data["model_class"] = model.model_class
        data["parameters"] = self._merged_parameters(base, model.parameters, described)
        data["effort"] = model.effort
        return data

    @staticmethod
    def _merged_parameters(
        base: dict[str, Any], requested: dict[str, Any], described: set[str]
    ) -> dict[str, Any]:
        """Replace the settings the editor manages, keep the ones it never saw.

        A hand-tuned key the provider does not describe (a `temperature`, say)
        has no control and no descriptor, so a save must carry it through rather
        than drop it. A described key is authoritative in the request, including
        by its absence, so clearing a field really clears it.
        """
        existing = base.get("parameters", {})
        if not isinstance(existing, dict):
            existing = {}
        preserved = {
            key: value for key, value in existing.items() if key not in described
        }
        return {**preserved, **requested}

    @staticmethod
    def _described_keys(describing: dict[str, Any], where: str) -> set[str]:
        """Top-level keys the descriptors claim, so nesting is replaced whole."""
        return {
            field.key.split(".")[0]
            for field in validate_effort_fields(
                describing.get("effort_fields"), where=where
            )
        }

    def _cli_agent_rel_path(self, agent: CliAgentDefinition) -> str:
        """Where an AI CLI tool's definition lives.

        The path *is* the reference, so there is nothing to normalize.
        """
        return agent.path

    def _cli_def_yaml(
        self, base: dict[str, Any], agent: CliAgentDefinition, described: set[str]
    ) -> dict[str, Any]:
        # A native tool is driven by its adapter, so its file may only carry an
        # effort mapping -- never a script or env block.
        if is_native_cli_agent(cli_agent_name_from_path(agent.path)):
            return {"parameters": agent.parameters, "effort": agent.effort}
        # Preserve the inherited script when the request does not carry one. The
        # editor only loads the mapped agent's script, so a newly selected agent
        # would otherwise overwrite a real script with an empty one.
        data = dict(base)
        data["env"] = agent.env
        data["parameters"] = self._merged_parameters(base, agent.parameters, described)
        data["effort"] = agent.effort
        if agent.script:
            data["script"] = agent.script
        data.setdefault("script", "")
        return data

    def _scope_dir(self, config_dir: Path, person_id: str | None) -> Path:
        if person_id:
            return config_dir / "team/members" / person_id
        return config_dir

    def _read_scoped_mapping(
        self, config_dir: Path, person_id: str | None, file_name: str
    ) -> dict[str, str]:
        data = self._read_merged_mapping(config_dir, person_id, file_name)
        mapping = {str(key): str(value) for key, value in data.items()}
        if file_name == "cli_agent_mapping.yml":
            return self._normalize_cli_mapping(mapping)
        return mapping

    def _read_merged_mapping(
        self, config_dir: Path, person_id: str | None, file_name: str
    ) -> dict[str, Any]:
        """Read a slot mapping merged over the team-level defaults.

        Slot mappings map a slot name to a definition reference. A member's own
        mapping file overrides matching slots while inheriting every slot it does
        not define, mirroring the runtime resolution in
        ``load_person_slot_mapping``. This keeps a partial member override from
        dropping team-provided slots.
        """
        team = self._read_optional_yaml(config_dir / "intelligences" / file_name)
        if not team:
            team = self._read_optional_yaml(
                get_template_path() / "intelligences" / file_name
            )
        merged: dict[str, Any] = dict(team)
        if person_id:
            member = self._read_optional_yaml(
                config_dir / "team/members" / person_id / "intelligences" / file_name
            )
            merged.update(member)
        return merged

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
            models.append(self._model_def_from_path(config_dir, person_id, model_path))
        return models

    def _model_def_from_path(
        self, config_dir: Path, person_id: str | None, model_path: str
    ) -> ModelDefinition:
        data = self._read_scoped_yaml(config_dir, person_id, model_path)
        parameters = data.get("parameters", {}) if isinstance(data, dict) else {}
        if not isinstance(parameters, dict):
            parameters = {}
        provider = self._provider_from_model_path(model_path)
        # What this definition falls back to, mirroring the runtime: the
        # provider's own default, then the packaged definition of this path. The
        # second matters because a workspace file shadows the template
        # wholesale, so a definition saved before effort existed would otherwise
        # show -- and run with -- nothing at all.
        provider_default = self._provider_default_yaml(config_dir, person_id, provider)
        packaged = self._template_yaml(f"models/{provider}/{PROVIDER_DEFAULT_FILENAME}")
        inherited = provider_default.get("effort") or packaged.get("effort", {})
        describing = self._model_effort_descriptors(config_dir, person_id, model_path)
        return ModelDefinition(
            path=model_path,
            provider=provider,
            model_class=str(data.get("model_class", "")) if data else "",
            parameters=parameters,
            effort=data.get("effort", {}) if data else {},
            inherited_effort=inherited,
            effort_fields=self._effort_fields(describing, f"provider '{provider}'"),
        )

    @staticmethod
    def _declaring(*candidates: dict[str, Any]) -> dict[str, Any]:
        """The first candidate that declares field descriptors.

        Descriptors say what the provider accepts, so they belong to the
        provider rather than to whichever scope happens to hold a copy of the
        file. A saved definition carries only the settings the editor manages,
        so without falling through to the packaged file a single save would cost
        that definition its typed editing and its save-time validation.
        """
        for candidate in candidates:
            if candidate.get("effort_fields"):
                return candidate
        return {}

    def _model_effort_descriptors(
        self, config_dir: Path, person_id: str | None, model_path: str
    ) -> dict[str, Any]:
        """Where a model definition's field descriptors come from."""
        provider = self._provider_from_model_path(model_path)
        return self._declaring(
            self._provider_default_yaml(config_dir, person_id, provider),
            # The packaged fallback is the provider's own default definition.
            # This slot's path would find nothing: only `default.yml` is packaged.
            self._template_yaml(f"models/{provider}/{PROVIDER_DEFAULT_FILENAME}"),
        )

    def _cli_tool_default(
        self, config_dir: Path, person_id: str | None, tool: str
    ) -> dict[str, Any]:
        """A tool's own definition, which every slot on it inherits from."""
        return self._read_scoped_yaml(
            config_dir, person_id, cli_agent_default_path(tool)
        )

    def _cli_inherited_effort(
        self, config_dir: Path, person_id: str | None, tool: str
    ) -> dict[str, dict]:
        default_path = cli_agent_default_path(tool)
        return self._cli_tool_default(config_dir, person_id, tool).get(
            "effort"
        ) or self._template_yaml(default_path).get("effort", {})

    def _cli_effort_descriptors(
        self, config_dir: Path, person_id: str | None, rel_path: str
    ) -> dict[str, Any]:
        """Where an AI CLI tool's field descriptors come from."""
        tool = cli_agent_name_from_path(rel_path)
        default_path = cli_agent_default_path(tool)
        return self._declaring(
            self._read_scoped_yaml(config_dir, person_id, rel_path),
            self._read_scoped_yaml(config_dir, person_id, default_path),
            self._template_yaml(default_path),
        )

    def _provider_default_yaml(
        self, config_dir: Path, person_id: str | None, provider: str
    ) -> dict[str, Any]:
        if not provider:
            return {}
        return self._read_scoped_yaml(
            config_dir, person_id, f"models/{provider}/{PROVIDER_DEFAULT_FILENAME}"
        )

    @staticmethod
    def _effort_fields(data: dict[str, Any], where: str) -> list[EffortFieldSpec]:
        return [
            EffortFieldSpec(
                key=field.key,
                type=field.type,
                values=list(field.values),
                minimum=field.minimum,
                maximum=field.maximum,
            )
            for field in validate_effort_fields(data.get("effort_fields"), where=where)
        ]

    def _read_cli_agents(
        self,
        config_dir: Path,
        person_id: str | None,
        cli_agent_mapping: dict[str, str],
    ) -> list[CliAgentDefinition]:
        # The executable to look up on PATH may differ from the tool name (a
        # native tool declares it in the built-in catalog, a script tool in its
        # yaml), so resolve it via the discovered catalog.
        executables = {
            agent.name: agent.executable
            for agent in discover_cli_agents(config_dir, person_id)
        }
        agents: list[CliAgentDefinition] = []
        seen: set[str] = set()
        for agent_path in cli_agent_mapping.values():
            if agent_path in seen:
                continue
            seen.add(agent_path)
            rel_path = agent_path
            tool = cli_agent_name_from_path(agent_path)
            if not tool:
                continue
            native = is_native_cli_agent(tool)
            data = self._read_scoped_yaml(config_dir, person_id, rel_path)
            effort_fields = self._effort_fields(
                self._cli_effort_descriptors(config_dir, person_id, rel_path),
                f"AI CLI tool '{agent_path}'",
            )
            effort_supported = bool(
                data.get(
                    "effort_supported",
                    self._cli_tool_default(config_dir, person_id, tool).get(
                        "effort_supported", True
                    ),
                )
            )
            inherited_effort = self._cli_inherited_effort(config_dir, person_id, tool)
            if native:
                data = {
                    "effort": data.get("effort", {}),
                    "parameters": data.get("parameters", {}),
                }
            env = data.get("env", {}) if isinstance(data, dict) else {}
            if not isinstance(env, dict):
                env = {}
            name = tool
            detected_path = resolve_cli_agent_path(executables.get(tool, tool))
            agents.append(
                CliAgentDefinition(
                    path=agent_path,
                    name=name,
                    env=env,
                    script=str(data.get("script", "")) if data else "",
                    detected=bool(detected_path),
                    detected_path=detected_path,
                    effort=data.get("effort", {}) if data else {},
                    parameters=data.get("parameters", {}) if data else {},
                    inherited_effort=inherited_effort,
                    effort_fields=effort_fields,
                    effort_supported=effort_supported,
                )
            )
        return agents

    @staticmethod
    def _reject_unusable_effort(
        effort: dict[str, dict], describing: dict[str, Any], *, where: str
    ) -> None:
        """Refuse an effort overlay the provider could not act on.

        A mistyped key is valid YAML and valid JSON, so nothing else would catch
        it until the provider rejected the run -- far from the edit that caused
        it. Providers that describe no fields are left alone, since nothing is
        known there about which keys are valid.
        """
        problems = describe_overlay_problems(
            effort, validate_effort_fields(describing.get("effort_fields"), where=where)
        )
        if problems:
            raise SetupServiceError(
                "invalid_effort_settings", f"{where}: {'; '.join(problems)}"
            )

    def _template_yaml(self, relative_path: str) -> dict[str, Any]:
        """What a scope inherits when it holds no file of its own."""
        return self._read_optional_yaml(
            get_template_path() / "intelligences" / relative_path
        )

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

    def _read_native_agent_policy(
        self, config_dir: Path, person_id: str | None
    ) -> NativeAgentPolicySettings:
        payload = self._read_scoped_yaml(
            config_dir, person_id, "native_agent_policy.yml"
        )
        policy = parse_native_agent_policy(payload)
        return NativeAgentPolicySettings(
            **{
                adapter: AdapterNativeAgentPolicySettings(
                    filesystem_access=cast(
                        Any, policy.for_adapter(adapter).filesystem_access
                    )
                )
                for adapter in POLICY_ADAPTERS
            }
        )

    def _write_native_agent_policy(
        self,
        target_dir: Path,
        policy: NativeAgentPolicySettings | None,
        files: list[CreatedFile],
    ) -> None:
        if policy is None:
            return
        target_dir.mkdir(parents=True, exist_ok=True)
        policy_file = target_dir / "native_agent_policy.yml"
        save_yaml_file(policy_file, policy.model_dump(mode="json"))
        files.append(CreatedFile(path=policy_file, action="update"))

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
