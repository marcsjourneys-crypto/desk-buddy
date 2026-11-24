import logging

import pytest

from desk_buddy.tools.registry import Tool, ToolError, ToolOutcome, ToolRegistry

SCHEMA = {
    "type": "object",
    "properties": {"word": {"type": "string"}},
    "required": ["word"],
    "additionalProperties": False,
}


def echo(word: str) -> str:
    return f"echo {word}"


def fails(word: str) -> str:
    raise ToolError("no such word")


def crashes(word: str) -> str:
    raise RuntimeError("disk on fire")


def registry(*funcs):
    return ToolRegistry(Tool(f.__name__, "desc", SCHEMA, f) for f in funcs)


def test_schemas_are_strict_api_tool_definitions():
    assert registry(echo).schemas() == [
        {"name": "echo", "description": "desc", "input_schema": SCHEMA, "strict": True}
    ]


def test_run_calls_the_function_with_its_arguments():
    outcome = registry(echo).run("echo", {"word": "hi"})
    assert (outcome.text, outcome.is_error) == ("echo hi", False)


def test_tool_error_message_goes_back_to_claude():
    outcome = registry(fails).run("fails", {"word": "x"})
    assert (outcome.text, outcome.is_error) == ("no such word", True)


def test_unexpected_exception_is_an_error_result_not_a_crash(caplog):
    caplog.set_level(logging.ERROR)
    outcome = registry(crashes).run("crashes", {"word": "x"})
    assert outcome.is_error
    assert "disk on fire" not in outcome.text  # internals stay in the log
    assert "disk on fire" in caplog.text


def test_unknown_tool_is_an_error_result():
    outcome = registry(echo).run("nope", {})
    assert outcome.is_error


def test_bad_arguments_are_an_error_result():
    assert registry(echo).run("echo", {"wrong": 1}).is_error


def test_duplicate_names_are_rejected():
    with pytest.raises(ValueError):
        registry(echo, echo)


def test_empty_registry_is_falsy():
    assert not ToolRegistry()
    assert registry(echo)


def test_every_call_is_logged(caplog):
    caplog.set_level(logging.INFO, logger="desk_buddy.tools.registry")
    registry(echo).run("echo", {"word": "hi"})
    assert "Tool echo -> echo hi" in caplog.messages


def test_type_error_inside_a_tool_is_a_crash_not_bad_arguments(caplog):
    def buggy(word: str) -> str:
        return word + 1  # TypeError from the tool's own code

    caplog.set_level(logging.ERROR)
    outcome = registry(buggy).run("buggy", {"word": "x"})
    assert outcome == ToolOutcome("The tool failed unexpectedly.", is_error=True)
    assert "crashed" in caplog.text
