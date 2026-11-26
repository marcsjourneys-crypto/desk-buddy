"""The page itself: no CDN or other outside URL, nothing inline (the CSP forbids it), and
untrusted text (transcripts, Spotify names) only ever rendered with textContent."""

import re

from desk_buddy.dashboard import STATIC_DIR, STATIC_FILES, TOKEN_PLACEHOLDER

HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
JS = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
CSS = (STATIC_DIR / "style.css").read_text(encoding="utf-8")


def test_no_outside_urls_anywhere():
    for text in (HTML, JS, CSS):
        assert not re.search(
            r"(https?:)?//(?!127\.0\.0\.1|localhost|www\.w3\.org/2000/svg)[\w.-]+\.\w", text
        )
        assert "@import" not in text


def test_the_html_has_nothing_inline():
    assert TOKEN_PLACEHOLDER in HTML
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", HTML)  # only external scripts
    assert "<style" not in HTML
    assert not re.search(r"\sstyle=", HTML)
    assert not re.search(r"\son[a-z]+=", HTML)  # no inline event handlers
    for name in STATIC_FILES:
        assert f"/static/{name}" in HTML


def test_the_script_never_renders_text_as_html():
    for sink in (
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "eval(",
        "Function(",
    ):
        assert sink not in JS
    assert "textContent" in JS
    assert 'setAttribute("style"' not in JS and "setAttribute('style'" not in JS
    assert not re.search(r"\.on[a-z]+\s*=\s*['\"]", JS)  # no handlers built from strings


def test_every_change_sends_the_token_and_reconnects_with_back_off():
    assert 'meta[name="carl-token"]' in JS
    assert "X-CARL-Token" in JS
    assert "/api/events" in JS
    assert "30000" in JS  # back-off cap


def test_the_four_tabs_are_there():
    for tab in ("live", "settings", "personality", "apps"):
        assert f'id="tab-{tab}"' in HTML
        assert f'data-tab="{tab}"' in HTML
        assert f'"{tab}"' in JS  # showTab knows it


def test_the_apps_tab_finds_filters_ticks_and_saves():
    tab = HTML[HTML.index('id="tab-apps"') :]
    for part in (
        'id="apps-find"',
        'id="apps-filter"',
        'id="apps-found"',
        'id="apps-chosen"',
        'id="apps-save"',
        'id="bookmarks"',
        'id="bookmark-add"',
        'id="bookmarks-save"',
    ):
        assert part in tab
    for call in (
        '"/api/apps/available"',
        'api("PUT", "/api/apps"',
        'api("PUT", "/api/bookmarks"',
        "apps.${i}",
        "bookmarks.${i}",
        'type: "checkbox"',
    ):
        assert call in JS
    # Listing the apps is a GET that needs the token: every request sends it.
    assert 'headers: { "X-CARL-Token": TOKEN }' in JS


def test_the_about_you_card_is_first_on_the_personality_tab_with_its_privacy_note():
    tab = HTML[HTML.index('id="tab-personality"') :]
    assert tab.index("About you") < tab.index("Character")
    assert 'id="about"' in tab
    assert "goes to Anthropic with every question" in tab
    assert "search provider" in tab
    for name in ("name", "city", "region", "country", "notes"):
        assert f"{name}:" in JS  # a label for every field
    assert "about.${name}" in JS  # per-field errors and reset


def test_saving_one_about_field_keeps_unsaved_edits_in_the_others():
    # Rows remember their saved value; a re-render carries over edits that differ from
    # it, except in the field that was just saved or cleared.
    assert '"data-saved": value' in JS
    assert "unsaved[name] = input.value" in JS
    assert JS.count("renderPersonality(await api(") >= 2
    assert "{ about: { [name]: input.value } }), name)" in JS
    assert "about.${name}`)}`), name)" in JS


def test_the_apps_tab_has_a_skills_section_with_its_safety_line():
    tab = HTML[HTML.index('id="tab-apps"') :]
    assert tab.index("Websites") < tab.index("Skills")
    for part in ('id="skills"', 'id="skills-refresh"', 'id="skills-note"', 'id="skills-empty"'):
        assert part in tab
    assert (
        "A skill runs your own script on this PC. Only tick skills you wrote or trust. "
        "Editing a skill switches it off until you tick it again."
    ) in tab
    for call in (
        'api("GET", "/api/skills")',
        "/approve`",
        "/disable`",
        "encodeURIComponent(skill.slug)",
        "Set SKILLS_DIR on the Settings tab",
        '"asks first"',
        "Setting up…",
        'changed: "changed: re-approve"',
        '"pre"',  # a failed setup's output, as text
        "${e.field}: ${e.reason}",
    ):
        assert call in JS
    assert "loadSkills()" in JS[JS.index("async function loadApps") :]


def test_a_smart_reply_shows_its_model_and_route_as_plain_text():
    assert "function modelLabel(event)" in JS
    assert 'event.model.replace(/^claude-/, "")' in JS
    assert "${event.route}" in JS
    assert 'el("span", { class: "model", text: label })' in JS  # textContent via el()
    assert "modelLabel(event)" in JS.split('case "reply":', 1)[1].split("break;", 1)[0]
    assert ".msg .meta .model" in CSS
