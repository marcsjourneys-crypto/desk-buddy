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
        '{"type": "hello", "token": ""}',
        '{"type": "hello", "token": "t", "fw_version": 1}',
        '{"type": "button", "action": "sideways"}',
        '{"type": "button"}',
        '{"type": "stats", "data": 5}',
    ],
)
def test_bad_messages_raise(text):
    with pytest.raises(ProtocolError):
        parse(text)


def test_deeply_nested_json_raises_protocol_error():
    with pytest.raises(ProtocolError):
        parse("[" * 100_000 + "]" * 100_000)


def test_long_token_is_rejected():
    with pytest.raises(ProtocolError):
        parse('{"type": "hello", "token": "%s"}' % ("x" * 300))


@pytest.mark.parametrize("action", ["down", "up"])
def test_button(action):
    assert parse(f'{{"type": "button", "action": "{action}"}}').data["action"] == action


@pytest.mark.parametrize("kind", ["touch", "wake", "stats"])
def test_simple_messages(kind):
    assert parse(f'{{"type": "{kind}"}}').type == kind


def test_stats_keeps_its_data():
    assert parse('{"type": "stats", "data": {"rssi": -60}}').data == {"data": {"rssi": -60}}


def test_error_text_never_contains_the_token():
    with pytest.raises(ProtocolError) as info:
        parse('{"type": "hello", "token": "%s"}' % ("s3cret" * 60))
    assert "s3cret" not in str(info.value)


def test_token_that_is_not_valid_utf8_is_rejected():
    # json.loads accepts a lone surrogate; it can't be encoded for compare_digest.
    with pytest.raises(ProtocolError):
        parse('{"type": "hello", "token": "\\ud800abc"}')


def test_fw_version_that_is_not_valid_utf8_is_sanitized():
    msg = parse('{"type": "hello", "token": "t", "fw_version": "\\ud8001.0"}')
    assert msg.data["fw_version"] == "?1.0"
    msg.data["fw_version"].encode()  # safe to log and encode


def test_hello_repr_masks_the_token():
    msg = parse('{"type": "hello", "token": "s3cret-s3cret-s3cret", "fw_version": "0.1"}')
    text = repr(msg)
    assert "s3cret" not in text
    assert "***" in text and "0.1" in text
    assert msg.data["token"] == "s3cret-s3cret-s3cret"  # masked in repr only


def test_other_message_repr_is_plain():
    assert "down" in repr(parse('{"type": "button", "action": "down"}'))


def test_server_messages():
    assert protocol.welcome() == {"type": "welcome"}
    assert protocol.state("idle") == {"type": "state", "name": "idle"}
    assert protocol.emotion("happy") == {"type": "emotion", "name": "happy"}
    assert protocol.audio_start() == {"type": "audio_start", "rate": 16000}
    assert protocol.audio_end() == {"type": "audio_end"}
    assert protocol.audio_stop() == {"type": "audio_stop"}
