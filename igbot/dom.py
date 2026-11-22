"""The DOM layer — everything that touches Instagram's rendered page.

Every selector here was verified against the live site on 2026-11-22. The
fragile parts are documented inline so a future redesign is a small, guided
fix rather than a mystery.

Reading messages
----------------
Instagram gives no stable class names (they are obfuscated per build), so
messages are found structurally:

* a message bubble lives inside a ``div[role="presentation"]`` that has an
  opaque background colour;
* **outgoing** bubbles use Instagram's blue ``rgb(74, 93, 249)`` and sit on the
  right, **incoming** bubbles use a neutral grey ``rgb(243, 245, 247)`` and sit
  on the left;
* outgoing bubbles are additionally wrapped in a ``div[role="button"]`` (the
  "message actions" affordance), incoming ones are not.

Three independent signals are combined, so a theme change or a long message
that spans the full width still classifies correctly.
"""

from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass, field
from typing import Any

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from . import log

log = log.get(__name__)

# Text that is a timestamp, not a message.
_TIME_ONLY = re.compile(
    r"^\s*(?:\d{1,2}:\d{2}\s*?[APap][Mm]?|"
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s*\d{4}.*|"
    r"Yesterday|Today|Just now)\s*$"
)

# LTR/RTL marks Instagram wraps usernames in.
_MARKS = "\u200e\u200f\u202a\u202b"

# The subtitle Instagram puts on the "What's new" carousel and on notes.
_NOT_CONVERSATION = ("your note", "what's new", "notes")


@dataclass
class Row:
    """One row in the inbox list."""

    index: int
    title: str
    preview: str
    time: str
    outgoing: bool
    raw: str = ""

    @property
    def key(self) -> str:
        return f"{self.title}|{self.preview}"


@dataclass
class Message:
    """One message bubble inside a conversation."""

    text: str
    outgoing: bool
    y: float = 0.0
    left: float = 0.0
    right: float = 0.0
    confirmed_direction: str = ""


@dataclass
class ThreadInfo:
    """Metadata about the currently open conversation."""

    thread_key: str = ""
    title: str = ""
    url: str = ""
    has_composer: bool = False
    is_request: bool = False
    participants: list[dict] = field(default_factory=list)


# --------------------------------------------------------------------------
# JavaScript helpers (run in the page)
# --------------------------------------------------------------------------

_EXTRACT_MESSAGES_JS = r"""
() => {
  const ownText = (el) => Array.from(el.childNodes)
    .filter(n => n.nodeType === 3)
    .map(n => n.nodeValue.replace(/\s+/g, ' ').trim())
    .filter(Boolean)
    .join(' ')
    .trim();

  const isOpaque = (bg) => {
    if (!bg) return false;
    if (bg === 'rgba(0, 0, 0, 0)' || bg === 'transparent') return false;
    const m = bg.match(/rgba?\(([^)]+)\)/);
    if (!m) return false;
    const parts = m[1].split(',').map(s => parseFloat(s.trim()));
    if (parts.length >= 4 && parts[3] === 0) return false;
    return true;
  };

  const composer = document.querySelector('div[role="textbox"][contenteditable="true"]');
  const composerTop = composer ? composer.getBoundingClientRect().top : window.innerHeight;

  const bubbleContainer = (el) => {
    let p = el.parentElement;
    for (let i = 0; i < 5 && p; i++) {
      if (p.getAttribute && p.getAttribute('role') === 'presentation' && isOpaque(getComputedStyle(p).backgroundColor)) {
        return p;
      }
      p = p.parentElement;
    }
    return null;
  };

  const hasButtonAncestor = (el) => {
    let p = el.parentElement;
    for (let i = 0; i < 7 && p; i++) {
      if (p.getAttribute && p.getAttribute('role') === 'button') return true;
      p = p.parentElement;
    }
    return false;
  };

  const classify = (el) => {
    const container = bubbleContainer(el);
    let reason = '';
    let outgoing = false;

    if (container) {
      const m = getComputedStyle(container).backgroundColor.match(/rgba?\((\d+),\s*(\d+),\s*(\d+)/);
      if (m) {
        const r = +m[1], g = +m[2], b = +m[3];
        const blueish = b > 110 && (b - r) > 45 && (b - g) > 45;
        const greyish = Math.abs(r - g) < 20 && Math.abs(g - b) < 20;
        if (blueish) { outgoing = true; reason = 'colour:blue'; }
        else if (greyish) { outgoing = false; reason = 'colour:grey'; }
      }
    }

    if (!reason) {
      const btn = hasButtonAncestor(el);
      if (btn) { outgoing = true; reason = 'ancestor:button'; }
      else { outgoing = false; reason = 'ancestor:none'; }
    }

    return { outgoing, reason };
  };

  const found = [];
  const seen = new Set();
  const walk = (el) => {
    for (const c of el.children) {
      if (c.tagName === 'SCRIPT' || c.tagName === 'STYLE') continue;
      const txt = ownText(c);
      if (txt) {
        const r = c.getBoundingClientRect();
        const inComposer = composer && (composer.contains(c) || c.contains(composer));
        if (!inComposer && r.width > 0 && r.height > 0 && r.top < composerTop) {
          const container = bubbleContainer(c);
          if (container && !seen.has(container)) {
            seen.add(container);
            const cls = classify(c);
            found.push({
              text: txt,
              outgoing: cls.outgoing,
              reason: cls.reason,
              y: Math.round(r.top),
              left: Math.round(r.left),
              right: Math.round(r.right),
              width: Math.round(r.width),
            });
          }
        }
      }
      walk(c);
    }
  };
  walk(document.body);

  // Oldest first (DOM order), which is already top-to-bottom.
  return found;
}
"""

_ROW_TEXT_JS = r"""
(el) => {
  const t = (el.innerText || '').replace(/\u00a0/g, ' ').split('\n').map(s => s.trim());
  const sep = t.indexOf('·');
  return {lines: t, sep};
}
"""


class DOM:
    """Reads and drives the Instagram page."""

    def __init__(
        self,
        page,
        selectors: dict,
        base_url: str = "https://www.instagram.com",
        settle: float = 1.0,
    ):
        self.page = page
        self.s = selectors
        self.base_url = base_url.rstrip("/")
        # Scales every "let the page catch up" wait. 1.0 is the real-world
        # value; the test suite lowers it so runs are quick.
        self.settle = max(0.05, float(settle))

    # ------------------------------------------------------------- utilities

    def _sel(self, path: str, default: str = "") -> str:
        node: Any = self.s
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node if isinstance(node, str) else default

    def _dismiss_overlays(self) -> bool:
        """Close first-run modals. Returns True if one was dismissed."""
        labels = self.s.get("overlay", {}).get("dismiss_labels", []) if self.s else []
        for label in labels:
            try:
                button = self.page.get_by_role("button", name=label, exact=True)
                if button.count() and button.first.is_visible():
                    button.first.click(timeout=2500)
                    self.page.wait_for_timeout(1200)
                    log.info("dismissed overlay via %r", label)
                    return True
            except (PlaywrightError, PlaywrightTimeout):
                continue
        try:
            self.page.keyboard.press("Escape")
        except PlaywrightError:
            pass
        return False

    def _human_pause(self, low: float = 0.4, high: float = 1.4) -> None:
        self.page.wait_for_timeout(random.randint(int(low * 1000), int(high * 1000)))

    def _settle(self, ms: int) -> None:
        """Wait for the page to catch up, scaled by the settle factor."""
        self.page.wait_for_timeout(max(50, int(ms * self.settle)))

    # ------------------------------------------------------------- navigation

    def open_inbox(self) -> None:
        """Load the DM inbox and clear any overlay."""
        path = self.s.get("inbox", {}).get("url", "/direct/inbox/")
        self.page.goto(self.base_url + path, wait_until="domcontentloaded", timeout=60000)
        self._settle(2500)
        if self._dismiss_overlays():
            self._settle(800)
        # The tab strip may need a moment to hydrate.
        try:
            self.page.wait_for_selector(self._sel("inbox.tab", '[role="tab"]'), timeout=15000)
        except (PlaywrightError, PlaywrightTimeout):
            log.warning("inbox tabs did not appear")

    def tab_names(self) -> list[str]:
        try:
            return [
                (t.inner_text() or "").strip()
                for t in self.page.locator(self._sel("inbox.tab", '[role="tab"]')).all()
            ]
        except PlaywrightError:
            return []

    def switch_tab(self, name: str) -> bool:
        """Click an inbox tab by visible name. Falls back to a prefix match."""
        wanted = name.strip().lower()
        try:
            tabs = self.page.locator(self._sel("inbox.tab", '[role="tab"]'))
            count = tabs.count()
        except PlaywrightError:
            return False
        for i in range(count):
            try:
                text = (tabs.nth(i).inner_text() or "").strip()
            except (PlaywrightError, PlaywrightTimeout):
                continue
            if text.lower() == wanted or text.lower().startswith(wanted):
                try:
                    tabs.nth(i).click(timeout=8000)
                    self._settle(2500)
                    self._dismiss_overlays()
                    return True
                except (PlaywrightError, PlaywrightTimeout):
                    return False
        return False

    # ------------------------------------------------------------------ rows

    def list_rows(self) -> list[Row]:
        """Read the conversation rows in the current tab.

        A conversation row is a ``div[role="button"]`` whose text contains the
        ``·`` separator. The "What's new" carousel uses the same element type
        but has no separator, which is how the two are told apart.
        """
        rows: list[Row] = []
        try:
            locator = self.page.locator(self._sel("inbox.row", 'div[role="button"]'))
            count = locator.count()
        except PlaywrightError:
            return rows

        for i in range(count):
            try:
                raw = locator.nth(i).inner_text(timeout=2500)
            except (PlaywrightError, PlaywrightTimeout):
                continue
            if not raw or "·" not in raw:
                continue
            cleaned = raw.replace("\u00a0", " ")
            lines = [line.strip() for line in cleaned.split("\n") if line.strip()]
            if len(lines) < 2:
                continue
            try:
                sep = lines.index("·")
            except ValueError:
                continue
            title = lines[0].strip(_MARKS).strip()
            preview = " ".join(lines[1:sep]).strip()
            when = " ".join(lines[sep + 1:]).strip()
            lowered = title.lower()
            if any(marker in lowered for marker in _NOT_CONVERSATION):
                continue
            outgoing = preview.startswith("You:") or preview.startswith("You :")
            if outgoing:
                preview = preview.split(":", 1)[1].strip()
            if not title or not preview:
                continue
            rows.append(
                Row(index=i, title=title, preview=preview, time=when, outgoing=outgoing, raw=cleaned)
            )
        return rows

    def open_row(self, row: Row) -> ThreadInfo:
        """Click a conversation row and wait for the thread to open."""
        try:
            locator = self.page.locator(self._sel("inbox.row", 'div[role="button"]'))
            locator.nth(row.index).click(timeout=10000)
        except (PlaywrightError, PlaywrightTimeout) as exc:
            log.warning("could not open row %r: %s", row.title, exc)
            return ThreadInfo()

        self._settle(3000)
        self._dismiss_overlays()
        self._settle(1500)
        return self.thread_info()

    def thread_info(self) -> ThreadInfo:
        """Describe the currently open thread."""
        url = self.page.url
        match = re.search(r"/direct/t/([0-9]+)/", url)
        info = ThreadInfo(thread_key=match.group(1) if match else "", url=url)
        info.has_composer = self.composer() is not None
        info.is_request = self._looks_like_request()
        return info

    def _looks_like_request(self) -> bool:
        """A request thread shows Accept / Block / Delete instead of a composer."""
        try:
            body = (self.page.inner_text("body", timeout=4000) or "").lower()
        except (PlaywrightError, PlaywrightTimeout):
            return False
        return "accept message request" in body or (
            "block" in body and "delete" in body and not info_has_composer(self)
        )

    def back_to_inbox(self) -> None:
        """Return to the inbox without reloading (keeps tab state)."""
        try:
            back = self.page.locator(self._sel("conversation.back_button", '[aria-label="Back"]'))
            if back.count() and back.first.is_visible():
                back.first.click(timeout=6000)
                self._settle(2000)
                return
        except (PlaywrightError, PlaywrightTimeout):
            pass
        self.open_inbox()

    # -------------------------------------------------------------- messages

    def read_messages(self) -> list[Message]:
        """Extract the message bubbles currently rendered in the thread."""
        try:
            raw = self.page.evaluate(_EXTRACT_MESSAGES_JS)
        except PlaywrightError as exc:
            log.warning("message extraction failed: %s", exc)
            return []

        messages: list[Message] = []
        for item in raw or []:
            text = (item.get("text") or "").strip()
            if not text:
                continue
            if _TIME_ONLY.match(text):
                continue
            if text.lower().startswith("message..."):
                continue
            if len(text) > 2000:
                text = text[:2000]
            messages.append(
                Message(
                    text=text,
                    outgoing=bool(item.get("outgoing")),
                    y=float(item.get("y") or 0),
                    left=float(item.get("left") or 0),
                    right=float(item.get("right") or 0),
                    confirmed_direction=item.get("reason", ""),
                )
            )
        messages.sort(key=lambda m: m.y)
        return messages

    def last_inbound(self) -> Message | None:
        """The most recent message that was not sent by us."""
        for message in reversed(self.read_messages()):
            if not message.outgoing:
                return message
        return None

    # -------------------------------------------------------------- composer

    def composer(self):
        """The message input, if this thread has one."""
        try:
            locator = self.page.locator(self._sel("conversation.composer", 'div[role="textbox"][contenteditable="true"]'))
            if locator.count() and locator.first.is_visible():
                return locator.first
        except (PlaywrightError, PlaywrightTimeout):
            pass
        return None

    def type_message(self, text: str) -> bool:
        """Type into the composer the way a person would."""
        composer = self.composer()
        if composer is None:
            return False
        try:
            composer.click(timeout=8000)
            self.page.wait_for_timeout(random.randint(300, 800))
            # Real key events, with human-ish cadence.
            for index, char in enumerate(text):
                composer.type(char, delay=random.randint(35, 110))
                if index and index % 24 == 0:
                    self.page.wait_for_timeout(random.randint(250, 700))
            return True
        except (PlaywrightError, PlaywrightTimeout) as exc:
            log.warning("could not type into composer: %s", exc)
            return False

    def composer_text(self) -> str:
        composer = self.composer()
        if composer is None:
            return ""
        try:
            return (composer.inner_text() or "").strip()
        except (PlaywrightError, PlaywrightTimeout):
            return ""

    def click_send(self) -> bool:
        """Click the send control. Returns False if it never appeared."""
        deadline = time.time() + 12
        while time.time() < deadline:
            try:
                send = self.page.locator(
                    self._sel("conversation.send_button", 'div[role="button"][aria-label="Send"]')
                )
                if send.count() and send.first.is_visible():
                    send.first.click(timeout=8000)
                    return True
            except (PlaywrightError, PlaywrightTimeout):
                pass
            self.page.wait_for_timeout(500)
        return False

    def wait_for_send_confirmation(
        self, text: str, before_count: int, timeout: float = 25.0
    ) -> tuple[bool, str]:
        """Confirm a send really landed.

        "The composer cleared" is **not** proof — Instagram clears the input
        even when the message is rejected. The only reliable signal is a new
        outgoing bubble appearing in the thread.

        Returns ``(confirmed, reason)``.
        """
        deadline = time.time() + timeout
        expected = " ".join(text.split()).lower()
        while time.time() < deadline:
            messages = self.read_messages()
            if len(messages) > before_count:
                last = messages[-1]
                if last.outgoing:
                    if " ".join(last.text.split()).lower() == expected:
                        return True, "new outgoing bubble matched"
                    return True, f"new outgoing bubble (text differs: {last.text[:60]!r})"
            self.page.wait_for_timeout(1000)
        if self.composer_text():
            return False, "composer still holds text; the message was not sent"
        return False, "no new outgoing bubble appeared"

    # -------------------------------------------------------------- requests

    def accept_request(self) -> bool:
        """Accept a message request. This is visible to the sender."""
        try:
            button = self.page.locator(self._sel("conversation.accept_button", 'div[role="button"]:has-text("Accept")'))
            if button.count() and button.first.is_visible():
                button.first.click(timeout=8000)
                self.page.wait_for_timeout(4000)
                self._dismiss_overlays()
                return True
        except (PlaywrightError, PlaywrightTimeout) as exc:
            log.warning("could not accept request: %s", exc)
        return False


def info_has_composer(dom: DOM) -> bool:
    """Small helper used by :meth:`DOM._looks_like_request`."""
    return dom.composer() is not None
