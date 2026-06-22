import json
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml  # type: ignore
from pydantic import BaseModel

from guildbotics.entities.message import Message
from guildbotics.intelligences.common import MessageResponse
from guildbotics.runtime import Context
from guildbotics.utils.import_utils import ClassResolver


def to_text[TBaseModel: BaseModel](
    obj: dict | list[dict] | TBaseModel | list[TBaseModel],
) -> str:
    """
    Convert a Pydantic model, its subclass, or a list of them to a text representation.

    Args:
        obj (dict | list[dict] | TBaseModel | list[TBaseModel]): The object to convert.

    Returns:
        str: The text representation of the object.
    """

    def _clean(item: dict | BaseModel) -> dict:
        """Clean a single item by dumping BaseModel and removing empty values."""
        data = item.model_dump() if isinstance(item, BaseModel) else item
        return {k: v for k, v in data.items() if v not in ["", [], None]}

    # Normalize to list and track if original was single
    if isinstance(obj, (BaseModel, dict, list)):
        single = not isinstance(obj, list)
        items = [obj] if single else obj  # type: ignore[list-item]
        cleaned = [_clean(item) for item in items]  # type: ignore[arg-type]
        to_dump = cleaned[0] if single else cleaned
        return yaml.dump(to_dump, default_flow_style=False, allow_unicode=True).strip()
    # Fallback for other types
    return str(obj).strip()


def messages_to_json(messages: list[Message]) -> str:
    """
    Convert a list of Message objects to JSON-compatible format.
    Args:
        messages (list[Message]): The list of Message objects to convert.
    Returns:
        dict: The JSON-compatible representation of the message.
    """
    message_list = [
        {
            "content": message.content,
            "author": message.author,
            "author_type": message.author_type,
        }
        for message in messages
    ]
    return json.dumps(message_list, ensure_ascii=False, indent=2)


def to_dict[TBaseModel: BaseModel](
    context: Context,
    params: dict | None = None,
    cwd: Path | None = None,
    response_model: type[TBaseModel] | None = None,
) -> dict[str, Any]:
    kwargs: dict = {}
    if params is not None:
        params["context"] = context
        now = datetime.now().astimezone()
        if "now" not in params:
            params["now"] = now.strftime("%Y-%m-%d %H:%M")
        if "today" not in params:
            params["today"] = now.strftime("%Y-%m-%d")
        kwargs["session_state"] = params
        kwargs["add_state_in_messages"] = True
    if cwd:
        kwargs["cwd"] = cwd
    if response_model:
        kwargs["response_model"] = response_model
    return kwargs


async def _get_content[TBaseModel: BaseModel](
    context: Context,
    name: str,
    message: str,
    params: dict | None = None,
    cwd: Path | None = None,
    response_model: type[TBaseModel] | None = None,
) -> Any:
    brain = context.get_brain(name, None, None)
    kwargs = to_dict(context, params, cwd, response_model)
    return await brain.run(message=message, **kwargs)


async def convert_object[TBaseModel: BaseModel](
    context: Context,
    message: str,
    response_model: type[TBaseModel],
) -> TBaseModel:
    """
    Convert a message to the specified object based on a JSON schema.

    Args:
        message (str): The message to convert.
        response_model (TypeVar, optional): The Pydantic model class to convert to.

    Returns:
        TBaseModel: The converted object.
    """
    return await _get_content(
        context,
        "functions/convert_object",
        message=message,
        response_model=response_model,
    )


async def get_content(
    context: Context,
    name: str,
    message: str,
    params: dict | None = None,
    cwd: Path | None = None,
    config: dict | None = None,
    class_resolver: ClassResolver | None = None,
) -> Any:
    brain = context.get_brain(name, config, class_resolver)
    kwargs = to_dict(context, params, cwd)
    output = await brain.run(message=message, **kwargs)

    if not brain.response_class:
        return output

    if isinstance(output, brain.response_class):
        return output

    return await convert_object(context, output, brain.response_class)


async def talk_as(
    context: Context,
    topic: str,
    context_location: str,
    conversation_history: list[Message],
) -> str:
    """
    To talk a message as the character about the specified topic or content.
    Args:
        topic (str): The topic or content to talk about.
        context_location (str): The location or context of the conversation.
        conversation_history (list[Message]): The history of the conversation.
    Returns:
        str: The generated response text.
    """
    session_state = {"topic": topic}
    if context_location:
        session_state["context_location"] = context_location
    if conversation_history:
        session_state["conversation_history"] = messages_to_json(conversation_history)

    reply: MessageResponse = await get_content(
        context,
        "functions/talk_as",
        message="",
        params=session_state,
    )
    return reply.content.strip() if reply else ""
