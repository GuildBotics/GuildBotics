from pydantic import BaseModel

from guildbotics.commands.utils import stringify_output


class _SampleModel(BaseModel):
    value: str


def test_stringify_output_handles_model_and_primitives():
    model_output = _SampleModel(value="ok")
    assert "value: ok" in stringify_output(model_output)
    assert stringify_output({"a": 1}) == "a: 1"
    assert stringify_output(["foo", "bar"]) == "foo\nbar"
