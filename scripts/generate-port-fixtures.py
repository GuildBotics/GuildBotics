"""Regenerate service goldens by executing the frozen Python host models.

Run with `uv run --no-sync python scripts/generate-port-fixtures.py`. No
provider, credential store, or external service is loaded. The source commit
is pinned so a changed branch cannot silently change the contract.
"""

import ast
import json
import subprocess
import sys
import types
import typing
from pathlib import Path

root = Path(__file__).resolve().parents[1]
FROZEN_HOST = "fea9e05aaf8b4bca96e19d615b6811ea0eacb7d0"


def source(name):
    return subprocess.check_output(
        ["git", "show", f"{FROZEN_HOST}:guildbotics/runtime/{name}.py"],
        cwd=root,
        text=True,
    )


def module(name, code):
    mod = types.ModuleType(name)
    sys.modules[name] = mod
    exec(code, mod.__dict__)
    return mod


module("guildbotics.utils.i18n_tool", "def t(key): return key")
module("guildbotics.utils.process_limits", "STREAM_READ_LIMIT=10*1024*1024")
port = module("guildbotics.runtime.port", source("port"))
res = module("guildbotics.runtime.resources", source("resources"))
extra = {}
for name in ("ticket_service", "code_hosting_service"):
    tree = ast.parse(source(name))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and any(
            isinstance(b, ast.Name) and b.id == "ReadModel" for b in node.bases
        ):
            extra[node.name] = ast.unparse(node)
ns = dict(res.__dict__)
for name, code in extra.items():
    exec(code, ns)


def sample(typ):
    origin = typing.get_origin(typ)
    args = typing.get_args(typ)
    if typ is res.ActorName:
        return "public-account"
    if typ is str:
        return "値 🦀"
    if typ is int:
        return 17
    if typ is bool:
        return True
    if typ is type(None):
        return None
    if origin is list:
        return [sample(args[0])]
    if origin is typing.Literal:
        return args[0]
    if origin in (typing.Union, types.UnionType):
        return sample(args[0])
    if isinstance(typ, type) and issubclass(typ, res.ReadModel):
        return {
            k: sample(f.annotation)
            for k, f in typ.model_fields.items()
            if f.is_required()
        }
    raise ValueError(typ)


models = {c.__name__: c for c in res.RESOURCES.values()}
models.update({name: ns[name] for name in extra})
models.update(
    {
        n: getattr(res, n)
        for n in (
            "WorkTarget",
            "TicketRelative",
            "CommentableLine",
            "Check",
            "CompletionBlocker",
            "FailedLog",
            "AdvisoryIdentifier",
            "CommentWritten",
            "ReactionAdded",
        )
    }
)
cases = []
for name, model in models.items():
    data = sample(model)
    variants = [data, {k: sample(f.annotation) for k, f in model.model_fields.items()}]
    nullable = {
        k: None if type(None) in typing.get_args(f.annotation) else sample(f.annotation)
        for k, f in model.model_fields.items()
    }
    variants.append(nullable)
    if name == "Review":
        variants.append({**data, "verdict": "dismissed"})
    for data in variants:
        expected = model.model_validate(data).model_dump(mode="json")
        cases.append(
            {"model": name, "input": data, "expected": expected, "valid": True}
        )
    for key, field in model.model_fields.items():
        if field.is_required():
            bad = sample(model)
            del bad[key]
            cases.append({"model": name, "input": bad, "valid": False})
        bad = sample(model)
        bad[key] = {}
        try:
            model.model_validate(bad)
        except ValueError:
            cases.append({"model": name, "input": bad, "valid": False})
    cases.append(
        {"model": name, "input": {**sample(model), "unknown": True}, "valid": False}
    )
queries = []
for resource in res.RESOURCES:
    inputs = [
        None,
        {},
        [],
        False,
        {"method": "POST"},
        {"page_size": 0},
        {"page_size": 1},
        {"page_size": 100},
        {"page_size": 101},
        {"page_size": True},
        {"page_size": 1.0},
        {"page_size": "2"},
        {"node": "thread"},
        {"node": ""},
        {"node": "x" * 257},
        {"text": "a b"},
        {"text": "🦀" * 256},
        {"text": "🦀" * 257},
        {"label": "l"},
        {"state": "all"},
        {"state": "closed"},
        {"state": "resolved"},
        {"id": "alert"},
        {"id": "alert", "state": "open"},
        {"id": ""},
        {"failed_logs": True},
        {"failed_logs": 1},
        {"log_tail_bytes": 1},
        {"log_tail_bytes": res.MAX_LOG_TAIL_BYTES},
        {"log_tail_bytes": res.MAX_LOG_TAIL_BYTES + 1},
    ]
    for params in inputs:
        for cursor in ("", "next"):
            c = {"resource": resource, "parameters": params, "continuation": cursor}
            try:
                c["expected"] = res.read_conditions(resource, params, cursor)
            except port.ServiceError as e:
                c["error"] = str(e).rsplit(".", 1)[-1]
            queries.append(c)
actors = {}
for resource, model in res.RESOURCES.items():
    actors[resource] = [
        k
        for k, f in model.model_fields.items()
        if f.annotation is res.ActorName
        or typing.get_origin(f.annotation) is list
        and typing.get_args(f.annotation) == (res.ActorName,)
    ]
target = root / "crates/gb-ports/tests/fixtures"
target.mkdir(parents=True, exist_ok=True)
resources = {
    name: {
        "subject": res.RESOURCE_SUBJECTS[name],
        "ticket_service": name in res.TICKET_RESOURCES,
        "detail": name in res.DETAIL_RESOURCES,
    }
    for name in res.RESOURCES
}
for name, value in [
    ("models", cases),
    ("queries", queries),
    ("actors", actors),
    ("resources", resources),
]:
    lines = (
        [json.dumps(item, ensure_ascii=False) for item in value]
        if isinstance(value, list)
        else None
    )
    content = (
        "[\n" + ",\n".join(lines) + "\n]\n"
        if lines
        else json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    )
    (target / f"{name}.json").write_text(content, encoding="utf-8")
print(len(cases), len(queries))
