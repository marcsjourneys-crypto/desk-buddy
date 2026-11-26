"""Skills: the manifest rules, slugs, the fingerprint and its limits, the approvals file
(atomic saves, corrupt files, the limit of 20) and spoken-name clashes."""

import json
import logging
import os

import pytest

import desk_buddy.skills_store as skills_store
from desk_buddy.apps_store import AppEntry, Bookmark
from desk_buddy.skills_store import (
    APPROVED,
    AVAILABLE,
    CHANGED,
    INVALID,
    MAX_APPROVED,
    MAX_MANIFEST_BYTES,
    Choice,
    SkillsStore,
    TextInput,
    check_manifest,
    check_slug,
    find_clashes,
    fingerprint,
    inspect_skill,
    parse_manifest,
    taken_names,
    tool_name,
)


@pytest.fixture(autouse=True)
def _no_real_app_data(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))  # never the real one


def manifest(**changes):
    base = {
        "name": "Order check",
        "spoken_names": ["orders", "order check"],
        "description": "Checks this week's orders.",
    }
    base.update(changes)
    return {k: v for k, v in base.items() if v is not ...}


def make_skill(root, slug="order-check", data=None, main="print('hi')\n"):
    folder = root / slug
    folder.mkdir(parents=True)
    raw = data if isinstance(data, (bytes, str)) else json.dumps(data or manifest())
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    (folder / "skill.json").write_bytes(raw)
    if main is not None:
        (folder / "main.py").write_text(main, encoding="utf-8")
    return folder


def errors_of(raw):
    result, errors = check_manifest(raw)
    assert result is None
    return {e.field: e.reason for e in errors}


def ok(raw):
    result, errors = check_manifest(raw)
    assert errors == ()
    return result


# --- slugs and tool names ---


@pytest.mark.parametrize("slug", ["a", "order-check", "hello-time-2", "x" * 40, "42"])
def test_good_slugs(slug):
    assert check_slug(slug) == slug


@pytest.mark.parametrize(
    "slug", ["", "x" * 41, "Order", "order_check", "order check", "ordér", "a.b", None, 3]
)
def test_bad_slugs(slug):
    with pytest.raises(ValueError):
        check_slug(slug)


@pytest.mark.parametrize("slug", ["con", "prn", "aux", "nul", "com1", "com9", "lpt1", "lpt9"])
def test_windows_device_names_are_not_slugs(slug):
    with pytest.raises(ValueError, match="Windows"):
        check_slug(slug)
    assert check_slug(slug + "-x") == slug + "-x"


def test_tool_names_swap_dashes_for_underscores():
    assert tool_name("order-check") == "skill_order_check"
    assert tool_name("hello") == "skill_hello"


def test_a_badly_named_folder_is_invalid(tmp_path):
    s = inspect_skill(make_skill(tmp_path, "Order_Check"))
    assert s.status == INVALID
    assert [e.field for e in s.errors] == ["folder"]


# --- the manifest ---


def test_a_minimal_manifest_gets_the_defaults():
    m = ok(manifest())
    assert m.name == "Order check"
    assert m.spoken_names == ("orders", "order check")
    assert m.description == "Checks this week's orders."
    assert m.choices == () and m.text is None
    assert m.needs_yes is False and m.timeout_seconds == 30


def test_a_full_manifest():
    m = ok(
        manifest(
            spoken_names=["Send  Email"],
            inputs={
                "choices": {
                    "format": {"options": ["short", "long"], "default": "short"},
                    "who": {"options": ["team", "boss", "the board"], "description": "To whom"},
                },
                "text": {"description": "The message", "max_length": 200, "required": True},
            },
            needs_yes=True,
            timeout_seconds=120,
        )
    )
    assert m.spoken_names == ("send email",)  # lowercased, spaces tidied: as for apps
    assert m.choices == (
        Choice("format", ("short", "long"), "", "short"),
        Choice("who", ("team", "boss", "the board"), "To whom", None),
    )
    assert not m.choices[0].required and m.choices[1].required
    assert m.text == TextInput("The message", 200, True)
    assert m.needs_yes is True and m.timeout_seconds == 120


def test_text_input_defaults():
    m = ok(manifest(inputs={"text": {"description": "What to say"}}))
    assert m.text == TextInput("What to say", 300, False)


@pytest.mark.parametrize("key", ["name", "spoken_names", "description"])
def test_required_keys(key):
    assert key in errors_of(manifest(**{key: ...}))


def test_unknown_keys_are_errors_at_every_level():
    assert "colour" in errors_of(manifest(colour="red"))
    assert "inputs.files" in errors_of(manifest(inputs={"files": {}}))
    bad_choice = {"choices": {"fmt": {"options": ["a", "b"], "label": "x"}}}
    assert "inputs.choices.fmt.label" in errors_of(manifest(inputs=bad_choice))
    bad_text = {"text": {"description": "d", "placeholder": "x"}}
    assert "inputs.text.placeholder" in errors_of(manifest(inputs=bad_text))


def test_every_problem_is_reported_with_its_field():
    errors = errors_of(manifest(name="", description=5, needs_yes="yes", timeout_seconds=0))
    assert set(errors) == {"name", "description", "needs_yes", "timeout_seconds"}


@pytest.mark.parametrize(
    ("value", "good"),
    [
        ("x", True),
        ("x" * 40, True),
        ("  padded  ", True),
        ("x" * 41, False),
        ("", False),
        ("   ", False),
        ("two\nlines", False),
        ("tab\there", False),
        ("flip‮here", False),
        ("line sep", False),
        (5, False),
        (["x"], False),
    ],
)
def test_the_name_is_one_short_line(value, good):
    if good:
        assert ok(manifest(name=value)).name == value.strip()
    else:
        assert "name" in errors_of(manifest(name=value))


@pytest.mark.parametrize(
    ("value", "good"),
    [
        (["orders"], True),
        (["a", "b", "c"], True),
        (["a", "b", "c", "d"], False),
        ([], False),
        ("orders", False),
        (["orders", "Orders"], False),  # the same once lowercased
        (["order/check"], False),
        (["x" * 41], False),
        (["o'brien's list", "v1.2"], True),
    ],
)
def test_spoken_names_follow_the_app_rules(value, good):
    if good:
        ok(manifest(spoken_names=value))
    else:
        assert "spoken_names" in errors_of(manifest(spoken_names=value))


@pytest.mark.parametrize(
    ("value", "good"),
    [
        ("x", True),
        ("x" * 500, True),
        ("Line one.\nLine two.", True),
        ("x" * 501, False),
        ("", False),
        ("bell\x07", False),
        (None, False),
    ],
)
def test_the_description(value, good):
    raw = manifest(description=value) if value is not None else manifest(description=...)
    if good:
        assert ok(raw).description == value
    else:
        assert "description" in errors_of(raw)


def choices(**specs):
    return manifest(inputs={"choices": specs})


def test_at_most_four_choices():
    spec = {"options": ["a", "b"]}
    ok(choices(a=spec, b=spec, c=spec, d=spec))
    assert "inputs.choices" in errors_of(choices(a=spec, b=spec, c=spec, d=spec, e=spec))


@pytest.mark.parametrize("name", ["format", "a", "two_words", "x" * 20])
def test_good_choice_names(name):
    ok(choices(**{name: {"options": ["a", "b"]}}))


@pytest.mark.parametrize("name", ["Format", "with-dash", "x1", "x" * 21, "text"])
def test_bad_choice_names(name):
    assert f"inputs.choices.{name}" in errors_of(choices(**{name: {"options": ["a", "b"]}}))


@pytest.mark.parametrize(
    ("options", "good"),
    [
        (["a", "b"], True),
        ([str(i) for i in range(10)], True),
        (["the board", "x_y", "a-b", "x" * 40], True),
        (["only"], False),
        ([str(i) for i in range(11)], False),
        ("a,b", False),
    ],
)
def test_option_counts(options, good):
    raw = choices(pick={"options": options})
    if good:
        assert ok(raw).choices[0].options == tuple(options)
    else:
        assert "inputs.choices.pick.options" in errors_of(raw)


@pytest.mark.parametrize("bad", ["Long", "a.b", "", " lead", "trail ", "x" * 41, 3, "a", "é"])
def test_bad_options(bad):
    errors = errors_of(choices(pick={"options": ["ok", bad, "a"] if bad != "a" else ["a", "a"]}))
    assert "inputs.choices.pick.options.1" in errors


def test_a_choice_default_must_be_an_option():
    assert ok(choices(pick={"options": ["a", "b"], "default": "b"})).choices[0].default == "b"
    assert "inputs.choices.pick.default" in errors_of(
        choices(pick={"options": ["a", "b"], "default": "c"})
    )
    assert "inputs.choices.pick.default" in errors_of(
        choices(pick={"options": ["a", "b"], "default": None})
    )


def test_a_choice_description():
    spec = {"options": ["a", "b"], "description": "x" * 200}
    assert ok(choices(pick=spec)).choices[0].description == "x" * 200
    for bad in ["x" * 201, "", "two\nlines", 4]:
        spec = {"options": ["a", "b"], "description": bad}
        assert "inputs.choices.pick.description" in errors_of(choices(pick=spec))


def test_choices_and_specs_must_be_objects():
    assert "inputs" in errors_of(manifest(inputs=[]))
    assert "inputs.choices" in errors_of(manifest(inputs={"choices": ["a"]}))
    assert "inputs.choices.pick" in errors_of(choices(pick=["a", "b"]))
    assert "inputs.text" in errors_of(manifest(inputs={"text": "say"}))


@pytest.mark.parametrize(
    ("text", "field"),
    [
        ({}, "inputs.text.description"),
        ({"description": ""}, "inputs.text.description"),
        ({"description": "x" * 201}, "inputs.text.description"),
        ({"description": "d", "max_length": 0}, "inputs.text.max_length"),
        ({"description": "d", "max_length": 501}, "inputs.text.max_length"),
        ({"description": "d", "max_length": "300"}, "inputs.text.max_length"),
        ({"description": "d", "max_length": True}, "inputs.text.max_length"),
        ({"description": "d", "required": "no"}, "inputs.text.required"),
    ],
)
def test_text_input_rules(text, field):
    assert field in errors_of(manifest(inputs={"text": text}))


def test_text_input_limits_are_inclusive():
    assert ok(manifest(inputs={"text": {"description": "d", "max_length": 1}})).text.max_length == 1
    m = ok(manifest(inputs={"text": {"description": "x" * 200, "max_length": 500}}))
    assert m.text.max_length == 500


@pytest.mark.parametrize(("value", "good"), [(True, True), (False, True), (1, False), ("x", False)])
def test_needs_yes_is_a_boolean(value, good):
    if good:
        assert ok(manifest(needs_yes=value)).needs_yes is value
    else:
        assert "needs_yes" in errors_of(manifest(needs_yes=value))


@pytest.mark.parametrize(
    ("value", "good"),
    [(1, True), (120, True), (0, False), (121, False), (2.5, False), (True, False)],
)
def test_timeout_seconds(value, good):
    if good:
        assert ok(manifest(timeout_seconds=value)).timeout_seconds == value
    else:
        assert "timeout_seconds" in errors_of(manifest(timeout_seconds=value))


def test_the_manifest_file_itself():
    good = json.dumps(manifest()).encode("utf-8")
    assert parse_manifest(good)[1] == ()
    assert parse_manifest(b"\xef\xbb\xbf" + good)[1] == ()  # Notepad's BOM is fine
    for raw in [
        b"{not json",
        b"[1, 2]",
        "caf\xe9".encode("latin-1"),
        b'{"name": "a", "name": "b"}',
    ]:
        result, errors = parse_manifest(raw)
        assert result is None and [e.field for e in errors] == ["skill.json"]


@pytest.mark.parametrize("raw", [b"[" * 5000, b'{"a":' * 5000])
def test_deeply_nested_json_is_invalid_not_a_crash(tmp_path, raw):
    result, errors = parse_manifest(raw[:MAX_MANIFEST_BYTES])
    assert result is None and [e.field for e in errors] == ["skill.json"]
    root = tmp_path / "skills"
    make_skill(root, "deep", data=raw[:MAX_MANIFEST_BYTES])
    scan = SkillsStore(tmp_path / "skills.json").scan(root)
    assert scan.get("deep").status == INVALID


def test_the_manifest_is_at_most_16_kb():
    padded = manifest(description="x")
    raw = json.dumps(padded).encode("utf-8")
    exact = raw[:-1] + b" " * (MAX_MANIFEST_BYTES - len(raw)) + b"}"
    assert len(exact) == MAX_MANIFEST_BYTES and parse_manifest(exact)[1] == ()
    result, errors = parse_manifest(exact[:-1] + b" }")
    assert result is None and "16384" in errors[0].reason


def test_a_skill_needs_its_files(tmp_path):
    s = inspect_skill(make_skill(tmp_path, "no-main", main=None))
    assert s.status == INVALID and [e.field for e in s.errors] == ["main.py"]
    folder = tmp_path / "no-manifest"
    folder.mkdir()
    (folder / "main.py").write_text("")
    s = inspect_skill(folder)
    assert s.status == INVALID and [e.field for e in s.errors] == ["skill.json"]


def test_a_good_skill_is_available(tmp_path):
    s = inspect_skill(make_skill(tmp_path))
    assert s.status == AVAILABLE and s.valid and s.errors == ()
    assert s.tool_name == "skill_order_check"
    assert len(s.fingerprint) == 64


def test_a_bad_manifest_makes_the_skill_invalid(tmp_path):
    s = inspect_skill(make_skill(tmp_path, data=manifest(name="")))
    assert s.status == INVALID and s.manifest is None and not s.valid
    assert [e.field for e in s.errors] == ["name"]


# --- the fingerprint ---


def test_the_fingerprint_covers_each_file_but_not_env(tmp_path):
    folder = make_skill(tmp_path)
    (folder / "lib").mkdir()
    (folder / "lib" / "helper.py").write_text("x = 1\n")
    (folder / "pyproject.toml").write_text("[project]\n")
    (folder / "uv.lock").write_text("version = 1\n")
    (folder / ".env").write_text("TOKEN=one\n")
    before = fingerprint(folder)

    for rel in ["skill.json", "main.py", "lib/helper.py", "pyproject.toml", "uv.lock"]:
        path = folder / rel
        old = path.read_bytes()
        path.write_bytes(old + b" ")
        assert fingerprint(folder) != before, rel
        path.write_bytes(old)
        assert fingerprint(folder) == before

    (folder / ".env").write_text("TOKEN=two\n")
    assert fingerprint(folder) == before


@pytest.mark.parametrize(
    "rel",
    [
        "helper.pyw",
        "helper.pyd",
        "x.pyc",  # outside __pycache__, Python imports it
        "lib/y.pyc",
        "uv.toml",
        ".python-version",
        "data.json",
        "notes.txt",
        "lib/.hidden-file",
        "lib/.env",  # only the top-level .env is left out
    ],
)
def test_every_other_file_changes_the_fingerprint(tmp_path, rel):
    folder = make_skill(tmp_path)
    before = fingerprint(folder)
    path = folder / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"planted")
    assert fingerprint(folder) != before


def test_the_manifest_is_hashed_from_the_bytes_that_were_parsed(tmp_path):
    folder = make_skill(tmp_path)
    on_disk = (folder / "skill.json").read_bytes()
    assert fingerprint(folder, manifest_bytes=on_disk) == fingerprint(folder)
    other = json.dumps(manifest(name="Other")).encode("utf-8")
    assert fingerprint(folder, manifest_bytes=other) != fingerprint(folder)
    # inspect_skill reads skill.json once and hashes what it parsed
    reads = []
    real_open = type(folder).open

    def counting_open(self, *args, **kwargs):
        if self.name == "skill.json":
            reads.append(self)
        return real_open(self, *args, **kwargs)

    from unittest import mock

    with mock.patch.object(type(folder), "open", counting_open):
        s = inspect_skill(folder)
    assert s.fingerprint == fingerprint(folder) and len(reads) == 1


def test_new_or_moved_files_change_the_fingerprint(tmp_path):
    folder = make_skill(tmp_path)
    before = fingerprint(folder)
    (folder / "extra.py").write_text("")
    with_extra = fingerprint(folder)
    assert with_extra != before
    (folder / "pkg").mkdir()
    (folder / "extra.py").rename(folder / "pkg" / "extra.py")
    assert fingerprint(folder) not in (before, with_extra)
    (folder / "pyproject.toml").write_text("")  # an empty file still counts
    assert fingerprint(folder) != before


def test_the_fingerprint_skips_venv_pycache_and_dot_folders(tmp_path):
    folder = make_skill(tmp_path)
    before = fingerprint(folder)
    for sub in [".venv/Lib", "__pycache__", ".git", "pkg/__pycache__", "pkg/.cache"]:
        (folder / sub).mkdir(parents=True, exist_ok=True)
        (folder / sub / "mod.py").write_text("x = 1\n")
    (folder / "pkg" / "__pycache__" / "mod.cpython-312.pyc").write_bytes(b"\0")
    assert fingerprint(folder) == before


def test_the_fingerprint_is_stable_and_path_aware(tmp_path):
    a = make_skill(tmp_path / "one")
    b = make_skill(tmp_path / "two")
    assert fingerprint(a) == fingerprint(b)  # the same files in another place
    (a / "x.py").write_text("ab")
    (b / "xa.py").write_text("b")  # same bytes run together, different files
    assert fingerprint(a) != fingerprint(b)


def test_at_most_200_files(tmp_path):
    folder = make_skill(tmp_path)
    for i in range(198):  # with skill.json and main.py: 200
        (folder / f"m{i}.py").write_text("")
    assert inspect_skill(folder).status == AVAILABLE
    (folder / "one_more.py").write_text("")
    s = inspect_skill(folder)
    assert s.status == INVALID and s.fingerprint is None
    assert [e.field for e in s.errors] == ["files"]


def test_at_most_5_mb_in_total(tmp_path, monkeypatch):
    monkeypatch.setattr(skills_store, "MAX_TOTAL_BYTES", 1000)
    folder = make_skill(tmp_path)
    used = sum(len(p.read_bytes()) for p in folder.iterdir())
    (folder / "big.py").write_bytes(b"#" * (1000 - used))
    assert inspect_skill(folder).status == AVAILABLE
    (folder / ".env").write_bytes(b"x" * 5000)  # not covered, so not counted
    assert inspect_skill(folder).status == AVAILABLE
    (folder / "big.py").write_bytes(b"#" * (1001 - used))
    s = inspect_skill(folder)
    assert s.status == INVALID and [e.field for e in s.errors] == ["files"]


def test_a_too_large_file_is_never_read(tmp_path, monkeypatch):
    monkeypatch.setattr(skills_store, "MAX_TOTAL_BYTES", 1000)
    folder = make_skill(tmp_path)
    (folder / "huge.bin").write_bytes(b"x" * 2000)
    real_open = type(folder).open

    def guarded_open(self, *args, **kwargs):
        assert self.name != "huge.bin", "the oversized file was read"
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(type(folder), "open", guarded_open)
    with pytest.raises(ValueError, match="MB"):
        fingerprint(folder)
    assert [e.field for e in inspect_skill(folder).errors] == ["files"]


def test_the_real_size_limit_is_5_mb():
    assert skills_store.MAX_TOTAL_BYTES == 5 * 1024 * 1024
    assert skills_store.MAX_FILES == 200


# --- the store: approvals ---


def approvals_file(tmp_path):
    return tmp_path / "skills.json"


def test_the_default_file_is_in_local_app_data(tmp_path):
    s = SkillsStore()
    assert s.approvals_file == tmp_path / "appdata" / "desk-buddy" / "skills.json"


def test_approvals_round_trip(tmp_path):
    path = approvals_file(tmp_path)
    store = SkillsStore(path)
    assert store.approvals() == {} and store.error is None and not path.exists()
    store.approve("order-check", "a" * 64)
    store.approve("hello", "b" * 64)
    assert json.loads(path.read_text(encoding="utf-8")) == [
        {"slug": "order-check", "fingerprint": "a" * 64},
        {"slug": "hello", "fingerprint": "b" * 64},
    ]
    assert SkillsStore(path).approvals() == {"order-check": "a" * 64, "hello": "b" * 64}
    assert store.disable("hello") is True
    assert store.disable("hello") is False
    assert SkillsStore(path).approvals() == {"order-check": "a" * 64}


def test_the_version_goes_up_with_each_change(tmp_path):
    store = SkillsStore(approvals_file(tmp_path))
    store.approve("hello", "a" * 64)
    assert store.version == 1
    store.approve("hello", "a" * 64)  # no change
    assert store.version == 1
    store.approve("hello", "b" * 64)
    store.disable("hello")
    assert store.version == 3


@pytest.mark.parametrize(
    ("slug", "fp"),
    [("Bad", "a" * 64), ("ok", "A" * 64), ("ok", "a" * 63), ("ok", None), ("ok", "g" * 64)],
)
def test_approve_checks_its_input(tmp_path, slug, fp):
    store = SkillsStore(approvals_file(tmp_path))
    with pytest.raises(ValueError):
        store.approve(slug, fp)
    assert store.approvals() == {}


def test_at_most_20_skills_are_approved(tmp_path):
    store = SkillsStore(approvals_file(tmp_path))
    for i in range(MAX_APPROVED):
        store.approve(f"s{i}", "a" * 64)
    with pytest.raises(ValueError, match="20"):
        store.approve("one-more", "a" * 64)
    store.approve("s3", "b" * 64)  # re-approving one that's on is fine
    store.disable("s0")
    store.approve("one-more", "a" * 64)
    assert len(store.approvals()) == MAX_APPROVED


def test_saves_are_atomic(tmp_path, monkeypatch):
    path = approvals_file(tmp_path)
    store = SkillsStore(path)
    store.approve("hello", "a" * 64)

    def broken_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", broken_replace)
    with pytest.raises(OSError):
        store.approve("other", "b" * 64)
    assert store.approvals() == {"hello": "a" * 64}  # unchanged in memory
    assert json.loads(path.read_text(encoding="utf-8")) == [
        {"slug": "hello", "fingerprint": "a" * 64}
    ]
    assert [p.name for p in tmp_path.iterdir()] == ["skills.json"]  # no temp file left


@pytest.mark.parametrize(
    "content", ["{not json", '{"slug": "x"}', "\xff\xfe", "[" * 100000, '{"a":' * 100000]
)
def test_a_corrupt_file_is_ignored_with_a_banner(tmp_path, caplog, content):
    path = approvals_file(tmp_path)
    path.write_text(content, encoding="latin-1")
    with caplog.at_level(logging.WARNING):
        store = SkillsStore(path)
    assert store.approvals() == {}
    assert store.error and "skills.json" in store.error
    assert "skills.json" in caplog.text
    store.approve("hello", "a" * 64)  # saving a good list clears the banner
    assert store.error is None


def test_bad_entries_are_skipped(tmp_path, caplog):
    path = approvals_file(tmp_path)
    entries = [
        {"slug": "good", "fingerprint": "a" * 64},
        {"slug": "Bad", "fingerprint": "a" * 64},
        {"slug": "short", "fingerprint": "a"},
        "text",
        {"slug": "good", "fingerprint": "b" * 64},
    ]
    path.write_text(json.dumps(entries), encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        store = SkillsStore(path)
    assert store.approvals() == {"good": "a" * 64}
    assert "skill 2" in store.error and "skill 5" in store.error


def test_only_the_first_20_saved_approvals_count(tmp_path):
    path = approvals_file(tmp_path)
    entries = [{"slug": f"s{i}", "fingerprint": "a" * 64} for i in range(MAX_APPROVED + 2)]
    path.write_text(json.dumps(entries), encoding="utf-8")
    store = SkillsStore(path)
    assert list(store.approvals()) == [f"s{i}" for i in range(MAX_APPROVED)]
    assert "first 20" in store.error


# --- the store: scans and statuses ---


def test_a_blank_skills_dir_is_off(tmp_path):
    scan = SkillsStore(approvals_file(tmp_path)).scan("")
    assert scan.skills == () and scan.error is None and not scan.enabled
    assert SkillsStore(approvals_file(tmp_path)).scan(None).enabled is False


def test_a_missing_skills_dir_is_an_error(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        scan = SkillsStore(approvals_file(tmp_path)).scan(tmp_path / "nope")
    assert scan.skills == () and scan.enabled and "SKILLS_DIR" in scan.error
    assert "SKILLS_DIR" in caplog.text


def test_statuses(tmp_path):
    root = tmp_path / "skills"
    make_skill(root, "available")
    approved = make_skill(root, "approved", data=manifest(spoken_names=["two"]))
    changed = make_skill(root, "changed", data=manifest(spoken_names=["three"]))
    make_skill(root, "invalid", data=manifest(name=""))
    (root / ".hidden").mkdir()
    (root / "__pycache__").mkdir()
    (root / "loose-file.txt").write_text("")
    store = SkillsStore(approvals_file(tmp_path))
    store.approve("approved", fingerprint(approved))
    store.approve("changed", fingerprint(changed))
    (changed / "main.py").write_text("print('edited')\n")

    scan = store.scan(root)
    assert scan.error is None and scan.enabled
    statuses = {s.slug: (s.status, s.approved) for s in scan.skills}
    assert statuses == {
        "approved": (APPROVED, True),
        "available": (AVAILABLE, False),
        "changed": (CHANGED, True),
        "invalid": (INVALID, False),
    }
    assert [s.slug for s in scan.usable] == ["approved"]
    assert scan.get("invalid").errors[0].field == "name"
    assert scan.get("nope") is None


def test_an_approved_skill_that_breaks_is_invalid(tmp_path):
    root = tmp_path / "skills"
    folder = make_skill(root, "hello")
    store = SkillsStore(approvals_file(tmp_path))
    store.approve("hello", fingerprint(folder))
    (folder / "main.py").unlink()
    s = store.scan(root).get("hello")
    assert (s.status, s.approved) == (INVALID, True)


def test_check_gives_one_skill_as_it_is_now(tmp_path):
    root = tmp_path / "skills"
    folder = make_skill(root, "hello")
    store = SkillsStore(approvals_file(tmp_path))
    assert store.check(root, "hello").status == AVAILABLE
    store.approve("hello", fingerprint(folder))
    assert store.check(root, "hello").status == APPROVED
    (folder / "main.py").write_text("print('edited')\n")
    assert store.check(root, "hello").status == CHANGED
    assert store.check(root, "missing") is None
    assert store.check(root, "../hello") is None


def test_scan_warnings_are_logged_once(tmp_path, caplog):
    root = tmp_path / "skills"
    make_skill(root, "broken", data=manifest(name=""))
    store = SkillsStore(approvals_file(tmp_path))
    with caplog.at_level(logging.WARNING):
        store.scan(root)
        store.scan(root)
    assert caplog.text.count("broken") == 1
    assert store.scan(root).problems == ("broken: name: This can't be empty (1 to 40 characters).",)


def test_a_problem_that_comes_back_is_logged_again(tmp_path, caplog):
    root = tmp_path / "skills"
    folder = make_skill(root, "flaky")
    store = SkillsStore(approvals_file(tmp_path))
    with caplog.at_level(logging.WARNING):
        (folder / "main.py").unlink()
        store.scan(root)
        (folder / "main.py").write_text("")
        store.scan(root)  # fixed
        (folder / "main.py").unlink()
        store.scan(root)  # broken again
    assert caplog.text.count("flaky: main.py") == 2


# --- name clashes ---


def test_spoken_names_must_be_unique_across_skills(tmp_path):
    root = tmp_path / "skills"
    make_skill(root, "one", data=manifest(spoken_names=["orders", "first"]))
    make_skill(root, "two", data=manifest(spoken_names=["second", "orders"]))
    make_skill(root, "three", data=manifest(spoken_names=["third"]))
    scan = SkillsStore(approvals_file(tmp_path)).scan(root)
    statuses = {s.slug: s.status for s in scan.skills}
    assert statuses == {"one": INVALID, "two": INVALID, "three": AVAILABLE}
    (error,) = scan.get("one").errors
    assert error.field == "spoken_names" and "'two'" in error.reason


def test_spoken_names_must_not_clash_with_apps_or_bookmarks(tmp_path):
    root = tmp_path / "skills"
    make_skill(root, "notes", data=manifest(spoken_names=["notepad"]))
    make_skill(root, "news", data=manifest(spoken_names=["bbc"]))
    make_skill(root, "fine", data=manifest(spoken_names=["orders"]))
    names = taken_names(
        [AppEntry("Microsoft.WindowsNotepad!App", "Notepad", ("notepad", "editor"))],
        [Bookmark("bbc", "https://www.bbc.co.uk/")],
    )
    assert names == {"notepad", "editor", "bbc"}
    store = SkillsStore(approvals_file(tmp_path))
    store.approve("notes", fingerprint(root / "notes"))
    scan = store.scan(root, names)
    assert {s.slug: s.status for s in scan.skills} == {
        "notes": INVALID,  # even an approved one is no longer offered
        "news": INVALID,
        "fine": AVAILABLE,
    }
    assert "app or bookmark" in scan.get("news").errors[0].reason


def test_find_clashes_works_on_its_own(tmp_path):
    a = inspect_skill(make_skill(tmp_path, "a", data=manifest(spoken_names=["x", "y"])))
    b = inspect_skill(make_skill(tmp_path, "b", data=manifest(spoken_names=["z"])))
    assert find_clashes([a, b]) == {}
    clashes = find_clashes([a, b], ["y", "z"])
    assert set(clashes) == {"a", "b"}


def test_a_broken_skill_cant_take_a_name_from_a_good_one(tmp_path):
    root = tmp_path / "skills"
    make_skill(root, "good", data=manifest(spoken_names=["orders"]))
    make_skill(root, "broken", data=manifest(spoken_names=["orders"]), main=None)
    scan = SkillsStore(approvals_file(tmp_path)).scan(root)
    assert scan.get("good").status == AVAILABLE
    assert [e.field for e in scan.get("broken").errors] == ["main.py"]


# --- links ---


def _junction(link, target):
    """A directory junction, as Windows' mklink /J makes (no admin rights needed)."""
    _winapi = pytest.importorskip("_winapi")
    _winapi.CreateJunction(str(target), str(link))


def _symlink_dir(link, target):
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError as exc:  # Windows without Developer Mode or admin rights
        pytest.skip(f"symlinks aren't allowed here ({exc})")


@pytest.mark.parametrize("make_link", [_symlink_dir, _junction])
def test_a_linked_subfolder_makes_the_skill_invalid(tmp_path, make_link):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "evil.py").write_text("print('unhashed')\n")
    folder = make_skill(tmp_path / "skills")
    make_link(folder / "lib", outside)
    s = inspect_skill(folder)
    assert s.status == INVALID and s.fingerprint is None
    assert [(e.field, e.reason) for e in s.errors] == [
        ("files", "Links aren't allowed in a skill folder.")
    ]


@pytest.mark.parametrize("make_link", [_symlink_dir, _junction])
def test_a_link_in_a_skipped_folder_is_refused_too(tmp_path, make_link):
    outside = tmp_path / "outside"
    outside.mkdir()
    folder = make_skill(tmp_path / "skills")
    make_link(folder / ".venv", outside)
    assert inspect_skill(folder).status == INVALID


def test_a_linked_skill_folder_is_invalid(tmp_path):
    real = make_skill(tmp_path / "elsewhere", "hello")
    root = tmp_path / "skills"
    root.mkdir()
    _junction(root / "hello", real)
    s = SkillsStore(approvals_file(tmp_path)).scan(root).get("hello")
    assert s.status == INVALID and s.errors[0].reason == "Links aren't allowed in a skill folder."


def test_a_symlinked_file_makes_the_skill_invalid(tmp_path):
    folder = make_skill(tmp_path / "skills")
    target = tmp_path / "secret.py"
    target.write_text("")
    try:
        os.symlink(target, folder / "linked.py")
    except OSError as exc:
        pytest.skip(f"symlinks aren't allowed here ({exc})")
    assert inspect_skill(folder).status == INVALID
