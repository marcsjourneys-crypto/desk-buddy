"""The dashboard's settings file: precedence, validation with config's own rules, a
corrupt file, atomic saves, and live apply."""

import json
import os

import pytest

from desk_buddy.config import Settings
from desk_buddy.settings_store import EDITABLE, LiveSettings, SettingsStore

SECRET_KEY = "sk-ant-PLANTED-SECRET-0001"
SECRET_TOKEN = "robot-token-PLANTED-0002"
SECRET_SPOTIFY = "spotify-id-PLANTED-0003"


@pytest.fixture
def env(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        f"ANTHROPIC_API_KEY={SECRET_KEY}\nROBOT_TOKEN={SECRET_TOKEN}\n"
        f"SPOTIFY_CLIENT_ID={SECRET_SPOTIFY}\nMAX_SENTENCES=3\nTTS_ENGINE=sapi\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def store(tmp_path, env):
    return SettingsStore(tmp_path / "app" / "settings.json", env_path=env, environ={})


def entry(store, key):
    (found,) = [e for e in store.view() if e["key"] == key]
    return found


def test_the_view_shows_value_source_and_when_it_applies(store):
    assert entry(store, "MAX_SENTENCES") == {
        **entry(store, "MAX_SENTENCES"),
        "value": 3,
        "source": "env",
        "applies": "next question",
    }
    assert entry(store, "HISTORY_TURNS")["source"] == "default"
    assert entry(store, "PTT_KEY")["applies"] == "after restart"
    assert entry(store, "TTS_SPEED")["applies"] == "next sentence"
    assert entry(store, "COMPUTER_CONTROL")["value"] is False
    assert entry(store, "LLM_EFFORT")["choices"] == ["low", "medium", "high", "xhigh", "max"]
    assert [e["key"] for e in store.view()] == [e.key for e in EDITABLE]


def test_the_view_never_holds_a_secret(store):
    text = json.dumps(store.view())
    for secret in (SECRET_KEY, SECRET_TOKEN, SECRET_SPOTIFY):
        assert secret not in text


def test_an_update_wins_over_env_and_is_saved(store, tmp_path):
    settings, errors = store.update({"MAX_SENTENCES": 5, "COMPUTER_CONTROL": True})
    assert errors == {}
    assert (settings.max_sentences, settings.computer_control) == (5, True)
    assert entry(store, "MAX_SENTENCES")["source"] == "dashboard"
    saved = json.loads((tmp_path / "app" / "settings.json").read_text(encoding="utf-8"))
    assert saved == {"MAX_SENTENCES": "5", "COMPUTER_CONTROL": "on"}
    # A new store (a restart) reads it back.
    again = SettingsStore(tmp_path / "app" / "settings.json", env_path=store.env_path, environ={})
    assert again.settings().max_sentences == 5


def test_reset_goes_back_to_env_then_default(store):
    store.update({"MAX_SENTENCES": 5, "HISTORY_TURNS": 7})
    settings = store.reset("MAX_SENTENCES")
    assert settings.max_sentences == 3
    assert entry(store, "MAX_SENTENCES")["source"] == "env"
    store.reset("HISTORY_TURNS")
    assert entry(store, "HISTORY_TURNS") | {} == {
        **entry(store, "HISTORY_TURNS"),
        "value": 20,
        "source": "default",
    }


@pytest.mark.parametrize(
    ("changes", "bad"),
    [
        ({"MAX_SENTENCES": 0}, "MAX_SENTENCES"),
        ({"LLM_EFFORT": "turbo"}, "LLM_EFFORT"),
        ({"COMPUTER_CONTROL": "maybe"}, "COMPUTER_CONTROL"),
        ({"OBSIDIAN_VAULT_PATH": "Z:\\nowhere\\at\\all"}, "OBSIDIAN_VAULT_PATH"),
        ({"OBSIDIAN_DAILY_FOLDER": "..\\escape"}, "OBSIDIAN_DAILY_FOLDER"),
        ({"PTT_KEY": "banana"}, "PTT_KEY"),
        ({"ANTHROPIC_API_KEY": "x"}, "ANTHROPIC_API_KEY"),
        ({"SERVER_HOST": "0.0.0.0"}, "SERVER_HOST"),
        ({"MAX_SENTENCES": [1, 2]}, "MAX_SENTENCES"),
        ({"LLM_MODEL": "x" * 1000}, "LLM_MODEL"),
    ],
)
def test_invalid_changes_are_refused_per_field_and_nothing_is_saved(store, tmp_path, changes, bad):
    settings, errors = store.update({**changes, "HISTORY_TURNS": 9})
    assert settings is None
    assert list(errors) == [bad]
    assert not (tmp_path / "app" / "settings.json").exists()  # all or nothing
    assert entry(store, "HISTORY_TURNS")["source"] == "default"


def test_tts_speed_is_validated_for_piper(tmp_path):
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=k\nTTS_ENGINE=piper\n")
    store = SettingsStore(tmp_path / "s.json", env_path=env, environ={})
    assert "TTS_SPEED" in store.update({"TTS_SPEED": 3})[1]
    assert store.update({"TTS_SPEED": 1.25})[0].tts_speed == 1.25


def test_a_corrupt_file_is_ignored_with_a_banner(tmp_path, env):
    path = tmp_path / "settings.json"
    path.write_text("{not json", encoding="utf-8")
    store = SettingsStore(path, env_path=env, environ={})
    assert store.error and "settings.json" in store.error
    assert store.settings().max_sentences == 3
    store.update({"MAX_SENTENCES": 4})  # saving replaces the broken file
    assert json.loads(path.read_text(encoding="utf-8")) == {"MAX_SENTENCES": "4"}
    assert store.error is None


def test_saves_are_atomic(store, tmp_path, monkeypatch):
    store.update({"MAX_SENTENCES": 4})
    path = tmp_path / "app" / "settings.json"

    def broken_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", broken_replace)
    with pytest.raises(OSError):
        store.update({"MAX_SENTENCES": 6})
    assert json.loads(path.read_text(encoding="utf-8")) == {"MAX_SENTENCES": "4"}
    assert sorted(p.name for p in path.parent.iterdir()) == ["settings.json"]  # no temp left
    assert store.settings().max_sentences == 4  # the failed change didn't stick


def test_an_invalid_saved_value_never_blocks_other_edits(tmp_path, env):
    path = tmp_path / "settings.json"
    gone = tmp_path / "deleted-vault"
    path.write_text(
        json.dumps({"OBSIDIAN_VAULT_PATH": str(gone), "HISTORY_TURNS": "7"}), encoding="utf-8"
    )
    store = SettingsStore(path, env_path=env, environ={})
    vault = entry(store, "OBSIDIAN_VAULT_PATH")
    assert vault["source"] == "invalid"
    assert "not an existing folder" in vault["error"]
    assert vault["value"] == ""  # what CARL actually uses: the fallback
    assert store.settings().history_turns == 7  # the good saved value still counts
    settings, errors = store.update({"MAX_SENTENCES": 4})
    assert errors == {}
    assert settings.max_sentences == 4
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["OBSIDIAN_VAULT_PATH"] == str(gone)  # kept, so the user can see and fix it
    assert store.reset("HISTORY_TURNS").history_turns == 20
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    store.update({"OBSIDIAN_VAULT_PATH": str(vault_dir)})  # fixing it clears the mark
    assert entry(store, "OBSIDIAN_VAULT_PATH")["source"] == "dashboard"
    assert "error" not in entry(store, "OBSIDIAN_VAULT_PATH")


# --- live apply ---


def test_live_settings_apply_at_the_next_question():
    calls = []
    live = LiveSettings(Settings(anthropic_api_key="k"))
    live.on_question(
        lambda old, new: calls.append(("question", old.max_sentences, new.max_sentences))
    )
    live.on_now(lambda old, new: calls.append(("now", new.tts_speed)))
    live.propose(Settings(anthropic_api_key="k", max_sentences=4, tts_speed=1.5))
    assert calls == [("now", 1.5)]  # TTS speed: at once (the next sentence)
    assert live.current.max_sentences == 2  # turns: not yet
    live.apply()
    assert calls == [("now", 1.5), ("question", 2, 4)]
    assert live.current.max_sentences == 4
    live.apply()  # nothing pending: nothing runs
    assert len(calls) == 2


def test_a_failing_hook_never_raises_or_stops_the_others(caplog):
    seen = []
    live = LiveSettings(Settings(anthropic_api_key="k"))

    def broken(old, new):
        raise RuntimeError("no")

    live.on_question(broken)
    live.on_question(lambda old, new: seen.append(new.history_turns))
    live.propose(Settings(anthropic_api_key="k", history_turns=5))
    live.apply()
    assert seen == [5]
    assert "Live setting hook failed" in caplog.text
