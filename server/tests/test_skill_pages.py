"""Skills that hand back pages (`"pages": true`): the runner's strict JSON output, the
checks on every page (public http(s) addresses only, at most 10, clean titles), the pages
joining this question's search results (directly and after a yes), the numbered note at
the next question, open_search_result opening one by number after a spoken yes, and no
text or address on the Live tab or in the skill's log lines. Fake processes and openers
only: nothing is run or opened for real."""

import asyncio
import json
import logging

import pytest
from test_loop import FakeChat, FakeTranscriber, Opener, answer, talk
from test_skill_runner import PY, UV, FakePopen, FakeProc, Killer
from test_skill_tools import HELLO, FakeRunner, Rig, asked

from desk_buddy.skills_store import Manifest, Skill
from desk_buddy.tools.confirm import QUESTION
from desk_buddy.tools.launch import LaunchTools
from desk_buddy.tools.registry import ToolError, ToolRegistry
from desk_buddy.tools.skill_tools import SkillTools
from desk_buddy.tools.skills import (
    BAD_OUTPUT,
    DONE,
    MAX_PAGE_TITLE,
    PAGES_READ_LIMIT,
    SkillResult,
    SkillRunner,
    check_pages,
)
from desk_buddy.tools.untrusted import wrap
from desk_buddy.tools.urls import SearchResults

A1 = "https://news.example.com/world/a1"
A2 = "https://www.robots.example.org/story/a2?id=7"
A3 = "http://ai.example.net/a3"
BRIEF = {
    "name": "Morning brief",
    "spoken_names": ["morning brief"],
    "description": "Today's weather and headlines. Use it when the user asks for the news.",
    "pages": True,
}
READING = {
    "name": "Reading list",
    "spoken_names": ["reading list"],
    "description": "Saves and lists the reading list.",
    "needs_yes": True,
    "pages": True,
}


@pytest.fixture(autouse=True)
def _no_real_app_data(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))  # never the real one


@pytest.fixture(autouse=True)
def question_one():
    token = QUESTION.set(1)
    yield
    QUESTION.reset(token)


def output(text="SECRET-TEXT: World: 1. Calm seas.", pages=None, **extra) -> bytes:
    pages = (
        [{"title": "Calm seas", "url": A1}, {"title": "Robot wins", "url": A2}]
        if pages is None
        else pages
    )
    return json.dumps({"text": text, "pages": pages, **extra}).encode("utf-8")


def pages_skill(folder, pages=True) -> Skill:
    folder.mkdir(parents=True, exist_ok=True)
    manifest = Manifest(
        name="Morning brief",
        spoken_names=("morning brief",),
        description="Headlines.",
        pages=pages,
    )
    return Skill("morning-brief", folder, manifest, "0" * 64, status="approved", approved=True)


def run(tmp_path, out, pages=True):
    popen = FakePopen(FakeProc(out=out))
    runner = SkillRunner(UV, tmp_path / "appdata", python=PY, popen=popen, killer=Killer())
    return runner.run(pages_skill(tmp_path / "skills" / "morning-brief", pages), {})


# --- The runner: the JSON output ---


def test_text_is_wrapped_like_plain_output_and_pages_come_back_checked(tmp_path):
    result = run(tmp_path, output())
    assert result.ok and result.summary == "ok"
    assert result.text == wrap("skill:morning-brief", "SECRET-TEXT: World: 1. Calm seas.")
    assert result.pages == (
        ("Calm seas", "news.example.com", A1),
        ("Robot wins", "robots.example.org", A2),
    )


def test_empty_text_is_done_and_pages_may_be_empty(tmp_path):
    result = run(tmp_path, output(text="  \r\n", pages=[]))
    assert result.ok and result.text == DONE and result.pages == ()


def test_long_text_is_capped_and_marked_as_for_plain_output(tmp_path):
    result = run(tmp_path, output(text="x" * 5000))
    assert result.ok and result.text.endswith("(and more)\n</untrusted>")
    assert len(result.text) <= 1000 + len(wrap("skill:morning-brief", ""))


def test_json_past_the_plain_16_kb_read_is_still_read(tmp_path):
    long_pages = [{"title": "t" * 2000, "url": f"{A1}?n={i}"} for i in range(10)]
    out = output(pages=long_pages)
    assert 16_000 < len(out) <= PAGES_READ_LIMIT
    result = run(tmp_path, out)
    assert result.ok and len(result.pages) == 10
    assert all(len(title) == MAX_PAGE_TITLE for title, _, _ in result.pages)


@pytest.mark.parametrize(
    ("out", "reason"),
    [
        (b"", "not valid JSON"),
        (b"SECRET-TEXT plain words", "not valid JSON"),
        (b'["SECRET-TEXT"]', "not a JSON object"),
        (b'{"text": "SECRET-TEXT"}', "exactly the keys"),
        (output(extra="SECRET-TEXT"), "exactly the keys"),
        (json.dumps({"text": 5, "pages": []}).encode(), '"text" is not a string'),
        (json.dumps({"text": "SECRET-TEXT", "pages": {}}).encode(), '"pages" is not a list'),
        (output(pages=["SECRET-TEXT"]), "each page needs"),
        (output(pages=[{"title": "t", "url": A1, "x": 1}]), "each page needs"),
        (output(pages=[{"title": "t"}]), "each page needs"),
        (output(pages=[{"title": 3, "url": A1}]), "must be strings"),
        (output(pages=[{"title": "t", "url": None}]), "must be strings"),
        (b"[" * 100_000, "over"),
        (output(text="x" * PAGES_READ_LIMIT), "over"),
        (output() + b" {}", "not valid JSON"),
    ],
)
def test_bad_shapes_fail_with_bad_output_and_log_only_a_reason(tmp_path, caplog, out, reason):
    caplog.set_level(logging.DEBUG)
    result = run(tmp_path, out)
    assert not result.ok and result.pages == ()
    assert result.summary == BAD_OUTPUT == "failed (bad output)"
    assert result.text == "The skill failed (bad output)."
    assert any(reason in m and "bad output" in m for m in caplog.messages)
    assert not any("SECRET-TEXT" in m or A1 in m for m in caplog.messages)


def test_deeply_nested_json_is_bad_output(tmp_path):
    result = run(tmp_path, b'{"text": "", "pages": ' + b"[" * 20_000 + b"]" * 20_000 + b"}")
    assert not result.ok and result.summary == BAD_OUTPUT


def test_a_bom_before_the_json_is_fine(tmp_path):
    assert run(tmp_path, b"\xef\xbb\xbf" + output()).ok


@pytest.mark.parametrize(
    "url",
    [
        "file:///C:/Windows/win.ini",
        "javascript:alert(1)",
        "ftp://files.example.com/x",
        "http://localhost:8766/api/apps",
        "http://127.0.0.1/",
        "http://127.1/",
        "http://192.168.1.1/admin",
        "http://10.0.0.5/",
        "http://[::1]/",
        "http://169.254.169.254/latest/meta-data",
        "http://printer/",
        "http://nas.local/",
        "https://user:pw@example.com/",
        "https://example.com/a b",
        "https://example.com/\u202eevil",
        "https://example.com\\@evil.com/",
        "",
        "https://example.com/" + "x" * 500,
    ],
)
def test_local_private_and_bad_addresses_are_dropped_and_never_logged(tmp_path, caplog, url):
    caplog.set_level(logging.DEBUG)
    result = run(tmp_path, output(pages=[{"title": "Bad", "url": url}, {"title": "Ok", "url": A1}]))
    assert result.ok and result.pages == (("Ok", "news.example.com", A1),)
    assert "Skills: morning-brief dropped 1 pages" in caplog.messages
    assert not any(A1 in m or (url and url in m) for m in caplog.messages)


def test_at_most_10_pages_one_per_address_in_order(tmp_path, caplog):
    caplog.set_level(logging.WARNING)
    pages = [{"title": f"Story {i}", "url": f"{A1}?n={i}"} for i in range(14)]
    pages.insert(2, {"title": "Again", "url": f"{A1}?n=0"})
    result = run(tmp_path, output(pages=pages))
    assert [title for title, _, _ in result.pages] == [f"Story {i}" for i in range(10)]
    assert "Skills: morning-brief dropped 5 pages" in caplog.messages


def test_titles_are_cleaned_like_search_titles_and_cut_to_120():
    pages, dropped = check_pages(
        [
            {"title": "Line\none\u2028two\u202e flipped\t", "url": A1},
            {"title": "   ", "url": A2},
            {"title": "y" * 300, "url": A3},
        ]
    )
    assert dropped == 0
    assert [title for title, _, _ in pages] == [
        "Line one two flipped",
        "Untitled",
        "y" * MAX_PAGE_TITLE,
    ]
    assert [site for _, site, _ in pages] == [
        "news.example.com",
        "robots.example.org",
        "ai.example.net",
    ]


def test_a_skill_without_pages_is_unchanged(tmp_path):
    out = output()
    result = run(tmp_path, out, pages=False)
    assert result.ok and result.pages == ()
    assert result.text == wrap("skill:morning-brief", out.decode())  # the JSON, as text
    # ...and plain text from it is fine, as before.
    assert run(tmp_path, b"It's four.", pages=False).text == wrap(
        "skill:morning-brief", "It's four."
    )


def test_a_failing_pages_skill_still_fails_by_its_exit_code(tmp_path):
    popen = FakePopen(FakeProc(out=output(), code=2))
    runner = SkillRunner(UV, tmp_path / "appdata", python=PY, popen=popen, killer=Killer())
    result = runner.run(pages_skill(tmp_path / "s" / "morning-brief"), {})
    assert not result.ok and result.summary == "failed (exit 2)" and result.pages == ()


# --- The skill tools: into this question's results ---


class PagesRig(Rig):
    """The skill rig with SearchResults wired in as __main__ wires them, a pages skill and
    a needs_yes pages skill, and open_search_result with a fake opener."""

    def __init__(self, root, runner=None):
        self.results = SearchResults()
        super().__init__(root, runner=runner)
        self.add("morning-brief", BRIEF)
        self.add("reading-list", READING)
        self.build()
        self.opener = Opener()
        self.launch = LaunchTools(
            self.guard, results=self.results, confirmations=self.confirmations, opener=self.opener
        )

    def build(self):
        scan = self.store.scan(self.root)
        self.tools = SkillTools(
            self.guard,
            scan.usable,
            store=self.store,
            skills_dir=self.root,
            runner=self.runner,
            confirmations=self.confirmations,
            search_results=self.results,
        )
        self.registry = ToolRegistry(self.tools.tools())
        return self.tools

    def orchestrator(self, **overrides):
        overrides.setdefault("search_results", self.results)
        return super().orchestrator(**overrides)


PAGES = (("Calm seas", "news.example.com", A1), ("Robot wins", "robots.example.org", A2))
BRIEF_RESULT = SkillResult(True, wrap("skill:morning-brief", "SECRET-TEXT"), "ok", PAGES)


@pytest.fixture
def prig(tmp_path):
    return PagesRig(tmp_path, runner=FakeRunner(result=BRIEF_RESULT))


def test_pages_join_this_questions_results_after_its_searches(prig):
    search = [("Search hit", "bbc.co.uk", "https://www.bbc.co.uk/s"), ("Dup", "x", A2)]
    prig.results.add(search)  # a web search earlier in this question
    prig.call("skill_morning_brief")
    assert prig.results.resolve(A1) == A1  # this question's: by exact address
    prig.results.new_question()
    note = prig.results.note()
    assert "1. Search hit (bbc.co.uk)\n2. Dup (x)\n3. Calm seas (news.example.com)\n</" in note
    assert prig.results.resolve("3") == A1 and prig.results.resolve("4") is None


def test_a_search_later_in_the_question_comes_after_the_pages_and_the_list_is_capped(prig):
    prig.call("skill_morning_brief")
    later = [(f"Hit {i}", "bbc.co.uk", f"https://www.bbc.co.uk/{i}") for i in range(12)]
    prig.results.add([("Again", "x", A1), *later])
    prig.results.new_question()
    note = prig.results.note()
    assert note.count("\n") == 11  # ten pages, plus the block's own two lines
    assert "1. Calm seas (news.example.com)\n2. Robot wins (robots.example.org)\n3. Hit 0" in note
    assert "Again" not in note and "Hit 8" not in note
    assert prig.results.resolve("1") == A1 and prig.results.resolve("10")


def test_the_numbered_note_at_the_next_question_has_titles_but_no_addresses(prig):
    prig.call("skill_morning_brief")
    assert prig.results.note() is None  # not in this question
    prig.results.new_question()
    note = prig.results.note()
    assert note.startswith(
        "(System note: pages from your last search or skill, for open_search_result by "
        'number: <untrusted source="web_search_results">\n1. Calm seas (news.example.com)\n'
        "2. Robot wins (robots.example.org)\n</untrusted>)"
    )
    assert "https://" not in note and "/world/" not in note


def test_the_live_tab_and_log_say_ran_with_the_links_never_text_or_addresses(prig, caplog):
    caplog.set_level(logging.DEBUG)
    result = prig.call("skill_morning_brief")
    assert "SECRET-TEXT" in result  # Claude gets the text, not the addresses
    assert A1 not in result and A2 not in result
    (event,) = prig.bus.of("action")
    assert event["outcome"] == "ok" and event["result"] == "Ran Morning brief (2 links)."
    shown = json.dumps(prig.bus.of("action")) + "\n".join(caplog.messages)
    for secret in ("SECRET-TEXT", A1, A2, "news.example.com", "Calm seas"):
        assert secret not in shown
    assert "Action: skill_morning_brief" in shown and "Ran Morning brief (2 links)." in shown


def test_one_link_is_said_in_the_singular(tmp_path):
    one = SkillResult(True, "x", "ok", PAGES[:1])
    rig = PagesRig(tmp_path, runner=FakeRunner(result=one))
    rig.call("skill_morning_brief")
    assert rig.bus.of("action")[-1]["result"] == "Ran Morning brief (1 link)."


def test_a_skill_without_pages_adds_nothing_and_still_says_ran(prig):
    prig.runner.by_slug["hello-time"] = SkillResult(True, wrap("skill:x", "4pm"), "ok")
    prig.call("skill_hello_time", format="short", zone="utc")
    assert prig.bus.of("action")[-1]["result"] == "Ran Hello time."
    prig.results.new_question()
    assert prig.results.note() is None
    assert HELLO.get("pages") is None


def test_a_failed_run_adds_no_pages(prig):
    prig.runner.result = SkillResult(False, "The skill failed (bad output).", BAD_OUTPUT)
    with pytest.raises(ToolError, match="bad output"):
        prig.call("skill_morning_brief")
    prig.results.new_question()
    assert prig.results.note() is None
    assert prig.bus.of("action")[-1]["outcome"] == "failed"


def test_without_search_results_the_pages_are_simply_not_kept(tmp_path):
    rig = Rig(tmp_path, runner=FakeRunner(result=BRIEF_RESULT))
    rig.add("morning-brief", BRIEF)
    rig.build()
    assert "SECRET-TEXT" in rig.call("skill_morning_brief")
    assert rig.bus.of("action")[-1]["result"] == "Ran Morning brief."


def test_a_confirmed_runs_pages_belong_to_the_question_that_said_yes(prig):
    prig.call("skill_reading_list")  # question 1: waits for a yes
    prig.results.new_question()  # question 2 ("Yes.") starts
    asked(prig.confirmations)
    note = prig.confirmations.answer("Yes.", 2)
    assert note.startswith("(System note: the server asked and the user said yes; it ran")
    assert A1 not in note
    assert prig.bus.of("action")[-1]["result"] == "Ran Reading list (2 links)."
    assert prig.results.resolve(A1) == A1  # question 2's own results
    assert prig.results.note() is None  # question 1 listed nothing
    prig.results.new_question()  # question 3: now numbered
    assert prig.results.resolve("2") == A2


# --- End to end: the loop, the yes and the opener ---


class CallingChat(FakeChat):
    """Calls a tool from a tool thread (as ClaudeChat does), then replies."""

    def __init__(self, call, **kwargs):
        super().__init__(**kwargs)
        self.call = call

    async def stream_reply(self, history, text):
        await asyncio.to_thread(self.call)
        async for d in super().stream_reply(history, text):
            yield d


def real_runner(tmp_path, out):
    popen = FakePopen(FakeProc(out=out))
    return SkillRunner(UV, tmp_path / "appdata", python=PY, popen=popen, killer=Killer())


def test_open_number_2_from_a_skills_pages_opens_it_after_a_yes(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    rig = PagesRig(tmp_path, runner=real_runner(tmp_path, output()))
    brief = CallingChat(
        lambda: rig.call("skill_morning_brief"), deltas=("[happy] Calm seas, and a robot. ",)
    )
    orch, parts, _ = rig.orchestrator(chat=brief, transcriber=FakeTranscriber("Morning brief."))
    opening = CallingChat(lambda: rig.launch.open_search_result("2"), deltas=("[curious] Sure. ",))
    done = FakeChat(deltas=("[happy] There you go. ",))

    async def scenario():
        await talk(orch)
        await answer(orch, "Open number 2.", opening)
        await answer(orch, "Yes.", done)

    asyncio.run(scenario())
    assert brief.prompts == ["Morning brief."]  # no list yet
    (q2,) = opening.prompts
    assert q2.startswith(
        "Open number 2. (System note: pages from your last search or skill, for "
        'open_search_result by number: <untrusted source="web_search_results">\n'
        "1. Calm seas (news.example.com)\n2. Robot wins (robots.example.org)\n</untrusted>)"
    )
    assert A2 not in q2
    assert parts["speaker"].played == [
        "Calm seas, and a robot.",
        "Sure.",
        "Shall I open the page on robots.example.org?",
        "There you go.",
    ]
    assert rig.opener.calls == [A2]  # once, after the yes
    assert rig.outcomes() == ["ok", "waiting", "ok"]
    assert done.prompts[0].startswith("Yes. (System note: the server asked and the user said yes")
    skill_lines = [m for m in caplog.messages if "skill_" in m or "morning-brief" in m]
    assert skill_lines and not any(
        s in m for m in skill_lines for s in ("SECRET-TEXT", A1, A2, "Calm seas")
    )


def test_a_needs_yes_skills_pages_open_by_number_at_the_question_after(tmp_path):
    rig = PagesRig(tmp_path, runner=real_runner(tmp_path, output(text="Two saved pages.")))
    proposing = CallingChat(lambda: rig.call("skill_reading_list"), deltas=("[happy] Okay. ",))
    orch, parts, _ = rig.orchestrator(chat=proposing, transcriber=FakeTranscriber("Reading list"))
    after_yes = FakeChat(deltas=("[happy] Two pages saved. ",))
    opening = CallingChat(lambda: rig.launch.open_search_result("1"), deltas=("[curious] Sure. ",))
    done = FakeChat(deltas=("[happy] There you go. ",))

    async def scenario():
        await talk(orch)
        await answer(orch, "Yes.", after_yes)
        await answer(orch, "Open the first one.", opening)
        await answer(orch, "Yes.", done)

    asyncio.run(scenario())
    (q2,) = after_yes.prompts
    assert "it ran Reading list. Result: " in q2 and "Two saved pages." in q2
    assert "pages from your last search" not in q2  # question 1 listed nothing
    (q3,) = opening.prompts
    assert "1. Calm seas (news.example.com)\n2. Robot wins" in q3
    assert parts["speaker"].played == [
        "Okay.",
        "Shall I run Reading list?",
        "Two pages saved.",
        "Sure.",
        "Shall I open the page on news.example.com?",
        "There you go.",
    ]
    assert rig.opener.calls == [A1]
    assert rig.outcomes() == ["waiting", "ok", "waiting", "ok"]
