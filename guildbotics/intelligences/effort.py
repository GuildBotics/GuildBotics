"""Provider-neutral model effort labels and their resolution.

Effort is the single shared vocabulary for "how hard should the model think":
``low`` / ``default`` / ``high``. Translating a label into concrete provider
settings belongs to the layers that hold provider knowledge (model definition
YAML, AI CLI tool configuration, native adapters); this module only owns the
label vocabulary, its ordering, and the resolution order shared by every brain.

Resolution order (identical for every brain):

1. ``session_state["effort"]`` — the runtime channel. Both a CLI
   ``effort=<level>`` parameter and a workflow's automatic assessment arrive
   here, and the two are mutually exclusive.
2. the command frontmatter ``effort:``
3. unspecified

``default`` is not the same as unspecified: a runtime ``effort=default``
explicitly cancels a frontmatter ``effort: high``. Both mean "do not intervene",
but for a continued native CLI session that means "keep the settings the session
already has", never "restore the model default".
"""

from __future__ import annotations

from dataclasses import dataclass
from logging import Logger
from typing import Any

LOW = "low"
DEFAULT = "default"
HIGH = "high"

#: Every effort label, ordered from least to most effort. These are the values a
#: *request* may carry (frontmatter, a runtime parameter, a workflow decision).
EFFORT_LEVELS: tuple[str, ...] = (LOW, DEFAULT, HIGH)

#: The levels an effort *mapping* may describe. ``default`` is absent by
#: definition: it means "do not intervene", so a mapping for it could never be
#: applied. Settings meant to always apply belong in the model's own
#: ``parameters`` instead.
MAPPABLE_LEVELS: tuple[str, ...] = (LOW, HIGH)

_EFFORT_RANK = {level: rank for rank, level in enumerate(EFFORT_LEVELS)}

#: The session_state key that carries a runtime effort request.
EFFORT_KEY = "effort"


class EffortError(ValueError):
    """An explicitly supplied effort value is not a known level."""


@dataclass(frozen=True, slots=True)
class ResolvedEffort:
    """The outcome of resolving an effort request.

    Attributes:
        requested: The value as supplied by the winning source, unchanged.
        resolved: The level actually adopted, or ``""`` when unspecified.
        source: Where the winning value came from (``runtime`` / ``frontmatter``
            / ``""`` when nothing was specified).
    """

    requested: str = ""
    resolved: str = ""
    source: str = ""

    @property
    def intervenes(self) -> bool:
        """Whether a provider setting overlay should be applied at all."""
        return self.resolved in {LOW, HIGH}


def normalize_effort(
    value: Any, *, strict: bool = True, logger: Logger | None = None
) -> str:
    """Normalize an effort value to a known level.

    Args:
        value: The raw value (typically from frontmatter, params, or storage).
        strict: When True an unknown non-empty value raises; when False it is
            downgraded to ``""`` with a warning. Persisted values are read with
            ``strict=False`` so a corrupted state file cannot block execution.
        logger: Optional logger used for the non-strict warning.

    Returns:
        One of :data:`EFFORT_LEVELS`, or ``""`` when unspecified.

    Raises:
        EffortError: When ``strict`` and the value is not a known level.
    """
    if value is None:
        return ""
    text = str(value).strip().lower()
    if not text:
        return ""
    if text in _EFFORT_RANK:
        return text
    if strict:
        raise EffortError(
            f"Unknown effort '{value}'. Expected one of: {', '.join(EFFORT_LEVELS)}."
        )
    if logger is not None:
        logger.warning("Ignoring unknown stored effort value: %r", value)
    return ""


def promote_effort(
    current: Any, candidate: Any, *, logger: Logger | None = None
) -> str:
    """Return the higher of two effort levels (never a demotion).

    String comparison must not be used here: lexicographically ``low`` sorts
    above ``high`` and ``default``, which would invert the intended ordering.
    """
    current_level = normalize_effort(current, strict=False, logger=logger)
    candidate_level = normalize_effort(candidate, strict=False, logger=logger)
    if not current_level:
        return candidate_level
    if not candidate_level:
        return current_level
    return max(current_level, candidate_level, key=lambda level: _EFFORT_RANK[level])


def resolve_effort(
    session_state: Any, frontmatter_effort: Any = "", *, logger: Logger | None = None
) -> ResolvedEffort:
    """Resolve the effort for one brain run.

    Args:
        session_state: The brain's ``session_state`` mapping, or anything else
            (ignored) when the caller has none.
        frontmatter_effort: The command frontmatter ``effort:`` value.
        logger: Optional logger for stored-value warnings.

    Returns:
        The resolved effort, distinguishing the requested value from the
        adopted level.
    """
    runtime_value = ""
    if isinstance(session_state, dict):
        runtime_value = str(session_state.get(EFFORT_KEY, "") or "")
    if runtime_value.strip():
        return ResolvedEffort(
            requested=runtime_value,
            resolved=normalize_effort(runtime_value, logger=logger),
            source="runtime",
        )
    frontmatter_value = str(frontmatter_effort or "")
    if frontmatter_value.strip():
        return ResolvedEffort(
            requested=frontmatter_value,
            resolved=normalize_effort(frontmatter_value, logger=logger),
            source="frontmatter",
        )
    return ResolvedEffort()


def validate_effort_overlay(overlay: Any, *, where: str) -> dict[str, dict]:
    """Validate an ``effort:`` block from a configuration file.

    Args:
        overlay: The raw block: a mapping of effort level to provider settings.
        where: A human-readable location used in the error message.

    Returns:
        The validated block, with unspecified levels simply absent.

    Raises:
        EffortError: When a key is not a known level or a value is not a mapping.
    """
    if overlay in (None, ""):
        return {}
    if not isinstance(overlay, dict):
        raise EffortError(f"{where}: 'effort' must be a mapping of level to settings.")
    validated: dict[str, dict] = {}
    for level, settings in overlay.items():
        normalized = normalize_effort(level)
        if normalized == DEFAULT:
            raise EffortError(
                f"{where}: 'effort.{DEFAULT}' can never be applied, because "
                f"{DEFAULT} means 'do not intervene'. Put settings that should "
                "always apply in 'parameters' instead."
            )
        if not normalized:
            raise EffortError(
                f"{where}: 'effort' keys must be one of: {', '.join(MAPPABLE_LEVELS)}."
            )
        if not isinstance(settings, dict):
            raise EffortError(
                f"{where}: 'effort.{normalized}' must be a mapping of settings."
            )
        validated[normalized] = dict(settings)
    return validated


#: Editable value shapes an effort field can take. An editor renders a control
#: per type and needs no provider vocabulary of its own; which keys exist, and
#: what they mean, stays in the provider's own configuration file.
FIELD_TYPES: tuple[str, ...] = (
    "enum",
    "integer",
    "boolean",
    "string",
    # A model identifier. Editors may offer a picker when the provider can
    # enumerate its models, and otherwise fall back to free text.
    "model_id",
)


@dataclass(frozen=True, slots=True)
class EffortField:
    """One editable setting inside an effort level block.

    Attributes:
        key: Dotted path within the level block (``thinking.budget_tokens``
            addresses ``{"thinking": {"budget_tokens": ...}}``).
        type: One of :data:`FIELD_TYPES`.
        values: The permitted values, for ``enum``.
        minimum: Lower bound, for ``integer``.
        maximum: Upper bound, for ``integer``.
    """

    key: str
    type: str
    values: tuple[str, ...] = ()
    minimum: int | None = None
    maximum: int | None = None


def validate_effort_fields(fields: Any, *, where: str) -> list[EffortField]:
    """Validate an ``effort_fields:`` block describing a provider's settings.

    The block is what lets an editor offer typed controls without holding any
    provider knowledge itself, so a malformed descriptor is an error rather than
    something to skip: a silently dropped field would degrade to free-text JSON
    with no indication why.

    Raises:
        EffortError: When the block is not a list of well-formed descriptors.
    """
    if fields in (None, ""):
        return []
    if not isinstance(fields, list):
        raise EffortError(f"{where}: 'effort_fields' must be a list of descriptors.")
    described: list[EffortField] = []
    for entry in fields:
        if not isinstance(entry, dict):
            raise EffortError(f"{where}: each 'effort_fields' entry must be a mapping.")
        key = str(entry.get("key", "") or "").strip()
        if not key:
            raise EffortError(f"{where}: an 'effort_fields' entry has no 'key'.")
        field_type = str(entry.get("type", "") or "").strip()
        if field_type not in FIELD_TYPES:
            raise EffortError(
                f"{where}: 'effort_fields.{key}.type' must be one of: "
                f"{', '.join(FIELD_TYPES)}."
            )
        raw_values = entry.get("values") or []
        if not isinstance(raw_values, list):
            raise EffortError(f"{where}: 'effort_fields.{key}.values' must be a list.")
        if field_type == "enum" and not raw_values:
            raise EffortError(f"{where}: 'effort_fields.{key}' needs 'values'.")
        described.append(
            EffortField(
                key=key,
                type=field_type,
                values=tuple(str(value) for value in raw_values),
                minimum=_optional_int(entry.get("minimum")),
                maximum=_optional_int(entry.get("maximum")),
            )
        )
    return described


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise EffortError(f"'{value}' is not a whole number.") from exc


def describe_overlay_problems(
    overlay: dict[str, dict], fields: list[EffortField]
) -> list[str]:
    """Report settings that contradict the provider's own field descriptors.

    Used to reject a mistyped key while it is still being edited. Without it the
    first sign of a typo is the provider rejecting the run much later, by which
    time the connection to the edit is lost.

    Returns:
        Human-readable problems, empty when the overlay is consistent. Always
        empty when the provider describes no fields, since nothing is known
        about which keys are valid.
    """
    if not fields:
        return []
    described = {field.key: field for field in fields}
    problems: list[str] = []
    for level, settings in overlay.items():
        for key, value in _flatten(settings):
            field = described.get(key)
            if field is None:
                problems.append(
                    f"{level}.{key}: not a setting this provider accepts "
                    f"(expected one of: {', '.join(sorted(described))})"
                )
                continue
            problem = _field_problem(field, value)
            if problem:
                problems.append(f"{level}.{key}: {problem}")
    return problems


def _flatten(settings: Any, prefix: str = "") -> list[tuple[str, Any]]:
    """Expand nested settings into dotted key/value pairs."""
    if not isinstance(settings, dict):
        return [(prefix, settings)]
    flattened: list[tuple[str, Any]] = []
    for key, value in settings.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        flattened.extend(
            _flatten(value, path) if isinstance(value, dict) else [(path, value)]
        )
    return flattened


def _field_problem(field: EffortField, value: Any) -> str:
    if field.type == "enum":
        return (
            ""
            if str(value) in field.values
            else f"must be one of: {', '.join(field.values)}"
        )
    if field.type == "boolean":
        return "" if isinstance(value, bool) else "must be true or false"
    if field.type == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            return "must be a whole number"
        if field.minimum is not None and value < field.minimum:
            return f"must be at least {field.minimum}"
        if field.maximum is not None and value > field.maximum:
            return f"must be at most {field.maximum}"
        return ""
    return "" if isinstance(value, str) else "must be text"


def effort_settings(
    overlay: dict[str, dict], resolved: ResolvedEffort, *, logger: Logger | None = None
) -> dict:
    """Look up the provider settings for a resolved effort level.

    A level with no mapping is never an error: providers differ in how far their
    mappings are filled in, and stopping a run over a missing overlay would be
    worse than running without it. An explicit request is warned about and
    recorded as ``unsupported`` in diagnostics.
    """
    if not resolved.intervenes:
        return {}
    settings = overlay.get(resolved.resolved)
    if settings:
        return dict(settings)
    if logger is not None:
        logger.warning(
            "No 'effort.%s' mapping is configured; continuing without intervention.",
            resolved.resolved,
        )
    return {}


def effort_diagnostics(
    resolved: ResolvedEffort, settings: dict, *, model: str = ""
) -> dict[str, Any]:
    """Build the safe diagnostics payload for an effort decision.

    Only an allowlist is recorded. Effective provider settings may sit next to
    API keys, headers, and client configuration, so values are never written —
    just the keys that were applied.
    """
    payload: dict[str, Any] = {
        "requested": resolved.requested,
        "resolved": resolved.resolved,
        "source": resolved.source,
        "applied_keys": sorted(str(key) for key in settings),
        "unsupported": bool(resolved.intervenes and not settings),
    }
    if model:
        payload["model"] = model
    return payload
