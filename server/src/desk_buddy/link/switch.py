"""Route the voice loop's mic and speaker to the robot when one is attached, else the PC."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any, Protocol

from desk_buddy.link import protocol
from desk_buddy.loop import Mic, Player, State

log = logging.getLogger(__name__)


class LinkMic(Mic, Protocol):
    """A robot's mic: begin()/end() follow the talk button, start()/stop() the loop."""

    on_frame: Callable[[bytes], None] | None

    def begin(self) -> None: ...
    def end(self) -> None: ...


class Robot(Protocol):
    mic: LinkMic
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

    def begin_hands_free(self, on_frame: Callable[[bytes], None]) -> bool:
        """Start recording the robot's mic with no key held (after a wake word), with
        every frame also passed to on_frame. False (and nothing happens) without a robot:
        the PC mic has no wake word."""
        robot = self._switch.robot
        if robot is None:
            return False
        robot.mic.begin()
        robot.mic.on_frame = on_frame
        return True

    def end_hands_free(self, *, keep_recording: bool = False) -> None:
        """Stop passing frames on, and end the recording unless the key has taken it
        over (keep_recording). The loop then takes the clip with stop()."""
        robot = self._switch.robot
        if robot is None:
            return
        robot.mic.on_frame = None
        if not keep_recording:
            robot.mic.end()

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
        # Each device in its own try, so one failing device can't keep the other playing.
        try:
            self._pc.stop()
        except Exception:
            log.exception("Failed to stop the PC speaker")
        if robot is not None:
            try:
                robot.player.stop()
            except Exception:
                log.exception("Failed to stop the robot's speaker")
