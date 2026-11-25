# M7 YouTube, media keys and PC volume: design and plan

Approved by the user on 2026-11-25.

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

## Goal

CARL can:
- play a YouTube video, or open a YouTube search;
- press the play/pause, next and previous media keys for whatever is playing;
- set the PC's master volume;
- lower other apps (YouTube in the browser, podcasts) while it listens and talks.

This finishes M7.

## Decisions

- **The usual rules:** all four tools need `COMPUTER_CONTROL=on` and go through `ActionGuard`, which applies the 3-per-question and 8-per-minute limits, logs every call, and shows `ok`, `failed` or `refused` on the Live tab. Unknown or invalid input is refused through `guard.refuse`. None of these needs a spoken "yes".
- **Model text is only ever** a search query, or a choice from an enum or a number. The server builds every URL and key press.
- **No new dependencies:**
  - HTTPS uses `urllib.request`;
  - key presses use ctypes `SendInput`;
  - Core Audio uses `comtypes`, which is already a dependency.

## Part 1: YouTube

- **The key:** `YOUTUBE_API_KEY` in `.env` (a secret).
  - It is never shown on the dashboard and never logged; this includes URLs and exception text, which must be redacted. Tests plant the key and check it appears nowhere.
  - `.env.example` lists it blank. The README explains how to create it: a Google Cloud project, enable "YouTube Data API v3", then create an API key restricted to that API.
- **The client:** `tools/youtube.py`, `YouTubeClient`, with an injectable HTTP function and a 10 s timeout.
  - **Searching for a video:** `search.list` with `part=snippet&type=video&maxResults=5&q=<query>&safeSearch=moderate`. It costs 100 quota units.
  - **Latest from a channel:** `search.list type=channel maxResults=1` (100 units), then `channels.list part=contentDetails` to get the uploads playlist (1 unit), then `playlistItems.list maxResults=1` (1 unit).
  - **Errors:** 403 `quotaExceeded` means the daily limit is used up; a bad key or a 400 means YouTube isn't set up properly; a network failure means YouTube can't be reached.
- **`play_youtube(query: 1–200 chars, kind: video | latest_from_channel)`:**
  - **The video ID** must match `^[A-Za-z0-9_-]{11}$`. The server builds `https://www.youtube.com/watch?v=<id>`, checks it with `check_url`, and opens it in the default browser with the existing opener.
  - **The result** is "Playing <title> by <channel>.", wrapped as untrusted. Nothing found gives "I couldn't find that one."
  - **When it's offered:** only when the key is set. Otherwise the prompt says YouTube playback isn't set up.
- **`youtube_search(query: 1–200 chars)`:** opens `https://www.youtube.com/results?search_query=` + `urllib.parse.quote_plus(query)`. It needs no key.

## Part 2: Media keys

- **`media_control(action: play_pause | next | previous | stop)`:**
  - **The keys:** `VK_MEDIA_PLAY_PAUSE` (0xB3), `VK_MEDIA_NEXT_TRACK` (0xB0), `VK_MEDIA_PREV_TRACK` (0xB1) and `VK_MEDIA_STOP` (0xB2).
  - **How they're sent:** ctypes `SendInput`, as one key down and one key up, through an injectable sender. Tests never press real keys.
  - **The result:** "Done." CARL never claims which state it is in now.
- **Prompt rules:**
  - Spotify uses `music_control`.
  - "Pause the video", YouTube, or any other player uses `media_control`.
  - If it's unclear and Spotify is what CARL last played, use `music_control`.
  - Play/pause is one toggle, so press it only when asked to pause, play or resume.

## Part 3: PC volume and ducking other apps

- **`tools/winaudio.py`:** small comtypes bindings for Core Audio.
  - `IMMDeviceEnumerator`, then the default render endpoint, then `IAudioEndpointVolume` for the master level and mute.
  - `IAudioSessionManager2`, then the session enumerator, then `ISimpleAudioVolume` for each session, with each session's process ID.
  - All calls run on one worker thread that has COM initialised, with a timeout. The tests use a fake backend, and real COM only runs in a marked integration test.
- **`pc_volume`:** takes exactly one of `level` (0–100), `change` (−50..50) or `mute` (true/false), the same rule as `music_volume`.
  - **When the PC speakers are CARL's voice** (no robot attached, read from `DeviceSwitch.robot`), it refuses a level below 10 and refuses mute, with: "I'd lose my voice: connect the robot or turn it down on the PC."
  - **The result:** "Volume's at N." or "Muted."
- **Prompt rules:** "the music" means Spotify (`music_volume`). "The computer", "the PC", "the video" or "everything" means `pc_volume`.
- **Ducking other apps:**
  - **When:** with `MUSIC_DUCK=on`, while CARL isn't idle.
  - **What's lowered:** every audio session except CARL's own process and Spotify (Spotify keeps its API duck, so it's never lowered twice) is set to 20 % of its current volume.
  - **What happens at idle:** each session it lowered is put back to its exact earlier volume, unless the user changed it in the meantime (if the current level isn't the ducked level, it's left alone).
  - **Code:** a new `SessionDucker` in `tools/ducking.py`, driven by the same state hook as `MusicDucker`, on its own worker thread. It's best effort: it never blocks or fails a turn, logs one warning per failure, and restores everything at shutdown.
  - **Settings:** it works without `COMPUTER_CONTROL`, like the music ducker, because ducking isn't an action. `MUSIC_DUCK` off restores everything at once.

## Tasks

1. **The YouTube client and config:**
   - `YOUTUBE_API_KEY` in config and `.env.example`;
   - `tools/youtube.py` with the client, ID check and error mapping;
   - tests with a fake HTTP function: search, latest from a channel, quota, bad key, network, ID check, and the key appearing nowhere (logs, errors, repr, dashboard).

   Commit: `Add the YouTube client`.
2. **The YouTube tools:**
   - `play_youtube` and `youtube_search` in the registry (strict schemas), through the guard, opening through the shared opener;
   - the prompt rules.

   Tests: URL building and encoding, untrusted titles, the not-set-up prompt, guard limits and events. Commit: `Let CARL play and search YouTube`.
3. **Media keys:**
   - `tools/media.py` with the `SendInput` sender, `media_control`, and the prompt rules;
   - tests with a fake sender: the right key codes, down then up.

   Commit: `Add media key control`.
4. **PC volume:**
   - `tools/winaudio.py` (the endpoint part) and `pc_volume`;
   - the voice guard while there's no robot;
   - the prompt rules;
   - tests with a fake backend: level, change and clamp, mute, the voice guard with and without a robot, failures;
   - one marked integration test that reads (but doesn't change) the real volume.

   Commit: `Let CARL set the PC volume`.
5. **Ducking other apps:**
   - `winaudio` sessions and `SessionDucker`, wired next to `MusicDucker` in `__main__`;
   - tests with fake sessions: CARL's own process and Spotify are skipped; levels are restored exactly; a user change wins; it never blocks; the `MUSIC_DUCK` switch; shutdown;
   - a marked integration test that lists the real sessions.

   Commit: `Duck other apps while CARL talks`.
6. **Docs:** `CLAUDE.md` (tools, ducking, M7 done), `server/README.md` (the YouTube key steps), `decisions.md` and `status.md`. Commit: `Document M7 YouTube, media keys and volume`.

## User check

1. Add `YOUTUBE_API_KEY` to `server/.env` and restart. "Play lo-fi beats on YouTube": a video opens and plays.
2. "Play the latest video from <a channel you like>."
3. "Search YouTube for bread recipes": the results page opens.
4. "Pause the video", then "resume it". Then "pause the music" while Spotify plays: Spotify pauses, and the video doesn't toggle.
5. "Set the computer volume to 30" (with the robot connected). Without the robot, "mute the computer" is refused, and CARL says why.
6. Ask a question while a YouTube video plays: the video dips while CARL listens and talks, then comes back.
