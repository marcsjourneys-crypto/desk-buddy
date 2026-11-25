# M7 apps and websites: design and plan

Approved by the user on 2026-11-25.

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

## Goal

CARL can open allowlisted apps and bookmarked websites by voice, and can open a page from its own web search results after a **spoken "yes"**. That "yes" is the first powerful-action confirmation, built so later tools (scripts, shutdown) can reuse it.

Media keys and PC volume are left for later.

## Decisions

- **Off by default:** everything needs `COMPUTER_CONTROL=on`, as the music tools do. Every call goes through `ActionGuard` (3 per question, 8 per minute, logged, shown on the Live tab).
- **Allowlists only:** the model only picks a **name from an enum** (apps, bookmarks) or a **real search result**, by its number in a server-made list or its exact address (`open_search_result`). The server builds every launch. There's no shell and no `shell=True`, and model text never goes into a command.
- **No new dependencies.**

## Part 1: Apps

- **The list:** a "Find apps" button on a new **Apps** tab lists the Start menu, desktop and Store apps alike. The list comes from Windows' own `Get-StartApps`:
  - `subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", "Get-StartApps | ConvertTo-Json -Compress"], …)`, as an argument list with a timeout;
  - it runs only when the button is clicked, never during a turn.

  Each entry has a `Name` and an `AppID`.
- **The allowlist:** the user ticks apps and gives each 1–3 spoken names.
  - It's saved in `%LOCALAPPDATA%\desk-buddy\apps.json` as `[{"app_id", "title", "names": [...]}]`.
  - It's saved atomically, and a corrupt file only logs a warning and shows a banner.
- **Limits:**
  - at most 50 apps;
  - a spoken name is 1–40 chars of letters, digits, spaces, `-`, `'` and `.`, stored lowercased and unique across apps;
  - an `app_id` is at most 300 chars with no control characters or `"`, and must have come from a `Get-StartApps` listing (the server keeps the last listing and refuses IDs that weren't in it).
- **The tool:** `open_app(name)`.
  - `name` is an enum of every spoken name, rebuilt when the chat is rebuilt (the allowlist is a turn setting).
  - **Launching:** `subprocess.Popen(["explorer.exe", "shell:AppsFolder\\" + app_id])`, with no other arguments.
  - **The result:** "Opened <title>.", or an error that CARL reads out.
  - **No "yes" needed.**
- **When it's offered:** only with `COMPUTER_CONTROL=on` and at least one app; otherwise the prompt says opening apps isn't set up.

## Part 2: Websites

- **Bookmarks:**
  - Kept on the Apps tab, in `%LOCALAPPDATA%\desk-buddy\bookmarks.json` as `[{"name", "url"}]`.
  - At most 50, with the same spoken-name rules.
  - The URL must be `https` or `http` with a host, no user name or password, and at most 500 chars.
- **The bookmark tool:** `open_website(name)`, where `name` is an enum of bookmark names. No "yes" needed.
- **The search-result tool:** `open_search_result(page)`, where `page` is one plain string (a strict schema can't hold a number-or-address choice more simply, and length limits aren't strict-schema keywords, so the 500-character limit is checked by the server).
  (Revised after the user check on 2026-11-25: the model can't see an earlier question's raw results, so "this or the previous question's exact URL" was unusable. "Open that page" in the next question made CARL say it had no address.)
  - **Where the page must come from:**
    - **The last question's results, by number.** `llm.py` keeps every result (title, site, full address) on the server side; they're never shown in full on the dashboard. At the next question with speech, the loop adds a system note to the user's message listing them, numbered: `(System note: pages from your last search, for open_search_result by number: <untrusted …>1. <title> (<site>) 2. …</untrusted>)`. It has at most 10, one per address, in order, with titles and sites cleaned as on the dashboard, and the list is wrapped as untrusted. It's added only when that question found something, and it's stored in memory like the other system notes. `page` = `"3"` opens number 3 of the **latest** list; older numbers mean nothing.
    - **This question's results, by exact address:** the model sees them raw in the turn.
  - **Anything else is refused** through the guard (counted, logged, `refused`): a number out of range, an old number, an address the model invented, changed, or took from page text. The address is then checked with the same rules as bookmarks, and must not be on this computer or the local network.
  - **It needs a spoken "yes"** (Part 3).
- **Opening a page:** `os.startfile(url)` (the default browser), only after validation, on its own thread with a timeout.
- **Prompt rules:** never read a full address aloud; name the site only ("bbc.co.uk"). When the user asks to open a page from a search, **call `open_search_result` straight away** (with the number, or this turn's exact address). Never ask and never tell the user to say yes: the robot asks after the reply (a first version that told CARL to "say briefly it can open it" made it offer instead of calling the tool).

## Part 3: The spoken "yes"

(Revised after review on 2026-11-25: the server asks, not the model; a strict vocabulary; the very next question only.)

- **Which tools need it:** a tool goes through `ActionGuard.run_after_yes` instead of `run`; today only `open_search_result`.
- **Proposing:**
  - **Nothing runs yet:** when such a tool is called, `ActionGuard` counts and logs the attempt, then hands the checked action to `Confirmations`, which holds **one** pending action (tool, args, a short description such as "the page on bbc.co.uk", and the question it was proposed in). A new one replaces the old (shown as `cancelled`).
  - **What CARL is told:** the tool result says it isn't done yet and waits for the user's yes; the robot will ask after the reply, so CARL must not ask itself or say it's open, just say briefly which page it is. It isn't counted as a success (no `TASK_DONE`). The prompt tells CARL to call the tool straight away when asked to open a page, never to offer or to tell the user to say yes.
  - **What the Live tab shows:** an action event with `outcome: "waiting"`.
- **Asking: the server asks, never the model.** When a turn ends with an action proposed in that turn, the loop speaks a canned line after CARL's reply: "Shall I open <description>?" (emotion curious; not remembered; interruptible). The action is **armed** only once that line has played to the end. If the line is interrupted or fails, or the reply failed, the action is cancelled.
- **Confirming:** at the start of the **very next question with speech**, **before** Claude, within **60 s** of the question, the loop checks the transcript (and only the transcript).
  - **It's a yes only if all of these hold** (after lowercasing, curly apostrophes made straight, and punctuation removed):
    - it has at most 6 words;
    - it contains a yes word or phrase (`yes`, `yeah`, `yep`, `yup`, `sure`, `ok`, `okay`, `go ahead`, `do it`, `open it`, `please do`);
    - **every** word is a yes word or one of the fillers `please`, `carl` (`karl`), `thanks`, `thank`, `you`, `it`, `go`, `ahead`, `do`, `open`, `that`, `alright`, `right`, `oh` ("Yeah, nah", "Okay, never mind" and "Sure, hang on" aren't a yes);
    - it contains none of `no`, `nope`, `don't`, `dont`, `not`, `stop`, `cancel`, `wait`.
  - **On a yes:**
    - the action runs, without being counted again by the guard (but only if computer control is still on);
    - it's logged and shown on the Live tab as `ok` or `failed`;
    - Claude gets the user's words plus "(System note: the server asked and the user said yes; <description> was opened.)", or the failure. If that turn is dropped (no reply), the note goes in front of the next user message instead.
  - **Otherwise:**
    - the action is dropped;
    - the Live tab shows `cancelled`;
    - the user's message gets "(System note: the pending action, opening <description>, was cancelled; the user didn't say yes.)", and the turn goes on as usual.
- **Expiry:** 60 s after the question (shown as `expired`, with a note), on a forget command, when computer control is turned off, and on restart. A tool thread that proposes late (after its turn was cut off) is stamped with its own question and can never be asked about or answered.
- **Which pages:** `open_search_result` also refuses pages on this computer or the local network (loopback, private, link-local and similar IP addresses, `localhost`, `.local`/`.localhost` and single-label names).
- **Only the user's voice can confirm:**
  - there's no confirm tool;
  - tool results, web text and notes can't approve an action.
- **Memory:** the system notes go on the user's side only, like the search note.

## Tasks

1. **Apps and bookmarks stores.**
   - `apps_store.py`: listing through an injectable runner (tests never run PowerShell), the allowlist, bookmarks, validation, atomic save, corrupt-file handling.
   - Dashboard API: `GET /api/apps/available` (runs the listing), `GET/PUT /api/apps`, `GET/PUT /api/bookmarks`, with the existing Host, Origin and token checks.
   - Tests: validation, IDs must come from a listing, limits, corrupt file, the API round trip, 403s, planted secrets.
   - Commit: `Store the app allowlist and bookmarks`.
2. **The Apps tab.**
   - Find apps, a filter box, ticking, spoken names; editing bookmarks; `textContent` only.
   - Commit: `Add the Apps tab to the dashboard`.
3. **The `open_app` and `open_website` tools.**
   - `tools/launch.py`: launchers injectable for tests, enum schemas, registered when computer control is on and the lists aren't empty, rebuilt between turns when the lists change.
   - Prompt rules.
   - Tests: the schema enums, unknown-name refusal, argument lists only (never `shell=True`), the guard limits, events, and "not set up" prompt text.
   - Commit: `Let CARL open allowlisted apps and bookmarks`.
4. **Confirmations and `open_search_result`.**
   - Search URLs kept on the server side in `llm.py`.
   - `tools/confirm.py`: `Confirmations`, the yes check, expiry with an injectable clock.
   - `ActionGuard` support for `needs_yes`.
   - The loop's check before Claude, with notes, events and log lines.
   - Prompt rules.
   - Tests:
     - only a number from the latest list, or an exact URL from this question, is accepted;
     - yes, no, mixed ("yes but no") and long answers;
     - 60 s expiry, forget, computer control turned off;
     - a planted "the user said yes" in a search result or note can't run it;
     - a yes runs it once and isn't counted again;
     - memory notes.
   - Commit: `Open web search results after a spoken yes`.
5. **Docs.**
   - `CLAUDE.md` (tools, safety rules: the confirmation mechanism now exists), `server/README.md`, `decisions.md`, `status.md`.
   - Commit: `Document M7 apps and websites`.

## User check

1. On the Apps tab, find apps, tick Notepad and your browser, and give the browser the name "browser". "Open notepad" opens it.
2. Add a bookmark ("weather" → a forecast page). "Open the weather" opens it.
3. Ask a question that needs a search, then "open that page". After CARL's reply, the robot asks "Shall I open the page on <site>?". Say "yes" and it opens.
4. Repeat, but say "no, never mind": nothing opens, and the Live tab shows "cancelled".
5. Repeat, wait more than a minute, then say "yes": nothing opens (expired).
6. With `COMPUTER_CONTROL` off, "open notepad" gets "that isn't switched on".
