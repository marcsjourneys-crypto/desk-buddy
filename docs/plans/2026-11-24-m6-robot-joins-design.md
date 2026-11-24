# M6 robot joins the link: design

Approved by the user on 2026-11-24.

## Goal

The robot joins the home Wi-Fi, connects to the server's `/robot` WebSocket (`docs/protocol.md`), and becomes Buddy's ears, mouth and face:
- the mic streams the user's voice;
- the speaker plays Buddy's replies;
- the eyes follow Buddy's state and emotion.

Not in M6, because the hardware isn't wired yet: servos following `look`, and the antenna LED. Both stay reserved in the protocol.

## Decisions

- **The talk button is Right Ctrl on the PC.** The robot has no button or touch pad wired yet, and it stays on a USB cable for now.
  - While a robot is connected, the server uses the PC key for the robot instead of ignoring it.
  - A robot button, touch pad or wake word can be added later: they send the existing `button` message.
- **The link is Wi-Fi.** USB is for power and the serial monitor only. Nothing changes when the robot later runs from the 5 V supply.
- **Settings are typed once over serial and saved in NVS** (`Preferences`). They survive reflashing, and nothing secret goes into the code or the build.
  - The commands are `wifi`, `server`, `token`, `config` (masked) and `forget`.
  - Considered instead: a gitignored `secrets.h`, which means a rebuild per change; a captive portal, which needs more code and a library.
- **The robot streams mic audio exactly while the server's state is `listening`.** No new protocol message is needed, and the server stays the source of truth.
- **Firmware dependencies,** approved and pinned:
  - `links2004/WebSockets@2.7.3`, the WebSocket client (core 3.3 has none built in);
  - `bblanchon/ArduinoJson@7.4.3`, for the JSON control messages.
- **Server:** no new dependencies.

## Firmware

### Settings and connection

| Command | Effect |
|---|---|
| `wifi <name> <password>` | Saves the Wi-Fi credentials and (re)connects. |
| `server <ip>:<port>` | Saves the server address. |
| `token <value>` | Saves `ROBOT_TOKEN`. It is never printed in full. |
| `config` | Shows the settings, with the password and token masked. |
| `forget` | Wipes all three. |
| `net` | Shows the Wi-Fi RSSI, the link state and the reconnect count. |

- **Wi-Fi:** auto-reconnect. A DHCP reservation for the PC is recommended, since the server address is an IP. mDNS names can come later.
- **WebSocket:** `ws://<server>/robot`.
  - The robot sends `hello {token, fw_version}` and waits for `welcome`.
  - On a drop it reconnects with a back-off of 1 s, doubling up to 30 s.
- **Close codes:**
  - `4001`: stop retrying and print `Bad token (4001) - fix it with: token <value>`. A new `token` command resumes.
  - `4002`: stop retrying.
  - `4003`: stop retrying (replaced by a newer connection).
  - Anything else: reconnect with the back-off.

### Audio

- **Mic → server:**
  - While the state is `listening`, a task flushes stale DMA audio, then reads 20 ms frames (640 bytes).
  - It removes the DC offset with a one-pole high-pass filter and sends each frame as a binary frame.
- **Server → speaker:**
  - `audio_start` begins a sentence. Binary frames go into a ring buffer of about 1 s in PSRAM, and a playback task feeds the I2S speaker. `audio_end` lets the buffer play out.
  - `audio_stop` clears the buffer immediately, and audio frames are then dropped until the next `audio_start`.
- **Threading:** the mic and playback tasks run on core 1. The WebSocket and the eyes run in the main loop.
- **Serial commands:** `tone`, `mic`, `rec` and `play` are refused while the link is connected.

### Eyes

| Server state | Look |
|---|---|
| `idle` | idle, with blinks |
| `listening` | surprised (attentive) |
| `thinking` | look up, drifting slowly left and right |
| `speaking` | the reply's emotion |
| not connected | sleep |

| Emotion | Look |
|---|---|
| neutral | idle |
| happy | happy |
| sad | **sad** (new) |
| surprised | surprised |
| sleepy | sleep |
| thinking | skeptical |
| curious | look up |

- **The new eye-model parameter `tilt`** rotates the lid curves, mirrored for the right eye. The `sad` preset uses it to droop the outer corners.

## Server

- **PC key while a robot is connected:**
  - On press, the server calls `link.mic.begin()` and puts `down` on the events queue; on release, `link.mic.end()` and `up`.
  - `RobotMic` accepts frames for a 300 ms grace period after `end()`, so the last word isn't lost to network latency.
- **Hardening** (constants, configurable):
  - **Bad tokens:** 5 failures from one IP within 60 s block that IP for 60 s. Blocked IPs are closed with `4001` immediately. The list is in memory only.
  - **Half-open connections:** at most 4 connections waiting for their hello at once; extras are refused.
  - **Button floods:** at most 10 button messages per second; the rest are dropped and logged once per burst.
  - **Send queue:** capped at about 2 s of audio. On overflow the link closes with `1011`, and the robot reconnects.
- **LAN:**
  - `.env.example` documents `SERVER_HOST=0.0.0.0` for robot use.
  - The user runs the firewall command from `docs/protocol.md` once, as admin, and sets the Wi-Fi network to Private.
  - Startup logs the PC's LAN IP.

## Errors

| What happens | Response |
|---|---|
| Wi-Fi down | The eyes sleep and the robot keeps retrying. The server falls back to the PC mic and speaker. |
| Wrong token | A clear serial message; the robot stops retrying until the token is changed. |
| Server restart (`1012`) | The robot reconnects within seconds. |
| Send-queue overflow | The link closes and the robot reconnects. |

## Testing

- **pytest:**
  - the PC key driving a robot, through the fake robot (which also learns to stream only while `listening`);
  - the token throttle, the half-open cap, the button-flood limit and the send-queue cap.
- **Unity, on the board:**
  - the DC blocker;
  - the ring buffer (wrap-around, clear, overflow);
  - message → action parsing;
  - the reconnect back-off;
  - the `sad`/tilt preset fitting in the sprite.
- **Voice check by the user:**
  1. Set up the robot over serial.
  2. Start Buddy on the LAN.
  3. The robot connects, with idle eyes.
  4. Hold Right Ctrl and ask a question: the eyes go attentive, then thinking, then the emotion, and the reply plays on the robot.
  5. Interrupt mid-reply.
  6. Drop the Wi-Fi or stop the server, and check that it reconnects.

## Docs

- `protocol.md`: mic streaming tied to `listening`, the throttle and close behaviour, robot reconnect rules.
- `decisions.md`, `wiring.md` (if anything changes), `CLAUDE.md`, `status.md`, `server/README.md`, `firmware/README.md` (robot setup commands).
