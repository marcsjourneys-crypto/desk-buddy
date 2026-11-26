"""The example skills in examples/skills stay valid with the real store, and (integration)
run with CARL's Python through the real SkillRunner."""

import html
import http.server
import importlib.util
import io
import json
import re
import shutil
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from desk_buddy.skills_store import AVAILABLE, find_clashes, inspect_skill, parse_manifest
from desk_buddy.tools.skill_tools import input_schema, text_limit
from desk_buddy.tools.skills import (
    SkillRunner,
    check_pages,
    find_uv,
    has_project,
    parse_pages_output,
)

EXAMPLES = Path(__file__).resolve().parents[2] / "examples" / "skills"
NAMES = ("hello-time", "send-note-email", "morning-brief")
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "morning_brief"


def manifest_of(slug):
    manifest, errors = parse_manifest((EXAMPLES / slug / "skill.json").read_bytes())
    assert errors == ()
    assert manifest is not None
    return manifest


@pytest.mark.parametrize("slug", NAMES)
def test_example_is_a_valid_skill_without_packages(slug):
    skill = inspect_skill(EXAMPLES / slug)
    assert skill.errors == ()
    assert skill.status == AVAILABLE
    assert skill.fingerprint is not None
    assert not has_project(skill.folder)  # runs with CARL's Python, no setup


def test_examples_dont_clash():
    skills = [inspect_skill(EXAMPLES / slug) for slug in NAMES]
    assert find_clashes(skills) == {}


def test_hello_time_manifest():
    m = manifest_of("hello-time")
    assert m.name == "Hello time"
    assert not m.needs_yes
    assert m.timeout_seconds == 10
    assert m.text is None
    [choice] = m.choices
    assert (choice.name, choice.options, choice.default) == ("format", ("short", "long"), "short")
    schema = input_schema(m)
    assert schema["properties"]["format"]["enum"] == ["short", "long"]
    assert schema["required"] == ["format"]


def test_send_note_email_manifest():
    m = manifest_of("send-note-email")
    assert m.name == "Send note email"
    assert m.needs_yes
    assert m.choices == ()
    assert m.text is not None
    assert (m.text.description, m.text.max_length, m.text.required) == (
        "the message to send",
        200,
        True,
    )
    assert text_limit(m) == 200
    assert input_schema(m)["required"] == ["text"]


def test_send_note_email_never_sends():
    source = (EXAMPLES / "send-note-email" / "main.py").read_text(encoding="utf-8")
    code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
    for word in ("smtplib", "urllib", "socket", "http", "subprocess"):
        assert word not in code


# --- the morning brief's functions, against saved samples (no network) ---


@pytest.fixture(scope="module")
def brief():
    spec = importlib.util.spec_from_file_location(
        "morning_brief_main", EXAMPLES / "morning-brief" / "main.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sample(name):
    return (FIXTURES / name).read_bytes()


WORLD_URL = "https://feeds.example.com/world.xml"
AI_URL = "https://feeds.example.com/ai.xml"
ROBOTICS_URL = "https://feeds.example.com/robotics.xml"


def config(**place):
    return {
        "place": {"name": "", "latitude": None, "longitude": None, **place},
        "units": "fahrenheit",
        "feeds": {"world": WORLD_URL, "ai": AI_URL, "robotics": ROBOTICS_URL},
        "per_feed": 3,
    }


@pytest.fixture
def fake_net(brief, monkeypatch):
    """brief.fetch answers from the samples; a URL starting with anything in `failing`
    raises SourceFailed. `asked` lists the URLs fetched."""
    failing = set()
    asked = []
    feeds = {WORLD_URL: "rss.xml", AI_URL: "rss1.xml", ROBOTICS_URL: "atom.xml"}

    def fetch(url):
        asked.append(url)
        if any(url.startswith(prefix) for prefix in failing):
            raise brief.SourceFailed("test")
        if url.startswith(brief.GEOCODING_URL):
            return sample("geocoding.json")
        if url.startswith(brief.FORECAST_URL):
            return sample("forecast.json")
        return sample(feeds[url])

    monkeypatch.setattr(brief, "fetch", fetch)
    return failing, asked


def run_brief(brief, section, raw):
    return brief.run(section, brief.load_config_dict(raw))


def assert_numbered_like_pages(text, pages):
    """The text says 1..N in order, and number i starts like page i's title."""
    assert [int(n) for n in re.findall(r"(?:^|[:.)] )(\d+)\. ", text)] == list(
        range(1, len(pages) + 1)
    )
    for i, page in enumerate(pages, 1):
        start = text.index(f" {i}. ") + len(f" {i}. ")
        said = text[start:].split(f" {i + 1}. ")[0]
        assert page["title"][:12] == said[:12], (i, said, page["title"])


def test_morning_brief_manifest():
    m = manifest_of("morning-brief")
    assert m.name == "Morning brief"
    assert m.spoken_names == ("morning brief", "the news")
    assert m.pages is True
    assert not m.needs_yes
    assert m.timeout_seconds == 30
    assert m.text is None
    [choice] = m.choices
    assert choice.name == "section"
    assert choice.options == ("all", "weather", "world", "ai", "robotics")
    assert choice.default == "all"


def test_morning_brief_config_template(brief):
    path = EXAMPLES / "morning-brief" / "config.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert set(raw) == {"place", "units", "feeds", "per_feed"}
    assert raw["place"] == {"name": "", "latitude": None, "longitude": None}
    assert raw["units"] == "fahrenheit"
    assert set(raw["feeds"]) == {"world", "ai", "robotics"}
    assert all(url.startswith("https://") for url in raw["feeds"].values())
    assert raw["per_feed"] == 3
    assert brief.load_config(path)["feeds"] == raw["feeds"]


def test_parse_rss(brief):
    items = brief.parse_feed(sample("rss.xml"))
    assert len(items) == 6
    assert brief.newest(items, 3) == [
        ("Calm seas & clear skies forecast", "https://news.example.com/world/a1"),
        ("Leaders meet & agree on fish quotas", "https://news.example.com/world/a2"),
        ("Robot wins chess match", "https://news.example.com/world/a4"),
    ]  # the localhost link and the repeated address are left out


def test_parse_atom(brief):
    items = brief.parse_feed(sample("atom.xml"))
    assert items[0] == ("Arms & legs: a new walker", "https://robots.example.org/e1")
    assert brief.newest(items, 3) == [
        ("Arms & legs: a new walker", "https://robots.example.org/e1"),
        ("Tiny drones learn to fly", "https://robots.example.org/e5"),
        ("Servo smoothness explained", "https://robots.example.org/e6"),
    ]  # user:pass, the non-ASCII host and the private IP are left out


def test_parse_namespaced_rss_1(brief):
    assert brief.newest(brief.parse_feed(sample("rss1.xml")), 3) == [
        ("Models learn to say “I don’t know”", "https://ai.example.net/n1"),
        ("Chips get cheaper & faster", "https://ai.example.net/n2"),
        ("Why 42 is still the answer", "https://ai.example.net/n3"),
    ]


def test_clean_title(brief):
    raw = "  Fish &amp; chips\n\t<i>today</i>​ "
    assert brief.clean_title(raw) == "Fish & chips today"
    cut = brief.clean_title("word " * 40)
    assert len(cut) <= brief.TITLE_LIMIT and cut.endswith("word...")


@pytest.mark.parametrize("data", [b"not xml", b"<html><body>hi</body></html>", b""])
def test_parse_feed_refuses_what_isnt_a_feed(brief, data):
    with pytest.raises(brief.SourceFailed):
        brief.parse_feed(data)


def test_parse_feed_refuses_entity_expansion(brief):
    entities = ['<!ENTITY a "aaaaaaaaaa">']
    for i in range(1, 9):
        prev, name = chr(96 + i), chr(97 + i)
        entities.append(f'<!ENTITY {name} "{("&" + prev + ";") * 10}">')
    bomb = f'<?xml version="1.0"?><!DOCTYPE rss [{"".join(entities)}]>'
    bomb += "<rss><channel><item><title>&i;</title></item></channel></rss>"
    with pytest.raises(brief.SourceFailed):
        brief.parse_feed(bomb.encode())


@pytest.mark.parametrize(
    "url",
    [
        "https://www.bbc.co.uk/news/world-1",
        "http://spectrum.ieee.org/a?b=c#d",
        "https://xn--rbts-6qa.example.org/e",
    ],
)
def test_safe_link_accepts(brief, url):
    assert brief.safe_link(url)


@pytest.mark.parametrize(
    "url",
    [
        "",
        "ftp://example.com/a",
        "javascript:alert(1)",
        "https://localhost/a",
        "https://intranet/a",
        "https://printer.local/a",
        "https://nas.lan/a",
        "https://router.home.arpa/a",
        "https://db.internal/a",
        "http://127.0.0.1/a",
        "http://10.0.0.5/a",
        "http://8.8.8.8/a",
        "http://[::1]/a",
        "https://user:pass@example.com/a",
        "https://user@example.com/a",
        "https://ex\u00e4mple.com/a",
        "https://example.com/a b",
        "https://example.com/a\u200b",
        "https://example.com\\@evil.com/",
        "https://example.com/" + "a" * 490,
    ],
)
def test_safe_link_refuses(brief, url):
    assert not brief.safe_link(url)


def test_describe_weather(brief):
    data = json.loads(sample("forecast.json"))
    assert brief.describe_weather(data, "Leeds") == (
        "Weather in Leeds: high 54, low 41, 20 percent chance of rain, partly cloudy."
    )
    data["daily"]["precipitation_probability_max"] = [None]
    data["daily"]["weather_code"] = [95]
    assert brief.describe_weather(data, "") == "Weather today: high 54, low 41, thunderstorms."
    with pytest.raises(brief.SourceFailed):
        brief.describe_weather({"daily": {}}, "Leeds")


@pytest.mark.parametrize(
    ("code", "word"),
    [(0, "clear"), (2, "partly cloudy"), (3, "cloudy"), (45, "fog"), (53, "drizzle")]
    + [(63, "rain"), (75, "snow"), (81, "showers"), (99, "thunderstorms"), (42, "")],
)
def test_sky_word(brief, code, word):
    assert brief.sky_word(code) == word


def test_place_from_geocoding(brief):
    found = brief.place_from_geocoding(json.loads(sample("geocoding.json")))
    assert found == ("Leeds", 53.79648, -1.54785)
    assert brief.place_from_geocoding({"generationtime_ms": 1}) is None


def test_build_output_numbers_text_like_pages(brief):
    rss = brief.newest(brief.parse_feed(sample("rss.xml")), 3)
    atom = brief.newest(brief.parse_feed(sample("atom.xml")), 3)
    out = brief.build_output("Weather in Leeds: high 54.", [("World", rss), ("Robotics", atom)])
    assert set(out) == {"text", "pages"}
    assert [p["url"] for p in out["pages"]] == [url for _, url in rss + atom]
    assert out["text"] == (
        "Weather in Leeds: high 54. World: 1. Calm seas & clear skies forecast. "
        "2. Leaders meet & agree on fish quotas. 3. Robot wins chess match. "
        "Robotics: 4. Arms & legs: a new walker. 5. Tiny drones learn to fly. "
        "6. Servo smoothness explained."
    )
    assert_numbered_like_pages(out["text"], out["pages"])


def test_build_output_fits_nine_long_titles(brief):
    long = "Breaking & developing: " + "an extraordinarily lengthy headline " * 8
    sections = [
        (label, [(brief.clean_title(f"{long}{i}"), f"https://e.com/{label}/{i}") for i in range(3)])
        for label in ("World", "AI", "Robotics")
    ]
    weather = "Weather in Llanfairpwllgwyngyll: high 54, low 41, 20 percent chance of rain, fog."
    out = brief.build_output(weather, sections)
    assert len(out["pages"]) == 9
    assert all(len(p["title"]) <= brief.TITLE_LIMIT for p in out["pages"])
    escaped = out["text"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    assert len(escaped) <= 1000
    assert out["text"].startswith(weather)
    assert re.findall(r"(\d)\. Breaking", out["text"]) == [str(n) for n in range(1, 10)]
    for label in ("World: 1.", "AI: 4.", "Robotics: 7."):
        assert label in out["text"]


def test_build_output_leaves_out_an_address_another_feed_gave(brief):
    world = [("A", "https://e.com/1"), ("B", "https://e.com/2")]
    ai = [("A again", "https://e.com/1"), ("C", "https://e.com/3")]
    out = brief.build_output(None, [("World", world), ("AI", ai)])
    assert out["text"] == "World: 1. A. 2. B. AI: 3. C."
    assert [p["url"] for p in out["pages"]] == [
        "https://e.com/1",
        "https://e.com/2",
        "https://e.com/3",
    ]
    only_dupes = brief.build_output(None, [("World", world), ("AI", world)])
    assert only_dupes["text"] == "World: 1. A. 2. B. (The AI feed had no headlines.)"


def test_build_output_caps_pages_at_ten(brief):
    def items(label, n):
        return [(f"Story {i}", f"https://e.com/{label}/{i}") for i in range(n)]

    out = brief.build_output(
        None, [("World", items("w", 4)), ("AI", items("a", 3)), ("Robotics", items("r", 4))]
    )
    assert len(out["pages"]) == 10
    assert re.findall(r"(\d+)\. Story", out["text"]) == [str(n) for n in range(1, 11)]


def test_output_passes_the_server_parser(brief, fake_net):
    out, code = run_brief(brief, "all", config(name="Leeds"))
    assert code == 0
    data = json.dumps(out, ensure_ascii=True).encode("ascii")
    assert len(data) <= 64_000
    text, raw_pages = parse_pages_output(data)
    assert len(text) <= 1000
    pages, dropped = check_pages(raw_pages)
    assert dropped == 0
    assert [url for _, _, url in pages] == [p["url"] for p in out["pages"]]
    assert [title for title, _, _ in pages] == [p["title"] for p in out["pages"]]
    assert len(pages) == 9
    assert_numbered_like_pages(text, out["pages"])
    assert text.startswith("Weather in Leeds: high 54, low 41, 20 percent chance of rain")
    assert "World: 1. " in text and "AI: 4. " in text and "Robotics: 7. " in text


def test_run_looks_up_the_place_only_without_coordinates(brief, fake_net):
    _, asked = fake_net
    run_brief(brief, "weather", config(name="Leeds"))
    assert [url.split("?")[0] for url in asked] == [brief.GEOCODING_URL, brief.FORECAST_URL]
    asked.clear()
    out, code = run_brief(brief, "weather", config(name="Home", latitude=53.8, longitude=-1.55))
    assert [url.split("?")[0] for url in asked] == [brief.FORECAST_URL]
    assert "latitude=53.8000" in asked[0] and "temperature_unit=fahrenheit" in asked[0]
    text = "Weather in Home: high 54, low 41, 20 percent chance of rain, partly cloudy."
    assert out == {"text": text, "pages": []}
    assert code == 0


def test_run_without_a_place_asks_for_one(brief, fake_net):
    _, asked = fake_net
    out, code = run_brief(brief, "weather", config())
    assert out == {"text": brief.NO_PLACE, "pages": []}
    assert code == 0
    assert asked == []


@pytest.mark.parametrize(
    ("section", "label", "feed"),
    [("world", "World", WORLD_URL), ("ai", "AI", AI_URL), ("robotics", "Robotics", ROBOTICS_URL)],
)
def test_run_one_section(brief, fake_net, section, label, feed):
    _, asked = fake_net
    out, code = run_brief(brief, section, config(name="Leeds"))
    assert code == 0
    assert asked == [feed]  # no weather, no other feeds
    assert out["text"].startswith(f"{label}: 1. ")
    assert "Weather" not in out["text"]
    assert len(out["pages"]) == 3
    assert_numbered_like_pages(out["text"], out["pages"])


def test_run_with_a_failed_feed_goes_on(brief, fake_net):
    failing, _ = fake_net
    failing.add(AI_URL)
    out, code = run_brief(brief, "all", config(name="Leeds"))
    assert code == 0
    assert "(I couldn't reach the AI feed.)" in out["text"]
    assert "World: 1. " in out["text"] and "Robotics: 4. " in out["text"]
    assert len(out["pages"]) == 6
    assert_numbered_like_pages(out["text"], out["pages"])


def test_run_with_failed_weather_goes_on(brief, fake_net):
    failing, _ = fake_net
    failing.add(brief.FORECAST_URL)
    out, code = run_brief(brief, "all", config(name="Leeds"))
    assert code == 0
    assert out["text"].startswith("(I couldn't reach the weather service.) World: 1. ")


def test_run_fails_only_when_everything_failed(brief, fake_net):
    failing, _ = fake_net
    failing.update({WORLD_URL, AI_URL, ROBOTICS_URL, brief.FORECAST_URL})
    assert run_brief(brief, "all", config(name="Leeds"))[1] == 1
    assert run_brief(brief, "ai", config(name="Leeds"))[1] == 1


def test_main_prints_one_json_object(brief, fake_net, monkeypatch, capsys):
    cfg = brief.load_config_dict(config(name="Leeds"))
    monkeypatch.setattr(brief, "load_config", lambda path: cfg)
    monkeypatch.setattr("sys.stdin", io.StringIO('{"section": "robotics"}'))
    assert brief.main() == 0
    out = capsys.readouterr().out
    assert out.endswith("\n") and out.count("\n") == 1
    assert set(json.loads(out)) == {"text", "pages"}
    monkeypatch.setattr("sys.stdin", io.StringIO('{"section": "sport"}'))
    assert brief.main() == 2
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "change",
    [
        {"units": "kelvin"},
        {"per_feed": 4},
        {"per_feed": True},
        {"feeds": {"world": "file:///C:/feed.xml"}},
        {"place": {"name": "", "latitude": "north", "longitude": 1}},
        {"place": {"name": "", "latitude": 91, "longitude": 1}},
    ],
)
def test_load_config_refuses_bad_values(brief, change):
    with pytest.raises(ValueError):
        brief.load_config_dict({**config(), **change})


def test_clean_title_strips_tags_before_unescaping(brief):
    assert brief.clean_title("5 &lt; 6 and 7 &gt; 3, <b>bold</b>") == "5 < 6 and 7 > 3, bold"


def test_item_links_rel_case_and_permalink_guid(brief):
    feed = b"""<rss><channel>
      <item><title>Guid only</title><guid isPermaLink="true">https://e.com/g1</guid></item>
      <item><title>Guid not a link</title><guid isPermaLink="false">abc-123</guid></item>
      <item><title>Link wins</title><link>https://e.com/l3</link>
        <guid isPermaLink="true">https://e.com/g3</guid></item>
    </channel></rss>"""
    assert brief.parse_feed(feed) == [
        ("Guid only", "https://e.com/g1"),
        ("Guid not a link", ""),
        ("Link wins", "https://e.com/l3"),
    ]
    atom = b"""<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Caps</title>
      <link rel="SELF" href="https://e.com/self"/>
      <link rel=" Alternate " href="https://e.com/alt"/></entry></feed>"""
    assert brief.parse_feed(atom) == [("Caps", "https://e.com/alt")]


@pytest.mark.parametrize("encoding", ["shift_jis", "gb2312", "x-no-such-encoding"])
def test_feeds_in_unreadable_encodings_fail_alone(brief, monkeypatch, encoding):
    data = f'<?xml version="1.0" encoding="{encoding}"?><rss><channel/></rss>'.encode()
    with pytest.raises(brief.SourceFailed):
        brief.parse_feed(data)
    monkeypatch.setattr(brief, "fetch", lambda url: data)
    assert brief.get_feed("ai", AI_URL, 3) is None


@pytest.mark.parametrize(
    "body",
    [
        b'{"daily": {"temperature_2m_max": [NaN], "temperature_2m_min": [40]}}',
        b'{"daily": {"temperature_2m_max": [Infinity], "temperature_2m_min": [40]}}',
        b'{"daily": {"temperature_2m_max": [50], "temperature_2m_min": [-Infinity]}}',
        b'{"daily": {"temperature_2m_max": [1e400], "temperature_2m_min": [40]}}',
        b'{"daily": {"temperature_2m_max": [1' + b"0" * 400 + b'], "temperature_2m_min": [4]}}',
        b"[" * 100_000,
        b"not json",
    ],
)
def test_bad_weather_numbers_or_json_fail_alone(brief, monkeypatch, body):
    monkeypatch.setattr(brief, "fetch", lambda url: body)
    cfg = brief.load_config_dict(config(name="Leeds", latitude=53.8, longitude=-1.55))
    assert brief.get_weather(cfg) == (brief.NO_WEATHER, False)


def test_odd_optional_weather_numbers_are_left_out(brief):
    data = json.loads(sample("forecast.json"))
    data["daily"]["precipitation_probability_max"] = [float("nan")]
    data["daily"]["weather_code"] = [10**400]
    assert brief.describe_weather(data, "Leeds") == "Weather in Leeds: high 54, low 41."


def test_an_unexpected_error_fails_only_its_source(brief, monkeypatch, capsys):
    def boom(url):
        raise RuntimeError("secret detail")

    monkeypatch.setattr(brief, "fetch", boom)
    assert brief.get_feed("ai", AI_URL, 3) is None
    cfg = brief.load_config_dict(config(name="Leeds", latitude=53.8, longitude=-1.55))
    assert brief.get_weather(cfg) == (brief.NO_WEATHER, False)
    err = capsys.readouterr().err
    assert "RuntimeError" in err and "secret detail" not in err


def test_a_stuck_source_is_given_up_at_the_deadline(brief, fake_net, monkeypatch):
    release = threading.Event()
    real_fetch = brief.fetch

    def fetch(url):
        if url == AI_URL:
            release.wait(5)  # stuck, say in DNS, far past the deadline
        return real_fetch(url)

    monkeypatch.setattr(brief, "fetch", fetch)
    monkeypatch.setattr(brief, "RUN_DEADLINE_S", 0.3)
    try:
        start = time.monotonic()
        out, code = run_brief(brief, "all", config(name="Leeds"))
        assert time.monotonic() - start < 2
    finally:
        release.set()
    assert code == 0
    assert "(I couldn't reach the AI feed.)" in out["text"]
    assert out["text"].startswith("Weather in Leeds: high 54")
    assert "World: 1. " in out["text"] and "Robotics: 4. " in out["text"]
    assert len(out["pages"]) == 6
    assert_numbered_like_pages(out["text"], out["pages"])


def test_a_very_long_weather_line_drops_whole_headlines(brief):
    weather = "Weather in Leeds: " + "a long and windy note, " * 40
    sections = [
        (label, [(f"{label} headline number {i} " * 3, f"https://e.com/{label}/{i}")])
        for label, i in (("World", 1), ("AI", 2), ("Robotics", 3))
    ]
    out = brief.build_output(weather, sections)
    escaped = out["text"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    assert len(escaped) <= 1000
    assert 0 < len(out["pages"]) < 3
    assert_numbered_like_pages(out["text"], out["pages"])
    assert "had no headlines" not in out["text"]
    huge = brief.build_output("x" * 2000, sections)
    assert huge == {"text": "x" * 1000, "pages": []}


def test_config_needs_both_coordinates_or_neither(brief, tmp_path, monkeypatch, capsys):
    for place in ({"latitude": 53.8}, {"longitude": -1.55}, {"latitude": "", "longitude": 2}):
        with pytest.raises(ValueError, match="both"):
            brief.load_config_dict(config(**place))
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config(latitude=53.8)), encoding="utf-8")
    real_load = brief.load_config
    monkeypatch.setattr(brief, "load_config", lambda _: real_load(path))
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    assert brief.main() == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert 'set both "latitude" and "longitude"' in captured.err


# --- the morning brief's fetch against a local server ---


class _FeedHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        port = self.server.server_address[1]
        if self.path == "/feed":
            body = sample("rss.xml")
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/big":
            self.send_response(200)
            self.send_header("Content-Length", "300000")
            self.end_headers()
            self.wfile.write(b"x" * 300_000)
        elif self.path == "/drip":
            self.send_response(200)
            self.send_header("Content-Length", "1000")
            self.end_headers()
            try:
                for _ in range(40):  # a byte every 0.1 s: never idle long enough to time out
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    time.sleep(0.1)
            except OSError:
                pass
        elif self.path == "/to-local":
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{port}/feed")
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self.send_error(404)


@pytest.fixture
def feed_server():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FeedHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_fetch_reads_a_feed(brief, feed_server):
    assert brief.fetch(f"{feed_server}/feed") == sample("rss.xml")


def test_fetch_stops_past_the_size_cap(brief, feed_server, monkeypatch):
    monkeypatch.setattr(brief, "MAX_BYTES", 100_000)
    with pytest.raises(brief.SourceFailed, match="over"):
        brief.fetch(f"{feed_server}/big")


def test_fetch_stops_a_dripping_server_at_its_deadline(brief, feed_server, monkeypatch):
    monkeypatch.setattr(brief, "TIMEOUT_S", 0.5)
    start = time.monotonic()
    with pytest.raises(brief.SourceFailed, match="too slow"):
        brief.fetch(f"{feed_server}/drip")
    assert time.monotonic() - start < 2


def test_fetch_refuses_a_redirect_to_a_local_address(brief, feed_server):
    with pytest.raises(brief.SourceFailed, match="redirect refused"):
        brief.fetch(f"{feed_server}/to-local")


def test_fetch_fails_on_a_404(brief, feed_server):
    with pytest.raises(brief.SourceFailed, match="HTTPError"):
        brief.fetch(f"{feed_server}/nothing")


@pytest.mark.parametrize(
    ("start", "target", "allowed"),
    [
        ("https://feeds.example.com/a", "https://cdn.example.com/a", True),
        ("http://feeds.example.com/a", "https://feeds.example.com/a", True),
        ("http://feeds.example.com/a", "http://feeds.example.org/a", True),
        ("https://feeds.example.com/a", "http://feeds.example.com/a", False),
        ("https://feeds.example.com/a", "https://localhost/a", False),
        ("https://feeds.example.com/a", "https://10.0.0.1/a", False),
        ("https://feeds.example.com/a", "file:///C:/secret.txt", False),
    ],
)
def test_redirects_only_to_safe_addresses(brief, start, target, allowed):
    handler = brief.SafeRedirects()
    request = urllib.request.Request(start)
    if allowed:
        new = handler.redirect_request(request, None, 302, "Found", {}, target)
        assert new.full_url == target
    else:
        with pytest.raises(brief.SourceFailed):
            handler.redirect_request(request, None, 302, "Found", {}, target)
    assert handler.max_redirections == 3


# --- running them for real ---


def result_body(result, slug):
    assert result.ok, result
    head, tail = f'<untrusted source="skill:{slug}">\n', "\n</untrusted>"
    assert result.text.startswith(head) and result.text.endswith(tail)
    return html.unescape(result.text[len(head) : -len(tail)])


@pytest.fixture
def runner_and_skills(tmp_path):
    uv = find_uv()
    if uv is None:
        pytest.skip("uv isn't installed")
    runner = SkillRunner(uv, tmp_path / "appdata")
    copies = {}
    for slug in NAMES:
        folder = tmp_path / "skills" / slug
        shutil.copytree(EXAMPLES / slug, folder)
        copies[slug] = inspect_skill(folder)
        assert copies[slug].valid
    return runner, copies


@pytest.mark.integration
def test_hello_time_runs(runner_and_skills):
    runner, skills = runner_and_skills
    short = result_body(runner.run(skills["hello-time"], {"format": "short"}), "hello-time")
    assert re.fullmatch(r"It's \d\d:\d\d \(the short format\)\.", short), short
    long = result_body(runner.run(skills["hello-time"], {"format": "long"}), "hello-time")
    assert re.fullmatch(r"It's \w+ \d\d \w+ \d{4}, \d\d:\d\d:\d\d \(the long format\)\.", long)


@pytest.mark.integration
def test_send_note_email_only_says_what_it_would_send(runner_and_skills):
    runner, skills = runner_and_skills
    result = runner.run(skills["send-note-email"], {"text": "I'm running late <b> & ☕"})
    assert result_body(result, "send-note-email") == "Would send: I'm running late <b> & ☕"
    empty = runner.run(skills["send-note-email"], {})
    assert not empty.ok
    assert empty.summary == "failed (exit 2)"


@pytest.mark.integration
def test_morning_brief_runs(runner_and_skills, tmp_path):
    """The real feeds and Open-Meteo, through the real runner, with a fixed place."""
    runner, _ = runner_and_skills
    folder = tmp_path / "skills" / "morning-brief"
    path = folder / "config.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["place"] = {"name": "Leeds", "latitude": 53.8, "longitude": -1.55}
    path.write_text(json.dumps(raw), encoding="utf-8")
    skill = inspect_skill(folder)
    assert skill.valid
    result = runner.run(skill, {"section": "all"})
    body = result_body(result, "morning-brief")
    assert len(body) <= 1000
    assert body.startswith("Weather in Leeds: high ")
    assert 1 <= len(result.pages) <= 9
    assert all(url.startswith(("https://", "http://")) for _, _, url in result.pages)
    numbers = [int(n) for n in re.findall(r"(?:^|[:.)] )(\d+)\. ", body)]
    assert numbers == list(range(1, len(result.pages) + 1))
    sections = [label for label in ("World:", "AI:", "Robotics:") if label in body]
    print(f"\nmorning brief: {len(body)} chars, {len(result.pages)} pages, sections {sections}")
