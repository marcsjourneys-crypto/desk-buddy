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
    assert s.tts_engine == "piper"
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
        ("TTS_ENGINE", "espeak"),
        ("PTT_KEY", "banana"),
        ("TTS_SPEED", "fast"),
        ("TTS_SPEED", "0.4"),
        ("TTS_SPEED", "2.5"),
        ("TTS_SPEED", "nan"),
        ("TTS_VOICE", "northern"),
        ("TTS_VOICE", "en_GB-northern_english_male"),
        ("TTS_VOICE", "../../x-y-medium"),
        ("TTS_VOICE", "en_GB-a/b-medium"),
        ("TTS_VOICE", "en_GB-a\\b-medium"),
        ("TTS_VOICE", "en_GB-..-medium"),
        ("TTS_VOICE", "en_GB-é-medium"),
    ],
)
def test_invalid_values_are_rejected(key, value):
    with pytest.raises(ConfigError, match=key):
        load_settings(env_path=None, environ={"ANTHROPIC_API_KEY": "k", key: value})


def test_dashboard_values_win_over_env_and_defaults(tmp_path):
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=k\nMAX_SENTENCES=3\nLLM_EFFORT=high\n")
    s = load_settings(
        env_path=env, environ={}, dashboard={"MAX_SENTENCES": "5", "COMPUTER_CONTROL": "on"}
    )
    assert (s.max_sentences, s.computer_control, s.llm_effort) == (5, True, "high")


def test_a_blank_dashboard_value_means_the_default_even_over_env(tmp_path):
    env = tmp_path / ".env"
    vault = tmp_path / "vault"
    vault.mkdir()
    env.write_text(f"ANTHROPIC_API_KEY=k\nOBSIDIAN_VAULT_PATH={vault}\nMAX_SENTENCES=3\n")
    s = load_settings(
        env_path=env, environ={}, dashboard={"OBSIDIAN_VAULT_PATH": "", "MAX_SENTENCES": ""}
    )
    assert s.obsidian_vault_path == ""  # notes turned off from the dashboard
    assert s.max_sentences == 2


def test_dashboard_can_never_set_secrets_or_hosts(tmp_path):
    s = load_settings(
        env_path=None,
        environ={"ANTHROPIC_API_KEY": "k"},
        dashboard={
            "ANTHROPIC_API_KEY": "evil",
            "ROBOT_TOKEN": "x" * 20,
            "SPOTIFY_CLIENT_ID": "evil",
            "SERVER_HOST": "0.0.0.0",
            "SERVER_PORT": "1",
        },
    )
    assert s.anthropic_api_key == "k"
    assert (s.robot_token, s.spotify_client_id, s.server_host, s.server_port) == (
        "",
        "",
        "127.0.0.1",
        8765,
    )


def test_the_real_startup_reads_the_dashboard_file_and_skips_bad_values(
    monkeypatch, tmp_path, caplog
):
    import json

    import desk_buddy.config as config

    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=k\n")
    monkeypatch.setattr(config, "DEFAULT_ENV_PATH", env)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.delenv("MAX_SENTENCES", raising=False)
    settings_file = tmp_path / "desk-buddy" / "settings.json"
    settings_file.parent.mkdir()
    gone = tmp_path / "deleted-vault"
    settings_file.write_text(
        json.dumps({"MAX_SENTENCES": "4", "OBSIDIAN_VAULT_PATH": str(gone)}), encoding="utf-8"
    )
    s = config.load_settings(env_path=env)
    assert s.max_sentences == 4  # the good value is used
    assert s.obsidian_vault_path == ""  # the bad one is skipped, never fatal
    assert "OBSIDIAN_VAULT_PATH" in caplog.text


def test_tests_and_tools_never_read_the_dashboard_file(monkeypatch, tmp_path):
    import json

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    (tmp_path / "desk-buddy").mkdir()
    (tmp_path / "desk-buddy" / "settings.json").write_text(json.dumps({"MAX_SENTENCES": "9"}))
    s = load_settings(env_path=None, environ={"ANTHROPIC_API_KEY": "k"})
    assert s.max_sentences == 2


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("MAX_SENTENCES", "11"),
        ("HISTORY_TURNS", "51"),
        ("STT_COMPUTE_TYPE", "int4"),
        ("STT_MODEL", "../../evil"),
        ("STT_MODEL", "base en"),
        ("LLM_MODEL", "claude haiku"),
        ("LLM_MODEL", "x/y"),
    ],
)
def test_more_values_are_checked_so_startup_cant_be_broken(key, value):
    with pytest.raises(ConfigError, match=key):
        load_settings(env_path=None, environ={"ANTHROPIC_API_KEY": "k", key: value})


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("MAX_SENTENCES", "10"),
        ("HISTORY_TURNS", "50"),
        ("STT_COMPUTE_TYPE", "float16"),
        ("STT_MODEL", "small.en"),
        ("STT_MODEL", "Systran/faster-whisper-large-v3"),
        ("LLM_MODEL", "claude-sonnet-5-5"),
        ("LLM_MODEL", "us.anthropic.claude-haiku-5-5:0"),
    ],
)
def test_good_values_still_pass(key, value):
    load_settings(env_path=None, environ={"ANTHROPIC_API_KEY": "k", key: value})


def test_a_vault_cant_be_a_drive_root_or_the_home_folder(monkeypatch, tmp_path):
    from pathlib import Path

    for bad in (Path(tmp_path.anchor), Path.home()):
        with pytest.raises(ConfigError, match="OBSIDIAN_VAULT_PATH"):
            load_settings(
                env_path=None, environ={"ANTHROPIC_API_KEY": "k", "OBSIDIAN_VAULT_PATH": str(bad)}
            )


def test_dashboard_port():
    assert load_settings(env_path=None, environ={"ANTHROPIC_API_KEY": "k"}).dashboard_port == 8766
    s = load_settings(env_path=None, environ={"ANTHROPIC_API_KEY": "k", "DASHBOARD_PORT": "9000"})
    assert s.dashboard_port == 9000
    with pytest.raises(ConfigError, match="DASHBOARD_PORT"):
        load_settings(env_path=None, environ={"ANTHROPIC_API_KEY": "k", "DASHBOARD_PORT": "0"})


def test_computer_control_defaults():
    s = load_settings(env_path=None, environ={"ANTHROPIC_API_KEY": "k"})
    assert s.computer_control is False
    assert s.spotify_client_id == ""
    assert s.music_duck is True


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("on", True), ("ON", True), ("true", True), ("off", False), ("False", False)],
)
def test_on_off_switches_are_read(raw, expected):
    s = load_settings(
        env_path=None,
        environ={"ANTHROPIC_API_KEY": "k", "COMPUTER_CONTROL": raw, "MUSIC_DUCK": raw},
    )
    assert (s.computer_control, s.music_duck) == (expected, expected)


@pytest.mark.parametrize("key", ["COMPUTER_CONTROL", "MUSIC_DUCK"])
def test_a_bad_switch_value_is_a_clear_error(key):
    with pytest.raises(ConfigError, match=f"{key} must be on or off"):
        load_settings(env_path=None, environ={"ANTHROPIC_API_KEY": "k", key: "maybe"})


def test_spotify_client_id_is_read():
    s = load_settings(
        env_path=None, environ={"ANTHROPIC_API_KEY": "k", "SPOTIFY_CLIENT_ID": "abc123"}
    )
    assert s.spotify_client_id == "abc123"


def test_voice_defaults():
    s = load_settings(env_path=None, environ={"ANTHROPIC_API_KEY": "k"})
    assert s.tts_voice == "en_GB-northern_english_male-medium"
    assert s.tts_speed == 1.0
    assert Path(s.voices_dir).parts[-2:] == ("desk-buddy", "voices")


def test_default_voices_dir_is_under_localappdata(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    s = load_settings(env_path=None, environ={"ANTHROPIC_API_KEY": "k"})
    assert Path(s.voices_dir) == tmp_path / "desk-buddy" / "voices"


def test_voice_settings_are_read(tmp_path):
    s = load_settings(
        env_path=None,
        environ={
            "ANTHROPIC_API_KEY": "k",
            "TTS_ENGINE": "Piper",
            "TTS_VOICE": "en_US-lessac-medium",
            "TTS_SPEED": "1.25",
            "VOICES_DIR": str(tmp_path),
        },
    )
    assert (s.tts_engine, s.tts_voice, s.tts_speed) == ("piper", "en_US-lessac-medium", 1.25)
    assert s.voices_dir == str(tmp_path)


def test_sapi_ignores_the_piper_settings_so_it_always_works_as_the_fallback():
    s = load_settings(
        env_path=None,
        environ={
            "ANTHROPIC_API_KEY": "k",
            "TTS_ENGINE": "sapi",
            "TTS_VOICE": "../not a voice",
            "TTS_SPEED": "warp 9",
        },
    )
    assert s.tts_engine == "sapi"
    assert (s.tts_voice, s.tts_speed) == (Settings.tts_voice, Settings.tts_speed)


def test_tts_voice_may_be_a_path_to_an_onnx_file(tmp_path):
    model = tmp_path / "my voice.onnx"
    model.write_bytes(b"x")
    s = load_settings(env_path=None, environ={"ANTHROPIC_API_KEY": "k", "TTS_VOICE": str(model)})
    assert s.tts_voice == str(model)


def test_a_missing_onnx_path_is_a_clear_error(tmp_path):
    with pytest.raises(ConfigError, match="TTS_VOICE"):
        load_settings(
            env_path=None,
            environ={"ANTHROPIC_API_KEY": "k", "TTS_VOICE": str(tmp_path / "nope.onnx")},
        )


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


def test_obsidian_defaults():
    s = load_settings(env_path=None, environ={"ANTHROPIC_API_KEY": "k"})
    assert s.obsidian_vault_path == ""
    assert s.obsidian_daily_folder == "Daily"
    assert s.obsidian_ideas_folder == "Ideas"


def test_obsidian_vault_path_is_read(tmp_path):
    s = load_settings(
        env_path=None,
        environ={"ANTHROPIC_API_KEY": "k", "OBSIDIAN_VAULT_PATH": str(tmp_path)},
    )
    assert s.obsidian_vault_path == str(tmp_path)


def test_missing_vault_folder_is_a_clear_error(tmp_path):
    with pytest.raises(ConfigError, match="OBSIDIAN_VAULT_PATH"):
        load_settings(
            env_path=None,
            environ={"ANTHROPIC_API_KEY": "k", "OBSIDIAN_VAULT_PATH": str(tmp_path / "nope")},
        )


@pytest.mark.parametrize("key", ["OBSIDIAN_DAILY_FOLDER", "OBSIDIAN_IDEAS_FOLDER"])
@pytest.mark.parametrize(
    "value", ["../outside", "C:\\abs", "/abs", "a/../../b", "C:abs", "\\\\server\\share"]
)
def test_note_folders_must_stay_inside_the_vault(key, value):
    with pytest.raises(ConfigError, match=key):
        load_settings(env_path=None, environ={"ANTHROPIC_API_KEY": "k", key: value})


def test_nested_note_folder_is_allowed():
    s = load_settings(
        env_path=None, environ={"ANTHROPIC_API_KEY": "k", "OBSIDIAN_DAILY_FOLDER": "Buddy/Daily"}
    )
    assert s.obsidian_daily_folder == "Buddy/Daily"


def test_robot_link_defaults():
    s = load_settings(env_path=None, environ={"ANTHROPIC_API_KEY": "k"})
    assert s.robot_token == ""
    assert s.server_host == "127.0.0.1"
    assert s.server_port == 8765


def test_robot_token_must_be_long_enough():
    with pytest.raises(ConfigError, match="ROBOT_TOKEN"):
        load_settings(env_path=None, environ={"ANTHROPIC_API_KEY": "k", "ROBOT_TOKEN": "short"})


def test_robot_token_error_does_not_leak_the_token():
    with pytest.raises(ConfigError) as excinfo:
        load_settings(env_path=None, environ={"ANTHROPIC_API_KEY": "k", "ROBOT_TOKEN": "s3cret"})
    assert "s3cret" not in str(excinfo.value)


def test_robot_token_is_not_in_repr():
    s = load_settings(env_path=None, environ={"ANTHROPIC_API_KEY": "k", "ROBOT_TOKEN": "x" * 20})
    assert s.robot_token == "x" * 20
    assert "x" * 20 not in repr(s)


def test_server_host_and_port_are_read():
    s = load_settings(
        env_path=None,
        environ={"ANTHROPIC_API_KEY": "k", "SERVER_HOST": "0.0.0.0", "SERVER_PORT": "9000"},
    )
    assert (s.server_host, s.server_port) == ("0.0.0.0", 9000)


@pytest.mark.parametrize("port", ["0", "70000", "abc"])
def test_server_port_must_be_valid(port):
    with pytest.raises(ConfigError, match="SERVER_PORT"):
        load_settings(env_path=None, environ={"ANTHROPIC_API_KEY": "k", "SERVER_PORT": port})
