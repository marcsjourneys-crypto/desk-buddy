"""The spoken "yes" (tools.confirm) and open_search_result: only an exact address from this
or the last question's search results, asked about by the server, and run only on a yes
at the very next question."""

import logging

import pytest

from desk_buddy.tools.actions import LIMIT_MESSAGE, OFF_MESSAGE, ActionGuard
from desk_buddy.tools.confirm import (
    EXPIRY_S,
    QUESTION,
    Confirmations,
    PendingAction,
    is_yes,
)
from desk_buddy.tools.launch import LOCAL_PAGE, NOT_A_RESULT, LaunchTools
from desk_buddy.tools.registry import NotDone, Tool, ToolError, ToolRegistry
from desk_buddy.tools.urls import SearchResults, is_public_host, page_description, site_name

PAGE = "https://www.bbc.co.uk/weather/2644688"
OTHER = "https://www.metoffice.gov.uk/weather/leeds"


class RecordingBus:
    def __init__(self):
        self.events = []

    def publish(self, type_, **fields):
        self.events.append({"type": type_, **fields})

    def outcomes(self):
        return [e["outcome"] for e in self.events if e["type"] == "action"]


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Opener:
    def __init__(self, fail=None):
        self.calls, self.fail = [], fail

    def __call__(self, url):
        self.calls.append(url)
        if self.fail is not None:
            raise self.fail


def make(urls=(PAGE, OTHER), *, enabled=True, opener=None, **guard_kwargs):
    bus, clock = RecordingBus(), Clock()
    guard = ActionGuard(enabled, bus=bus, **guard_kwargs)
    confirmations = Confirmations(bus=bus, clock=clock)
    results = SearchResults()
    results.add(urls)
    opener = opener or Opener()
    tools = LaunchTools(guard, results=results, confirmations=confirmations, opener=opener)
    return tools, confirmations, results, opener, bus, clock, guard


@pytest.fixture(autouse=True)
def question_one():
    """Tool calls in these tests belong to question 1 (the loop sets this per turn)."""
    token = QUESTION.set(1)
    yield
    QUESTION.reset(token)


def asked(confirmations, question=1):
    """The server asked about the pending action at `question`, to the end."""
    action = confirmations.to_ask(question)
    assert action is not None
    assert confirmations.arm(action, question)
    return action


def propose(confirmations, run=lambda: "Opened it.", description="the page on bbc.co.uk", q=1):
    token = QUESTION.set(q)
    try:
        confirmations.propose(
            PendingAction(
                "open_search_result", '{"url": "x"}', '{"site": "bbc.co.uk"}', description, run
            )
        )
    finally:
        QUESTION.reset(token)


# --- The yes check ---


@pytest.mark.parametrize(
    "text",
    [
        "Yes.",
        "yeah",
        "Yep, please.",
        "Sure!",
        "OK",
        "Okay.",
        "Go ahead.",
        "Do it.",
        "Yes, open it.",
        "Please do.",
        "Yes please",
        "Yeah go ahead Carl",
        "Okay, open it",
        "Oh, alright, yes, thank you.",
    ],
)
def test_short_answers_of_only_yes_words_and_fillers_are_yes(text):
    assert is_yes(text)


@pytest.mark.parametrize(
    "text",
    [
        "No.",
        "Nope.",
        "No, never mind.",
        "Yes but no.",
        "Yes, no, wait.",
        "Yeah, nah.",
        "Okay, never mind.",
        "Sure, hang on.",
        "Okay, what's the time?",
        "Okay then.",
        "Don't.",
        "Don’t open it.",
        "dont do it",
        "Not now.",
        "Stop.",
        "Cancel that.",
        "Wait, yes.",
        "",
        "What's the weather like in Leeds?",
        "yes yes yes yes yes yes yes",  # more than 6 words
    ],
)
def test_anything_else_is_not_yes(text):
    assert not is_yes(text)


def test_a_yes_phrase_alone_is_yes_but_fillers_alone_are_not():
    assert is_yes("open it please")
    assert not is_yes("please")
    assert not is_yes("it")
    assert not is_yes("thank you")


# --- Confirmations on their own ---


def test_nothing_waiting_means_no_note():
    assert Confirmations().answer("yes", 2) is None


def test_a_yes_at_the_next_question_runs_it_once_with_a_note_event_and_log_line(caplog):
    bus, runs = RecordingBus(), []
    confirmations = Confirmations(bus=bus)
    propose(confirmations, run=lambda: runs.append(1) or "Opened the page on bbc.co.uk.")
    action = asked(confirmations)
    assert action.ask == "Shall I open the page on bbc.co.uk?"
    with caplog.at_level(logging.INFO, logger="desk_buddy.tools.confirm"):
        note = confirmations.answer("Yes please.", 2)
    assert note == (
        "(System note: the server asked and the user said yes; the page on bbc.co.uk was opened.)"
    )
    assert runs == [1]
    assert bus.events[-1]["outcome"] == "ok" and bus.events[-1]["args"] == '{"site": "bbc.co.uk"}'
    assert 'Action: open_search_result {"url": "x"} -> ok: Opened the page' in caplog.text
    assert confirmations.answer("yes", 3) is None  # gone: never runs twice
    assert runs == [1]


def test_never_asked_means_it_can_never_be_answered():
    bus, runs = RecordingBus(), []
    confirmations = Confirmations(bus=bus)
    propose(confirmations, run=lambda: runs.append(1) or "ok")
    assert confirmations.answer("yes", 2) is None
    assert runs == [] and bus.outcomes() == ["cancelled"]


def test_only_the_very_next_question_can_answer():
    bus, runs = RecordingBus(), []
    confirmations = Confirmations(bus=bus)
    propose(confirmations, run=lambda: runs.append(1) or "ok")
    asked(confirmations, 1)
    note = confirmations.answer("yes", 3)  # a question was skipped
    assert runs == [] and bus.outcomes() == ["expired"] and "expired" in note


def test_a_proposal_is_only_asked_about_in_its_own_question():
    bus = RecordingBus()
    confirmations = Confirmations(bus=bus)
    propose(confirmations, q=1)  # e.g. a late tool thread from question 1
    assert confirmations.to_ask(2) is None
    assert confirmations.pending is None and bus.outcomes() == ["cancelled"]


def test_an_unasked_one_is_cancelled_but_an_asked_one_is_kept():
    bus = RecordingBus()
    confirmations = Confirmations(bus=bus)
    propose(confirmations)
    confirmations.cancel_unarmed("The question was interrupted.")
    assert confirmations.pending is None and bus.outcomes() == ["cancelled"]
    propose(confirmations)
    asked(confirmations)
    confirmations.cancel_unarmed("nothing to do")
    assert confirmations.pending is not None and confirmations.pending.armed


def test_arming_a_replaced_action_does_nothing():
    confirmations = Confirmations()
    propose(confirmations)
    old = confirmations.to_ask(1)
    propose(confirmations)
    assert not confirmations.arm(old, 1)
    assert not confirmations.pending.armed


def test_anything_but_yes_cancels_it(caplog):
    bus, runs = RecordingBus(), []
    confirmations = Confirmations(bus=bus)
    propose(confirmations, run=lambda: runs.append(1) or "ok")
    asked(confirmations)
    with caplog.at_level(logging.INFO, logger="desk_buddy.tools.confirm"):
        note = confirmations.answer("Okay, never mind.", 2)
    assert note == (
        "(System note: the pending action, opening the page on bbc.co.uk, was cancelled; "
        "the user didn't say yes.)"
    )
    assert runs == [] and bus.outcomes() == ["cancelled"]
    assert "-> cancelled: The user didn't say yes." in caplog.text


def test_60_seconds_after_the_question_it_has_expired():
    bus, clock, runs = RecordingBus(), Clock(), []
    confirmations = Confirmations(bus=bus, clock=clock)
    propose(confirmations, run=lambda: runs.append(1) or "ok")
    clock.now += 500  # a long reply before the question doesn't count
    asked(confirmations)
    clock.now += EXPIRY_S + 0.1
    note = confirmations.answer("Yes.", 2)
    assert runs == [] and bus.outcomes() == ["expired"]
    assert "expired before the user answered; nothing was opened" in note


def test_just_inside_60_seconds_a_yes_still_counts():
    clock, runs = Clock(), []
    confirmations = Confirmations(clock=clock)
    propose(confirmations, run=lambda: runs.append(1) or "ok")
    asked(confirmations)
    clock.now += EXPIRY_S - 0.1
    confirmations.answer("yes", 2)
    assert runs == [1]


def test_expire_drops_it_with_an_event():
    bus, runs = RecordingBus(), []
    confirmations = Confirmations(bus=bus)
    confirmations.expire("nothing waits")  # nothing to do
    propose(confirmations, run=lambda: runs.append(1) or "ok")
    asked(confirmations)
    confirmations.expire("Computer control was turned off.")
    assert confirmations.answer("yes", 2) is None
    assert runs == [] and bus.outcomes() == ["expired"]


def test_a_new_request_replaces_the_old_one():
    bus, runs = RecordingBus(), []
    confirmations = Confirmations(bus=bus)
    propose(confirmations, run=lambda: runs.append("old") or "ok")
    propose(confirmations, run=lambda: runs.append("new") or "ok")
    assert bus.outcomes() == ["cancelled"]
    asked(confirmations)
    confirmations.answer("yes", 2)
    assert runs == ["new"]


def test_a_failing_action_is_failed_with_a_note():
    bus = RecordingBus()
    confirmations = Confirmations(bus=bus)

    def broken():
        raise ToolError("I couldn't open the page on bbc.co.uk.")

    propose(confirmations, run=broken)
    asked(confirmations)
    assert "couldn't be opened" in confirmations.answer("yes", 2)
    propose(confirmations, run=lambda: 1 / 0)
    asked(confirmations)
    assert "couldn't be opened" in confirmations.answer("yes", 2)
    assert bus.outcomes() == ["failed", "failed"]


# --- The guard ---


def test_the_guard_counts_a_proposal_once_and_never_the_confirmed_run():
    tools, confirmations, _, opener, bus, _, guard = make(per_question=1)
    result = tools.open_search_result(PAGE)
    assert isinstance(result, NotDone)
    assert "don't ask and never say it's open" in result
    assert opener.calls == []  # nothing runs yet
    assert bus.outcomes() == ["waiting"]
    asked(confirmations)
    confirmations.answer("yes", 2)
    assert opener.calls == [PAGE]
    assert bus.outcomes() == ["waiting", "ok"]
    assert (guard._this_question, len(guard._recent)) == (1, 1)  # not counted again
    with pytest.raises(ToolError, match="enough actions"):  # the limit still holds
        tools.open_search_result(OTHER)
    assert bus.events[-1]["result"] == LIMIT_MESSAGE


def test_a_waiting_result_is_not_a_success():
    tools, *_ = make()
    registry = ToolRegistry(tools.tools())
    outcome = registry.run("open_search_result", {"url": PAGE})
    assert not outcome.is_error and not outcome.done
    done = ToolRegistry([Tool("t", "d", {"type": "object"}, lambda: "ok")]).run("t", {})
    assert done.done


def test_with_computer_control_off_nothing_is_proposed():
    tools, confirmations, _, _, bus, _, _ = make(enabled=False)
    with pytest.raises(ToolError, match="off"):
        tools.open_search_result(PAGE)
    assert confirmations.pending is None and bus.outcomes() == ["refused"]


def test_turned_off_after_proposing_a_yes_runs_nothing():
    tools, confirmations, _, opener, bus, _, guard = make()
    tools.open_search_result(PAGE)
    asked(confirmations)
    guard.enabled = False  # e.g. a race with the dashboard switch
    assert "couldn't be opened" in confirmations.answer("yes", 2)
    assert opener.calls == []
    assert bus.events[-1]["result"] == OFF_MESSAGE


def test_the_dashboard_sees_the_site_never_the_full_address():
    tools, confirmations, _, _, bus, _, _ = make()
    tools.open_search_result(PAGE)
    asked(confirmations)
    confirmations.answer("yes", 2)
    with pytest.raises(ToolError):
        tools.open_search_result(PAGE + "?made=up")
    for event in bus.events:
        assert "2644688" not in str(event)
        assert event["args"] == '{"site": "bbc.co.uk"}'


# --- open_search_result ---


def test_only_offered_with_search_results_and_confirmations():
    tools, *_ = make()
    (tool,) = tools.tools()
    schema = tool.schema()
    assert schema["name"] == "open_search_result" and schema["strict"] is True
    url = schema["input_schema"]["properties"]["url"]
    assert url == {**url, "type": "string", "maxLength": 500}
    assert schema["input_schema"]["required"] == ["url"]
    assert schema["input_schema"]["additionalProperties"] is False
    assert LaunchTools(ActionGuard(True)).tools() == []


@pytest.mark.parametrize(
    "url",
    [
        "https://www.bbc.co.uk/weather/2644689",  # changed
        "https://www.bbc.co.uk/weather/2644688/",  # changed
        "HTTPS://www.bbc.co.uk/weather/2644688",  # not exact
        "https://evil.example/",  # made up
        "",
    ],
)
def test_only_an_exact_result_address_is_accepted(url, caplog):
    tools, confirmations, _, opener, bus, _, guard = make()
    with caplog.at_level(logging.INFO, logger="desk_buddy.tools.actions"):
        with pytest.raises(ToolError) as info:
            tools.open_search_result(url)
    assert str(info.value) == NOT_A_RESULT
    assert confirmations.pending is None and opener.calls == []
    # Refused through the guard: counted, logged and shown, with the site only.
    assert guard._this_question == 1
    ((event),) = bus.events
    assert event["outcome"] == "refused"
    assert event["args"] in (
        '{"site": "bbc.co.uk"}',
        '{"site": "evil.example"}',
        '{"site": "unknown"}',
    )
    assert "-> refused: I can only open a page" in caplog.text


def test_this_and_the_last_questions_results_count_but_not_older_ones():
    tools, confirmations, results, *_ = make(urls=(PAGE,))
    results.new_question()
    results.add([OTHER])
    tools.open_search_result(PAGE)  # the last question's
    tools.open_search_result(OTHER)  # this question's
    results.new_question()
    with pytest.raises(ToolError):
        tools.open_search_result(PAGE)  # two questions ago
    results.clear()  # a forget command
    with pytest.raises(ToolError):
        tools.open_search_result(OTHER)


def test_a_result_address_that_fails_the_url_check_is_refused():
    bad = "https://user:pw@bbc.co.uk/"
    tools, confirmations, _, _, bus, *_ = make(urls=(bad,))
    with pytest.raises(ToolError, match="isn't allowed"):
        tools.open_search_result(bad)
    assert confirmations.pending is None and bus.outcomes() == ["refused"]


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/admin",
        "http://localhost./",
        "http://127.0.0.1:8766/",
        "http://127.1/",
        "http://2130706433/",
        "http://0x7f000001/",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://172.16.0.1/",
        "http://169.254.169.254/latest/meta-data",
        "http://0.0.0.0/",
        "http://router/",
        "http://printer.local/",
        "http://dev.localhost/",
    ],
)
def test_pages_on_this_computer_or_the_local_network_are_refused(url):
    assert not is_public_host(url)
    tools, confirmations, _, opener, bus, *_ = make(urls=(url,))
    with pytest.raises(ToolError) as info:
        tools.open_search_result(url)
    assert str(info.value) == LOCAL_PAGE
    assert confirmations.pending is None and bus.outcomes() == ["refused"]


@pytest.mark.parametrize(
    "url", [PAGE, "https://93.184.216.34/", "https://[2606:4700::1111]/", "https://news.example/"]
)
def test_public_hosts_are_fine(url):
    assert is_public_host(url)


def test_calling_it_again_never_runs_the_pending_one():
    tools, confirmations, _, opener, bus, _, _ = make()
    tools.open_search_result(PAGE)
    tools.open_search_result(PAGE)  # "the user said yes": the model tries again
    assert opener.calls == []
    assert bus.outcomes() == ["waiting", "cancelled", "waiting"]
    assert confirmations.pending is not None and not confirmations.pending.armed


def test_a_failed_open_is_shown_as_failed():
    tools, confirmations, _, opener, bus, *_ = make(opener=Opener(fail=OSError("no browser")))
    tools.open_search_result(PAGE)
    asked(confirmations)
    assert "couldn't be opened" in confirmations.answer("Yes.", 2)
    assert bus.outcomes() == ["waiting", "failed"]


def test_the_site_name_is_plain_or_left_out():
    assert site_name(PAGE) == "bbc.co.uk"
    assert site_name("https://xn--bcher-kva.example/") == "xn--bcher-kva.example"
    assert site_name("https://bücher.example/") == ""
    assert site_name("https://[::1]/") == ""
    assert page_description(PAGE) == "the page on bbc.co.uk"
    assert page_description("https://bücher.example/") == "the page"
    assert PendingAction("t", "", "", "the page", lambda: "").ask == "Shall I open the page?"
