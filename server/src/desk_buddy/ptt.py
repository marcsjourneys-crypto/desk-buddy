"""Push-to-talk: a global hold-to-talk key read with Win32 GetAsyncKeyState."""

from __future__ import annotations

# Virtual-key codes: https://learn.microsoft.com/windows/win32/inputdev/virtual-key-codes
VK_CODES: dict[str, int] = {
    "right_ctrl": 0xA3,
    "left_ctrl": 0xA2,
    "right_alt": 0xA5,
    "right_shift": 0xA1,
    "scroll_lock": 0x91,
    "pause": 0x13,
    **{f"f{n}": 0x6F + n for n in range(1, 13)},
}


def parse_key(name: str) -> int:
    """Map a key name (e.g. 'right_ctrl', 'f8') or hex code (e.g. '0xA3') to a virtual-key code."""
    key = name.strip().lower()
    if key in VK_CODES:
        return VK_CODES[key]
    if key.startswith("0x"):
        try:
            code = int(key, 16)
        except ValueError:
            pass
        else:
            if 0 < code < 0x100:
                return code
    names = ", ".join(sorted(VK_CODES))
    raise ValueError(f"unknown key {name!r}; use one of {names}, or a hex code like 0xA3")
