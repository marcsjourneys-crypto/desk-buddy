# Morning brief: design and plan

Approved by the user on 2026-11-26.

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

## Goal

"CARL, morning brief" gets today's weather and the newest world, AI and robotics headlines. Then "open number 4" (or "open the robotics one") opens that article after the spoken yes. Web search stays available for anything more. Email comes later: Outlook needs its own project.

## Decisions

- **A skill, not a built-in tool:** it's built on local skills (`docs/plans/2026-11-26-local-skills.md`) and uses only the standard library, so it runs with CARL's Python and needs no `uv sync`.
- **The sources:**
  - **Weather:** Open-Meteo, free and with no key.
  - **Headlines:** RSS or Atom feeds the user chooses.
- **Skills can return links**, through a small server addition that any skill can use. Links join the numbered list web search already fills, so opening one reuses `open_search_result` and its spoken yes.
- **No new dependencies.**

## Part 1: Skills that return pages (server)

- **The manifest:** an optional `"pages": true`, default false. The manifest rules and the README's manifest table gain it.
- **The output:** with `pages`, stdout must be one JSON object, `{"text": str, "pages": [{"title": str, "url": str}, …]}`.
  - `text` is what CARL reads, with the same 1000-character cap and the same untrusted wrapping as plain output.
  - There are at most 10 pages. Each title is cleaned the way search titles are and cut to 120 characters.
  - Each URL must pass `check_url` and `is_public_host` (no local or private addresses). A page that fails is dropped and logged as "dropped N pages"; the URL itself is never logged.
  - Output that isn't valid JSON or has the wrong shape makes the run `failed (bad output)`. Claude gets only that, and the log gets a short reason.
- **Into the numbered list:** good pages are added to `SearchResults` for this question, the same way web search results are, after any search results from the same question. At most 10 are kept, one per address. So:
  - in this question, `open_search_result` accepts their exact addresses;
  - at the next question with speech, they appear in the numbered note: `(System note: pages from your last search, …)`. The wording becomes "pages from your last search or skill".
- **Opening:** exactly as for search results: the guard, then "Shall I open the page on <site>?", then a strict yes.
- **The Live tab and log:** they still show "Ran <name>.", never the text or URLs. The skill's action event may add "N links".
- **Plumbing:** the runner returns the parsed pages to `SkillTools`, which adds them to `SearchResults`, passed in from the `Brain` (`brain.search_results`). Pages from a confirmed run, after a yes, are added to that question's results the same way.
- **Prompt:** one sentence added to `SKILLS_TEXT`: a skill's result may list pages the user can open afterwards by number with `open_search_result`, exactly like search results; never read out addresses.

## Part 2: The `morning-brief` example skill

`examples/skills/morning-brief/`:

- **`skill.json`:**
  - name "Morning brief";
  - spoken names "morning brief", "the news";
  - a description saying when to use it;
  - one choice, `section`: all (the default), weather, world, ai, robotics;
  - `needs_yes: false`, `pages: true`, `timeout_seconds: 30`.
- **`config.json`** (the user edits it, which switches the skill off until they re-tick it):
  ```json
  {
    "place": {"name": "", "latitude": null, "longitude": null},
    "units": "fahrenheit",
    "feeds": {
      "world": "<BBC World RSS>",
      "ai": "<TechCrunch AI RSS>",
      "robotics": "<IEEE Spectrum robotics RSS>"
    },
    "per_feed": 3
  }
  ```
  - **The place:** if latitude and longitude are blank but a name is set, the skill looks it up with Open-Meteo's geocoding API. If neither is set, it skips the weather with "set your place in config.json".
  - **Feeds:** the builder checks each default URL works today and picks working ones.
- **`main.py`** (standard library only):
  - reads the `section` from stdin;
  - fetches the weather and the chosen feeds in parallel threads, with 8 s per request, a user agent of `desk-buddy-morning-brief`, and at most 2 MB read per feed;
  - parses RSS 2.0 and Atom with `xml.etree.ElementTree` (expat refuses entity expansion attacks);
  - takes the newest `per_feed` items from each feed;
  - prints the JSON:
    - `text`: one weather sentence (today's high and low, the chance of rain, a plain sky word from the WMO code), then each section as "World: 1. <title>. 2. …";
    - `pages`: every headline in the same numbered order, so the numbers CARL says match the numbers that open;
  - a feed that fails adds "(couldn't reach the AI feed)" to the text, and the rest goes on;
  - it never exits non-zero for one failed source, only when everything failed.
- **Tests** (in `server/tests/test_example_skills.py`):
  - the manifest is valid;
  - `main.py`'s parsing and formatting functions, against saved sample RSS, Atom and Open-Meteo JSON, with no network: the numbering matches between text and pages, and the text fits in 1000 characters with 3 × 3 long titles;
  - one marked integration test runs it for real.

## Tasks

1. **Pages from skills.** The manifest flag and validation; the runner parses JSON output (`SkillResult` gains `pages`); `SkillTools` adds the pages to `SearchResults`, directly and after a yes; the note's wording; the prompt sentence.
   - Tests:
     - shape errors give `failed (bad output)`;
     - local and private URLs and bad schemes are dropped;
     - the 10 cap;
     - pages added after this question's search results;
     - the numbered note at the next question;
     - `open_search_result` by number opens a skill page after a yes;
     - no URL or text in the log or the Live tab;
     - a skill without `pages` is unchanged.
   - Commit: `Let skills hand back pages to open`.
2. **The morning-brief skill.** Files, a checked list of default feeds, and the tests above. Commit: `Add the morning brief example skill`.
3. **Docs.**
   - `CLAUDE.md`: the skills bullet (pages) and the `open_search_result` bullet (skill pages too).
   - `server/README.md`: "Writing a skill" gets `pages` and the JSON output; the morning brief setup.
   - `decisions.md` and `status.md`.
   - Commit: `Document the morning brief`.

## User check

1. Copy `morning-brief` into `carl-skills`, set your place in `config.json`, and tick it on the Apps tab.
2. "Morning brief": CARL gives the weather and the world, AI and robotics headlines.
3. "Open number 4": the robot asks "Shall I open the page on …?". Say yes and the article opens.
4. "Just the AI news": only that section.
5. "Tell me more about the second AI story": CARL searches the web.
