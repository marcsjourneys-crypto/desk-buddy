"""An example skill for CARL that needs the spoken "yes": it pretends to email a message.

It ONLY prints "Would send: <text>" and never sends anything. The comments show where a
real script would load its own secrets and send the mail.

CARL sends the inputs as JSON on stdin, e.g. {"text": "I'm running late"}: exactly the
text the robot read back before the user said yes.
"""

import json
import sys


def main() -> int:
    try:
        inputs = json.load(sys.stdin)
    except ValueError:
        return 2
    text = inputs.get("text", "") if isinstance(inputs, dict) else ""
    if not isinstance(text, str) or not text.strip():
        return 2  # CARL hears only "failed (exit 2)"; never print secrets or details

    # A real skill would load its own secrets here, from the .env next to this file.
    # CARL never reads that file and passes no keys to the script, so the script does it
    # itself (standard library only, or a package such as python-dotenv declared in
    # this skill's own pyproject.toml):
    #
    #     from pathlib import Path
    #     env = {}
    #     for line in (Path(__file__).parent / ".env").read_text("utf-8").splitlines():
    #         key, sep, value = line.partition("=")
    #         if sep and not key.strip().startswith("#"):
    #             env[key.strip()] = value.strip()
    #
    # ...and send the mail here, e.g. with smtplib:
    #
    #     import smtplib
    #     from email.message import EmailMessage
    #     msg = EmailMessage()
    #     msg["From"], msg["To"] = env["MAIL_FROM"], env["MAIL_TO"]
    #     msg["Subject"] = "A note from my desk"
    #     msg.set_content(text)
    #     with smtplib.SMTP_SSL(env["SMTP_HOST"], 465, timeout=20) as smtp:
    #         smtp.login(env["SMTP_USER"], env["SMTP_PASSWORD"])
    #         smtp.send_message(msg)

    print(f"Would send: {text}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
