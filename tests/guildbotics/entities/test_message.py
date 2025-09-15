import pathlib
import sys

# Add project root to sys.path for test execution
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3]))


from guildbotics.entities.message import FileInfo, Message, Reaction


def test_classvars_exist_and_values():
    # ClassVar values should be exposed as strings
    assert isinstance(Message.USER, str)
    assert isinstance(Message.ASSISTANT, str)
    assert Message.USER == "User"
    assert Message.ASSISTANT == "Assistant"


def test_to_simple_dict_user_and_assistant():
    # User authored message
    m_user = Message(content="hello", author="alice", author_type=Message.USER)
    d_user = m_user.to_simple_dict()
    assert d_user == {"User": "hello"}
    # Assistant authored message
    m_asst = Message(content="hi there", author="bot", author_type=Message.ASSISTANT)
    d_asst = m_asst.to_simple_dict()
    assert d_asst == {"Assistant": "hi there"}


def test_reaction_basic_construction():
    # Defaults
    r_default = Reaction()
    assert r_default.emoji == ""
    assert isinstance(r_default.users, list) and r_default.users == []
    # With values
    r = Reaction(emoji="👍", users=["u1", "u2"])
    assert r.emoji == "👍"
    assert r.users == ["u1", "u2"]


def test_fileinfo_basic_construction_and_defaults():
    # Minimum required fields
    f = FileInfo(name="report.pdf", size=1024, type="application/pdf")
    assert f.name == "report.pdf"
    assert f.size == 1024
    assert f.type == "application/pdf"
    assert f.url == ""
    assert f.local_path == ""

    # All fields provided
    f2 = FileInfo(
        name="image.png",
        size=2048,
        type="image/png",
        url="https://example.com/image.png",
        local_path="/tmp/image.png",
    )
    assert f2.url.endswith("image.png")
    assert f2.local_path.endswith("image.png")


def test_message_with_reactions_and_files():
    files = [FileInfo(name="a.txt", size=1, type="text/plain")]
    reacts = [Reaction(emoji="✨", users=["u1"])]
    m = Message(content="see file", author="alice", file_info=files, reactions=reacts)
    assert m.file_info == files
    assert m.reactions == reacts
