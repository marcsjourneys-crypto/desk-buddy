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


def test_the_three_tabs_are_there():
    for tab in ("live", "settings", "personality"):
        assert f'id="tab-{tab}"' in HTML


def test_the_about_you_card_is_first_on_the_personality_tab_with_its_privacy_note():
    tab = HTML[HTML.index('id="tab-personality"') :]
    assert tab.index("About you") < tab.index("Character")
    assert 'id="about"' in tab
    assert "goes to Anthropic with every question" in tab
    assert "search provider" in tab
    for name in ("name", "city", "region", "country", "notes"):
        assert f"{name}:" in JS  # a label for every field
    assert "about.${name}" in JS  # per-field errors and reset
