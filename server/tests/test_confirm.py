"""The spoken "yes" (tools.confirm) and open_search_result: only an exact address from this
or the last question's search results, held until the user's next words say yes."""

import logging

import pytest

from desk_buddy.tools.actions import LIMIT_MESSAGE, OFF_MESSAGE, ActionGuard
from desk_buddy.tools.confirm import EXPIRY_S, Confirmations, PendingAction, is_yes
from desk_buddy.tools.launch import NOT_A_RESULT, LaunchTools
from desk_buddy.tools.registry import ToolError
from desk_buddy.tools.urls import SearchResults, site_name

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


# --- The yes check ---


@pytest.mark.parametrize(
    "text",
    [
        "Yes.",
        "yeah",
        "Yep, please.",
        "Sure!",
        "OK",
        "Okay then.",
        "Go ahead.",
        "Do it.",
        "Yes, open it.",
        "Please do.",
        "Yes please, CARL.",
    ],
)
def test_short_answers_with_a_yes_word_are_yes(text):
    assert is_yes(text)


@pytest.mark.parametrize(
    "text",
    [
        "No.",
        "Nope.",
        "No, never mind.",
        "Yes but no.",
        "Yes, no, wait.",
        "Don't.",
        "Don’t open it.",
        "dont do it",
        "Not now.",
        "Stop.",
        "Cancel that.",
        "Wait, yes.",
        "",
        "What's the weather like in Leeds?",
        "yes I think so but tell me more about it first",  # more than 6 words
        "open the weather",  # no yes word or phrase
        "it",
    ],
)
def test_anything_else_is_not_yes(text):
    assert not is_yes(text)


# --- Confirmations on their own ---


def pending(run=lambda: "Opened it.", description="the page on bbc.co.uk"):
    return PendingAction(
        "open_search_result", '{"url": "x"}', '{"site": "bbc.co.uk"}', description, run
    )


def test_nothing_waiting_means_no_note():
    assert Confirmations().answer("yes") is None


def test_a_yes_runs_it_once_with_a_note_event_and_log_line(caplog):
    bus, runs = RecordingBus(), []
    confirmations = Confirmations(bus=bus)
    confirmations.propose(pending(run=lambda: runs.append(1) or "Opened the page on bbc.co.uk."))
    with caplog.at_level(logging.INFO, logger="desk_buddy.tools.confirm"):
        note = confirmations.answer("Yes please.")
    assert note == (
        "(System note: you asked and the user said yes; the page on bbc.co.uk was opened.)"
    )
    assert runs == [1]
    assert bus.events[-1]["outcome"] == "ok" and bus.events[-1]["args"] == '{"site": "bbc.co.uk"}'
    assert 'Action: open_search_result {"url": "x"} -> ok: Opened the page' in caplog.text
    assert confirmations.answer("yes") is None  # gone: never runs twice
    assert runs == [1]


def test_anything_but_yes_cancels_it(caplog):
    bus, runs = RecordingBus(), []
    confirmations = Confirmations(bus=bus)
    confirmations.propose(pending(run=lambda: runs.append(1) or "ok"))
    with caplog.at_level(logging.INFO, logger="desk_buddy.tools.confirm"):
        note = confirmations.answer("No, never mind.")
    assert note == (
        "(System note: the pending action, opening the page on bbc.co.uk, was cancelled; "
        "the user didn't say yes.)"
    )
    assert runs == [] and bus.outcomes() == ["cancelled"]
    assert "-> cancelled: The user didn't say yes." in caplog.text


def test_after_60_seconds_it_has_expired():
    bus, clock, runs = RecordingBus(), Clock(), []
    confirmations = Confirmations(bus=bus, clock=clock)
    confirmations.propose(pending(run=lambda: runs.append(1) or "ok"))
    clock.now += EXPIRY_S + 0.1
    note = confirmations.answer("Yes.")
    assert runs == [] and bus.outcomes() == ["expired"]
    assert "expired before the user answered; nothing was opened" in note


def test_just_inside_60_seconds_a_yes_still_counts():
    clock, runs = Clock(), []
    confirmations = Confirmations(clock=clock)
    confirmations.propose(pending(run=lambda: runs.append(1) or "ok"))
    clock.now += EXPIRY_S - 0.1
    confirmations.answer("yes")
    assert runs == [1]


def test_expire_drops_it_with_an_event():
    bus, runs = RecordingBus(), []
    confirmations = Confirmations(bus=bus)
    confirmations.expire("nothing waits")  # nothing to do
    confirmations.propose(pending(run=lambda: runs.append(1) or "ok"))
    confirmations.expire("Computer control was turned off.")
    assert confirmations.answer("yes") is None
    assert runs == [] and bus.outcomes() == ["expired"]


def test_a_new_request_replaces_the_old_one():
    bus, runs = RecordingBus(), []
    confirmations = Confirmations(bus=bus)
    confirmations.propose(pending(run=lambda: runs.append("old") or "ok"))
    confirmations.propose(pending(run=lambda: runs.append("new") or "ok"))
    assert bus.outcomes() == ["cancelled"]
    confirmations.answer("yes")
    assert runs == ["new"]


def test_a_failing_action_is_failed_with_a_note():
    bus = RecordingBus()
    confirmations = Confirmations(bus=bus)

    def broken():
        raise ToolError("I couldn't open the page on bbc.co.uk.")

    confirmations.propose(pending(run=broken))
    note = confirmations.answer("yes")
    assert "couldn't be opened" in note and bus.outcomes() == ["failed"]
    confirmations.propose(pending(run=lambda: 1 / 0))
    assert "couldn't be opened" in confirmations.answer("yes")
    assert bus.outcomes() == ["failed", "failed"]


# --- The guard ---


def test_the_guard_counts_a_proposal_once_and_never_the_confirmed_run():
    tools, confirmations, _, opener, bus, _, guard = make(per_question=1)
    result = tools.open_search_result(PAGE)
    assert result == (
        "Not done yet: ask the user to confirm opening the page on bbc.co.uk; it runs only "
        "if they say yes."
    )
    assert opener.calls == []  # nothing runs yet
    assert bus.outcomes() == ["waiting"]
    confirmations.answer("yes")
    assert opener.calls == [PAGE]
    assert bus.outcomes() == ["waiting", "ok"]
    assert (guard._this_question, len(guard._recent)) == (1, 1)  # not counted again
    with pytest.raises(ToolError, match="enough actions"):  # the limit still holds
        tools.open_search_result(OTHER)
    assert bus.events[-1]["result"] == LIMIT_MESSAGE


def test_with_computer_control_off_nothing_is_proposed():
    tools, confirmations, _, _, bus, _, _ = make(enabled=False)
    with pytest.raises(ToolError, match="off"):
        tools.open_search_result(PAGE)
    assert confirmations.pending is None and bus.outcomes() == ["refused"]


def test_turned_off_after_proposing_a_yes_runs_nothing():
    tools, confirmations, _, opener, bus, _, guard = make()
    tools.open_search_result(PAGE)
    guard.enabled = False  # e.g. a race with the dashboard switch
    assert "couldn't be opened" in confirmations.answer("yes")
    assert opener.calls == []
    assert bus.events[-1]["result"] == OFF_MESSAGE


def test_the_dashboard_sees_the_site_never_the_full_address():
    tools, confirmations, _, _, bus, _, _ = make()
    tools.open_search_result(PAGE)
    confirmations.answer("yes")
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
def test_only_an_exact_result_address_is_accepted(url):
    tools, confirmations, _, opener, bus, _, guard = make()
    with pytest.raises(ToolError) as info:
        tools.open_search_result(url)
    assert str(info.value) == NOT_A_RESULT
    assert confirmations.pending is None and bus.events == [] and guard._this_question == 0


def test_this_and_the_last_questions_results_count_but_not_older_ones():
    tools, confirmations, results, *_ = make(urls=(PAGE,))
    results.new_question()
    results.add([OTHER])
    tools.open_search_result(PAGE)  # the last question's
    tools.open_search_result(OTHER)  # this question's
    results.new_question()
    with pytest.raises(ToolError):
        tools.open_search_result(PAGE)  # two questions ago
    tools.open_search_result(OTHER)


def test_a_result_address_that_fails_the_url_check_is_refused():
    bad = "https://user:pw@bbc.co.uk/"
    tools, confirmations, *_ = make(urls=(bad,))
    with pytest.raises(ToolError, match="isn't allowed"):
        tools.open_search_result(bad)
    assert confirmations.pending is None


def test_calling_it_again_never_runs_the_pending_one():
    tools, confirmations, _, opener, bus, _, _ = make()
    tools.open_search_result(PAGE)
    tools.open_search_result(PAGE)  # "the user said yes": the model tries again
    assert opener.calls == []
    assert bus.outcomes() == ["waiting", "cancelled", "waiting"]
    assert confirmations.pending is not None


def test_a_failed_open_is_shown_as_failed():
    tools, confirmations, _, opener, bus, *_ = make(opener=Opener(fail=OSError("no browser")))
    tools.open_search_result(PAGE)
    assert "couldn't be opened" in confirmations.answer("Yes.")
    assert bus.outcomes() == ["waiting", "failed"]


def test_the_site_name_is_plain_or_that_page():
    assert site_name(PAGE) == "bbc.co.uk"
    assert site_name("https://xn--bcher-kva.example/") == "xn--bcher-kva.example"
    assert site_name("https://bücher.example/") == "that page"
    assert site_name("https://[::1]/") == "that page"
