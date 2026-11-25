"""The dashboard's event bus: never blocks or raises into CARL, keeps the last 100 events,
and a slow subscriber loses events instead of stalling anything."""

import asyncio
import threading

from desk_buddy.events import HISTORY, MAX_TEXT, NULL_BUS, EventBus


def test_publish_without_subscribers_keeps_a_ring_of_recent_events():
    bus = EventBus()
    for n in range(HISTORY + 20):
        bus.publish("state", name=f"s{n}")
    recent = bus.recent()
    assert len(recent) == HISTORY
    assert recent[0]["name"] == "s20" and recent[-1]["name"] == f"s{HISTORY + 19}"
    assert all(e["type"] == "state" and "ts" in e and "id" in e for e in recent)
    assert [e["id"] for e in recent] == sorted(e["id"] for e in recent)


def test_a_subscriber_gets_the_history_then_live_events():
    bus = EventBus()
    bus.publish("state", name="idle")

    async def run():
        queue, history = bus.subscribe()
        bus.publish("emotion", name="happy")
        live = await asyncio.wait_for(queue.get(), 1)
        bus.unsubscribe(queue)
        bus.publish("state", name="listening")  # not delivered after unsubscribing
        return history, live, queue.qsize()

    history, live, left = asyncio.run(run())
    assert [e["type"] for e in history] == ["state"]
    assert live["type"] == "emotion" and live["name"] == "happy"
    assert left == 0


def test_events_from_worker_threads_reach_the_loop():
    bus = EventBus()

    async def run():
        queue, _ = bus.subscribe()
        thread = threading.Thread(target=bus.publish, args=("action",), kwargs={"tool": "x"})
        thread.start()
        thread.join()
        return await asyncio.wait_for(queue.get(), 1)

    assert asyncio.run(run())["tool"] == "x"


def test_a_slow_subscriber_loses_events_but_never_blocks_publish():
    bus = EventBus(queue_size=5)

    async def run():
        queue, _ = bus.subscribe()
        for n in range(50):
            bus.publish("state", name=str(n))  # nobody reads: the queue fills up
        return queue.qsize()

    assert asyncio.run(run()) == 5
    assert len(bus.recent()) == 50  # the ring still has them all


def test_publish_never_raises():
    bus = EventBus()

    class Weird:
        def __str__(self):
            raise RuntimeError("no")

    bus.publish("state", name=Weird())  # unusual values must not break CARL
    bus._subs = None  # even a broken bus  # type: ignore[assignment]
    bus.publish("state", name="idle")


def test_long_text_is_bounded():
    bus = EventBus()
    bus.publish("heard", text="x" * 10_000)
    assert len(bus.recent()[0]["text"]) <= MAX_TEXT + 3


def test_publish_after_the_loop_closed_is_harmless():
    bus = EventBus()

    async def run():
        bus.subscribe()

    asyncio.run(run())
    bus.publish("state", name="idle")  # the loop is gone: no error


def test_the_null_bus_does_nothing():
    NULL_BUS.publish("state", name="idle")
    assert NULL_BUS.recent() == []
