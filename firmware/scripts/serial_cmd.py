"""Reset the board on COM9, optionally send one command, and print the output.

Usage: python serial_cmd.py ["command"] [seconds] [port]
Run it with the PlatformIO Python (it has pyserial). Close any serial monitor first.
"""

import codecs
import sys
import time

import serial

READY = "Type 'help'"  # printed at the end of boot
READY_FALLBACK_S = 5.0  # send the command anyway after this long


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 6.0
    port = sys.argv[3] if len(sys.argv) > 3 else "COM9"
    s = serial.Serial(baudrate=115200, timeout=0.2)
    s.port = port
    # Released before opening, so opening doesn't hold the board in reset or boot mode.
    s.dtr = False
    s.rts = False
    try:
        s.open()
    except serial.SerialException:
        print(f"Can't open {port}: close any serial monitor first")
        sys.exit(1)
    with s:
        s.rts = True  # pulse EN: reset into the app
        time.sleep(0.1)
        s.rts = False
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        start = time.time()
        end = start + seconds
        sent = not command
        seen = ""
        while time.time() < end:
            data = s.read(4096)
            if data:
                text = decoder.decode(data)
                sys.stdout.write(text)
                sys.stdout.flush()
                if not sent:
                    seen += text
            if not sent and (READY in seen or time.time() - start > READY_FALLBACK_S):
                s.write((command + "\n").encode())
                sent = True
        sys.stdout.write(decoder.decode(b"", final=True))


if __name__ == "__main__":
    main()
