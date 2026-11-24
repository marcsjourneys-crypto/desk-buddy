"""Reset the board on COM9, optionally send one command, and print the output.

Usage: python serial_cmd.py ["command"] [seconds] [port]
Run it with the PlatformIO Python (it has pyserial). Close any serial monitor first.
"""

import sys
import time

import serial


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 6.0
    port = sys.argv[3] if len(sys.argv) > 3 else "COM9"
    with serial.Serial(port, 115200, timeout=0.2) as s:
        s.dtr = False
        s.rts = True  # pulse EN: reset into the app
        time.sleep(0.1)
        s.rts = False
        end = time.time() + seconds
        sent = not command
        boot_done = time.time() + 2.5  # boot blinks the LED and prints info first
        while time.time() < end:
            data = s.read(4096)
            if data:
                sys.stdout.write(data.decode("utf-8", "replace"))
                sys.stdout.flush()
            if not sent and time.time() > boot_done:
                s.write((command + "\n").encode())
                sent = True


if __name__ == "__main__":
    main()
