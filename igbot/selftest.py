"""Self test — verifies the send path without involving anybody else.

The test opens a conversation with **your own account** (Instagram's
"New message" flow, choosing yourself as the recipient), sends one clearly
labelled test message, and confirms that a new outgoing bubble actually
appears in the thread.

Nothing is sent to another person, and nothing is published. This is the
safest possible end-to-end rehearsal of the code path that matters.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from . import log

log = log.get(__name__)


@dataclass
class SelfTestResult:
    ok: bool = False
    lines: list[str] = field(default_factory=list)

    def say(self, message: str) -> None:
        self.lines.append(message)
        log.info("selftest: %s", message)


def _dismiss(page) -> None:
    for label in ("Not Now", "Not now", "Later", "Close", "Skip"):
        try:
            button = page.get_by_role("button", name=label, exact=True)
            if button.count() and button.first.is_visible():
                button.first.click(timeout=2000)
                page.wait_for_timeout(1200)
                return
        except (PlaywrightError, PlaywrightTimeout):
            continue


def _open_self_thread(page, handle: str, result: SelfTestResult) -> bool:
    """Use the 'New message' flow to start a conversation with yourself."""
    try:
        page.locator('[aria-label="New message"]').first.click(timeout=10000)
        result.say("opened the New message sheet")
    except (PlaywrightError, PlaywrightTimeout) as exc:
        result.say(f"FAILED to open the New message sheet: {exc}")
        return False
    page.wait_for_timeout(2500)
    _dismiss(page)

    try:
        box = page.locator('input[placeholder="Search..."]').first
        box.fill(handle)
        result.say(f"searched for {handle}")
    except (PlaywrightError, PlaywrightTimeout) as exc:
        result.say(f"FAILED to type in the recipient search: {exc}")
        return False
    page.wait_for_timeout(3500)

    candidates = page.locator('div[role="button"], [role="option"], li[role="option"]')
    count = candidates.count()
    for i in range(count):
        try:
            text = candidates.nth(i).inner_text(timeout=1200)
        except (PlaywrightError, PlaywrightTimeout):
            continue
        if any(line.strip().lower() == handle.lower() for line in text.split("\n")):
            candidates.nth(i).click(timeout=8000)
            result.say("selected your own account")
            page.wait_for_timeout(2500)
            break
    else:
        result.say("FAILED to find your own account in the recipient list")
        return False

    try:
        nxt = page.get_by_role("button", name="Next", exact=False)
        if nxt.count() and nxt.first.is_visible():
            nxt.first.click(timeout=8000)
            result.say("clicked Next")
    except (PlaywrightError, PlaywrightTimeout):
        pass
    page.wait_for_timeout(5000)
    return True


def run_selftest(browser, page, dom, selectors: dict, cfg) -> SelfTestResult:
    """Run the whole self test and report what happened."""
    result = SelfTestResult()
    handle = browser.account_handle() or str(cfg.get("account.username", "") or "")
    if not handle:
        result.say("FAILED: could not determine your Instagram handle")
        return result

    result.say(f"testing as @{handle}")

    base = str(cfg.get("urls.base", "https://www.instagram.com")).rstrip("/")
    page.goto(base + "/direct/inbox/", wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(4000)
    _dismiss(page)

    if not _open_self_thread(page, handle, result):
        return result

    if "/direct/t/" not in page.url:
        result.say(f"FAILED: did not reach a conversation (url {page.url})")
        return result
    result.say(f"conversation open at {page.url}")

    if dom.composer() is None:
        result.say("FAILED: no message composer found in the conversation")
        return result
    result.say("composer found")

    before = dom.read_messages()
    before_count = len(before)
    result.say(f"{before_count} existing message(s) in the thread")

    marker = f"igbot selftest {random.randint(1000, 9999)}"
    if not dom.type_message(marker):
        result.say("FAILED: could not type into the composer")
        return result
    result.say("typed the test message")

    if not dom.click_send():
        result.say("FAILED: the Send button never appeared")
        return result
    result.say("clicked Send")

    confirmed, reason = dom.wait_for_send_confirmation(marker, before_count, timeout=30)
    if confirmed:
        result.say(f"SUCCESS: the message appeared in the thread ({reason})")
        result.ok = True
    else:
        result.say(f"FAILED: the send was not confirmed ({reason})")
    return result
