# M2 – Personality: design

Agreed with the user on 2026-11-23. M2 gives Buddy a character, emotion tags, short spoken replies, and memory of the conversation.

## Decisions from the user

- **Character:** cheerful and calm by default, with the occasional dry, witty line.
- **Memory:** kept until the server restarts, plus a spoken "forget" command.
- **Forget command:** fixed phrases checked by code, not a Claude tool.

## 1. Personality, emotion tags, short replies

**Character** (`brain/personality.py`):
- Buddy is a small retro tin robot on the user's desk.
- It is warm, upbeat and calm: never hyper or gushing.
- Now and then it says something dry and witty. It never makes jokes at the user's expense and is never sarcastic when the user is frustrated.
- It talks in the first person and never says "as an AI".
- Spoken style: no markdown, lists, emoji or code. Numbers and units are written as they are said.

**Short replies:**
- At most `MAX_SENTENCES` short sentences (default 2), around 30 words in total, unless the user asks for more.
- This is enforced by the prompt only. A hard cutoff in code would break requests like "tell me three facts".

**Emotion tags** (`brain/emotion.py`):
- Every reply must start with exactly one of: `[neutral] [happy] [sad] [surprised] [sleepy] [thinking] [curious]`.
- A streaming parser holds back the first characters until it has a complete leading tag. It strips the tag and reports the emotion.
- A missing or unknown tag counts as `neutral`, and the text is passed through unchanged.
- Known tags that appear later in the reply are removed so they are never spoken.
- Other bracketed text, such as `[1]`, is left alone.
- The output does not depend on how the stream is chunked.
- The loop logs `Buddy [emotion]: …` and calls an `on_emotion` callback, which the robot's eyes will use from M4/M6.

**Canned lines:**
- The error, refusal and "fresh start" lines live in `personality.py`.
- Wake and sleep lines are added in M6, when a sleep state exists.

## 2. Memory and the forget command

**Storage** (`brain/memory.py`):
- `Conversation` holds a list of (user text, Buddy reply) exchanges in memory, until the server restarts.
- It is capped at the last `HISTORY_TURNS` exchanges (new `.env` key, default 20).
- The loop owns the conversation. The `Chat` protocol becomes `stream_reply(history, user_text)`, so every provider gets memory.

**What is recorded:**
- **Normal turn:** the user's text and Buddy's full reply, with its emotion tag.
- **Interrupted turn:** only the sentences that started playing, followed by an "(interrupted)" note.
- **Interrupted before any speech:** nothing.
- **Error or refusal turn:** nothing.
- To support this, the loop's consumer records each sentence when it starts playing, not when it is produced.

**Forget command:**
- The loop checks the transcript before calling Claude.
- The text is normalised: lowercase, no punctuation, and a leading "buddy", "okay"/"ok" or "please" is dropped.
- After that it must exactly match one of: "forget that", "forget it", "forget everything", "new topic", "start over", "lets start over", "clear your memory", "change the subject".
- On a match, the memory is cleared and Buddy speaks a canned line. Claude is not called.
- The phrase list lives in one place.

## 3. Testing

**Unit tests:**
- The emotion parser is chunking-invariant, and covers fallbacks, stray tags and false positives.
- Memory: order, cap and forget.
- Forget phrases: positives and near-misses.
- Loop with fakes: history carries across turns, the interrupt rules, error turns aren't recorded, a forget phrase skips Claude, and the emotion callback fires.
- Personality prompt: contents.

**Integration test (live API):**
- A two-turn Labrador follow-up resolves "they".
- Both replies start with a valid tag.

**Manual check:**
- The Lab follow-up works.
- After an interrupt, "what were you saying?" reflects only what was heard.
- "New topic" clears memory.
- The log shows emotion tags.
- Replies feel short and calm.

**Docs:**
- `decisions.md` entries.
- `HISTORY_TURNS` in `.env.example`.
- `CLAUDE.md` personality section.
- `status.md`.

## Out of scope

- Wake and sleep lines (M6).
- Tools (M3).
- A final name for Buddy.
- Voice changes (Piper).
