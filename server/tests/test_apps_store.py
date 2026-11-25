"""The app allowlist and bookmarks: Windows' list through an injectable runner (tests
never run PowerShell), validation, limits, IDs only from a listing, atomic saves and
corrupt files."""

import json
import logging
import subprocess

import pytest

import desk_buddy.apps_store as apps_store
from desk_buddy.apps_store import (
    LIST_COMMAND,
    MAX_APPS,
    MAX_BOOKMARKS,
    AppEntry,
    AppsStore,
    Bookmark,
    ListError,
    check_name,
    parse_listing,
)

NOTEPAD = {"Name": "Notepad", "AppID": "Microsoft.WindowsNotepad_8wekyb3d8bbwe!App"}
EDGE = {"Name": "Microsoft Edge", "AppID": "MSEdge"}
CALC = {"Name": "Calculator", "AppID": "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"}


def runner_for(*apps, raw=None):
    calls = []

    def run(args, timeout):
        calls.append((tuple(args), timeout))
        text = raw if raw is not None else json.dumps(list(apps))
        return text.encode("utf-8")

    run.calls = calls
    return run


def store(tmp_path, *apps, raw=None):
    return AppsStore(
        tmp_path / "apps.json", tmp_path / "bookmarks.json", runner=runner_for(*apps, raw=raw)
    )


# --- Windows' list ---


def test_the_listing_runs_the_fixed_command_through_the_runner(tmp_path):
    s = store(tmp_path, NOTEPAD, EDGE)
    listing = s.available()
    assert listing == [
        {"name": "Microsoft Edge", "app_id": "MSEdge"},
        {"name": "Notepad", "app_id": NOTEPAD["AppID"]},
    ]
    ((args, timeout),) = s._runner.calls
    assert args == (
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        "Get-StartApps | ConvertTo-Json -Compress",
    )
    assert args == LIST_COMMAND and 0 < timeout <= 60


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", []),
        ("  \r\n", []),
        ("[]", []),
        (json.dumps(NOTEPAD), [{"name": "Notepad", "app_id": NOTEPAD["AppID"]}]),  # one app
        ("\ufeff" + json.dumps([CALC]), [{"name": "Calculator", "app_id": CALC["AppID"]}]),
    ],
    ids=["empty", "blank", "none", "single-object", "bom"],
)
def test_the_listing_handles_odd_output(raw, expected):
    assert parse_listing(raw.encode("utf-8")) == expected


def test_the_listing_skips_bad_entries_and_repeats():
    raw = json.dumps(
        [
            NOTEPAD,
            NOTEPAD,
            {"Name": "No id"},
            {"AppID": "no-name"},
            {"Name": "Quote", "AppID": 'a"b'},
            {"Name": "Control", "AppID": "a\x01b"},
            {"Name": "Long", "AppID": "x" * 301},
            {"Name": "  Two\nlines  ", "AppID": "two"},
            "junk",
            None,
        ]
    )
    assert parse_listing(raw) == [
        {"name": "Notepad", "app_id": NOTEPAD["AppID"]},
        {"name": "Two lines", "app_id": "two"},
    ]


@pytest.mark.parametrize("raw", ["not json", "42", '"text"'])
def test_unreadable_output_is_a_list_error(raw):
    with pytest.raises(ListError):
        parse_listing(raw)


def test_output_in_the_console_code_page_still_reads():
    raw = json.dumps([{"Name": "Caf\u00e9", "AppID": "cafe"}], ensure_ascii=False)
    out = raw.encode("cp1252")  # not valid UTF-8
    (entry,) = parse_listing(out)
    assert entry["app_id"] == "cafe" and entry["name"].startswith("Caf")


def test_the_real_runner_uses_an_argument_list_and_a_timeout(monkeypatch):
    seen = {}

    def fake_run(args, **kwargs):
        seen["args"], seen["kwargs"] = args, kwargs
        return subprocess.CompletedProcess(args, 0, stdout=b"[]", stderr=b"")

    monkeypatch.setattr(apps_store.subprocess, "run", fake_run)
    assert apps_store.run_command(LIST_COMMAND, 7.0) == b"[]"
    assert seen["args"] == list(LIST_COMMAND)
    assert seen["kwargs"]["timeout"] == 7.0
    assert "shell" not in seen["kwargs"]


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (FileNotFoundError(), "wasn't found"),
        (subprocess.TimeoutExpired("powershell.exe", 1), "too long"),
        (PermissionError(), "PermissionError"),
        ("exit", "exit code 1"),
    ],
)
def test_the_real_runner_turns_failures_into_list_errors(monkeypatch, failure, message):
    def fake_run(args, **kwargs):
        if failure == "exit":
            return subprocess.CompletedProcess(args, 1, stdout=b"", stderr=b"boom")
        raise failure

    monkeypatch.setattr(apps_store.subprocess, "run", fake_run)
    with pytest.raises(ListError, match=message):
        apps_store.run_command(LIST_COMMAND, 1.0)


# --- spoken names ---


@pytest.mark.parametrize(
    ("raw", "name"),
    [
        ("Browser", "browser"),
        ("  my   Browser ", "my browser"),
        ("o'reilly", "o'reilly"),
        ("vs-code 2.0", "vs-code 2.0"),
        ("Café", "café"),
        ("x" * 40, "x" * 40),
    ],
)
def test_good_names_are_tidied_and_lowercased(raw, name):
    assert check_name(raw) == name


@pytest.mark.parametrize(
    "raw", [None, 3, "", "   ", "x" * 41, "a/b", "a_b", "a\u202eb", "<b>", "...", "a,b", "rm;x"]
)
def test_bad_names_are_refused(raw):
    with pytest.raises(ValueError):
        check_name(raw)


# --- the app allowlist ---


def test_apps_save_only_ids_from_a_listing(tmp_path):
    s = store(tmp_path, NOTEPAD, EDGE)
    errors = s.update_apps([{"app_id": NOTEPAD["AppID"], "names": ["Notepad"]}])
    assert errors == {"apps.0": "That app isn't in Windows' list: use Find apps."}
    assert not (tmp_path / "apps.json").exists()
    s.available()
    errors = s.update_apps(
        [
            {"app_id": NOTEPAD["AppID"], "names": ["Notepad", "Notes app"], "title": "Evil"},
            {"app_id": "invented!App", "names": ["thing"]},
        ]
    )
    assert errors == {"apps.1": "That app isn't in Windows' list: use Find apps."}
    assert s.update_apps([{"app_id": NOTEPAD["AppID"], "names": ["Notepad", "Notes app"]}]) == {}
    saved = json.loads((tmp_path / "apps.json").read_text(encoding="utf-8"))
    # The title comes from Windows' list, never from the page.
    assert saved == [
        {"app_id": NOTEPAD["AppID"], "title": "Notepad", "names": ["notepad", "notes app"]}
    ]
    _, apps, _ = s.snapshot()
    assert apps == (AppEntry(NOTEPAD["AppID"], "Notepad", ("notepad", "notes app")),)


def test_saved_apps_stay_allowed_without_a_new_listing(tmp_path):
    s = store(tmp_path, NOTEPAD)
    s.available()
    assert s.update_apps([{"app_id": NOTEPAD["AppID"], "names": ["notepad"]}]) == {}
    again = store(tmp_path)  # a restart: no listing yet
    assert again.update_apps([{"app_id": NOTEPAD["AppID"], "names": ["editor"]}]) == {}
    assert again.snapshot()[1][0].title == "Notepad"
    # But one that was removed needs a listing again.
    assert again.update_apps([]) == {}
    assert "apps.0" in again.update_apps([{"app_id": NOTEPAD["AppID"], "names": ["editor"]}])


@pytest.mark.parametrize(
    ("entry", "why"),
    [
        ("notepad", "needs an app_id"),
        ({"app_id": NOTEPAD["AppID"]}, "1 to 3"),
        ({"app_id": NOTEPAD["AppID"], "names": []}, "1 to 3"),
        ({"app_id": NOTEPAD["AppID"], "names": "notepad"}, "1 to 3"),
        ({"app_id": NOTEPAD["AppID"], "names": ["a", "b", "c", "d"]}, "At most 3"),
        ({"app_id": NOTEPAD["AppID"], "names": ["pad", "PAD"]}, "twice"),
        ({"app_id": NOTEPAD["AppID"], "names": ["no/slash"]}, "only have"),
        ({"app_id": 'a"b', "names": ["x"]}, "quotes"),
        ({"app_id": "x" * 301, "names": ["x"]}, "300"),
    ],
)
def test_bad_app_entries_are_refused(tmp_path, entry, why):
    s = store(tmp_path, NOTEPAD)
    s.available()
    errors = s.update_apps([entry])
    assert why in errors["apps.0"]


def test_names_are_unique_across_apps_and_apps_appear_once(tmp_path):
    s = store(tmp_path, NOTEPAD, EDGE)
    s.available()
    errors = s.update_apps(
        [
            {"app_id": NOTEPAD["AppID"], "names": ["editor"]},
            {"app_id": "MSEdge", "names": ["Editor"]},
        ]
    )
    assert set(errors) == {"apps.1"} and "already used" in errors["apps.1"]
    errors = s.update_apps(
        [{"app_id": "MSEdge", "names": ["browser"]}, {"app_id": "MSEdge", "names": ["web"]}]
    )
    assert "twice" in errors["apps.1"]
    assert s.snapshot()[1] == ()  # all or nothing


def test_at_most_fifty_apps(tmp_path):
    apps = [{"Name": f"App {i}", "AppID": f"app{i}"} for i in range(MAX_APPS + 1)]
    s = store(tmp_path, *apps)
    s.available()
    entries = [{"app_id": f"app{i}", "names": [f"app {i}"]} for i in range(MAX_APPS + 1)]
    assert s.update_apps(entries) == {"apps": "At most 50 apps."}
    assert s.update_apps(entries[:MAX_APPS]) == {}
    assert len(s.snapshot()[1]) == MAX_APPS
    assert s.update_apps("not a list") == {"apps": "Send a list of apps."}


def test_the_version_goes_up_only_when_a_list_changes(tmp_path):
    s = store(tmp_path, NOTEPAD)
    s.available()
    v0 = s.snapshot()[0]
    entry = [{"app_id": NOTEPAD["AppID"], "names": ["notepad"]}]
    s.update_apps(entry)
    v1 = s.snapshot()[0]
    assert v1 > v0
    s.update_apps(entry)
    assert s.snapshot()[0] == v1  # the same list again
    s.update_bookmarks([{"name": "weather", "url": "https://example.com/weather"}])
    assert s.snapshot()[0] > v1
    s.update_apps([{"app_id": NOTEPAD["AppID"], "names": ["bad/name"]}])
    assert s.snapshot()[0] == v1 + 1  # a refused change changes nothing


# --- bookmarks ---


def test_bookmarks_round_trip(tmp_path):
    s = store(tmp_path)
    assert (
        s.update_bookmarks(
            [
                {"name": "Weather", "url": " https://example.com/weather "},
                {"name": "news", "url": "http://news.example"},
            ]
        )
        == {}
    )
    assert s.snapshot()[2] == (
        Bookmark("weather", "https://example.com/weather"),
        Bookmark("news", "http://news.example"),
    )
    again = store(tmp_path)
    assert again.bookmarks_view()["bookmarks"] == [
        {"name": "weather", "url": "https://example.com/weather"},
        {"name": "news", "url": "http://news.example"},
    ]
    assert again.error is None


@pytest.mark.parametrize(
    "entry",
    [
        {"name": "x", "url": "file:///C:/Windows/notepad.exe"},
        {"name": "x", "url": "https://user:pw@example.com"},
        {"name": "x", "url": "javascript:alert(1)"},
        {"name": "x", "url": "https://example.com/" + "a" * 500},
        {"name": "x"},
        {"name": "bad/name", "url": "https://example.com"},
        "https://example.com",
    ],
)
def test_bad_bookmarks_are_refused(tmp_path, entry):
    s = store(tmp_path)
    errors = s.update_bookmarks([entry])
    assert set(errors) == {"bookmarks.0"}
    assert not (tmp_path / "bookmarks.json").exists()


def test_bookmark_names_are_unique_and_there_are_at_most_fifty(tmp_path):
    s = store(tmp_path)
    errors = s.update_bookmarks(
        [{"name": "news", "url": "https://a.example"}, {"name": "News", "url": "https://b.example"}]
    )
    assert "already used" in errors["bookmarks.1"]
    many = [{"name": f"site {i}", "url": f"https://{i}.example"} for i in range(MAX_BOOKMARKS + 1)]
    assert s.update_bookmarks(many) == {"bookmarks": "At most 50 bookmarks."}
    assert s.update_bookmarks(many[:MAX_BOOKMARKS]) == {}


# --- files ---


@pytest.mark.parametrize("name", ["apps.json", "bookmarks.json"])
@pytest.mark.parametrize("content", ["{not json", '{"a": 1}', "\x00\x01"])
def test_a_corrupt_file_is_a_warning_and_a_banner(tmp_path, caplog, name, content):
    caplog.set_level(logging.WARNING)
    (tmp_path / name).write_text(content, encoding="utf-8")
    s = store(tmp_path)
    assert s.error and name in s.error
    assert name in caplog.text
    assert s.snapshot()[1:] == ((), ())
    # Saving a good list replaces it and clears the banner.
    if name == "bookmarks.json":
        assert s.update_bookmarks([{"name": "a", "url": "https://a.example"}]) == {}
    else:
        assert s.update_apps([]) == {}
    assert s.error is None


def test_bad_entries_in_a_file_are_skipped(tmp_path, caplog):
    caplog.set_level(logging.WARNING)
    (tmp_path / "apps.json").write_text(
        json.dumps(
            [
                {"app_id": "good", "title": "Good", "names": ["good"]},
                {"app_id": 'bad"id', "title": "Bad", "names": ["bad"]},
                {"app_id": "dup", "title": "Dup", "names": ["good"]},
                "junk",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "bookmarks.json").write_text(
        json.dumps(
            [
                {"name": "ok", "url": "https://ok.example"},
                {"name": "evil", "url": "file:///C:/x.exe"},
            ]
        ),
        encoding="utf-8",
    )
    s = store(tmp_path)
    _, apps, bookmarks = s.snapshot()
    assert [a.app_id for a in apps] == ["good"]
    assert [b.name for b in bookmarks] == ["ok"]
    assert "app 2" in s.error and "bookmark 2" in s.error


def test_a_failed_save_keeps_the_old_list(tmp_path, monkeypatch):
    s = store(tmp_path)
    s.update_bookmarks([{"name": "a", "url": "https://a.example"}])

    def disk_full(path, data):
        raise OSError("disk full")

    monkeypatch.setattr(apps_store, "write_json_atomic", disk_full)
    with pytest.raises(OSError):
        s.update_bookmarks([{"name": "b", "url": "https://b.example"}])
    assert [b.name for b in s.snapshot()[2]] == ["a"]


def test_the_default_files_are_in_local_app_data(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    s = AppsStore(runner=runner_for())
    assert s.apps_file == tmp_path / "desk-buddy" / "apps.json"
    assert s.bookmarks_file == tmp_path / "desk-buddy" / "bookmarks.json"
