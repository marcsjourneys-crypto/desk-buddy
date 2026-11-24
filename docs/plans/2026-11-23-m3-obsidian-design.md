# M3 – Obsidian dictation: design

Agreed with the user on 2026-11-23.

## Decisions from the user

- **Vault:** a vault of Buddy's own. There are no existing daily-note conventions to match, so Buddy owns the layout.
- **Note text:** lightly cleaned. Claude fixes mishearings, punctuation and filler but keeps the user's words. Buddy confirms briefly.
- **Tool wiring:** our own tool loop inside `ClaudeChat`, not the SDK's beta tool runner, and no code-only shortcut.

## 1. Vault and tools

**Vault:**
- Path: `OBSIDIAN_VAULT_PATH`. If it is blank, there are no note tools and Buddy says it has nowhere to write.
- Folders: `OBSIDIAN_DAILY_FOLDER` (default `Daily`) and `OBSIDIAN_IDEAS_FOLDER` (default `Ideas`), created on first use.
- Buddy only adds lines and creates files. It never edits or deletes.

**`take_note(text, kind)`:**
- `kind = note` appends `- HH:MM text` to `Daily/YYYY-MM-DD.md`.
  - A new file starts with a heading like `# Monday 23 November 2026`.
  - Line breaks in the text are joined into one line.
- `kind = idea` takes an extra short `title` chosen by Claude and creates `Ideas/YYYY-MM-DD <title>.md`.
  - The file has frontmatter `date` and `tags: [idea, buddy]`, then the text.
  - The title is cleaned into a safe Windows filename: no path separators, `..`, reserved names or forbidden characters, and the length is capped.
  - If the name is taken, the file becomes `… 2.md`, `… 3.md`, and so on.
- After saving, Buddy confirms in a few words.

**`read_notes(date)`:**
- `date` is `YYYY-MM-DD` and defaults to today.
- Returns that day's daily-note lines plus the ideas created that day.
- Claude summarises them aloud in a sentence or two.

**Safety:**
- Code builds every path from the date and the cleaned title.
- Each resolved path must sit inside the vault before anything is written.
- Claude never supplies a path.

## 2. Tool loop

**Package layout:**
- `tools/registry.py` defines `Tool` (name, description, strict JSON schema, function) and `ToolRegistry`, the single place tools are registered.
- `tools/notes.py` defines `NoteVault`. It takes the vault path, the folder names and an injectable clock.
- `build_tools(settings)` returns the registry. It is empty when no vault is set.

**`ClaudeChat` handles each question in up to 3 rounds:**
1. Stream the reply with the tool schemas, yielding text as it arrives.
2. If `stop_reason == "tool_use"`:
   - Append the assistant's full `content`: text, `tool_use` and thinking blocks, unchanged.
   - Run each tool in a worker thread.
   - Send all `tool_result` blocks back in one user message.
   - Repeat.
3. The last round sends `tool_choice: none`, so Claude must answer in words.

**Details:**
- Tools are `strict: true` with `additionalProperties: false`.
- A refusal or `max_tokens` stop is checked before any tool runs.
- A tool exception becomes a `tool_result` with `is_error: true`.
- Every call is logged, e.g. `Tool take_note(note) -> Daily/2026-11-23.md`. This is the start of the action log the M8 dashboard will show.
- The system prompt carries today's date and weekday, rebuilt on every request.

**Unchanged:**
- The voice loop, memory, the emotion parser and interrupts.
- Memory stores only spoken text, not tool calls or results.
- A second emotion tag after a tool round is stripped as a mid-reply tag.

**Known limit:** an interrupt during a tool call may still save the note, even though the user didn't hear the confirmation.

## 3. Testing

**Unit tests** (temp vault, fixed clock):
- **Notes:** file creation and heading, line format, multi-line joining, empty text rejected.
- **Ideas:** frontmatter, filename cleaning (`../../x`, `CON`, `a:b/c`, 300-character titles), duplicate-name suffixes.
- **`read_notes`:** a day's notes plus that day's ideas, the empty-day message, and a bad date.
- **Safety:** nothing written outside the vault, and no tools when no vault is set.
- **Registry:** valid strict schemas; an unknown tool or a raising tool gives an error result.
- **`ClaudeChat` loop:**
  - Streaming order is text, then the tool call, then text.
  - Results go back in one message with matching ids.
  - Thinking blocks are passed back.
  - The last round sends `tool_choice: none`.
  - A refusal stops the turn before any tool runs.
- **Prompt:** includes today's date.

**Integration test (live API, temp vault):**
- "Take a note: buy batteries" writes the line.
- "What did I note today?" mentions batteries.

**Manual check:**
- The user sets the vault path.
- A note, an idea, and "what did I note today".
- Optionally, cleanup of a misheard word.

## Out of scope

- Wake word.
- Editing, deleting or searching notes across days.
- The web UI (M8).

## Related: web UI ideas (roadmap only)

The user wants a browser UI on the server for:
- settings, such as the vault path;
- an expression library for the eyes;
- editing the personality.

These are recorded in the roadmap:
- M8 grows to cover settings and personality editing in the browser.
- An expression library comes after M6.
- Non-secret settings will move to a file the UI can edit. Secrets stay in `.env`.
- This makes FastAPI the likely choice in M4.
