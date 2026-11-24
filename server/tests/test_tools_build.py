from desk_buddy.config import Settings
from desk_buddy.tools import build_tools


def test_no_vault_means_no_tools():
    assert len(build_tools(Settings(anthropic_api_key="k"))) == 0


def test_vault_gives_note_tools(tmp_path):
    tools = build_tools(Settings(anthropic_api_key="k", obsidian_vault_path=str(tmp_path)))
    names = [s["name"] for s in tools.schemas()]
    assert names == ["take_note", "read_notes"]
    for schema in tools.schemas():
        inp = schema["input_schema"]
        assert inp["additionalProperties"] is False
        assert set(inp["required"]) == set(inp["properties"])


def test_take_note_tool_writes_into_the_vault(tmp_path):
    tools = build_tools(Settings(anthropic_api_key="k", obsidian_vault_path=str(tmp_path)))
    outcome = tools.run("take_note", {"text": "hello", "kind": "note", "title": ""})
    assert not outcome.is_error
    assert list((tmp_path / "Daily").glob("*.md"))
