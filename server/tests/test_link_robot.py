import asyncio
import threading

from desk_buddy.audio import BYTES_PER_SECOND, CHUNK_BYTES
from desk_buddy.link.robot import RobotLink, RobotMic, RobotPlayer


class FakeWebSocket:
    def __init__(self, fail_after: int | None = None):
        self.sent, self.closed_with = [], None
        self.fail_after = fail_after

    async def _record(self, item):
        if self.fail_after is not None and len(self.sent) >= self.fail_after:
            raise ConnectionError("socket gone")
        self.sent.append(item)

    async def send_bytes(self, data):
        await self._record(data)

    async def send_json(self, data):
        await self._record(data)

    async def close(self, code=1000):
        self.closed_with = code


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


def test_mic_stop_takes_the_buffer_but_does_not_end_recording():
    # A fast re-press: the orchestrator handles the first "up" (stop) after the link has
    # already begun the second clip. The second clip's start must not be lost.
    mic = RobotMic()
    mic.begin()
    mic.feed(b"\x01\x00")
    assert mic.stop() == b"\x01\x00"
    mic.feed(b"\x02\x00")  # still recording: begin() was not followed by end()
    assert mic.stop() == b"\x02\x00"
    mic.end()
    mic.feed(b"\x03\x00")
    assert mic.stop() == b""


def test_mic_is_capped():
    mic = RobotMic(max_seconds=0.01)
    mic.begin()
    mic.feed(b"\x00" * BYTES_PER_SECOND)
    assert len(mic.stop()) == int(0.01 * BYTES_PER_SECOND)


def test_play_sends_start_chunks_end():
    player, sent, _ = make_player()
    pcm = b"\x01\x00" * (CHUNK_BYTES // 2) * 3  # 60 ms
    player.play(pcm, player.current_token())
    assert sent[0] == {"type": "audio_start", "rate": 16000}
    assert sent[1:4] == [
        pcm[:CHUNK_BYTES],
        pcm[CHUNK_BYTES : 2 * CHUNK_BYTES],
        pcm[2 * CHUNK_BYTES :],
    ]
    assert sent[4] == {"type": "audio_end"}
    assert len(sent) == 5


def test_play_drops_a_trailing_odd_byte():
    player, sent, _ = make_player()
    player.play(b"\x01\x00\x02", player.current_token())
    assert sent[1] == b"\x01\x00"


def test_play_is_paced_to_real_time_with_lead():
    player, sent, clock = make_player(lead=0.2)
    pcm = b"\x00" * BYTES_PER_SECOND  # 1 s
    player.play(pcm, player.current_token())
    # It returns when about `lead` of the sentence is left, so it blocked about 0.8 s.
    assert 0.75 <= clock.now <= 0.85
    assert max(clock.slept) <= 0.02 + 1e-9
    assert sent[-1] == {"type": "audio_end"}


def test_play_never_runs_more_than_lead_ahead():
    sent, clock = [], FakeClock()
    ahead = []

    def send(item):
        sent.append(item)
        if isinstance(item, bytes):
            audio_sent = sum(len(m) for m in sent if isinstance(m, bytes)) / BYTES_PER_SECOND
            ahead.append(audio_sent - clock.now)

    player = RobotPlayer(send, clock=clock.clock, sleep=clock.sleep, lead=0.2)
    player.play(b"\x00" * BYTES_PER_SECOND, player.current_token())
    # A chunk is sent once the previous one is within `lead`, so at most lead + one chunk.
    assert max(ahead) <= 0.2 + CHUNK_BYTES / BYTES_PER_SECOND + 1e-9


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


def test_stop_bumps_the_token_even_if_send_fails():
    def send(item):
        raise ConnectionError("socket gone")

    player = RobotPlayer(send)
    player.stop()  # must not raise: the orchestrator's interrupt must finish
    assert player.current_token() == 1


def test_stop_after_close_sends_nothing():
    player, sent, _ = make_player()
    player.close()
    player.stop()
    assert sent == []


def test_stop_from_another_thread_is_safe():
    player, sent, _ = make_player()
    threads = [threading.Thread(target=player.stop) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert player.current_token() == 10
    assert sent == [{"type": "audio_stop"}] * 10


def test_link_sends_in_order_from_any_thread_until_closed():
    ws = FakeWebSocket()

    async def run():
        link = RobotLink(ws, asyncio.get_running_loop(), "1.0")
        sender = asyncio.create_task(link.run_sender())
        link.send({"type": "welcome"})
        await asyncio.to_thread(link.send, b"\x01\x00")
        link.send({"type": "audio_end"})
        await asyncio.sleep(0.05)
        await link.close(4003)
        link.send({"type": "late"})  # after close: dropped
        await asyncio.wait_for(sender, 2)
        return link

    link = asyncio.run(run())
    assert ws.sent == [{"type": "welcome"}, b"\x01\x00", {"type": "audio_end"}]
    assert ws.closed_with == 4003
    assert link.closed and link.fw_version == "1.0"


def test_link_close_halts_its_player():
    ws = FakeWebSocket()

    async def run():
        link = RobotLink(ws, asyncio.get_running_loop())
        token = link.player.current_token()
        await link.close()
        link.player.play(b"\x00" * CHUNK_BYTES, token)
        link.player.stop()

    asyncio.run(run())
    assert ws.sent == []


def test_a_failed_send_closes_the_link():
    ws = FakeWebSocket(fail_after=1)

    async def run():
        link = RobotLink(ws, asyncio.get_running_loop())
        sender = asyncio.create_task(link.run_sender())
        link.send({"type": "welcome"})
        link.send({"type": "state", "name": "idle"})
        await asyncio.wait_for(sender, 2)
        return link

    link = asyncio.run(run())
    assert link.closed
    assert ws.sent == [{"type": "welcome"}]
    assert ws.closed_with == 1011
