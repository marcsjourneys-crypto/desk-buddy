"""Claude streaming and sentence splitting for spoken replies."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit

import anthropic

from desk_buddy.brain.memory import merge_consecutive
from desk_buddy.brain.personality import Personality, system_prompt
from desk_buddy.config import Settings
from desk_buddy.tools.actions import publish_action
from desk_buddy.tools.registry import ToolRegistry
from desk_buddy.tools.urls import SearchResults

log = logging.getLogger(__name__)

# Boundary candidates, in buffer order: a newline, or sentence-ending punctuation
# plus any closing quotes, brackets or markdown emphasis.
_BOUNDARY = re.compile(r"(?P<nl>\n)|(?P<end>[.!?\u2026]+[\"')\]\u201d\u2019*_]*)")
_CLOSERS = "\"')]\u201d\u2019*_"
_OPENERS = "\"'([\u201c\u2018*_"
_CLAUSE_BREAKS = ",;:"
_ABBREVIATIONS = {"mr", "mrs", "ms", "dr", "st", "vs", "etc", "e.g", "i.e", "jr", "sr", "prof"}


def _speakable(text: str) -> bool:
    return any(c.isalnum() for c in text)


class SentenceSplitter:
    """Buffers streamed text and releases complete sentences.

    A sentence ends at . ! ? or … (plus any closing quotes, brackets or markdown emphasis)
    followed by whitespace, or at a newline. A "." after an abbreviation, a number or a
    single letter does not end a sentence. Pieces with no letters or digits are never
    released on their own; they join the next sentence. If the buffer passes max_chars
    with no boundary, it is cut at the last , ; : or whitespace before the cap (hard cut
    as a last resort). The output does not depend on how the text is chunked.
    """

    def __init__(self, max_chars: int = 200) -> None:
        self._buf = ""
        self._max_chars = max_chars

    def feed(self, text: str) -> list[str]:
        self._buf += text
        sentences: list[str] = []
        while (cut := self._find_boundary()) is not None:
            sentence, self._buf = self._buf[:cut].strip(), self._buf[cut:]
            if _speakable(sentence):
                sentences.append(sentence)
        return sentences

    def flush(self) -> list[str]:
        rest, self._buf = self._buf.strip(), ""
        return [rest] if _speakable(rest) else []

    def _find_boundary(self) -> int | None:
        for match in _BOUNDARY.finditer(self._buf):
            cut = match.start() if match.group("nl") else match.end()
            if cut > self._max_chars:
                break  # past the cap: same decision however the text was chunked
            if not _speakable(self._buf[:cut]):
                continue  # nothing to say yet; let it join the next sentence
            if match.group("nl"):
                return cut
            if cut >= len(self._buf):
                return None  # wait for the next chunk to confirm
            if not self._buf[cut].isspace():
                continue  # "3.5", "e.g" mid-token, "example.com"
            if match.group("end").rstrip(_CLOSERS) == "." and self._is_non_final_dot(match.start()):
                continue
            return cut
        if len(self._buf) > self._max_chars:
            return self._cap_cut()
        return None

    def _is_non_final_dot(self, pos: int) -> bool:
        word = self._last_word(pos)
        return word in _ABBREVIATIONS or word.isdigit() or (len(word) == 1 and word.isalpha())

    def _cap_cut(self) -> int:
        head = self._buf[: self._max_chars]
        for i in range(len(head) - 1, 0, -1):
            if (head[i] in _CLAUSE_BREAKS or head[i].isspace()) and _speakable(head[: i + 1]):
                return i + 1
        return self._max_chars  # hard cut (dropped by feed if it has nothing speakable)

    def _last_word(self, pos: int) -> str:
        words = self._buf[max(0, pos - 20) : pos].split()
        return words[-1].lower().lstrip(_OPENERS) if words else ""


MAX_TOKENS = 2048  # thinking counts toward this; spoken replies are short
MAX_TOOL_ROUNDS = 3  # API calls per reply; the last one may not call tools
# Remembered for a tool call whose result never came (the turn was cut off).
INTERRUPTED_RESULT = "Interrupted; the action may have completed."

# Anthropic's web search, a server tool: it runs on Anthropic's side and only reads, so
# it lives outside the ToolRegistry and the ActionGuard.
WEB_SEARCH_TOOL = "web_search_20250305"
MAX_SOURCES = 3  # source titles shown per search on the dashboard
MAX_TITLE = 80  # characters of each source title there
WEB_SEARCH_OFF_HINT = (
    "web search isn't enabled for this API key: turn it on in the Anthropic Console, "
    "or set WEB_SEARCH=off"
)
# Set once a request with web search was refused because the account has it turned off.
# Module-level on purpose: the chat is rebuilt between turns, and search must stay off
# (and the prompt must say so) until the next restart.
_web_search_blocked = False


def web_search_blocked() -> bool:
    """True once the API refused web search for this key (until the next restart)."""
    return _web_search_blocked


def _block_web_search() -> None:
    global _web_search_blocked
    if not _web_search_blocked:
        log.warning("%s. Searching is off until the next restart.", WEB_SEARCH_OFF_HINT)
    _web_search_blocked = True


@dataclass(frozen=True)
class Search:
    """One web search Claude ran: the query, ok or failed, and what came back."""

    query: str
    ok: bool
    count: int = 0  # results
    sources: tuple[tuple[str, str], ...] = ()  # up to MAX_SOURCES (title, site)
    error: str = ""  # the API's error_code when it failed
    # Every result as (title, site, full address): server side only (open_search_result
    # and its numbered list for Claude), never on the dashboard.
    pages: tuple[tuple[str, str, str], ...] = field(default=(), compare=False, repr=False)

    @property
    def urls(self) -> tuple[str, ...]:
        return tuple(url for _, _, url in self.pages)

    def summary(self) -> str:
        """Plain text for the dashboard (untrusted titles: shown as text, never links)."""
        if not self.ok:
            return f"Search failed: {self.error}"
        if not self.count:
            return "No results"
        shown = "; ".join(f"{title} ({site})" if site else title for title, site in self.sources)
        noun = "result" if self.count == 1 else "results"
        return f"{self.count} {noun}: {shown}"


class LLMError(Exception):
    """Claude could not produce a reply (network, API error). Already retried by the SDK."""


class LLMRefusal(LLMError):
    """Claude declined the request (stop_reason == 'refusal')."""


class Chat(Protocol):
    """What the voice loop needs from any LLM provider. A provider that runs tools may
    also keep a `tools_succeeded` count, `last_tool_rounds` and `searches` (see
    ClaudeChat); the loop reads them if present."""

    def stream_reply(
        self, history: Sequence[Mapping[str, Any]], user_text: str
    ) -> AsyncGenerator[str, None]: ...


def _llm_error(exc: anthropic.APIError) -> LLMError:
    if isinstance(exc, anthropic.RateLimitError):
        return LLMError("rate limited by the API")
    if isinstance(exc, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
        return LLMError("API key rejected (check ANTHROPIC_API_KEY)")
    if isinstance(exc, anthropic.APIStatusError):
        # status 200 here means an error event arrived mid-stream; type names it
        return LLMError(f"API error {exc.status_code} ({getattr(exc, 'type', None)})")
    if isinstance(exc, anthropic.APIConnectionError):
        return LLMError("can't reach the API")
    return LLMError(f"unexpected API error ({type(exc).__name__})")


class ClaudeChat:
    """Streaming chat with a tool loop. The caller owns the history (see brain.memory)."""

    def __init__(
        self,
        client: Any,
        *,
        model: str,
        effort: str,
        system: str | Callable[[], str],
        tools: ToolRegistry | None = None,
        web_search_max: int | None = None,
        location: Callable[[], Mapping[str, str]] = dict,
        on_search: Callable[[Search], None] | None = None,
    ) -> None:
        """web_search_max: searches per request, or None for no web search. location:
        the user's rough location ({"city", "region", "country"}, any subset), read for
        every request. on_search: told about each search (the dashboard); never raises
        into the reply."""
        # client: anthropic.AsyncAnthropic (typed Any so tests can pass a fake)
        self._client = client
        self._model = model
        self._effort = effort
        self._system = system if callable(system) else (lambda: system)
        self._tools = tools or ToolRegistry()
        self._web_search_max = web_search_max
        self._location = location
        self._on_search = on_search
        # Tool calls that succeeded, ever. Only grows; the loop compares it before and
        # after a reply to tell whether that reply's turn ran a tool successfully.
        self.tools_succeeded = 0
        # The latest reply's tool rounds, (tool_use blocks, tool_result blocks) each, as
        # plain dicts for the conversation memory. Reset when a reply starts.
        self.last_tool_rounds: list[tuple[list[dict[str, Any]], list[dict[str, Any]]]] = []
        # The latest reply's web searches. Reset when a reply starts.
        self.searches: list[Search] = []

    @property
    def web_search(self) -> bool:
        """Whether requests offer web search now (off once the API refused it)."""
        return self._web_search_max is not None and not web_search_blocked()

    async def stream_reply(
        self, history: Sequence[Mapping[str, Any]], user_text: str
    ) -> AsyncGenerator[str, None]:
        """Yield text deltas as they arrive, running tools between rounds.

        A "\\n" goes before text that follows something else (a new round, a web search,
        a tool call, thinking), so those sentences never run together. Text blocks that
        follow each other (a cited answer is split into several) join with nothing.
        A paused turn (stop_reason "pause_turn", during a long web search) is resumed; it
        uses up a round like a tool call.

        Each web search is reported as soon as its result arrives, so a reply cut off
        later still shows and remembers it.

        Raises LLMError (any API or network failure) or LLMRefusal. Either can arrive
        after some text was already yielded, so callers must handle a partial reply.
        """
        # A remembered turn can end on tool results: the new text then joins that message.
        messages: list[Any] = merge_consecutive([*history, {"role": "user", "content": user_text}])
        self.last_tool_rounds = []
        self.searches = []
        searching: dict[str, str] = {}  # web search id -> query, until its result arrives
        reported: set[str] = set()  # web search ids already reported
        spoke = False
        for round_no in range(1, MAX_TOOL_ROUNDS + 1):
            last = round_no == MAX_TOOL_ROUNDS
            retried = False
            while True:
                web = self.web_search and not retried
                new_block = True  # the first text of a round follows the last round
                said_here = False
                try:
                    async with self._client.messages.stream(
                        **self._params(messages, last=last, web=web)
                    ) as stream:
                        async for event in stream:
                            if event.type == "content_block_start":
                                if event.content_block.type != "text":
                                    new_block = True
                                self._note_block(event.content_block, searching, reported)
                            elif event.type == "content_block_stop":
                                block = getattr(event, "content_block", None)
                                if block is not None:
                                    self._note_block(block, searching, reported)
                            elif (
                                event.type == "content_block_delta"
                                and event.delta.type == "text_delta"
                            ):
                                if new_block and spoke:
                                    yield "\n"  # keep sentences from separate parts apart
                                new_block = False
                                spoke = said_here = True
                                yield event.delta.text
                        message = await stream.get_final_message()
                    break
                except anthropic.BadRequestError as exc:
                    if web and not said_here and _mentions_web_search(exc):
                        if _search_not_enabled(exc):
                            _block_web_search()
                        else:
                            log.warning(
                                "Web search request refused (%s); trying without search",
                                _api_message(exc),
                            )
                        retried = True
                        continue  # this round again, without search
                    raise _llm_error(exc) from exc
                except anthropic.APIError as exc:
                    raise _llm_error(exc) from exc
            for block in message.content:
                self._note_block(block, searching, reported)
            if message.stop_reason == "refusal":
                raise LLMRefusal("Claude declined to answer")
            if message.stop_reason == "max_tokens":
                log.warning("reply truncated: stop_reason=max_tokens (MAX_TOKENS=%d)", MAX_TOKENS)
                if not spoke:
                    raise LLMError("reply ran out of room before saying anything")
                return
            if message.stop_reason == "pause_turn":
                if last:
                    log.warning("reply paused on the last round; ending it")
                    if not spoke:
                        raise LLMError("reply paused before saying anything")
                    return
                # Resume: the content goes back as is, with no new user message.
                messages = merge_consecutive(
                    [*messages, {"role": "assistant", "content": message.content}]
                )
                continue
            if message.stop_reason != "tool_use":
                return
            uses = [block for block in message.content if block.type == "tool_use"]
            if not uses:
                log.warning("stop_reason=tool_use but no tool_use blocks; ending the reply")
                return
            # For memory: plain tool_use blocks (no thinking, no text, which was spoken),
            # recorded before they run. Each result starts as a placeholder and is filled
            # in as its tool finishes, so a call cut off mid-run (or never reached) is
            # still remembered, and every tool_use keeps a result.
            results: list[dict[str, Any]] = [
                {"type": "tool_result", "tool_use_id": b.id, "content": INTERRUPTED_RESULT}
                for b in uses
            ]
            self.last_tool_rounds.append(
                (
                    [
                        {"type": "tool_use", "id": b.id, "name": b.name, "input": b.input}
                        for b in uses
                    ],
                    results,
                )
            )
            waiting = False
            for i, block in enumerate(uses):
                results[i], done = await self._run_tool(block)
                waiting = waiting or not done
            if waiting:
                # An action waits for the user's spoken yes: the turn ends here, and the
                # robot asks the question itself. No more rounds, so the model can't ask
                # (or claim anything) on top of it.
                log.info("Ending the reply: an action waits for the user's yes")
                return
            # New list each round: the content goes back unchanged (thinking and search
            # blocks too), joined to a paused round's content if there was one.
            messages = merge_consecutive(
                [
                    *messages,
                    {"role": "assistant", "content": message.content},
                    {"role": "user", "content": results},
                ]
            )

    def _params(self, messages: list[Any], *, last: bool, web: bool) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": self._model,
            "max_tokens": MAX_TOKENS,
            "system": self._system(),
            "output_config": {"effort": self._effort},
            "messages": messages,
        }
        tools = self._tools.schemas()
        if web:
            tools.append(self._web_tool())
        if tools:
            params["tools"] = tools
            if last:  # also stops a search
                params["tool_choice"] = {"type": "none"}
        return params

    def _web_tool(self) -> dict[str, Any]:
        tool: dict[str, Any] = {
            "type": WEB_SEARCH_TOOL,
            "name": "web_search",
            "max_uses": self._web_search_max,
        }
        try:
            where = {
                k: v
                for k, v in dict(self._location()).items()
                if k in ("city", "region", "country") and isinstance(v, str) and v
            }
        except Exception:
            log.warning("Couldn't read the location for web search", exc_info=True)
            where = {}
        if where:
            tool["user_location"] = {"type": "approximate", **where}
        return tool

    def _note_block(self, block: Any, searching: dict[str, str], reported: set[str]) -> None:
        """Note a web search from one content block, seen while streaming (a finished
        server_tool_use, or a web_search_tool_result, which arrives whole) or in the final
        message. Each result is matched by id to its query (which may come in an earlier
        message, before a pause) and reported once."""
        kind = _field(block, "type")
        if kind == "server_tool_use" and _field(block, "name") == "web_search":
            query = _field(block, "input") or {}
            text = query.get("query", "") if isinstance(query, Mapping) else ""
            searching[str(_field(block, "id"))] = " ".join(str(text).split())
        elif kind == "web_search_tool_result":
            search_id = str(_field(block, "tool_use_id"))
            query = searching.get(search_id)
            if query is not None and search_id not in reported:
                reported.add(search_id)
                self._report(_search(query, _field(block, "content")))

    def _report(self, search: Search) -> None:
        self.searches.append(search)
        args = _search_args(search)
        if search.ok:
            log.info("Action: web_search %s -> ok (%d results)", args, search.count)
        else:
            log.info("Action: web_search %s -> failed: %s", args, search.error)
        if self._on_search is not None:
            try:
                self._on_search(search)
            except Exception:
                log.debug("Search report failed", exc_info=True)

    async def _run_tool(self, block: Any) -> tuple[dict[str, Any], bool]:
        """The tool_result block, and False if the action only waits for a yes."""
        # If the turn is cancelled here the thread still finishes (a note may be saved),
        # but the cancel propagates at once and the reply ends.
        outcome = await asyncio.to_thread(self._tools.run, block.name, block.input)
        result: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": block.id,
            "content": outcome.text,
        }
        done = getattr(outcome, "done", True)
        if outcome.is_error:
            result["is_error"] = True
        elif done:  # waiting for a yes isn't a success yet
            self.tools_succeeded += 1
        return result, outcome.is_error or done


def _field(obj: Any, name: str) -> Any:
    """An attribute of an SDK block, or a key of the same block as a plain dict."""
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


def _search(query: str, content: Any) -> Search:
    """A Search from a web_search_tool_result's content: a list of results, or an error."""
    if not isinstance(content, (list, tuple)):
        return Search(query, ok=False, error=str(_field(content, "error_code") or "unknown"))
    pages = []
    for result in content:
        url = _field(result, "url")
        title = _plain(str(_field(result, "title") or ""))
        if len(title) > MAX_TITLE:
            title = title[:MAX_TITLE] + "..."
        pages.append((title or "Untitled", _site(str(url or "")), url))
    sources = tuple((title, site) for title, site, _ in pages[:MAX_SOURCES])
    kept = tuple(p for p in pages if isinstance(p[2], str) and p[2])
    return Search(query, ok=True, count=len(content), sources=sources, pages=kept)


# Bidi controls: shown text from the web must not flip the dashboard's text direction.
_BIDI = dict.fromkeys(
    [*range(0x202A, 0x202F), *range(0x2066, 0x206A)],
    " ",
)


def _plain(text: str) -> str:
    """One line of plain text: bidi controls and every line break (including U+0085,
    U+2028 and U+2029, which split() counts as whitespace) become single spaces."""
    return " ".join(text.translate(_BIDI).split())


def _site(url: str) -> str:
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return ""
    return _plain(host.removeprefix("www."))


def _search_args(search: Search) -> str:
    args = json.dumps({"query": search.query}, ensure_ascii=False)
    return args if len(args) <= 200 else args[:200] + "..."


def _mentions_web_search(exc: anthropic.BadRequestError) -> bool:
    """A 400 that names web search: the round is retried once without it."""
    text = str(exc).lower()
    return "web search" in text or "web_search" in text


_NOT_ENABLED = ("not enabled", "not available", "disabled")


def _search_not_enabled(exc: anthropic.BadRequestError) -> bool:
    """A 400 saying web search is turned off for this account or organisation: then
    search stays off until the next restart. Any other web-search 400 doesn't count."""
    text = str(exc).lower()
    return _mentions_web_search(exc) and any(phrase in text for phrase in _NOT_ENABLED)


def _api_message(exc: anthropic.APIStatusError) -> str:
    """The API's own error message, on one line and bounded, for the log."""
    text = " ".join(str(getattr(exc, "api_message", None) or exc).split())
    return text if len(text) <= 200 else text[:200] + "..."


def publish_search(bus: Any, search: Search) -> None:
    """A web search as an action event on the dashboard. Never raises."""
    publish_action(
        bus, "web_search", _search_args(search), search.summary(), "ok" if search.ok else "failed"
    )


def create_chat(
    settings: Settings,
    tools: ToolRegistry | None = None,
    personality: Callable[[], Personality] = Personality,
    bus: Any = None,
    search_results: SearchResults | None = None,
) -> Chat:
    """Build the chat client for settings.llm_provider. Add new providers here. The
    system prompt (and web search's location) is rebuilt for every request, with the
    personality as it is then. Each web search is shown on `bus` (the dashboard), and its
    result addresses go into `search_results` (server side only: what
    open_search_result may open)."""

    def on_search(search: Search) -> None:
        if search_results is not None:
            search_results.add(search.pages)
        publish_search(bus, search)

    if settings.llm_provider == "anthropic":
        return ClaudeChat(
            anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key),
            model=settings.llm_model,
            effort=settings.llm_effort,
            system=lambda: prompt_for(settings, tools, personality()),
            tools=tools,
            web_search_max=settings.web_search_max if settings.web_search else None,
            location=lambda: personality().about.location,
            on_search=on_search,
        )
    raise ValueError(f"unsupported LLM_PROVIDER {settings.llm_provider!r}")


def prompt_for(settings: Settings, tools: ToolRegistry | None, personality: Personality) -> str:
    """The full system prompt CARL gets (also the dashboard's read-only preview). Web
    search counts as off once the API refused it for this key."""
    return system_prompt(
        settings.max_sentences,
        notes=tools is not None and "take_note" in tools,
        music=tools is not None and "play_music" in tools,
        web=settings.web_search and not web_search_blocked(),
        apps=tools is not None and "open_app" in tools,
        websites=tools is not None and "open_website" in tools,
        pages=tools is not None and "open_search_result" in tools,
        youtube=tools is not None and "play_youtube" in tools,
        youtube_search=tools is not None and "youtube_search" in tools,
        computer_control=settings.computer_control,
        character=personality.character,
        about=personality.about,
    )
