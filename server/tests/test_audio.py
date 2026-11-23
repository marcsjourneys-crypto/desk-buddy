from desk_buddy.audio import BYTES_PER_SECOND, PcmBuffer, duration_seconds


def test_duration():
    assert duration_seconds(b"\x00" * BYTES_PER_SECOND) == 1.0


def test_buffer_collects_and_resets():
    buf = PcmBuffer(max_seconds=1)
    buf.append(b"ab")
    buf.append(b"cd")
    assert buf.take() == b"abcd"
    assert buf.take() == b""


def test_buffer_caps_size():
    buf = PcmBuffer(max_seconds=0.001)  # 32 bytes at 16 kHz int16
    buf.append(b"x" * 20)
    buf.append(b"y" * 20)
    buf.append(b"z" * 20)
    assert buf.take() == b"x" * 20 + b"y" * 12
