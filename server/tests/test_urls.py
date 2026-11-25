import pytest

from desk_buddy.tools.urls import MAX_URL, check_url


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "http://example.com/a/b?c=d#e",
        "HTTPS://Example.com:8443/path",
        "https://bbc.co.uk/weather/2644688",
        "https://例え.jp/ページ",
        "http://127.0.0.1:8080/",
    ],
)
def test_good_addresses_pass_unchanged(url):
    assert check_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        None,
        42,
        "",
        "example.com",
        "ftp://example.com",
        "file:///C:/Windows/System32/calc.exe",
        "javascript:alert(1)",
        "spotify:track:1",
        "https://",
        "https:example.com",
        "https:///path",
        "https://user@example.com",
        "https://user:pw@example.com",
        "https://:pw@example.com",
        "https://example.com@evil.example",
        "https://exa mple.com",
        "https://example.com/ x",
        " https://example.com",
        "https://example.com\n",
        "https://example.com/\t",
        "https://example.com/\x00",
        "https://example.com/\x7f",
        "https://example.com/\u202e",  # right-to-left override
        "https://example.com/\u200b",  # zero-width space
        "https://example.com/\u00a0",  # no-break space
        "https://good.example\\@evil.example",
        "http:\\\\evil.example",
        "https://example.com:99999/",
        "https://example.com:0/",
        "https://example.com:port/",
        "https://[::1/",
        "https://example.com/" + "a" * MAX_URL,
    ],
)
def test_bad_addresses_are_refused_with_a_sentence(url):
    with pytest.raises(ValueError) as info:
        check_url(url)
    assert str(info.value).endswith(".")


def test_the_length_limit_is_inclusive():
    url = "https://example.com/"
    url += "a" * (MAX_URL - len(url))
    assert len(url) == MAX_URL
    assert check_url(url) == url
    with pytest.raises(ValueError):
        check_url(url + "a")
