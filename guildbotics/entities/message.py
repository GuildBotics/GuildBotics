from typing import ClassVar

from pydantic import BaseModel, Field


class FileInfo(BaseModel):
    """
    A class representing file information.

    Attributes:
        name (str): The name of the file.
        size (int): The size of the file in bytes.
        type (str): The type of the file.
        url (str): The URL to access the file.
        local_path (str): The local path where the file is stored.
    """

    name: str = Field(..., description="The name of the file.")
    size: int = Field(..., description="The size of the file in bytes.")
    type: str = Field(..., description="The type of the file.")
    url: str = Field("", description="The URL to access the file.")
    local_path: str = Field("", description="The local path where the file is stored.")


class Reaction(BaseModel):
    """
    A class representing a reaction to a message.
    Attributes:
        emoji (str): The emoji used for the reaction.
        users (list[str]): A list of user IDs who reacted with this emoji.
    """

    emoji: str = Field("", description="The emoji used for the reaction.")
    users: list[str] = Field(
        default_factory=list,
        description="A list of user IDs who reacted with this emoji.",
    )


class Message(BaseModel):
    """
    A class representing a message in a chat channel.
    Attributes:
        content (str): The content of the message.
        author (str): The author of the message.
        author_type (str): The type of the author (user or assistant).
        timestamp (str): The timestamp when the message was sent.
        reactions (list[Reaction]): A list of reactions to the message.
        file_info (list[FileInfo]): A list of file information associated with the message.
    """

    USER: ClassVar[str] = "User"
    ASSISTANT: ClassVar[str] = "Assistant"

    content: str = Field("", description="The content of the message.")
    author: str = Field("", description="The author of the message.")
    author_type: str = Field(
        USER, description="The type of the author (User or Assistant)."
    )
    timestamp: str = Field("", description="The timestamp when the message was sent.")
    reactions: list[Reaction] = Field(
        default_factory=list, description="A list of reactions to the message."
    )
    file_info: list[FileInfo] = Field(
        default_factory=list,
        description="A list of file information associated with the message.",
    )

    def to_simple_dict(self) -> dict:
        """
        Convert the message to a simple dictionary format.

        Returns:
            dict: A dictionary representation of the message with author type and content.
        """
        return {self.author_type: self.content}
