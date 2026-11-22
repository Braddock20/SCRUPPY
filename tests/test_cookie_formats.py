import json
import time

from igbot.browser import (
    parse_cookie_json,
    parse_cookie_source,
    parse_netscape_cookies,
)


def test_netscape_cookie_jar():
    expiry = int(time.time()) + 3600
    sample = (
        "# Netscape HTTP Cookie File\n"
        ".instagram.com\tTRUE\t/\tTRUE\t%d\tcsrftoken\tcsrf-value\n" % expiry
        + ".instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\tsession-value\n"
    )
    cookies = parse_netscape_cookies(sample)
    assert [c["name"] for c in cookies] == ["csrftoken", "sessionid"]
    assert cookies[0]["domain"] == ".instagram.com"
    assert cookies[0]["secure"] is True
    assert cookies[1]["value"] == "session-value"
    assert "expires" not in cookies[1]


def test_netscape_httponly_and_expired():
    past = int(time.time()) - 10
    sample = (
        "#HttpOnly_.instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\tsecret\n"
        ".instagram.com\tTRUE\t/\tTRUE\t%d\told\tvalue\n" % past
    )
    cookies = parse_netscape_cookies(sample)
    assert len(cookies) == 1
    assert cookies[0]["httpOnly"] is True


def test_browser_json_cookie_array():
    raw = json.dumps({"cookies": [{
        "name": "sessionid", "value": "abc", "domain": ".instagram.com",
        "path": "/", "secure": True, "httpOnly": True
    }]})
    cookies = parse_cookie_json(raw)
    assert cookies[0]["name"] == "sessionid"
    assert cookies[0]["httpOnly"] is True


def test_auto_detect_header():
    cookies = parse_cookie_source("a=1; b=two==")
    assert [c["name"] for c in cookies] == ["a", "b"]
    assert cookies[1]["value"] == "two=="


def test_auto_detect_netscape():
    raw = "# Netscape HTTP Cookie File\n.instagram.com\tTRUE\t/\tTRUE\t0\ta\tb\n"
    cookies = parse_cookie_source(raw)
    assert cookies[0]["domain"] == ".instagram.com"
