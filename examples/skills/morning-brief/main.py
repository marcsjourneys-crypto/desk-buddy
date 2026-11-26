"""An example skill for CARL that hands back pages: today's weather and the newest
headlines from a few RSS or Atom feeds.

CARL sends {"section": "all" | "weather" | "world" | "ai" | "robotics"} on stdin. With
"pages": true in skill.json, the script prints one JSON object:
{"text": "<what CARL says>", "pages": [{"title": ..., "url": ...}, ...]}. Page i is
number i, so the headlines in the text are numbered in the same order as the pages,
and "open number 4" opens the fourth one (after a spoken "yes").

The place, the units and the feeds are in config.json next to this file. Weather comes
from Open-Meteo (free, no key). Standard library only, so it runs with CARL's own
Python and needs no setup. Warnings go to stderr (CARL's log), never stdout.
"""

import html
import ipaddress
import json
import re
import sys
import unicodedata
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from xml.etree import ElementTree

USER_AGENT = "desk-buddy-morning-brief"
TIMEOUT_S = 8  # per request
MAX_BYTES = 2_000_000  # read at most this much of any response
TEXT_LIMIT = 1000  # characters CARL reads (counted after CARL escapes & < >)
MAX_PAGES = 10
MAX_URL = 500
TITLE_LIMIT = 110  # characters of a headline; shorter in the text if it must fit
SECTIONS = ("world", "ai", "robotics")
LABELS = {"world": "World", "ai": "AI", "robotics": "Robotics"}
CHOICES = ("all", "weather", *SECTIONS)
UNITS = ("fahrenheit", "celsius")

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"

NO_PLACE = "Set your place in config.json to get the weather."
NO_WEATHER = "(I couldn't reach the weather service.)"
NO_PLACE_FOUND = "(I couldn't find the place in config.json for the weather.)"


class SourceFailed(Exception):
    """A weather or feed source couldn't be fetched or read."""


# --- links: a small, conservative version of CARL's own checks ---

_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_HOST = re.compile(rf"(?:{_LABEL}\.)+[a-z][a-z0-9-]*[a-z0-9]")  # needs a dot; no IPs
_LOCAL_SUFFIXES = (".local", ".localhost", ".internal", ".lan", ".home.arpa")


def safe_link(url):
    """Whether CARL would accept the address as a page: http(s), at most 500 characters,
    no spaces or invisible characters, no user name or password, and a public host name
    of plain ASCII letters, digits, dots and hyphens (no IP addresses at all)."""
    if not isinstance(url, str) or not url or len(url) > MAX_URL:
        return False
    if "\\" in url or any(unicodedata.category(c)[0] in "CZ" for c in url):
        return False
    try:
        parts = urllib.parse.urlsplit(url)
        port_ok = parts.port is None or parts.port > 0
    except ValueError:
        return False
    if parts.scheme.lower() not in ("http", "https") or not port_ok or "@" in parts.netloc:
        return False
    host = parts.hostname or ""
    if host == "localhost" or host.endswith(_LOCAL_SUFFIXES) or not _HOST.fullmatch(host):
        return False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return True
    return False  # the pattern already refuses IPs; this is belt and braces


# --- feeds ---


def _local(tag):
    """An element's tag without its namespace: "{http://www.w3.org/2005/Atom}entry" is
    "entry"."""
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def clean_title(text, limit=TITLE_LIMIT):
    """One plain line: HTML entities unescaped, tags and invisible characters removed,
    whitespace collapsed, cut at a word to `limit` characters."""
    text = html.unescape(text or "")
    text = re.sub(r"<[^>]*>", " ", text)
    text = "".join(" " if unicodedata.category(c)[0] in "CZ" else c for c in text)
    text = " ".join(text.split())
    return cut(text, limit)


def cut(text, limit):
    if len(text) <= limit:
        return text
    head = text[: limit - 3]
    if " " in head[limit // 2 :]:
        head = head.rsplit(" ", 1)[0]
    return head.rstrip(" ,;:-") + "..."


def _item_link(item):
    """RSS: the <link> text (or an atom:link's href). Atom: the href of the
    <link rel="alternate"> or the <link> with no rel."""
    for child in item:
        if _local(child.tag) != "link":
            continue
        text = (child.text or "").strip()
        if text:
            return text
        href = (child.get("href") or "").strip()
        if href and child.get("rel", "alternate") == "alternate":
            return href
    return ""


def _item_title(item):
    for child in item:
        if _local(child.tag) == "title":
            return "".join(child.itertext())
    return ""


def parse_feed(data):
    """An RSS 2.0 (or 1.0) or Atom feed's items, in feed order, as [(title, url)].
    Raises SourceFailed if it isn't a feed. The titles are cleaned; the links aren't
    checked here."""
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError as exc:
        raise SourceFailed(f"not XML ({exc})") from None
    if _local(root.tag) not in ("rss", "feed", "RDF"):
        raise SourceFailed(f"not a feed (<{_local(root.tag)}>)")
    items = []
    for el in root.iter():
        if _local(el.tag) in ("item", "entry"):
            items.append((clean_title(_item_title(el)), _item_link(el)))
    return items


def newest(items, count):
    """The first `count` usable items (feeds list the newest first): a title, a link
    CARL accepts, and no address given twice."""
    picked, seen = [], set()
    for title, url in items:
        if len(picked) >= count:
            break
        if title and safe_link(url) and url not in seen:
            seen.add(url)
            picked.append((title, url))
    return picked


# --- weather ---

_SKY = (
    ((0, 1), "clear"),
    ((2,), "partly cloudy"),
    ((3,), "cloudy"),
    ((45, 48), "fog"),
    ((51, 53, 55, 56, 57), "drizzle"),
    ((61, 63, 65, 66, 67), "rain"),
    ((71, 73, 75, 77, 85, 86), "snow"),
    ((80, 81, 82), "showers"),
    ((95, 96, 99), "thunderstorms"),
)


def sky_word(code):
    """A plain word for a WMO weather code, or "" if it's unknown."""
    for codes, word in _SKY:
        if code in codes:
            return word
    return ""


def _first(daily, key):
    values = daily.get(key)
    if isinstance(values, list) and values and isinstance(values[0], int | float):
        if not isinstance(values[0], bool):
            return values[0]
    return None


def describe_weather(data, place, units="fahrenheit"):
    """One sentence about today from an Open-Meteo forecast response, e.g. "Weather in
    Leeds: high 54, low 41, 20 percent chance of rain, partly cloudy." Raises
    SourceFailed if the response has no high or low for today."""
    daily = data.get("daily") if isinstance(data, dict) else None
    if not isinstance(daily, dict):
        raise SourceFailed("no daily forecast")
    high = _first(daily, "temperature_2m_max")
    low = _first(daily, "temperature_2m_min")
    if high is None or low is None:
        raise SourceFailed("no high or low")
    parts = [f"high {round(high)}", f"low {round(low)}"]
    rain = _first(daily, "precipitation_probability_max")
    if rain is not None:
        parts.append(f"{round(rain)} percent chance of rain")
    code = _first(daily, "weather_code")
    word = sky_word(int(code)) if code is not None else ""
    if word:
        parts.append(word)
    place = clean_title(place, 60)
    head = f"Weather in {place}" if place else "Weather today"
    return f"{head}: {', '.join(parts)}."


def place_from_geocoding(data):
    """(name, latitude, longitude) of the first result of an Open-Meteo geocoding
    response, or None."""
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list) or not results or not isinstance(results[0], dict):
        return None
    first = results[0]
    lat, lon = first.get("latitude"), first.get("longitude")
    if not all(isinstance(v, int | float) and not isinstance(v, bool) for v in (lat, lon)):
        return None
    return str(first.get("name") or ""), float(lat), float(lon)


def forecast_url(lat, lon, units):
    query = urllib.parse.urlencode(
        {
            "latitude": f"{lat:.4f}",
            "longitude": f"{lon:.4f}",
            "daily": "temperature_2m_max,temperature_2m_min,"
            "precipitation_probability_max,weather_code",
            "timezone": "auto",
            "forecast_days": 1,
            "temperature_unit": units,
        }
    )
    return f"{FORECAST_URL}?{query}"


def geocoding_url(name):
    query = urllib.parse.urlencode({"name": name, "count": 1, "language": "en", "format": "json"})
    return f"{GEOCODING_URL}?{query}"


# --- the output ---


def _escaped_len(text):
    """The length CARL counts: it escapes & < > before applying its 1000 cap."""
    return len(text) + 4 * text.count("&") + 3 * (text.count("<") + text.count(">"))


def _sentence(title):
    return title if title.endswith((".", "!", "?")) else title + "."


def _compose(weather, sections, title_limit):
    parts = [weather] if weather else []
    n = 0
    for label, items in sections:
        if items is None:
            parts.append(f"(I couldn't reach the {label} feed.)")
        elif not items:
            parts.append(f"(The {label} feed had no headlines.)")
        else:
            heads = []
            for title, _ in items:
                n += 1
                heads.append(f"{n}. {_sentence(cut(title, title_limit))}")
            parts.append(f"{label}: " + " ".join(heads))
    return " ".join(parts)


def build_output(weather, sections):
    """The JSON object to print. `weather` is a sentence or None; `sections` is
    [(label, items or None if the feed failed)], items being [(title, url)] already
    checked with `newest`. Headlines are numbered across sections in the same order as
    the pages (at most MAX_PAGES), and titles are cut shorter in the text if needed to
    fit CARL's 1000 characters."""
    budget, kept, seen = MAX_PAGES, [], set()
    for label, items in sections:
        if items is not None:
            # An address another feed already gave is left out (CARL would drop it).
            items = [(t, u) for t, u in items if u not in seen][: max(budget, 0)]
            seen.update(u for _, u in items)
            budget -= len(items)
        kept.append((label, items))
    pages = [{"title": title, "url": url} for _, items in kept if items for title, url in items]
    for limit in (TITLE_LIMIT, 90, 75, 60, 45, 30):
        text = _compose(weather, kept, limit)
        if _escaped_len(text) <= TEXT_LIMIT:
            break
    while _escaped_len(text) > TEXT_LIMIT:  # only with a very long place name or labels
        text = text[:-1]
    return {"text": text, "pages": pages}


# --- fetching ---


def fetch(url):
    """The response body, at most MAX_BYTES; SourceFailed on any error or if it's
    bigger."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            data = response.read(MAX_BYTES + 1)
    except Exception as exc:  # any network failure is just "couldn't reach"
        raise SourceFailed(type(exc).__name__) from None
    if len(data) > MAX_BYTES:
        raise SourceFailed("over 2 MB")
    return data


def fetch_json(url):
    try:
        return json.loads(fetch(url))
    except ValueError:
        raise SourceFailed("not JSON") from None


def get_weather(config):
    """(sentence, ok): ok is False when the weather source failed, None when it wasn't
    tried (no place set)."""
    place = config["place"]
    name, lat, lon = place["name"], place["latitude"], place["longitude"]
    try:
        if lat is None or lon is None:
            if not name:
                return NO_PLACE, None
            found = place_from_geocoding(fetch_json(geocoding_url(name)))
            if found is None:
                return NO_PLACE_FOUND, False
            _, lat, lon = found
        return describe_weather(fetch_json(forecast_url(lat, lon, config["units"])), name), True
    except SourceFailed as exc:
        print(f"weather: {exc}", file=sys.stderr)
        return NO_WEATHER, False


def get_feed(section, url, count):
    try:
        return newest(parse_feed(fetch(url)), count)
    except SourceFailed as exc:
        print(f"{section} feed: {exc}", file=sys.stderr)
        return None


# --- config and main ---


def _number(value):
    return isinstance(value, int | float) and not isinstance(value, bool)


def load_config(path):
    """config.json, checked. Raises ValueError (or OSError) with a short reason."""
    return load_config_dict(json.loads(path.read_text(encoding="utf-8-sig")))


def load_config_dict(raw):
    """The config, checked and with blanks filled in. Raises ValueError."""
    if not isinstance(raw, dict):
        raise ValueError("config.json must be an object")
    place = raw.get("place") or {}
    if not isinstance(place, dict):
        raise ValueError('"place" must be an object')
    name = place.get("name") or ""
    lat, lon = place.get("latitude"), place.get("longitude")
    if not isinstance(name, str):
        raise ValueError('"place.name" must be a string')
    if lat in ("", None) or lon in ("", None):
        lat = lon = None
    elif not (_number(lat) and _number(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError('"latitude" and "longitude" must be numbers (or both blank)')
    units = raw.get("units", "fahrenheit")
    if units not in UNITS:
        raise ValueError('"units" must be "fahrenheit" or "celsius"')
    feeds = raw.get("feeds") or {}
    if not isinstance(feeds, dict) or not all(isinstance(v, str) for v in feeds.values()):
        raise ValueError('"feeds" must map sections to addresses')
    for section, url in feeds.items():
        if url and urllib.parse.urlsplit(url).scheme.lower() not in ("http", "https"):
            raise ValueError(f"the {section} feed must be an http or https address")
    per_feed = raw.get("per_feed", 3)
    if not isinstance(per_feed, int) or isinstance(per_feed, bool) or not 1 <= per_feed <= 3:
        raise ValueError('"per_feed" must be 1, 2 or 3')
    return {
        "place": {"name": name.strip(), "latitude": lat, "longitude": lon},
        "units": units,
        "feeds": {s: feeds.get(s, "").strip() for s in SECTIONS},
        "per_feed": per_feed,
    }


def run(section, config):
    """(output dict, exit code). Exit 1 only when every source that was tried failed."""
    want_weather = section in ("all", "weather")
    feeds = [s for s in SECTIONS if section in ("all", s)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        weather_job = pool.submit(get_weather, config) if want_weather else None
        jobs = {
            s: pool.submit(get_feed, s, config["feeds"][s], config["per_feed"])
            for s in feeds
            if config["feeds"][s]
        }
        weather, weather_ok = weather_job.result() if weather_job else (None, None)
        results = {s: job.result() for s, job in jobs.items()}
    sections, notes = [], []
    for s in feeds:
        if s in results:
            sections.append((LABELS[s], results[s]))
        else:
            notes.append(f"(No {LABELS[s]} feed is set in config.json.)")
    output = build_output(" ".join(filter(None, [weather, *notes])) or None, sections)
    tried = [weather_ok] if weather_ok is not None else []
    tried += [results[s] is not None for s in results]
    failed = bool(tried) and not any(tried)
    return output, (1 if failed else 0)


def main():
    try:
        inputs = json.load(sys.stdin)
    except ValueError:
        inputs = {}
    section = inputs.get("section", "all") if isinstance(inputs, dict) else "all"
    if section not in CHOICES:
        print(f"Unknown section: {section}", file=sys.stderr)
        return 2
    try:
        config = load_config(Path(__file__).with_name("config.json"))
    except (OSError, ValueError) as exc:
        print(f"config.json: {exc}", file=sys.stderr)
        return 2
    output, code = run(section, config)
    if code == 0:
        sys.stdout.write(json.dumps(output, ensure_ascii=True) + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
