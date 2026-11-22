"""End-to-end tests against the mock Instagram.

These exercise the real browser against a faithful copy of Instagram's DOM:
the bot opens the inbox, reads rows, opens a thread, types, sends, and the
mock records what was actually delivered.

The mock server is what makes the whole pipeline testable without pointing a
new bot at a live business account.
"""

import json
import sys
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from igbot.ai import AIReplyEngine  # noqa: E402
from igbot.config import Config  # noqa: E402
from igbot.dom import DOM  # noqa: E402
from igbot.dm import DMBot  # noqa: E402
from igbot.guardrails import Guardrails  # noqa: E402
from igbot.rules import Rulebook  # noqa: E402
from igbot.store import Store  # noqa: E402

import yaml  # noqa: E402

mock_server = None
mock_base = ""


def _control(path, payload=None):
    url = f"{mock_base}/_control/{path}"
    data = json.dumps(payload or {}).encode()
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode())


def _state():
    with urllib.request.urlopen(f"{mock_base}/_control/state", timeout=10) as response:
        return json.loads(response.read().decode())


@pytest.fixture(scope="module")
def mock():
    global mock_server, mock_base
    sys.path.insert(0, str(ROOT / "mock"))
    import mock_instagram

    mock_server = mock_instagram.start(0)
    mock_base = f"http://127.0.0.1:{mock_server.server_address[1]}"
    yield mock_base
    mock_server.shutdown()


@pytest.fixture(scope="module")
def browser():
    from playwright.sync_api import sync_playwright

    playwright = sync_playwright().start()
    instance = playwright.chromium.launch(
        headless=True, args=["--disable-blink-features=AutomationControlled"]
    )
    yield instance
    instance.close()
    playwright.stop()


@pytest.fixture()
def selectors():
    return yaml.safe_load((ROOT / "igbot" / "selectors.yaml").read_text(encoding="utf-8"))


@pytest.fixture()
def cfg(tmp_path):
    return Config(
        {
            "urls": {"base": mock_base},
            "behaviour": {
                "poll_seconds": 1,
                "reply_delay_min": 0,
                "reply_delay_max": 0,
                "cooldown_seconds": 0,
                "hourly_cap": 20,
                "daily_cap": 50,
                "global_daily_cap": 200,
                "baseline_only": False,
                "accept_requests": False,
                "tabs": ["Primary", "Requests"],
            },
            "reply": {
                "default_reply": "Thanks for reaching out!",
                "rules": [{"name": "pricing", "match": ["price", "how much"], "reply": "Here are our prices!"}],
                "ai": {"enabled": False},
            },
        },
        tmp_path,
    )


@pytest.fixture()
def world(browser, mock, selectors, cfg, tmp_path):
    """A fresh mock state, a fresh page and a wired-up bot."""
    _control("reset")

    context = browser.new_context(viewport={"width": 414, "height": 863})
    page = context.new_page()
    store = Store(tmp_path / "state.db")
    guard = Guardrails(store, cfg)
    book = Rulebook.from_config(cfg)
    ai = AIReplyEngine(cfg)
    dom = DOM(page, selectors, mock_base, settle=0.12)
    dom.open_inbox()
    bot = DMBot(dom, store, guard, book, ai, cfg)

    yield {
        "page": page,
        "context": context,
        "store": store,
        "guard": guard,
        "bot": bot,
        "dom": dom,
        "cfg": cfg,
    }

    store.close()
    context.close()


# --------------------------------------------------------------------- tests


def test_reads_inbox_rows(world):
    rows = world["dom"].list_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row.title == "Auren | Mint"
    assert row.preview == "Is the blue one still available?"
    assert row.outgoing is False


def test_carousel_rows_are_not_mistaken_for_conversations(world):
    """The 'What's new' carousel uses role=button but has no separator."""
    world["dom"].open_inbox()
    titles = [r.title for r in world["dom"].list_rows()]
    assert "Friday feeling..." not in titles
    assert "Final" not in titles


def test_outgoing_preview_is_recognised(world):
    _control("inject", {"thread": "1001", "text": "Just checking in", "outgoing": True})
    world["dom"].open_inbox()  # the mock's inbox is server-rendered, so reload it
    rows = world["dom"].list_rows()
    assert rows[0].outgoing is True
    assert rows[0].preview == "Just checking in"


def test_message_direction_is_detected(world):
    world["dom"].open_inbox()
    world["dom"].open_row(world["dom"].list_rows()[0])
    messages = world["dom"].read_messages()
    assert len(messages) == 2
    assert messages[0].text == "Hello"
    assert messages[0].outgoing is True
    assert messages[1].text == "Is the blue one still available?"
    assert messages[1].outgoing is False


def test_baseline_mode_reads_but_never_replies(world):
    world["cfg"].set("behaviour.baseline_only", True)
    result = world["bot"].poll_once()
    # One inbound message in Primary and one message request.
    assert result.inbound == 2
    assert result.replied == 0
    assert len(_state()["threads"]["1001"]["messages"]) == 2, (
        "the bot must not send anything in baseline mode"
    )
    assert len(_state()["threads"]["2002"]["messages"]) == 1


def test_replies_to_an_inbound_message(world):
    result = world["bot"].poll_once()
    assert result.inbound == 2  # one in Primary, one message request
    assert result.replied == 1, result.summary()

    messages = _state()["threads"]["1001"]["messages"]
    assert len(messages) == 3
    assert messages[-1]["outgoing"] is True
    assert messages[-1]["text"] == "Thanks for reaching out!"


def test_a_matching_rule_is_used(world):
    _control("inject", {"thread": "1001", "text": "How much is it?"})
    world["bot"].poll_once()
    messages = _state()["threads"]["1001"]["messages"]
    assert messages[-1]["text"] == "Here are our prices!"


def test_the_same_message_is_not_answered_twice(world):
    world["bot"].poll_once()
    assert len(_state()["threads"]["1001"]["messages"]) == 3
    second = world["bot"].poll_once()
    assert second.replied == 0
    assert len(_state()["threads"]["1001"]["messages"]) == 3


def test_a_new_message_is_answered(world):
    world["bot"].poll_once()
    _control("inject", {"thread": "1001", "text": "Actually, do you ship?"})
    result = world["bot"].poll_once()
    assert result.replied == 1
    # 2 original + 1 injected + 1 reply from each poll
    assert len(_state()["threads"]["1001"]["messages"]) == 5


def test_cooldown_blocks_a_second_reply(world):
    world["cfg"].set("behaviour.cooldown_seconds", 3600)
    world["bot"].poll_once()
    _control("inject", {"thread": "1001", "text": "one more question"})
    result = world["bot"].poll_once()
    assert result.blocked == 1
    assert result.replied == 0
    # The blocked message is NOT marked handled, so it is retried later.
    # 2 original + 1 reply + 1 injected message, and no second reply.
    assert len(_state()["threads"]["1001"]["messages"]) == 4


def test_message_request_is_read_but_not_accepted_by_default(world):
    result = world["bot"].poll_once()
    requests = [n for n in result.notes if "request" in n.lower()]
    assert requests, result.notes
    # The request thread must not have received a reply.
    assert len(_state()["threads"]["2002"]["messages"]) == 1
    assert _state()["threads"]["2002"]["request"] is True


def test_message_request_can_be_accepted_and_answered(world):
    world["cfg"].set("behaviour.accept_requests", True)
    world["bot"].poll_once()
    thread = _state()["threads"]["2002"]
    assert thread["request"] is False, "the request should have been accepted"
    assert len(thread["messages"]) == 2
    assert thread["messages"][-1]["outgoing"] is True


def test_unconfirmed_send_is_not_counted_as_a_reply(world, monkeypatch):
    """A send that never produces a bubble must not be reported as sent."""
    world["dom"].click_send = lambda: True
    world["dom"].wait_for_send_confirmation = lambda text, count, timeout=5: (False, "stubbed")

    result = world["bot"].poll_once()
    assert result.replied == 0
    assert result.failed == 1
    assert any("unconfirmed" in n for n in result.notes)


def test_send_confirmation_detects_the_new_bubble(world):
    world["dom"].open_inbox()
    world["dom"].open_row(world["dom"].list_rows()[0])
    before = world["dom"].read_messages()
    world["dom"].type_message("probe text")
    assert world["dom"].click_send() is True
    confirmed, reason = world["dom"].wait_for_send_confirmation("probe text", len(before))
    assert confirmed, reason


def test_composer_clearing_alone_is_not_treated_as_success(world):
    """The old false positive: the box empties but nothing was sent."""
    world["dom"].open_inbox()
    world["dom"].open_row(world["dom"].list_rows()[0])
    before = world["dom"].read_messages()
    world["dom"].type_message("never sent")
    # Clear the composer without sending, mimicking Instagram's behaviour
    # when a message is rejected.
    world["page"].evaluate(
        "() => { const c = document.querySelector('[role=textbox][contenteditable=true]');"
        "if (c) c.innerText = ''; }"
    )
    confirmed, reason = world["dom"].wait_for_send_confirmation("never sent", len(before), timeout=4)
    assert confirmed is False
    assert "composer" in reason or "bubble" in reason


def test_timestamps_are_not_mistaken_for_messages(world):
    _control("inject", {"thread": "1001", "text": "hello there", "outgoing": False})
    world["dom"].open_inbox()
    world["dom"].open_row(world["dom"].list_rows()[0])
    for message in world["dom"].read_messages():
        assert ":" not in message.text or not message.text.endswith("AM")
        assert message.text not in ("12:02 PM", "Message...")
