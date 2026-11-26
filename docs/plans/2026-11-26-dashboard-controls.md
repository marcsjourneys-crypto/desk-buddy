# Dashboard controls: Stop, Forget, Talk

Approved by the user on 2026-11-26.

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** three buttons on the Live tab, Stop, Forget and Talk (hold), that do what the key and "forget that" do, for when the CARL window isn't in front.

**Architecture:** three dashboard endpoints call one `control(event)` callback given to `create_dashboard`. In `__main__`:
- `down` and `up` go through the existing `key_handler`, so the robot's 250 ms release delay applies exactly as it does for Right Ctrl;
- `stop` and `forget` go straight onto the orchestrator's event queue.

The loop handles two new events. No new dependencies.

## Decisions

- **The endpoints:**
  - `POST /api/control/stop`, `POST /api/control/forget` and `POST /api/control/talk` with the body `{"action": "down"|"up"}` (anything else is a 400).
  - Each one is a change, so the existing middleware already demands our Host, our Origin and the page's `X-CARL-Token`.
  - Each returns `{"ok": true}`, or a 503 when there is no control callback.
  - They are not computer-control actions: no guard, no switch.
- **Stop (the `stop` event):**
  - ends any hands-free listening or follow-up window (`_stop_hands_free(why="stopped from the dashboard")`);
  - interrupts a turn, which covers a reply, the pondering line, the yes question and a greeting;
  - drops a recording in progress (the mic is stopped and the clip thrown away, never transcribed), and CARL goes idle.
  - A pending yes is left alone and expires as usual. It also resets the follow-up count. The log says `Stopped from the dashboard`.
- **Forget (the `forget` event):**
  - first does everything Stop does;
  - then does exactly what the spoken forget does: expire a pending yes, clear the search results, `conversation.forget()` and the smart router's `forget()`;
  - then says the `forget` line as an interruptible turn, the same way the greeting runs.
  - Factor the spoken path's clearing into one `_forget_everything()` that both use.
- **Talk:**
  - `down` and `up` reach `on_key`, exactly like the key.
  - **The server caps a hold at 60 s** (`TALK_MAX_HOLD`): a `down` from the page schedules an `up` 60 s later, and a later `up` or `down` from the page cancels it.
  - **The page releases on** `pointerup`, `pointercancel`, `lostpointercapture`, the pointer leaving the button, `visibilitychange` to hidden, `pagehide`, `blur`, and the event socket closing. A release is sent only if a press was sent. Use `setPointerCapture` and prevent the default touch actions (no context menu or text selection on a long press).
- **The Live tab:**
  - A `control` event with `action` (`stop`, `forget`, `talk`) is published for Stop and Forget, shown as a short line: "Stopped from the dashboard" / "Forgot everything (dashboard)".
  - Talk publishes nothing new, because the state changes already show it.
  - The buttons sit under the state line, disabled until the event socket is open and again when it closes.
  - They render text only with `textContent`, keep the strict CSP (no inline handlers or styles), and follow the page's existing button styles.

## Tasks

1. **The loop's `stop` and `forget` events** (`loop.py`), with `_forget_everything()` shared with the spoken forget. Tests in `tests/test_loop.py` / `tests/test_wake_loop.py`, in each state:
   - idle;
   - listening on the key: the clip is dropped, never transcribed;
   - thinking and speaking: interrupted;
   - a hands-free question and a follow-up window: ended;
   - the yes question: interrupted, and Stop leaves the pending action as is while Forget expires it;
   - Forget clears memory, the page list and the smart stay, and says the line;
   - a key press interrupts that line.

   **Commit:** `Handle stop and forget events in the loop`
2. **The endpoints and wiring** (`dashboard.py` and `__main__.py`, using `control` and `TALK_MAX_HOLD`). Tests in `tests/test_dashboard.py` and `tests/test_main.py`:
   - each endpoint refuses a missing or wrong token, a wrong Origin and a wrong Host;
   - a bad talk action is a 400, and no callback gives a 503;
   - each endpoint calls the callback with the right event;
   - the 60 s cap fires an `up` (with a fake loop or a short cap), and an `up` cancels it;
   - in `__main__`, `down`/`up` go through `key_handler` and `stop`/`forget` onto the queue.

   **Commit:** `Add stop, forget and talk endpoints to the dashboard`
3. **The buttons** (`dashboard_static/index.html`, `app.js` and `style.css`). Page tests in `tests/test_dashboard_page.py`, following the existing ones:
   - the three buttons exist;
   - the requests send the token;
   - every release trigger is listened to;
   - `control` events are rendered with `textContent`;
   - no inline handlers.

   **Commit:** `Add Stop, Forget and Talk buttons to the Live tab`
4. **Docs:**
   - `CLAUDE.md`: the dashboard bullet (the controls and the endpoints), and the M8 roadmap line (in-page controls done; music buttons not included);
   - `docs/decisions.md`;
   - `docs/status.md`, with a **"Waiting for your check"** list: the smart model steps, the morning brief, a TV evening for false wakes, the servo sweep, and these controls;
   - `server/README.md` (the Live tab).

   **Commit:** `Document the dashboard controls`

## User check

1. With the robot connected, open the dashboard, hold **Talk**, ask something and let go: CARL answers through the robot.
2. During a long answer press **Stop**: it stops at once and goes idle.
3. Ask something, then press **Forget**: CARL says its fresh-start line, and "what did I just ask?" draws a blank.
4. Hold **Talk** and switch to another tab: CARL stops listening and answers what it heard.
5. With no robot connected, **Talk** uses the PC mic.
