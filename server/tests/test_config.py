from pathlib import Path

import pytest

from desk_buddy.config import ENV_KEYS, ConfigError, Settings, load_settings, parse_env_file

ENV_EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"


def test_parse_env_file_handles_comments_blanks_and_quotes():
    text = '# comment\n\nA=1\nB = two \nC="quoted value"\nD=\nnot a pair\n'
    assert parse_env_file(text) == {"A": "1", "B": "two", "C": "quoted value", "D": ""}


def test_defaults_apply_when_only_api_key_is_set():
    s = load_settings(env_path=None, environ={"ANTHROPIC_API_KEY": "k"})
    assert s == Settings(anthropic_api_key="k")
    assert s.llm_provider == "anthropic"
    assert s.llm_model == "claude-haiku-5-5"
    assert s.llm_effort == "low"
    assert s.stt_model == "base.en"
    assert s.stt_device == "cpu"
    assert s.stt_compute_type == "int8"
    assert s.tts_engine == "sapi"
    assert s.max_sentences == 2
    assert s.history_turns == 20
    assert s.ptt_key == "right_ctrl"


def test_env_file_values_are_used(tmp_path):
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=file-key\nSTT_MODEL=small.en\nMAX_SENTENCES=3\n")
    s = load_settings(env_path=env, environ={})
    assert (s.anthropic_api_key, s.stt_model, s.max_sentences) == ("file-key", "small.en", 3)


def test_blank_values_fall_back_to_defaults(tmp_path):
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=k\nSTT_MODEL=\nLLM_MODEL=   \n")
    s = load_settings(env_path=env, environ={})
    assert s.stt_model == "base.en"
    assert s.llm_model == "claude-haiku-5-5"


def test_process_environment_overrides_env_file(tmp_path):
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=file-key\nSTT_MODEL=small.en\n")
    s = load_settings(env_path=env, environ={"STT_MODEL": "tiny.en"})
    assert s.stt_model == "tiny.en"
    assert s.anthropic_api_key == "file-key"


def test_missing_env_file_is_fine(tmp_path):
    s = load_settings(env_path=tmp_path / "nope.env", environ={"ANTHROPIC_API_KEY": "k"})
    assert s.anthropic_api_key == "k"


def test_missing_api_key_is_a_clear_error():
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        load_settings(env_path=None, environ={})


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("LLM_PROVIDER", "kimi"),
        ("MAX_SENTENCES", "zero"),
        ("MAX_SENTENCES", "0"),
        ("HISTORY_TURNS", "0"),
        ("HISTORY_TURNS", "lots"),
        ("LLM_EFFORT", "turbo"),
        ("STT_DEVICE", "gpu"),
        ("TTS_ENGINE", "piper"),
        ("PTT_KEY", "banana"),
    ],
)
def test_invalid_values_are_rejected(key, value):
    with pytest.raises(ConfigError, match=key):
        load_settings(env_path=None, environ={"ANTHROPIC_API_KEY": "k", key: value})


def test_api_key_is_hidden_from_repr():
    s = load_settings(env_path=None, environ={"ANTHROPIC_API_KEY": "sk-secret"})
    assert "sk-secret" not in repr(s)


def test_every_settings_key_is_in_env_example():
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    listed = set(parse_env_file(text))
    assert set(ENV_KEYS) <= listed


def test_utf8_bom_is_ignored(tmp_path):
    env = tmp_path / ".env"
    env.write_bytes(b"\xef\xbb\xbfANTHROPIC_API_KEY=k\n")
    s = load_settings(env_path=env, environ={})
    assert s.anthropic_api_key == "k"


def test_parse_env_file_strips_export_prefix():
    assert parse_env_file("export A=1\n") == {"A": "1"}


def test_parse_env_file_cuts_inline_comments_on_unquoted_values():
    assert parse_env_file("B=3  # note\n") == {"B": "3"}


def test_parse_env_file_keeps_hash_inside_quotes():
    assert parse_env_file('C="x # y"\n') == {"C": "x # y"}


def test_parse_env_file_ignores_empty_keys():
    assert parse_env_file("=v\n") == {}


def test_blank_process_env_value_does_not_override_env_file(tmp_path):
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=k\nSTT_MODEL=small.en\n")
    s = load_settings(env_path=env, environ={"STT_MODEL": ""})
    assert s.stt_model == "small.en"


def test_whitespace_only_api_key_is_missing():
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        load_settings(env_path=None, environ={"ANTHROPIC_API_KEY": "   "})


def test_config_error_does_not_leak_api_key():
    with pytest.raises(ConfigError) as excinfo:
        load_settings(
            env_path=None, environ={"ANTHROPIC_API_KEY": "sk-secret", "MAX_SENTENCES": "zero"}
        )
    assert "sk-secret" not in str(excinfo.value)


def test_effort_is_case_insensitive():
    s = load_settings(env_path=None, environ={"ANTHROPIC_API_KEY": "k", "LLM_EFFORT": "HIGH"})
    assert s.llm_effort == "high"


def test_choice_error_shows_the_value_as_typed():
    with pytest.raises(ConfigError, match="'GPU'"):
        load_settings(env_path=None, environ={"ANTHROPIC_API_KEY": "k", "STT_DEVICE": "GPU"})


def test_history_turns_is_read(tmp_path):
    s = load_settings(env_path=None, environ={"ANTHROPIC_API_KEY": "k", "HISTORY_TURNS": "5"})
    assert s.history_turns == 5
