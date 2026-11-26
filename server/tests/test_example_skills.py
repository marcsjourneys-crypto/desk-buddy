"""The example skills in examples/skills stay valid with the real store, and (integration)
run with CARL's Python through the real SkillRunner."""

import html
import re
import shutil
from pathlib import Path

import pytest

from desk_buddy.skills_store import AVAILABLE, find_clashes, inspect_skill, parse_manifest
from desk_buddy.tools.skill_tools import input_schema, text_limit
from desk_buddy.tools.skills import SkillRunner, find_uv, has_project

EXAMPLES = Path(__file__).resolve().parents[2] / "examples" / "skills"
NAMES = ("hello-time", "send-note-email")


def manifest_of(slug):
    manifest, errors = parse_manifest((EXAMPLES / slug / "skill.json").read_bytes())
    assert errors == ()
    assert manifest is not None
    return manifest


@pytest.mark.parametrize("slug", NAMES)
def test_example_is_a_valid_skill_without_packages(slug):
    skill = inspect_skill(EXAMPLES / slug)
    assert skill.errors == ()
    assert skill.status == AVAILABLE
    assert skill.fingerprint is not None
    assert not has_project(skill.folder)  # runs with CARL's Python, no setup


def test_examples_dont_clash():
    skills = [inspect_skill(EXAMPLES / slug) for slug in NAMES]
    assert find_clashes(skills) == {}


def test_hello_time_manifest():
    m = manifest_of("hello-time")
    assert m.name == "Hello time"
    assert not m.needs_yes
    assert m.timeout_seconds == 10
    assert m.text is None
    [choice] = m.choices
    assert (choice.name, choice.options, choice.default) == ("format", ("short", "long"), "short")
    schema = input_schema(m)
    assert schema["properties"]["format"]["enum"] == ["short", "long"]
    assert schema["required"] == ["format"]


def test_send_note_email_manifest():
    m = manifest_of("send-note-email")
    assert m.name == "Send note email"
    assert m.needs_yes
    assert m.choices == ()
    assert m.text is not None
    assert (m.text.description, m.text.max_length, m.text.required) == (
        "the message to send",
        200,
        True,
    )
    assert text_limit(m) == 200
    assert input_schema(m)["required"] == ["text"]


def test_send_note_email_never_sends():
    source = (EXAMPLES / "send-note-email" / "main.py").read_text(encoding="utf-8")
    code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
    for word in ("smtplib", "urllib", "socket", "http", "subprocess"):
        assert word not in code


# --- running them for real ---


def result_body(result, slug):
    assert result.ok, result
    head, tail = f'<untrusted source="skill:{slug}">\n', "\n</untrusted>"
    assert result.text.startswith(head) and result.text.endswith(tail)
    return html.unescape(result.text[len(head) : -len(tail)])


@pytest.fixture
def runner_and_skills(tmp_path):
    uv = find_uv()
    if uv is None:
        pytest.skip("uv isn't installed")
    runner = SkillRunner(uv, tmp_path / "appdata")
    copies = {}
    for slug in NAMES:
        folder = tmp_path / "skills" / slug
        shutil.copytree(EXAMPLES / slug, folder)
        copies[slug] = inspect_skill(folder)
        assert copies[slug].valid
    return runner, copies


@pytest.mark.integration
def test_hello_time_runs(runner_and_skills):
    runner, skills = runner_and_skills
    short = result_body(runner.run(skills["hello-time"], {"format": "short"}), "hello-time")
    assert re.fullmatch(r"It's \d\d:\d\d \(the short format\)\.", short), short
    long = result_body(runner.run(skills["hello-time"], {"format": "long"}), "hello-time")
    assert re.fullmatch(r"It's \w+ \d\d \w+ \d{4}, \d\d:\d\d:\d\d \(the long format\)\.", long)


@pytest.mark.integration
def test_send_note_email_only_says_what_it_would_send(runner_and_skills):
    runner, skills = runner_and_skills
    result = runner.run(skills["send-note-email"], {"text": "I'm running late <b> & ☕"})
    assert result_body(result, "send-note-email") == "Would send: I'm running late <b> & ☕"
    empty = runner.run(skills["send-note-email"], {})
    assert not empty.ok
    assert empty.summary == "failed (exit 2)"
