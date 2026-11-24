# M6 Robot Joins the Link Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** The ESP32-S3 robot connects over Wi-Fi to the server's `/robot` WebSocket and becomes Buddy's mic, speaker and face, with Right Ctrl on the PC as the talk button. The server is hardened for the LAN.

**Architecture:**
- **Server:**
  - the PC key drives a connected robot, with a 250 ms release delay so in-flight audio arrives;
  - per-IP token throttling, a cap on half-open connections, a button-flood limit and a send-queue cap;
  - the LAN IP logged at startup;
  - the fake robot streams while `listening`.
- **Firmware:**
  - settings in NVS, set over serial;
  - a Wi-Fi and WebSocket client (links2004) that sends `hello` and handles `welcome`, close codes and back-off;
  - ArduinoJson for messages;
  - the mic task streams 20 ms DC-blocked frames through a queue to the main loop, and a playback task drains a PSRAM ring buffer into I2S;
  - the eyes map state and emotion to looks, including a new `tilt` parameter and a `sad` look.

**Tech stack:**
- Server: Python 3.12, FastAPI/uvicorn, pytest.
- Firmware: pioarduino core 3.3.12, LovyanGFX 1.2.36, `links2004/WebSockets@2.7.3`, `bblanchon/ArduinoJson@7.4.3`, Unity.

Design: `docs/plans/2026-11-24-m6-robot-joins-design.md`. Protocol: `docs/protocol.md`.

---

## Conventions (read first)

- **Server:**
  - run commands in `server/`: `uv run pytest -q` and `uv run ruff check`/`format`;
  - all server tests must pass before every commit;
  - async tests use plain `asyncio.run` (no pytest-asyncio);
  - real-socket tests use the existing loopback-server helpers in `tests/test_server.py`.
- **Firmware:**
  - run `pio` **from PowerShell only**: `& "$env:USERPROFILE\.platformio\penv\Scripts\pio.exe"`, in `firmware/`;
  - the board is on **COM9**, and only one program can hold it, so if it's busy, wait and retry, then report;
  - commands: `pio run`, `pio run -t upload --upload-port COM9`, `pio test --upload-port COM9 --test-port COM9`;
  - when the board is unavailable: `pio test --without-uploading --without-testing`, which compiles the tests only;
  - the serial helper is `scripts/serial_cmd.py "<cmd>" <secs>`, run with the PlatformIO Python.
- **Write files with Edit/Write only.** No shell heredocs.
- **Never print or log the robot token or the Wi-Fi password,** on either side. Firmware masks them in `config`.
- **Never edit `server/.env`.** The user sets values themselves.
- **Every commit ends with these trailer lines:**
  ```
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01S4FHmMu7pMrHuZS9GJZYSY
  ```
- **👀 marks a check by the user.** Don't ask the user yourself; report what is needed.
- **The pure firmware logic goes in header-only files in `firmware/include/`,** tested in `test/test_logic/test_main.cpp`. The hardware and network code goes in `src/`.

---

## Part A: Server

### Task 1: The PC key drives a connected robot

**Files:**
- Modify: `server/src/desk_buddy/__main__.py` (`key_handler`)
- Modify: `server/src/desk_buddy/link/switch.py` (the `Robot` protocol: `mic` must expose `begin()` and `end()`)
- Test: `server/tests/test_main.py`

**Behaviour:**
- **No robot:** unchanged; the event goes straight onto `events`.
- **Robot attached, key `down`:**
  - first, if a delayed release is pending, fire it now (`robot.mic.end()` then put `up`), so the order stays down/up/down;
  - then `robot.mic.begin()`, then put `down`.
- **Robot attached, key `up`:** schedule the release 0.25 s later (`RELEASE_DELAY = 0.25`) with `loop.call_later`. When it fires:
  - if the same robot is still attached, `robot.mic.end()`;
  - in every case, put `up`, so the orchestrator never gets stuck listening.
- `key_handler` keeps its signature, `(switch, events) -> Callable[[str], None]`, and runs on the event loop. Take the loop with `asyncio.get_running_loop()` inside `on_key`. Allow injecting `delay` for tests.

**Tests** (TDD), using a fake robot object whose `mic` records the begin/end calls:
- with no robot, events pass straight through;
- with a robot, `down` → `begin` then `down` queued;
- `up` → nothing queued straight away, then after the delay `end` and `up`;
- a re-press during the delay → `end`, `up`, `begin`, `down`, in that order;
- the robot detaches during the delay → `up` is still queued and `end` isn't called on the old robot;
- update the existing test that asserted the key is ignored while a robot is attached.

**Commit:** `Let the PC talk key drive a connected robot`

### Task 2: Hardening

**Files:**
- Modify: `server/src/desk_buddy/server.py`
- Modify: `server/src/desk_buddy/link/robot.py`
- Test: `server/tests/test_server.py`, `server/tests/test_link_robot.py`

**Behaviour** (module constants, plus an injectable `clock` on `create_app`):
1. **Token throttle, per client IP** (`ws.client.host`; use `"unknown"` if it's missing):
   - Keep a deque of failure times per IP. If there are `MAX_AUTH_FAILURES = 5` failures within `AUTH_WINDOW = 60` s, block that IP until `now + BLOCK_SECONDS = 60`.
   - A blocked IP is accepted and immediately closed with `4001`, before reading a hello. Log `Robot refused: <ip> blocked after repeated bad tokens` once per block.
   - Prune old entries so the dict can't grow without bound: drop IPs whose deque is empty and whose block has expired.
   - A successful auth clears that IP's failures.
2. **Half-open cap:** a counter of connections currently waiting for their hello. If it's already `MAX_PENDING_HELLOS = 4`, accept and close with `4002` straight away. The counter is decremented in a `finally` block.
3. **Button flood limit in `_receive_loop`:** a token bucket of 10 per second with a burst of 10. Excess `button` messages are dropped. Log `Robot button flood: dropping` once per burst, resetting after one quiet second.
   - Dropping a `down` while its `up` gets through is harmless: the orchestrator ignores an `up` when it isn't listening. Document this in a comment.
4. **Send-queue cap in `RobotLink`:**
   - `send()` schedules `self._enqueue(item)` with `call_soon_threadsafe`.
   - `_enqueue` checks `self._outbox.qsize() >= MAX_OUTBOX = 128` (about 2.5 s of 20 ms audio chunks).
   - On overflow it logs a warning once and schedules `self.close(1011)` (a task kept in an attribute so it isn't garbage-collected), then drops the item.

**Tests** (TDD):
- 5 bad tokens → the 6th connection with the right token from the same IP gets `4001`;
- after advancing the fake clock past `BLOCK_SECONDS`, the right token is accepted;
- 4 connections that never send a hello plus a 5th → the 5th is closed with `4002` at once;
- 30 rapid button messages → at most about 10 `down`/`up` events reach the queue;
- `RobotLink` with a sender that never drains → after 128 items, the link closes with `1011` and later sends are ignored.

**Commit:** `Harden the robot link for the LAN`

### Task 3: LAN setup, and the fake robot streams while `listening`

**Files:**
- Modify: `server/src/desk_buddy/__main__.py`, which logs the LAN IP when the host is `0.0.0.0`/`::`.
- Modify: `server/.env.example`
- Modify: `server/src/desk_buddy/fake_robot.py`
- Test: `server/tests/test_fake_robot.py`, `server/tests/test_main.py`

**Behaviour:**
- **`lan_ip() -> str | None`:**
  - open a UDP socket, `connect(("192.0.2.1", 80))` (TEST-NET-1; no packet is sent), and read `getsockname()[0]`;
  - return `None` on `OSError`.
  - When the bound host is a wildcard, startup logs `Robot link: on (ws://<lan-ip>:<port>/robot — set the robot with: server <lan-ip>:<port>)`.
- **`.env.example`:** in the robot section, say that the real robot needs `SERVER_HOST=0.0.0.0` plus the firewall rule in `docs/protocol.md`.
- **Fake robot:**
  - stop reading the key and sending `button`;
  - stream `mic.drain()` every 20 ms while the last `state` received is `listening`, and stop when it changes;
  - log `Hold right_ctrl in the Buddy window to talk` at connect.
  
  The PC key now lives in the server, so the fake robot and the real robot behave the same. Update its tests to match.

**Commit:** `Log the LAN address and drive the fake robot from the server state`

---

## Part B: Firmware

### Task 4: Pure helpers, plus ArduinoJson and WebSockets

**Files:**
- Modify: `firmware/platformio.ini`. Append to `lib_deps`:
  ```
  links2004/WebSockets@2.7.3
  bblanchon/ArduinoJson@7.4.3
  ```
- Create: `firmware/include/dc_block.h`, `ring_buffer.h`, `backoff.h`, `link_msg.h`, `looks.h`
- Test: `firmware/test/test_logic/test_main.cpp`

**Spec:**
- **`dc_block.h`:**
  - `struct DcBlocker { float r = 0.995f; float x1 = 0, y1 = 0; int16_t process(int16_t x); void processBlock(int16_t* buf, size_t n); }`
  - `y = x - x1 + r*y1`, saturated to int16.
  - Tests:
    - a constant input of 1000 decays to `|y| < 50` after 2000 samples;
    - a 1 kHz sine at 16 kHz keeps over 95 % of its amplitude;
    - saturation at the extremes.
- **`ring_buffer.h`:**
  - `class Int16Ring { Int16Ring(int16_t* storage, size_t capacity); size_t write(const int16_t*, size_t); size_t read(int16_t*, size_t); size_t available() const; size_t space() const; void clear(); }`
  - Writes are partial when full (it returns how many fit). No locking here: the caller locks.
  - Tests: write and read across the wrap; a partial write when full; `clear()` empties; `available` + `space` == `capacity`.
- **`backoff.h`:**
  - `class Backoff { uint32_t next(); void reset(); }`
  - It returns 1000, 2000, 4000, 8000, 16000, 30000, 30000… ms; `reset()` starts again at 1000.
- **`link_msg.h`** (uses ArduinoJson):
  - `enum class Msg { Welcome, State, Emotion, AudioStart, AudioEnd, AudioStop, Unknown, Bad }`
  - `struct Parsed { Msg type; char name[16]; }`
  - `Parsed parseServerText(const char* text, size_t len)`. `name` is filled for `state`/`emotion` and truncated safely. Bad JSON → `Bad`; an unknown `type` → `Unknown`.
  - `size_t buildHello(char* out, size_t cap, const char* token, const char* fw)` uses ArduinoJson to escape correctly.
  - Tests:
    - each message type;
    - a name that's too long gets truncated;
    - garbage gives `Bad`;
    - `{"type":"look"}` gives `Unknown`;
    - `buildHello` output parses back with the same token and fw;
    - a token with a quote is escaped.
- **`looks.h`:**
  - `const char* lookForState(const char* state, const char* emotion)`. `state` is `idle`/`listening`/`thinking`/`speaking`; anything else → `"sleep"`.
  - `const char* lookForEmotion(const char* emotion)`, following the design's tables. `listening` → `"listening"`, `thinking` → `"thinking"`, `speaking` → `lookForEmotion(emotion)`. An unknown emotion → `"idle"`.
  - Tests cover every row of both tables.

**Steps:**
1. Write the tests and see the build fail.
2. Implement the headers.
3. `pio test`: everything passes (29 existing tests plus the new ones).
4. Commit with the message `Add firmware helpers for the robot link, with tests`.

### Task 5: Eye tilt, `sad`, and the `listening` and `thinking` looks

**Files:**
- Modify: `firmware/include/eye_model.h`
- Modify: `firmware/src/eyes.cpp`
- Modify: `firmware/src/main.cpp` (help text)
- Test: `test_main.cpp`

**Spec:**
- **`Params`** gains `float tilt = 0;`, in radians, positive meaning the outer corner goes down. `lerp` and `same` include it.
- **Drawing:** `drawLid` rotates each point about the eye centre by `tilt`, mirrored for the right eye (`side == 1` uses `-tilt`). `fitInSprite` must allow for the rotation: grow the extents by `radius * |sin(tilt)|`.
- **New presets:**
  - `sad`: `upper = -0.55`, `lower = 0.55`, `tilt = 0.35`, `gaze_y = 12`;
  - `listening`: the same as `surprised`.
- **`thinking`:** `look(0, -24)`, with `eyes.cpp` adding a slow drift while this look is active: `gaze_x = 14 * sin(2π · t / 3000 ms)`, applied in `update()`.
- **`eyes::show`** accepts `sad`, `listening` and `thinking`, alongside the existing looks.
- **Tests:**
  - `sad` and `thinking` (with its maximum drift) fit the sprite, including the tilt extents;
  - `lerp` of tilt works.

**👀 The user** looks at `eyes sad`, `eyes listening` and `eyes thinking`. The builder reports that this check is needed.

**Commit:** `Add eye tilt with sad, listening and thinking looks`

### Task 6: Settings in NVS, and the serial commands

**Files:**
- Create: `firmware/src/settings.h`, `firmware/src/settings.cpp`
- Modify: `firmware/src/main.cpp`

**Spec:**
- Uses `Preferences`, namespace `"buddy"`, with the keys `ssid`, `pass`, `host`, `port` (uint16) and `token`.
- **API:**
  - `settings::load()`
  - `const char* ssid()`, `pass()`, `host()`, `token()`, `uint16_t port()`
  - `bool complete()`
  - `setWifi`, `setServer` (which parses `host:port` and validates the port, 1–65535), `setToken` (16–256 characters), `forget()`
- **Serial commands:**
  - `wifi <ssid> <password>`: the SSID can't contain spaces. Document this; a quoted form can come later if it's ever needed.
  - `server <host>:<port>`
  - `token <value>`
  - `config`: prints the SSID, `pass: ****` (or `(not set)`), `server host:port`, and `token: ****` plus its last 2 characters.
  - `forget`
- Each setter prints `Saved. ` followed by a hint: `(reconnecting)` once the link exists, or `Set wifi/server/token, then the robot connects.`
- **The serial line buffer grows to 300 characters,** because a token can be up to 256.

**Check:** `serial_cmd.py`: `config` shows `(not set)` for each value; `token abc` is rejected as too short; `server 1.2.3.4:99999` is rejected.

**Commit:** `Store robot settings in NVS, set over serial`

### Task 7: Streaming audio, with a playback task and a mic task

**Files:**
- Modify: `firmware/src/audio.h`, `firmware/src/audio.cpp`
- Modify: `firmware/src/main.cpp`

**Spec:**
- **Playback:**
  - `audio::startStreaming()` allocates a PSRAM ring of 16000 samples (1 s) and starts a FreeRTOS task, `playback`, pinned to core 1 at priority 5.
  - The task loops: read up to 320 samples from the ring under a `portMUX` spinlock, and if there were any, scale them by volume and write them to I2S. If there were none, `vTaskDelay(5 ms)`. With `auto_clear`, the I2S plays silence when starved.
  - `audio::streamBegin()` handles `audio_start`: accept frames.
  - `audio::streamPush(const uint8_t*, size_t)` handles binary frames: it drops an odd trailing byte, and drops everything while not accepting. When the ring is full it drops the oldest data. In practice that doesn't happen, because the server paces ~200 ms ahead.
  - `audio::streamEnd()` handles `audio_end`: nothing to do, since the ring plays out.
  - `audio::streamStop()` handles `audio_stop`: clear the ring and stop accepting until the next `streamBegin()`.
- **Mic:**
  - `audio::setMicStreaming(bool on)`. On the change to `on`: `flushMic()` and reset the `DcBlocker`.
  - A FreeRTOS task, `micstream`, on core 1 at priority 4. While streaming, it reads 320 samples, runs `DcBlocker.processBlock`, and `xQueueSend`s a 640-byte frame onto a queue 8 deep, with no waiting; if the queue is full, it drops the frame and counts it. While not streaming, it sleeps 10 ms.
  - `bool audio::popMicFrame(uint8_t out[640])` is called by the main loop, which sends the frame.
  - `audio::micDropped()` returns the drop count, shown in `net`.
- **The M5 blocking functions** (`play`, `playTone`, `readMic`) stay for the serial tests.
  - `main.cpp` refuses `tone`/`mic`/`rec`/`play` while `net::connected()`.
  - Until Task 8 exists, use a stub `bool linkActive = false`.
- **Concurrency:**
  - only the playback task writes to the speaker's I2S once streaming has started;
  - the blocking `play`/`playTone` must not run while the streaming task is active, so either stop the task or guard it. Simplest: `startStreaming()` is called only when the link first connects, and the test commands are refused while connected.
  - Document this in `audio.h`.

**Check:** build plus `pio test`. Without the link this is hard to exercise; Task 8's voice check covers it.

**Commit:** `Stream mic frames and play streamed audio through a ring buffer`

### Task 8: Wi-Fi and the WebSocket link

**Files:**
- Create: `firmware/src/net.h`, `firmware/src/net.cpp`
- Modify: `firmware/src/main.cpp`

**Step 0 (spike, 10 minutes): find the close code.**
- Read `.pio/libdeps/esp32s3/WebSockets/src/WebSockets.cpp`/`.h` and `WebSocketsClient.cpp` to see whether the close code reaches the `WStype_DISCONNECTED` event. It may be in `payload`, or reachable through a field or hook.
- **If it isn't exposed,** use this fallback:
  - track whether `welcome` arrived since the last connect;
  - if the link closes three times in a row before any `welcome`, stop retrying and print `Server refused the robot (bad token or protocol) - check: token <value>`;
  - **document this in the report.**

**Spec:**
- **`net::begin()`:**
  - if settings are complete, `WiFi.mode(WIFI_STA)`, `WiFi.setSleep(false)` (lower latency) and `WiFi.begin(ssid, pass)`;
  - configure `WebSocketsClient` with `begin(host, port, "/robot")`, `onEvent(handler)` and `setReconnectInterval(0)`, since our own back-off drives reconnects.
  - Also call `enableHeartbeat(15000, 5000, 2)` if available, so a dead server is detected. The server's 20 s pings also keep the link alive.
- **`net::loop()`** runs every main-loop pass:
  - call `ws.loop()`;
  - manage the state machine: `NoConfig`, `WifiConnecting`, `Connecting`, `WaitingWelcome`, `Linked`, `Backoff`, `Refused`;
  - send queued mic frames (`audio::popMicFrame` → `ws.sendBIN`), at most 4 per pass.
- **Events:**
  - **`WStype_CONNECTED`:** send `buildHello(token, kFwVersion)`, then go to `WaitingWelcome` with a 5 s timeout. On timeout, disconnect and back off.
  - **`WStype_TEXT`:** `parseServerText`.
    - `Welcome` → `Linked`, and `Backoff.reset()`. On the first link, call `audio::startStreaming()`.
    - `State` → store the state; `audio::setMicStreaming(state == "listening")`; `eyes::show(lookForState(state, lastEmotion))`.
    - `Emotion` → store `lastEmotion`. If the state is `speaking`, re-show the look.
    - `AudioStart` / `AudioEnd` / `AudioStop` → the `audio::stream*` calls.
    - `Unknown` → ignore. `Bad` → log once.
  - **`WStype_BIN`:** `audio::streamPush`.
  - **`WStype_DISCONNECTED`:**
    - `setMicStreaming(false)`, `streamStop()`, `eyes::show("sleep")`;
    - close code 4001/4002/4003 (or the fallback rule) → `Refused`, printing `Bad token (4001) - fix it with: token <value>`, `Refused (4002)` or `Replaced by another connection (4003)`;
    - otherwise → `Backoff` with `next()`.
- **Settings changes** (`wifi`/`server`/`token`) call `net::restart()`, which leaves `Refused`, resets the back-off and reconnects.
- **`net` command:** prints the Wi-Fi status and RSSI, the IP, the link state, the reconnect count, mic frames sent and dropped.
- **`main.cpp`:**
  - `setup()`: `settings::load()`, `net::begin()`, and `eyes::show("sleep")` until the link is up.
  - `loop()`: `net::loop()`, `eyes::update()`, `pollSerial()`.
  - Remove any `delay()` longer than 1 ms from the loop.
- **Logging:** log only the link transitions (`Wi-Fi connected <ip>`, `Linked to <host:port>`, `Disconnected (code N), retry in Xs`). Never log the token or the password.

**👀 Voice check (the user does this; the builder only reports that it's needed):**
1. Server:
   - set `SERVER_HOST=0.0.0.0` in `server/.env` (the user does this);
   - add the firewall rule;
   - run `uv run python -m desk_buddy` and note the LAN IP in the log.
2. Robot serial: `wifi <ssid> <pass>`, then `server <lan-ip>:8765`, then `token <ROBOT_TOKEN>`. Expect `Linked`, and the eyes go idle.
3. Hold Right Ctrl and ask a question. The eyes go listening, then thinking, then the emotion, and the reply plays on the robot.
4. Interrupt mid-reply.
5. Stop the server: the eyes sleep. Restart it: the robot relinks.

**Commit:** `Connect the robot over Wi-Fi and the WebSocket link`

---

## Part C: Docs

### Task 9: Docs

- **`docs/protocol.md`:**
  - the robot streams mic audio while the state is `listening`;
  - a button is optional, and the PC key drives the robot;
  - the throttle (`4001` while blocked), the half-open cap (`4002`) and the button rate limit;
  - send-queue overflow closes with `1011`;
  - the robot's reconnect rules, matching the firmware.
- **`docs/decisions.md`:** a new `## 2026-11-24 — M6 robot joins the link` section with:
  - the PC key, the 250 ms release delay, the NVS settings and both libraries;
  - state-driven streaming;
  - the hardening numbers;
  - the close-code finding from Task 8 Step 0;
  - measured latency, if the voice check gives numbers.
- **`CLAUDE.md`:** the firmware stack line (the two libraries), and the protocol summary (mic streams while `listening`; the PC key).
- **`firmware/README.md`:** a "Connect the robot" section (the serial commands and the firewall), and the `net` command.
- **`server/README.md`:** running for the robot (`SERVER_HOST=0.0.0.0`, the firewall, the LAN IP log line).
- **`docs/status.md`:** M6 status. Memory: update `m1-resume-point.md`.

**Commit:** `Document M6: the robot joins the link`
