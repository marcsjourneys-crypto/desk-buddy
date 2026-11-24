# M4 Robot Link Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task by task.

**Goal:** A robot connects to the Desk Buddy server over one authenticated WebSocket and takes over the mic, speaker and button. A fake robot, used in tests and as a voice tool, proves the link works before the hardware exists.

**Architecture:**
- A FastAPI app with one `/robot` WebSocket route, served by uvicorn in the same asyncio loop as the `Orchestrator`.
- A `DeviceSwitch` exposes a `Mic` and a `Player` (the existing protocols in `loop.py`). Each routes to the attached robot, or to the PC devices when no robot is attached.
- `RobotLink` owns one connection:
  - an outbox, with a single sender task;
  - a `RobotMic`, which buffers frames between button down and up;
  - a `RobotPlayer`, which sends TTS audio paced in real time with about 200 ms of lead.

**Tech stack:** Python 3.12, FastAPI, uvicorn[standard] and websockets, plus the existing stack. Tests use pytest with plain `asyncio.run`, like the rest of the suite.

**Design:** `docs/plans/2026-11-23-m4-robot-link-design.md`. One change from it: **httpx is not added.** The tests start the real uvicorn server on `127.0.0.1` with a free port (port 0) and connect with the fake robot client, which tests the code that actually runs. Binding to loopback counts as a unit test here; real network and devices stay in `-m integration`.

**Conventions (apply to every task):**
- Run everything from `server/`: `uv run pytest -q`, `uv run ruff check`, `uv run ruff format`.
- Write files with the Edit/Write tools, never Bash heredocs: Git Bash mangles backslashes.
- Never edit `server/.env`.
- Make one commit per task. End each commit message with:
  ```
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01S4FHmMu7pMrHuZS9GJZYSY
  ```
- Never log the robot token.

---

### Task 1: Dependencies and settings

**Files:**
- Modify: `server/pyproject.toml` (via `uv add`)
- Modify: `server/src/desk_buddy/config.py`
- Modify: `server/.env.example` (the Robot link section)
- Test: `server/tests/test_config.py`

**Step 1: Add the dependencies**

Run: `uv add fastapi "uvicorn[standard]" websockets`
Expected: `pyproject.toml` lists all three, and `uv.lock` is updated.
Then run `uv run python -c "import uvicorn, inspect; print(uvicorn.__version__); print(inspect.signature(uvicorn.Server.serve)); print(hasattr(uvicorn.Server, 'capture_signals'))"` and note the output. Task 5 relies on `serve(sockets=...)` and `capture_signals`.

**Step 2: Write failing tests** (append to `tests/test_config.py`; use the existing helper style in that file to call `load_settings` with an `environ` dict and `env_path=None`)

```python
def test_robot_link_defaults():
    s = load_settings(env_path=None, environ={"ANTHROPIC_API_KEY": "k"})
    assert s.robot_token == ""
    assert s.server_host == "127.0.0.1"
    assert s.server_port == 8765


def test_robot_token_must_be_long_enough():
    with pytest.raises(ConfigError, match="ROBOT_TOKEN"):
        load_settings(env_path=None, environ={"ANTHROPIC_API_KEY": "k", "ROBOT_TOKEN": "short"})


def test_robot_token_is_not_in_repr():
    s = load_settings(
        env_path=None, environ={"ANTHROPIC_API_KEY": "k", "ROBOT_TOKEN": "x" * 20}
    )
    assert "x" * 20 not in repr(s)


@pytest.mark.parametrize("port", ["0", "70000", "abc"])
def test_server_port_must_be_valid(port):
    with pytest.raises(ConfigError, match="SERVER_PORT"):
        load_settings(env_path=None, environ={"ANTHROPIC_API_KEY": "k", "SERVER_PORT": port})
```

**Step 3: Run them and check they fail**

Run: `uv run pytest tests/test_config.py -q`
Expected: the new tests FAIL because `Settings` has no `robot_token`.

**Step 4: Implement**

In `config.py`:
- Add `"ROBOT_TOKEN", "SERVER_HOST", "SERVER_PORT"` to `ENV_KEYS`.
- Add `MIN_TOKEN_CHARS = 16`.
- Add these fields to `Settings`, after the obsidian fields:
  ```python
  robot_token: str = field(default="", repr=False)  # blank = robot link off
  server_host: str = "127.0.0.1"
  server_port: int = 8765
  ```
- Add these helpers and use them in `load_settings`:
  ```python
  def _robot_token(get: Callable[[str, str], str]) -> str:
      token = get("ROBOT_TOKEN", "")
      if token and len(token) < MIN_TOKEN_CHARS:
          raise ConfigError(f"ROBOT_TOKEN must be at least {MIN_TOKEN_CHARS} characters")
      return token


  def _port(get: Callable[[str, str], str], key: str, default: int) -> int:
      raw = get(key, str(default))
      try:
          value = int(raw)
      except ValueError:
          value = 0
      if not 1 <= value <= 65535:
          raise ConfigError(f"{key} must be a port number 1-65535, got {raw!r}")
      return value
  ```
- Never put the token value in the error message.

In `.env.example`, replace the Robot link section with:

```
# --- Robot link ---
# Shared secret the robot sends in its hello message. At least 16 characters.
# Blank = robot link off (PC mic/speaker only). Make one with:
#   uv run python -c "import secrets; print(secrets.token_urlsafe(24))"
ROBOT_TOKEN=
# Interface to listen on. Default: 127.0.0.1 (this PC only). Use 0.0.0.0 for the LAN (M6).
SERVER_HOST=
# WebSocket port. Default: 8765
SERVER_PORT=
```

**Step 5: Run the tests and lint**

Run: `uv run pytest -q && uv run ruff check`
Expected: all pass. There is an existing test that keeps `.env.example` values blank; it must still pass.

**Step 6: Commit**

Message: "Add robot link settings and FastAPI/uvicorn/websockets dependencies".

---

### Task 2: Protocol messages

**Files:**
- Create: `server/src/desk_buddy/link/__init__.py` (a one-line docstring: `"""The robot link: WebSocket protocol, robot devices and the device switch."""`)
- Create: `server/src/desk_buddy/link/protocol.py`
- Test: `server/tests/test_link_protocol.py`

**Step 1: Write failing tests**

```python
import pytest

from desk_buddy.link import protocol
from desk_buddy.link.protocol import ProtocolError, parse


def test_hello():
    msg = parse('{"type": "hello", "token": "t", "fw_version": "0.1"}')
    assert msg.type == "hello"
    assert msg.data["token"] == "t"
    assert msg.data["fw_version"] == "0.1"


def test_hello_fw_version_is_optional():
    assert parse('{"type": "hello", "token": "t"}').data["fw_version"] == ""


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "[1, 2]",
        '{"no": "type"}',
        '{"type": 5}',
        '{"type": "dance"}',
        '{"type": "hello"}',
        '{"type": "hello", "token": 5}',
        '{"type": "button", "action": "sideways"}',
        '{"type": "stats", "data": 5}',
    ],
)
def test_bad_messages_raise(text):
    with pytest.raises(ProtocolError):
        parse(text)


def test_long_token_is_rejected():
    with pytest.raises(ProtocolError):
        parse('{"type": "hello", "token": "%s"}' % ("x" * 300))


@pytest.mark.parametrize("action", ["down", "up"])
def test_button(action):
    assert parse(f'{{"type": "button", "action": "{action}"}}').data["action"] == action


@pytest.mark.parametrize("kind", ["touch", "wake", "stats"])
def test_simple_messages(kind):
    assert parse(f'{{"type": "{kind}"}}').type == kind


def test_error_text_never_contains_the_token():
    with pytest.raises(ProtocolError) as info:
        parse('{"type": "hello", "token": "%s"}' % ("s3cret" * 60))
    assert "s3cret" not in str(info.value)


def test_server_messages():
    assert protocol.welcome() == {"type": "welcome"}
    assert protocol.state("idle") == {"type": "state", "name": "idle"}
    assert protocol.emotion("happy") == {"type": "emotion", "name": "happy"}
    assert protocol.audio_start() == {"type": "audio_start", "rate": 16000}
    assert protocol.audio_end() == {"type": "audio_end"}
    assert protocol.audio_stop() == {"type": "audio_stop"}
```

**Step 2: Run them and check they fail**

Run: `uv run pytest tests/test_link_protocol.py -q`
Expected: FAIL with `ModuleNotFoundError`.

**Step 3: Implement `link/protocol.py`**

```python
"""Robot <-> server messages. Text frames are JSON objects with a "type"; audio is binary.

The full protocol is documented in docs/protocol.md. Parsing never raises anything but
ProtocolError, and error messages never include field values (the token must not leak).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from desk_buddy.audio import SAMPLE_RATE

MAX_TOKEN_CHARS = 256
BUTTON_ACTIONS = ("down", "up")
# wake is reserved for a later wake word; the server accepts and ignores it for now.
ROBOT_MESSAGES = ("hello", "button", "touch", "stats", "wake")


class ProtocolError(ValueError):
    """A text frame from the robot is not a valid message."""


@dataclass(frozen=True)
class Message:
    type: str
    data: dict[str, Any] = field(default_factory=dict)


def parse(text: str) -> Message:
    try:
        obj = json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise ProtocolError("not JSON") from exc
    if not isinstance(obj, dict):
        raise ProtocolError("not a JSON object")
    kind = obj.get("type")
    if not isinstance(kind, str):
        raise ProtocolError("missing or non-string type")
    if kind not in ROBOT_MESSAGES:
        raise ProtocolError(f"unknown type {kind[:32]!r}")
    if kind == "hello":
        token, fw = obj.get("token"), obj.get("fw_version", "")
        if not isinstance(token, str) or not 0 < len(token) <= MAX_TOKEN_CHARS:
            raise ProtocolError("hello needs a token string")
        if not isinstance(fw, str):
            raise ProtocolError("fw_version must be a string")
        return Message(kind, {"token": token, "fw_version": fw[:64]})
    if kind == "button":
        action = obj.get("action")
        if action not in BUTTON_ACTIONS:
            raise ProtocolError("button action must be down or up")
        return Message(kind, {"action": action})
    if kind == "stats":
        data = obj.get("data", {})
        if not isinstance(data, dict):
            raise ProtocolError("stats data must be an object")
        return Message(kind, {"data": data})
    return Message(kind)


def welcome() -> dict[str, Any]:
    return {"type": "welcome"}


def state(name: str) -> dict[str, Any]:
    return {"type": "state", "name": name}


def emotion(name: str) -> dict[str, Any]:
    return {"type": "emotion", "name": name}


def audio_start(rate: int = SAMPLE_RATE) -> dict[str, Any]:
    return {"type": "audio_start", "rate": rate}


def audio_end() -> dict[str, Any]:
    return {"type": "audio_end"}


def audio_stop() -> dict[str, Any]:
    return {"type": "audio_stop"}
```

**Step 4: Run the tests and lint.** Expected: PASS.

**Step 5: Commit.** Message: "Add robot protocol message parsing".

---

### Task 3: RobotMic and RobotPlayer

**Files:**
- Create: `server/src/desk_buddy/link/robot.py` (the devices only; `RobotLink` is added in Task 5)
- Test: `server/tests/test_link_robot.py`

**Behaviour**
- **`RobotMic`.** Recording is switched by the link itself, so no frame is lost while the orchestrator catches up on the `down` event.
  - `begin()` clears the buffer and starts recording.
  - `end()` stops recording.
  - `feed(data)` appends while recording. A frame with an odd number of bytes is dropped.
  - `start()` is a no-op, for the orchestrator's `Mic` protocol.
  - `stop()` ends recording and returns the buffer.
  - The buffer is a `PcmBuffer(30.0)` from `audio.py`.
- **`RobotPlayer(send, *, clock=time.monotonic, sleep=time.sleep, lead=0.2)`:**
  - `send` is thread-safe and takes a dict or bytes.
  - `play(pcm, token)` blocks. It sends `audio_start`, then 20 ms chunks, then `audio_end`. It sleeps so it is never more than `lead` seconds ahead of real time, and returns when about `lead` seconds of the sentence are left.
  - It stops sending as soon as the token is stale or the player is closed, and then sends no `audio_end`.
  - Sleeps are capped at 20 ms per call, so `stop()` takes effect quickly.
  - `stop()` bumps the generation and sends `audio_stop`.
  - `close()` makes every later `play` a no-op and sends nothing.

**Step 1: Write failing tests**

```python
import threading

from desk_buddy.audio import BYTES_PER_SECOND, CHUNK_BYTES
from desk_buddy.link.robot import RobotMic, RobotPlayer


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.slept = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def make_player(**kw):
    sent, clock = [], FakeClock()
    player = RobotPlayer(sent.append, clock=clock.clock, sleep=clock.sleep, **kw)
    return player, sent, clock


def test_mic_buffers_only_between_begin_and_end():
    mic = RobotMic()
    mic.feed(b"\x01\x00")  # before begin: dropped
    mic.begin()
    mic.feed(b"\x02\x00" * 4)
    mic.feed(b"\x03")  # odd length: dropped
    mic.end()
    mic.feed(b"\x04\x00")  # after end: dropped
    mic.start()  # orchestrator's start() must not clear what begin() captured
    assert mic.stop() == b"\x02\x00" * 4
    assert mic.stop() == b""


def test_mic_begin_clears_old_audio():
    mic = RobotMic()
    mic.begin()
    mic.feed(b"\x01\x00")
    mic.begin()
    mic.feed(b"\x02\x00")
    assert mic.stop() == b"\x02\x00"


def test_play_sends_start_chunks_end():
    player, sent, _ = make_player()
    pcm = b"\x01\x00" * (CHUNK_BYTES // 2) * 3  # 60 ms
    player.play(pcm, player.current_token())
    assert sent[0] == {"type": "audio_start", "rate": 16000}
    assert sent[1:4] == [pcm[:CHUNK_BYTES], pcm[CHUNK_BYTES : 2 * CHUNK_BYTES], pcm[2 * CHUNK_BYTES :]]
    assert sent[4] == {"type": "audio_end"}


def test_play_is_paced_to_real_time_with_lead():
    player, sent, clock = make_player(lead=0.2)
    pcm = b"\x00" * BYTES_PER_SECOND  # 1 s
    player.play(pcm, player.current_token())
    # It returns when about `lead` of the sentence is left, so it blocked about 0.8 s.
    assert 0.75 <= clock.now <= 0.85
    assert max(clock.slept) <= 0.02 + 1e-9


def test_stale_token_sends_nothing():
    player, sent, _ = make_player()
    token = player.current_token()
    player.stop()
    sent.clear()
    player.play(b"\x00" * CHUNK_BYTES, token)
    assert sent == []


def test_stop_sends_audio_stop_and_halts_a_play_in_progress():
    sent, stop_after = [], 5

    class StoppingClock(FakeClock):
        def sleep(self, seconds):
            super().sleep(seconds)
            if sum(isinstance(m, bytes) for m in sent) >= stop_after:
                player.stop()

    clock = StoppingClock()
    player = RobotPlayer(sent.append, clock=clock.clock, sleep=clock.sleep)
    player.play(b"\x00" * BYTES_PER_SECOND, player.current_token())
    assert {"type": "audio_stop"} in sent
    assert {"type": "audio_end"} not in sent
    assert sum(isinstance(m, bytes) for m in sent) < 50


def test_close_makes_play_a_no_op():
    player, sent, _ = make_player()
    player.close()
    player.play(b"\x00" * CHUNK_BYTES, player.current_token())
    assert sent == []


def test_stop_from_another_thread_is_safe():
    player, sent, _ = make_player()
    threads = [threading.Thread(target=player.stop) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert player.current_token() == 10
```

**Step 2: Run them and check they fail.** Expected: `ModuleNotFoundError`.

**Step 3: Implement** (in `link/robot.py`)

```python
"""One robot connection: its mic, its speaker and (Task 5) the link that owns them."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from desk_buddy.audio import BYTES_PER_SECOND, CHUNK_BYTES, PcmBuffer
from desk_buddy.link import protocol

log = logging.getLogger(__name__)

MAX_RECORD_SECONDS = 30.0
MAX_SLEEP = 0.02  # so stop() takes effect within one chunk

Send = Callable[[Any], None]  # thread-safe; takes a protocol dict or PCM bytes


class RobotMic:
    """Audio frames from the robot. The link calls begin()/end() on button down/up, so no
    frame is lost while the orchestrator catches up; start()/stop() are the Mic protocol."""

    def __init__(self, max_seconds: float = MAX_RECORD_SECONDS) -> None:
        self._buffer = PcmBuffer(max_seconds)
        self._recording = False

    def begin(self) -> None:
        self._buffer.take()
        self._recording = True

    def end(self) -> None:
        self._recording = False

    def feed(self, data: bytes) -> None:
        if not self._recording:
            return
        if len(data) % 2:
            log.debug("Dropped odd-length audio frame (%d bytes)", len(data))
            return
        self._buffer.append(data)

    def start(self) -> None:
        """No-op: recording already began when the button went down."""

    def stop(self) -> bytes:
        self._recording = False
        return self._buffer.take()


class RobotPlayer:
    """Sends TTS audio to the robot at real time plus `lead` seconds, so the robot's buffer
    stays small and stop() can still cut a sentence short. Same token scheme as
    audio.Speaker: stop() bumps a generation; play() only sends while its token is current.
    """

    def __init__(
        self,
        send: Send,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        lead: float = 0.2,
    ) -> None:
        self._send = send
        self._clock = clock
        self._sleep = sleep
        self._lead = lead
        self._generation = 0
        self._closed = False
        self._lock = threading.Lock()

    def current_token(self) -> int:
        return self._generation

    def _live(self, token: int) -> bool:
        return not self._closed and token == self._generation

    def play(self, pcm: bytes, token: int) -> None:
        pcm = pcm[: len(pcm) & ~1]
        with self._lock:
            if not self._live(token):
                return
            self._send(protocol.audio_start())
        start = self._clock()
        sent = 0.0
        for i in range(0, len(pcm), CHUNK_BYTES):
            chunk = pcm[i : i + CHUNK_BYTES]
            with self._lock:
                if not self._live(token):
                    return
                self._send(chunk)
            sent += len(chunk) / BYTES_PER_SECOND
            if not self._wait_until(start + sent - self._lead, token):
                return
        with self._lock:
            if not self._live(token):
                return
            self._send(protocol.audio_end())
        self._wait_until(start + sent - self._lead, token)

    def _wait_until(self, deadline: float, token: int) -> bool:
        while (left := deadline - self._clock()) > 0:
            if not self._live(token):
                return False
            self._sleep(min(left, MAX_SLEEP))
        return self._live(token)

    def stop(self) -> None:
        with self._lock:
            self._generation += 1
            if not self._closed:
                self._send(protocol.audio_stop())

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._generation += 1
```

Note on the pacing test: after the last chunk, `play` waits until `start + 1.0 - 0.2`, so the fake clock ends at about 0.8 s.

**Step 4: Run the tests and lint.** Expected: PASS.

**Step 5: Commit.** Message: "Add RobotMic and paced RobotPlayer".

---

### Task 4: DeviceSwitch and the orchestrator's reset event

**Files:**
- Create: `server/src/desk_buddy/link/switch.py`
- Modify: `server/src/desk_buddy/loop.py` (`Orchestrator.handle`)
- Test: `server/tests/test_link_switch.py`, `server/tests/test_loop.py`

**Behaviour**
- `DeviceSwitch(pc_mic, pc_speaker)` has these members:
  - `.mic`, a `SwitchedMic`;
  - `.player`, a `SwitchedPlayer`;
  - `.robot`, the attached robot or `None`;
  - `attach(robot)`, which returns the previously attached robot or `None`;
  - `detach(robot)`, which returns `True` only if that robot was the current one;
  - `send_state(state)` and `send_emotion(name)`, which do nothing without a robot.
- A "robot" here is anything with `.mic`, `.player` and `.send(item)`. `RobotLink` in Task 5 fits that shape.
- **`SwitchedMic`:**
  - `start()` starts the active mic.
  - `stop()` stops both mics, so a PC mic left recording can't keep running, and returns the audio from the active one.
- **`SwitchedPlayer`:**
  - It has its own generation, so tokens never mix between devices.
  - `play(pcm, token)` checks the token under a lock, captures the active device and its current token, then calls the device's `play` outside the lock.
  - `stop()` bumps the generation and stops the PC speaker and the robot, if one is attached.
- **The orchestrator's new `"reset"` event** interrupts any turn, stops the mic if it was listening, and goes idle. The server sends it when a robot attaches or detaches.

**Step 1: Write failing tests**

`tests/test_link_switch.py`:

```python
from desk_buddy.link.switch import DeviceSwitch
from desk_buddy.loop import State


class Mic:
    def __init__(self, pcm):
        self.pcm, self.started, self.stopped = pcm, 0, 0

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1
        return self.pcm


class Player:
    def __init__(self):
        self.gen, self.played, self.stops = 0, [], 0

    def current_token(self):
        return self.gen

    def play(self, pcm, token):
        if token == self.gen:
            self.played.append(pcm)

    def stop(self):
        self.stops += 1
        self.gen += 1


class Robot:
    def __init__(self):
        self.mic, self.player, self.sent = Mic(b"robot"), Player(), []

    def send(self, item):
        self.sent.append(item)


def make():
    pc_mic, pc_speaker = Mic(b"pc"), Player()
    return DeviceSwitch(pc_mic, pc_speaker), pc_mic, pc_speaker


def test_pc_devices_when_no_robot():
    switch, pc_mic, pc_speaker = make()
    switch.mic.start()
    assert switch.mic.stop() == b"pc"
    switch.player.play(b"a", switch.player.current_token())
    assert pc_speaker.played == [b"a"]


def test_robot_takes_over_and_gives_back():
    switch, pc_mic, pc_speaker = make()
    robot = Robot()
    assert switch.attach(robot) is None
    switch.mic.start()
    assert robot.mic.started == 1 and pc_mic.started == 0
    assert switch.mic.stop() == b"robot"
    assert pc_mic.stopped == 1  # both stopped, so the PC mic can't be left recording
    switch.player.play(b"a", switch.player.current_token())
    assert robot.player.played == [b"a"] and pc_speaker.played == []
    assert switch.detach(robot) is True
    assert switch.robot is None
    switch.player.play(b"b", switch.player.current_token())
    assert pc_speaker.played == [b"b"]


def test_attach_returns_the_replaced_robot_and_stale_detach_is_ignored():
    switch, _, _ = make()
    old, new = Robot(), Robot()
    switch.attach(old)
    assert switch.attach(new) is old
    assert switch.detach(old) is False
    assert switch.robot is new


def test_stop_invalidates_tokens_and_stops_every_device():
    switch, _, pc_speaker = make()
    robot = Robot()
    switch.attach(robot)
    token = switch.player.current_token()
    switch.player.stop()
    switch.player.play(b"late", token)
    assert robot.player.played == [] and pc_speaker.played == []
    assert robot.player.stops == 1 and pc_speaker.stops == 1


def test_state_and_emotion_go_to_the_robot_only_when_attached():
    switch, _, _ = make()
    switch.send_state(State.IDLE)  # no robot: nothing happens
    robot = Robot()
    switch.attach(robot)
    switch.send_state(State.THINKING)
    switch.send_emotion("happy")
    assert robot.sent == [
        {"type": "state", "name": "thinking"},
        {"type": "emotion", "name": "happy"},
    ]
```

Append to `tests/test_loop.py`:

```python
def test_reset_interrupts_a_turn_and_goes_idle():
    speaker = FakeSpeaker(block=True)
    orch, _, states = make(speaker=speaker)

    async def scenario():
        await orch.handle("down")
        await orch.handle("up")
        assert await asyncio.to_thread(speaker.started.wait, 2)
        await orch.handle("reset")

    asyncio.run(scenario())
    assert speaker.stops == 1
    assert orch.state is State.IDLE
    assert states[-1] is State.IDLE


def test_reset_while_listening_stops_the_mic():
    mic = FakeMic()
    mic.stops = 0
    original = mic.stop

    def counting_stop():
        mic.stops += 1
        return original()

    mic.stop = counting_stop
    orch, _, states = make(mic=mic)

    async def scenario():
        await orch.handle("down")
        await orch.handle("reset")

    asyncio.run(scenario())
    assert mic.stops == 1
    assert states == [State.LISTENING, State.IDLE]
```

**Step 2: Run them and check they fail.**

**Step 3: Implement**

`link/switch.py`:

```python
"""Route the voice loop's mic and speaker to the robot when one is attached, else the PC."""

from __future__ import annotations

import threading
from typing import Any, Protocol

from desk_buddy.link import protocol
from desk_buddy.loop import Mic, Player, State


class Robot(Protocol):
    mic: Mic
    player: Player

    def send(self, item: Any) -> None: ...


class DeviceSwitch:
    def __init__(self, pc_mic: Mic, pc_speaker: Player) -> None:
        self.robot: Robot | None = None
        self.mic = SwitchedMic(self, pc_mic)
        self.player = SwitchedPlayer(self, pc_speaker)

    def attach(self, robot: Robot) -> Robot | None:
        old, self.robot = self.robot, robot
        return old

    def detach(self, robot: Robot) -> bool:
        if self.robot is not robot:
            return False
        self.robot = None
        return True

    def send_state(self, state: State) -> None:
        if self.robot is not None:
            self.robot.send(protocol.state(state.value))

    def send_emotion(self, name: str) -> None:
        if self.robot is not None:
            self.robot.send(protocol.emotion(name))


class SwitchedMic:
    def __init__(self, switch: DeviceSwitch, pc: Mic) -> None:
        self._switch, self._pc = switch, pc

    def _active(self) -> Mic:
        robot = self._switch.robot
        return robot.mic if robot is not None else self._pc

    def start(self) -> None:
        self._active().start()

    def stop(self) -> bytes:
        active = self._active()
        pcm = active.stop()
        if active is not self._pc:
            self._pc.stop()  # never leave the PC mic recording
        return pcm


class SwitchedPlayer:
    """Own generation counter, so tokens never mix between devices."""

    def __init__(self, switch: DeviceSwitch, pc: Player) -> None:
        self._switch, self._pc = switch, pc
        self._generation = 0
        self._lock = threading.Lock()

    def current_token(self) -> int:
        return self._generation

    def play(self, pcm: bytes, token: int) -> None:
        with self._lock:
            if token != self._generation:
                return
            robot = self._switch.robot
            device = robot.player if robot is not None else self._pc
            inner = device.current_token()
        # Outside the lock: play blocks. A stop() after this point bumps the device's
        # own token too, so this play stops at its next chunk.
        device.play(pcm, inner)

    def stop(self) -> None:
        with self._lock:
            self._generation += 1
            robot = self._switch.robot
        self._pc.stop()
        if robot is not None:
            robot.player.stop()
```

In `loop.py`, change `Orchestrator.handle`. Add this branch after the `"up"` branch, and update the docstring on `run`/`handle` to say the events are `"down"`, `"up"` and `"reset"`:

```python
        elif event == "reset":
            # A robot attached or detached: drop whatever was happening and wait.
            await self._interrupt()
            if self.state is State.LISTENING:
                self.mic.stop()
            self._set_state(State.IDLE)
```

**Step 4: Run all tests and lint.** Expected: PASS.

**Step 5: Commit.** Message: "Add DeviceSwitch and a reset event for robot takeover".

---

### Task 5: RobotLink, the FastAPI route, the server runner and the fake robot client

**Files:**
- Modify: `server/src/desk_buddy/link/robot.py` (add `RobotLink`)
- Create: `server/src/desk_buddy/server.py`
- Create: `server/src/desk_buddy/fake_robot.py` (the `FakeRobot` client class only; the command-line tool comes in Task 8)
- Test: `server/tests/test_server.py`

**`RobotLink`** (in `link/robot.py`):

```python
class RobotLink:
    """One connected robot. All sends go through one outbox and one sender task, so frames
    keep their order and never interleave. send() is safe from any thread."""

    def __init__(self, ws: Any, loop: asyncio.AbstractEventLoop, fw_version: str = "") -> None:
        self._ws = ws
        self._loop = loop
        self._outbox: asyncio.Queue[Any] = asyncio.Queue()
        self.fw_version = fw_version
        self.closed = False
        self.mic = RobotMic()
        self.player = RobotPlayer(self.send)

    def send(self, item: Any) -> None:
        if self.closed:
            return
        try:
            self._loop.call_soon_threadsafe(self._outbox.put_nowait, item)
        except RuntimeError:  # loop already closed during shutdown
            pass

    async def run_sender(self) -> None:
        """Runs until close(). A failed send closes the link (treated as a disconnect)."""
        try:
            while (item := await self._outbox.get()) is not None:
                if isinstance(item, bytes):
                    await self._ws.send_bytes(item)
                else:
                    await self._ws.send_json(item)
        except Exception as exc:
            log.warning("Send to robot failed: %s", exc)
            await self.close(1011)

    async def close(self, code: int = 1000) -> None:
        if self.closed:
            return
        self.closed = True
        self.player.close()
        self._outbox.put_nowait(None)
        with contextlib.suppress(Exception):
            await self._ws.close(code=code)
```

Add `import asyncio` and `import contextlib` at the top of `robot.py`.

**`server.py`:**

```python
"""The FastAPI app (robot WebSocket now, dashboard in M8) and an in-loop uvicorn runner."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import socket
from collections.abc import Callable
from dataclasses import dataclass

import uvicorn
from fastapi import FastAPI, WebSocket

from desk_buddy.link import protocol
from desk_buddy.link.protocol import ProtocolError
from desk_buddy.link.robot import RobotLink
from desk_buddy.link.switch import DeviceSwitch

log = logging.getLogger(__name__)

CLOSE_BAD_TOKEN = 4001
CLOSE_NO_HELLO = 4002
CLOSE_REPLACED = 4003
MAX_FRAME_BYTES = 64 * 1024
PING_SECONDS = 20.0


class ServerStartError(Exception):
    """The server could not listen (port in use, bad host)."""


def create_app(
    *,
    token: str,
    switch: DeviceSwitch,
    events: asyncio.Queue[str],
    current_state: Callable[[], str],
    hello_timeout: float = 5.0,
) -> FastAPI:
    app = FastAPI(title="Desk Buddy", docs_url=None, redoc_url=None, openapi_url=None)

    @app.websocket("/robot")
    async def robot(ws: WebSocket) -> None:
        await ws.accept()
        hello = await _read_hello(ws, hello_timeout)
        if hello is None:
            await ws.close(code=CLOSE_NO_HELLO)
            log.warning("Robot refused: no hello within %.0f s", hello_timeout)
            return
        if not hmac.compare_digest(hello.data["token"].encode(), token.encode()):
            await ws.close(code=CLOSE_BAD_TOKEN)
            log.warning("Robot refused: bad token")
            return
        link = RobotLink(ws, asyncio.get_running_loop(), hello.data["fw_version"])
        old = switch.attach(link)
        if old is not None:
            log.info("New robot connection replaces the old one")
            await old.close(CLOSE_REPLACED)
        log.info("Robot connected (firmware %s)", link.fw_version or "unknown")
        link.send(protocol.welcome())
        link.send(protocol.state(current_state()))
        events.put_nowait("reset")
        sender = asyncio.create_task(link.run_sender())
        try:
            await _receive_loop(ws, link, events)
        finally:
            await link.close()
            await sender
            if switch.detach(link):
                log.info("Robot disconnected")
                events.put_nowait("reset")

    return app


async def _read_hello(ws: WebSocket, timeout: float) -> protocol.Message | None:
    try:
        async with asyncio.timeout(timeout):
            frame = await ws.receive()
    except TimeoutError:
        return None
    text = frame.get("text")
    if text is None:
        return None
    try:
        msg = protocol.parse(text)
    except ProtocolError:
        return None
    return msg if msg.type == "hello" else None


async def _receive_loop(ws: WebSocket, link: RobotLink, events: asyncio.Queue[str]) -> None:
    while not link.closed:
        try:
            frame = await ws.receive()
        except Exception:  # socket closed under us (e.g. replaced)
            return
        if frame["type"] == "websocket.disconnect":
            return
        if (data := frame.get("bytes")) is not None:
            link.mic.feed(data)
            continue
        try:
            msg = protocol.parse(frame.get("text") or "")
        except ProtocolError as exc:
            log.warning("Ignored bad message from robot: %s", exc)
            continue
        if msg.type == "button":
            if msg.data["action"] == "down":
                link.mic.begin()
            else:
                link.mic.end()
            events.put_nowait(msg.data["action"])
        elif msg.type == "stats":
            log.debug("Robot stats: %s", msg.data["data"])
        elif msg.type == "hello":
            log.warning("Ignored a second hello")
        else:
            log.info("Robot %s (not used yet)", msg.type)


class _Server(uvicorn.Server):
    @contextlib.contextmanager
    def capture_signals(self):  # Desk Buddy owns Ctrl+C, not uvicorn
        yield


@dataclass
class RunningServer:
    server: uvicorn.Server
    task: asyncio.Task[None]
    port: int

    async def stop(self) -> None:
        self.server.should_exit = True
        await self.task


async def start_server(app: FastAPI, host: str, port: int) -> RunningServer:
    """Bind first (so a busy port is a clear error), then serve in this event loop."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, port))
    except OSError as exc:
        sock.close()
        raise ServerStartError(f"Can't listen on {host}:{port}: {exc}") from exc
    config = uvicorn.Config(
        app,
        log_level="warning",
        lifespan="off",
        ws_max_size=MAX_FRAME_BYTES,
        ws_ping_interval=PING_SECONDS,
        ws_ping_timeout=PING_SECONDS,
    )
    server = _Server(config)
    task = asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started:
        if task.done():
            task.result()  # re-raise why it stopped
            raise ServerStartError("server stopped during startup")
        await asyncio.sleep(0.01)
    return RunningServer(server, task, sock.getsockname()[1])
```

Builder: check the Step 1 output from Task 1.
- If this uvicorn version has no `capture_signals`, find how it installs signal handlers (`install_signal_handlers` in older versions) and override that instead.
- If `ws.receive()` raises on a socket we closed rather than returning a disconnect frame, the broad `except` in `_receive_loop` covers it. Keep it.

**`fake_robot.py`** (the client class):

```python
"""A stand-in robot: a WebSocket client for tests, and (Task 8) a voice tool."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from desk_buddy.audio import CHUNK_BYTES


class FakeRobot:
    def __init__(self, url: str, token: str, fw_version: str = "fake-robot") -> None:
        self.url, self.token, self.fw_version = url, token, fw_version
        self.ws: ClientConnection | None = None
        self.close_code: int | None = None

    async def connect(self, *, hello: bool = True) -> None:
        self.ws = await connect(self.url, max_size=2**16)
        if hello:
            await self.send({"type": "hello", "token": self.token, "fw_version": self.fw_version})

    async def send(self, msg: dict[str, Any]) -> None:
        await self.ws.send(json.dumps(msg))

    async def press(self) -> None:
        await self.send({"type": "button", "action": "down"})

    async def release(self) -> None:
        await self.send({"type": "button", "action": "up"})

    async def send_audio(self, pcm: bytes) -> None:
        for i in range(0, len(pcm), CHUNK_BYTES):
            await self.ws.send(pcm[i : i + CHUNK_BYTES])

    async def recv(self, timeout: float = 5.0) -> dict[str, Any] | bytes | None:
        """The next frame: a dict for JSON text, bytes for audio, None once closed."""
        try:
            frame = await asyncio.wait_for(self.ws.recv(), timeout)
        except ConnectionClosed as exc:
            self.close_code = exc.rcvd.code if exc.rcvd else None
            return None
        return frame if isinstance(frame, bytes) else json.loads(frame)

    async def recv_until(self, kind: str, timeout: float = 5.0) -> list[Any]:
        """Frames up to and including the first {"type": kind}. Raises if closed first."""
        frames = []
        while True:
            frame = await self.recv(timeout)
            if frame is None:
                raise ConnectionError(f"closed ({self.close_code}) before {kind!r}")
            frames.append(frame)
            if isinstance(frame, dict) and frame.get("type") == kind:
                return frames

    async def close(self) -> None:
        if self.ws is not None:
            await self.ws.close()
```

**Step 1: Write failing tests** in `tests/test_server.py`. These cover auth and connections only; the full turn comes in Task 6.

```python
import asyncio
import contextlib

import pytest

from desk_buddy.fake_robot import FakeRobot
from desk_buddy.link.switch import DeviceSwitch
from desk_buddy.server import (
    CLOSE_BAD_TOKEN,
    CLOSE_NO_HELLO,
    CLOSE_REPLACED,
    ServerStartError,
    create_app,
    start_server,
)

TOKEN = "t" * 20


class NullMic:
    def start(self):
        pass

    def stop(self):
        return b""


class NullPlayer:
    def current_token(self):
        return 0

    def play(self, pcm, token):
        pass

    def stop(self):
        pass


@contextlib.asynccontextmanager
async def running(hello_timeout=5.0):
    switch = DeviceSwitch(NullMic(), NullPlayer())
    events: asyncio.Queue[str] = asyncio.Queue()
    app = create_app(
        token=TOKEN,
        switch=switch,
        events=events,
        current_state=lambda: "idle",
        hello_timeout=hello_timeout,
    )
    server = await start_server(app, "127.0.0.1", 0)
    try:
        yield f"ws://127.0.0.1:{server.port}/robot", switch, events
    finally:
        await server.stop()


def test_good_hello_gets_welcome_and_state():
    async def run():
        async with running() as (url, switch, events):
            robot = FakeRobot(url, TOKEN)
            await robot.connect()
            assert await robot.recv() == {"type": "welcome"}
            assert await robot.recv() == {"type": "state", "name": "idle"}
            assert switch.robot is not None
            assert await asyncio.wait_for(events.get(), 2) == "reset"
            await robot.close()

    asyncio.run(run())


def test_bad_token_is_closed_4001():
    async def run():
        async with running() as (url, switch, _):
            robot = FakeRobot(url, "wrong-token-wrong-token")
            await robot.connect()
            assert await robot.recv() is None
            assert robot.close_code == CLOSE_BAD_TOKEN
            assert switch.robot is None

    asyncio.run(run())


def test_no_hello_is_closed_4002():
    async def run():
        async with running(hello_timeout=0.2) as (url, _, _):
            robot = FakeRobot(url, TOKEN)
            await robot.connect(hello=False)
            assert await robot.recv() is None
            assert robot.close_code == CLOSE_NO_HELLO

    asyncio.run(run())


def test_new_connection_replaces_old_with_4003():
    async def run():
        async with running() as (url, switch, _):
            old, new = FakeRobot(url, TOKEN), FakeRobot(url, TOKEN)
            await old.connect()
            await old.recv_until("state")
            await new.connect()
            await new.recv_until("state")
            assert await old.recv() is None
            assert old.close_code == CLOSE_REPLACED
            assert switch.robot is not None
            await new.close()

    asyncio.run(run())


def test_disconnect_detaches_and_sends_reset():
    async def run():
        async with running() as (url, switch, events):
            robot = FakeRobot(url, TOKEN)
            await robot.connect()
            await robot.recv_until("state")
            assert await asyncio.wait_for(events.get(), 2) == "reset"
            await robot.close()
            assert await asyncio.wait_for(events.get(), 2) == "reset"
            assert switch.robot is None

    asyncio.run(run())


def test_bad_messages_are_ignored_not_fatal():
    async def run():
        async with running() as (url, switch, events):
            robot = FakeRobot(url, TOKEN)
            await robot.connect()
            await robot.recv_until("state")
            await robot.ws.send("not json")
            await robot.send({"type": "dance"})
            await robot.press()
            assert await asyncio.wait_for(events.get(), 2) == "reset"
            assert await asyncio.wait_for(events.get(), 2) == "down"
            assert switch.robot is not None
            await robot.close()

    asyncio.run(run())


def test_busy_port_is_a_clear_error():
    async def run():
        async with running() as (url, _, _):
            port = int(url.rsplit(":", 1)[1].split("/")[0])
            app = create_app(
                token=TOKEN,
                switch=DeviceSwitch(NullMic(), NullPlayer()),
                events=asyncio.Queue(),
                current_state=lambda: "idle",
            )
            with pytest.raises(ServerStartError, match="Can't listen"):
                await start_server(app, "127.0.0.1", port)

    asyncio.run(run())
```

**Step 2: Run them and check they fail.**

**Step 3: Implement** `RobotLink`, `server.py` and the `FakeRobot` class as shown above.

**Step 4: Run all the tests, twice, to catch flaky timing.** Expected: PASS both times.
- If `test_busy_port_is_a_clear_error` doesn't fail to bind on Windows, check whether `SO_EXCLUSIVEADDRUSE` is needed.
- Don't set `SO_REUSEADDR`: on Windows it lets another process steal the port.

**Step 5: Commit.** Message: "Add the robot WebSocket route, uvicorn runner and fake robot client".

---

### Task 6: A full turn through the robot link

**Files:**
- Test: `server/tests/test_server.py` (append)

This task wires a real `Orchestrator`, using fakes for STT, LLM and TTS, behind the server. It checks:
- one complete turn, in order;
- that an interrupt sends `audio_stop`;
- that a disconnect mid-turn returns the loop to idle.

Fix any bugs it finds in Tasks 3–5.

**Step 1: Write the tests** (append to `tests/test_server.py`):

```python
from desk_buddy.audio import BYTES_PER_SECOND
from desk_buddy.brain.memory import Conversation
from desk_buddy.loop import Orchestrator, State


class EchoTranscriber:
    def __init__(self):
        self.heard = []

    def transcribe(self, pcm):
        self.heard.append(len(pcm))
        return "hello buddy"


class OneLineChat:
    async def stream_reply(self, history, text):
        yield "[happy] Hi there. "
        yield "Nice to see you."


class ToneSynth:
    def synthesize(self, text):
        return b"\x01\x00" * (BYTES_PER_SECOND // 10)  # 0.1 s per sentence


@contextlib.asynccontextmanager
async def buddy():
    pc_player = NullPlayer()
    switch = DeviceSwitch(NullMic(), pc_player)
    events: asyncio.Queue[str] = asyncio.Queue()
    transcriber = EchoTranscriber()
    orch = Orchestrator(
        mic=switch.mic,
        transcriber=transcriber,
        chat=OneLineChat(),
        synthesizer=ToneSynth(),
        speaker=switch.player,
        conversation=Conversation(),
        on_state=switch.send_state,
        on_emotion=switch.send_emotion,
    )
    app = create_app(
        token=TOKEN, switch=switch, events=events, current_state=lambda: orch.state.value
    )
    server = await start_server(app, "127.0.0.1", 0)
    loop_task = asyncio.create_task(orch.run(events))
    try:
        yield f"ws://127.0.0.1:{server.port}/robot", orch, transcriber
    finally:
        loop_task.cancel()
        await orch.shutdown()
        await server.stop()


def text_frames(frames):
    return [f for f in frames if isinstance(f, dict)]


def test_full_turn_through_the_robot():
    async def run():
        async with buddy() as (url, orch, transcriber):
            robot = FakeRobot(url, TOKEN)
            await robot.connect()
            await robot.recv_until("state")
            await robot.press()
            await robot.send_audio(b"\x00\x00" * BYTES_PER_SECOND // 2)  # 0.5 s
            await robot.release()
            frames = await robot.recv_until("idle_marker_never") if False else []
            # Read until Buddy is idle again.
            while True:
                frame = await robot.recv()
                assert frame is not None
                frames.append(frame)
                if frame == {"type": "state", "name": "idle"}:
                    break
            texts = text_frames(frames)
            kinds = [(t["type"], t.get("name")) for t in texts]
            assert ("state", "listening") in kinds
            assert kinds.index(("state", "thinking")) < kinds.index(("emotion", "happy"))
            assert kinds.index(("emotion", "happy")) < kinds.index(("state", "speaking"))
            assert sum(t["type"] == "audio_start" for t in texts) == 2
            assert sum(t["type"] == "audio_end" for t in texts) == 2
            audio = b"".join(f for f in frames if isinstance(f, bytes))
            assert len(audio) == 2 * (BYTES_PER_SECOND // 10) * 2 // 2  # two 0.1 s sentences
            assert transcriber.heard == [BYTES_PER_SECOND]  # 0.5 s of int16 = 16000 bytes
            await robot.close()

    asyncio.run(run())
```

Builder notes:
- Clean up the leftover `recv_until("idle_marker_never") if False` line: just start with `frames = []`. Fix the byte arithmetic so it reads clearly:
  - 0.5 s of int16 at 16 kHz is `BYTES_PER_SECOND // 2` bytes.
  - Two 0.1 s sentences are `2 * (BYTES_PER_SECOND // 10)` bytes.
- Then add these tests:

```python
def test_button_press_mid_reply_sends_audio_stop():
    # Use a synth that returns 2 s per sentence so the reply is still playing.
    # press() while state == speaking -> expect {"type": "audio_stop"} before the next
    # {"type": "state", "name": "listening"}.
    ...


def test_disconnect_mid_reply_returns_to_idle_on_the_pc():
    # Long reply; once state == speaking, close the robot. Then poll orch.state for up to
    # 2 s: it becomes State.IDLE, and the switch has no robot.
    ...
```

Write both tests in full, following the pattern of `test_full_turn_through_the_robot`. Make `buddy()` take a `synth` argument.

**Step 2: Run the tests.** Expected: they should pass if Tasks 3–5 are right. If one fails, use @superpowers:systematic-debugging, fix the cause in `src/`, and add a focused unit test for it.

**Step 3: Run the whole suite twice and lint.**

**Step 4: Commit.** Message: "Test a full turn, interrupt and disconnect through the robot link".

---

### Task 7: Run the server from `__main__`

**Files:**
- Modify: `server/src/desk_buddy/__main__.py`
- Test: `server/tests/test_main.py`

**Behaviour**
- Build `DeviceSwitch(mic, speaker)`. Give the orchestrator `mic=switch.mic`, `speaker=switch.player`, `on_state=switch.send_state` and `on_emotion=switch.send_emotion`.
- **Key presses.** The watcher callback becomes:
  ```python
  def on_key(event: str) -> None:
      if switch.robot is None:  # the robot takes over the button
          events.put_nowait(event)
  watcher = KeyWatcher(parse_key(settings.ptt_key), lambda e: loop.call_soon_threadsafe(on_key, e))
  ```
- **If `settings.robot_token` is set:**
  - Build the app with `create_app(token=..., switch=..., events=..., current_state=lambda: orchestrator.state.value)`.
  - Start it with `server = await start_server(app, settings.server_host, settings.server_port)`.
  - Register `stack.push_async_callback(server.stop)`.
  - Log `Robot link: on (ws://{host}:{port}/robot)`.
  - Turn `ServerStartError` into `StartupError`, so `main()` prints `Can't start: ...` and returns 2.
- **Otherwise**, log `Robot link: off (no ROBOT_TOKEN)`.
- Start the server *after* the orchestrator is built and *before* the watcher starts.
- **Shutdown order** (the exit stack reverses it): watcher stopped, server stopped, orchestrator shutdown, speaker, mic, synthesizer.

**Step 1: Write failing tests** (in `tests/test_main.py`)
- Patch `entry.start_server` and `entry.create_app` with fakes that record calls.
- `test_robot_link_off_without_token`: the existing settings have no token, so `start_server` is never called. The existing order test still passes unchanged.
- `test_robot_link_on_with_token`: `load_settings` returns `Settings(anthropic_api_key="k", robot_token="t"*20)`. Check that `start_server` was called with `("127.0.0.1", 8765)` and that the shutdown order puts `"server stopped"` between `"watcher stopped"` and `"orchestrator shutdown"`.
- `test_busy_port_is_a_clear_startup_error`: the fake `start_server` raises `ServerStartError("Can't listen on 127.0.0.1:8765: in use")`. Check that `main()` returns 2, stderr contains `Can't start: Can't listen`, and the devices built so far are closed.

**Step 2: Run them and check they fail.**

**Step 3: Implement** as described.

**Step 4: Run all tests and lint.**

**Step 5: Commit.** Message: "Run the robot link server alongside the voice loop".

---

### Task 8: The fake robot voice tool

**Files:**
- Modify: `server/src/desk_buddy/audio.py` (add `Microphone.drain()`)
- Modify: `server/src/desk_buddy/fake_robot.py` (add `main()`)
- Test: `server/tests/test_audio.py` (drain), `server/tests/test_fake_robot.py` (the URL helper)

**Behaviour of `python -m desk_buddy.fake_robot`**
- **Startup:**
  - It reads settings with `load_settings()` and needs `ROBOT_TOKEN`; without it, it prints a clear message and returns 2.
  - The URL is `ws://{host}:{port}/robot`. If the host is `0.0.0.0` it connects to `127.0.0.1`; this logic lives in a pure `robot_url(settings)` function.
  - It opens the PC `Microphone()` and `Speaker()`, and a `KeyWatcher` on `settings.ptt_key`.
  - Buddy (the server) must not be running with `PTT_KEY` active at the same time. While a robot is connected the server ignores the key, so this is fine.
- **Key down:** send `button down`, then start a task that calls `mic.drain()` every 20 ms and sends whatever audio it returns.
- **Key up:** stop that task, send a final `drain()`, `mic.stop()`, then `button up`.
- **Received frames:**
  - `state` and `emotion` are logged (`Robot state: thinking`, `Robot emotion: happy`).
  - Audio bytes go into a `queue.Queue` that a playback thread plays with `speaker.play(chunk, token)`. The thread uses the token captured when the chunk was queued.
  - `audio_stop` calls `speaker.stop()` and empties the queue.
- **Exit:** Ctrl+C or the server closing the connection exits cleanly and logs the close code.

**`Microphone.drain()`:**

```python
    def drain(self) -> bytes:
        """Audio captured so far, without stopping the recording (for streaming)."""
        return self._buffer.take()
```

**Step 1: Write failing tests**
- In `test_audio.py`, test `drain` with the existing `PcmBuffer` or `Microphone` test pattern. If `Microphone` needs a real device in the tests, test through `PcmBuffer.take()` semantics and keep `drain` a one-line delegate.
- In `test_fake_robot.py`:

```python
from desk_buddy.config import Settings
from desk_buddy.fake_robot import robot_url


def test_robot_url_uses_loopback_for_wildcard_host():
    s = Settings(anthropic_api_key="k", server_host="0.0.0.0", server_port=9000)
    assert robot_url(s) == "ws://127.0.0.1:9000/robot"


def test_robot_url_keeps_a_specific_host():
    s = Settings(anthropic_api_key="k", server_host="192.168.1.5")
    assert robot_url(s) == "ws://192.168.1.5:8765/robot"
```

**Step 2: Run them and check they fail.**

**Step 3: Implement** `drain`, `robot_url` and `main()`, plus `if __name__ == "__main__": sys.exit(main())`. Keep `main()` small: the asyncio code for the key events, the mic streaming task, the receive loop and the playback thread. It is checked by hand, not by unit tests.

**Step 4: Smoke-test without audio.** In one terminal, run Buddy with a test token. In another, run `uv run python -c "import desk_buddy.fake_robot"`; it must import cleanly. The full voice check is Task 9.

**Step 5: Run all tests and lint, then commit.** Message: "Add the fake robot voice tool".

---

### Task 9: Protocol doc, voice check and docs

**Files:**
- Create: `docs/protocol.md`
- Modify: `docs/decisions.md`, `docs/status.md`, `CLAUDE.md`, `server/README.md`

**Step 1: Write `docs/protocol.md`**
- Cover the connection, the hello and the close codes (4001, 4002, 4003).
- Include every message in both directions with a JSON example, and mark the reserved ones (`look`, `led`, `volume`, `wake`).
- Describe the audio format and framing, and the server pacing (about 200 ms ahead).
- State the limits: 64 KB frames, pings every 20 s, a 30 s recording cap.
- Add a "Going on the LAN (M6)" section:
  - Set `SERVER_HOST=0.0.0.0`.
  - Add the firewall rule from an elevated PowerShell:
    `New-NetFirewallRule -DisplayName "Desk Buddy robot link" -Direction Inbound -Protocol TCP -LocalPort 8765 -Action Allow -Profile Private`
  - Make sure the Wi-Fi network is set to Private.

**Step 2: Voice check by the user**
- The user adds `ROBOT_TOKEN` to `server/.env`; Claude never edits `.env`.
- Terminal 1: `uv run python -m desk_buddy`. The log should say `Robot link: on (ws://127.0.0.1:8765/robot)`.
- Terminal 2: `uv run python -m desk_buddy.fake_robot`. Terminal 1 should log `Robot connected (firmware fake-robot)`.
- Hold Right Ctrl and ask a question. The fake robot logs the states and the emotion, and the reply plays through it.
- Press mid-reply: the reply stops.
- Ctrl+C the fake robot: Buddy logs `Robot disconnected`, and Right Ctrl talks directly to Buddy again.

**Step 3: Update the docs**
- **`decisions.md`, new M4 section:**
  - FastAPI and uvicorn in the same loop;
  - the robot taking over the devices;
  - the newest connection replacing the old one;
  - localhost by default;
  - the pacing lead;
  - tests using a real loopback server instead of httpx and TestClient;
  - the `wake` message reserved.
- **`CLAUDE.md`:**
  - Server stack: FastAPI decided.
  - Protocol section: point to `docs/protocol.md` as final, with a short summary.
  - Repo layout: the `link/` package.
- **`README.md`:** `ROBOT_TOKEN`, how to generate it, and how to run the fake robot.
- **`status.md`:** M4 done; next is M5/M6.

**Step 4: Run all tests and lint, then commit.** Message: "Close M4: protocol doc and robot link docs".
