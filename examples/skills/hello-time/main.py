"""An example skill for CARL: the local time, in the format the user chose.

CARL sends the inputs as one JSON object on stdin, e.g. {"format": "long"}, and reads
what this prints (exit code 0) as the result. Standard library only, so it runs with
CARL's own Python and needs no setup.
"""

import json
import sys
from datetime import datetime


def main() -> int:
    try:
        inputs = json.load(sys.stdin)
    except ValueError:
        inputs = {}
    fmt = inputs.get("format", "short") if isinstance(inputs, dict) else "short"
    if fmt not in ("short", "long"):
        print(f"Unknown format: {fmt}", file=sys.stderr)  # stderr only goes to CARL's log
        return 2
    now = datetime.now()
    if fmt == "long":
        text = now.strftime("%A %d %B %Y, %H:%M:%S")
    else:
        text = now.strftime("%H:%M")
    print(f"It's {text} (the {fmt} format).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
