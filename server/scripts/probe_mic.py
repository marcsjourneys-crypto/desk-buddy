"""Diagnose mic access: try opening input devices in several formats.

Run from server/:  uv run python scripts/probe_mic.py
Any line starting with OK means that device/format can record.
"""

import time

import sounddevice as sd


def try_open(label: str, **kwargs) -> bool:
    chunks: list[int] = []
    try:
        with sd.RawInputStream(
            dtype="int16", callback=lambda data, *_: chunks.append(len(data)), **kwargs
        ):
            time.sleep(0.4)
    except Exception as exc:  # noqa: BLE001 - diagnostic script, report everything
        print(f"FAIL {label}: {type(exc).__name__}: {exc}")
        return False
    print(f"OK   {label}: {sum(chunks)} bytes")
    return True


def main() -> None:
    print("Default input:", sd.query_devices(kind="input")["name"])
    ok = try_open("default device, 16 kHz mono (Desk Buddy's format)", samplerate=16000, channels=1)
    for index, info in enumerate(sd.query_devices()):
        if info["max_input_channels"] < 1:
            continue
        hostapi = sd.query_hostapis(info["hostapi"])["name"]
        name = f"dev{index} {info['name'][:30]} ({hostapi})"
        rate = int(info["default_samplerate"])
        ok |= try_open(f"{name} 16 kHz mono", device=index, samplerate=16000, channels=1)
        ok |= try_open(f"{name} {rate} Hz mono", device=index, samplerate=rate, channels=1)
    print("\nVerdict:", "at least one input works" if ok else "NO input device could be opened")


if __name__ == "__main__":
    main()
