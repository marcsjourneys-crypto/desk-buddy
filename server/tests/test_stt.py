import numpy as np

from desk_buddy.stt import pcm_to_float32


def test_pcm_to_float32_scales_to_unit_range():
    pcm = np.array([0, 16384, -32768], dtype=np.int16).tobytes()
    assert pcm_to_float32(pcm).tolist() == [0.0, 0.5, -1.0]
