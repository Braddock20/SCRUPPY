"""Comment handling — read the comments on a post and optionally reply.

Like publishing, this drives Instagram's own UI, so the same rules apply:
selectors live in ``selectors.yaml``, and each step reports where it failed.

A comment reply is a public, visible action, so it is opt-in and rate
limited by the same guardrails as DMs.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from . import log

log = log.get(__name__)

_COMMENT_TEXT = r"""
() => {
  const ownText = (el) => Array.from(el.childNodes)
    .filter(n => n.nodeType === 3)
    .map(n => n.nodeValue.replace(/\s+/g, ' ').trim())
    .filter(Boolean).join(' ').trim();
  const out = [];
  const seen = new Set();
  const walk = (el, d) => {
    if (d > 30) return;
    for (const c of el.children) {
      if (c.tagName === 'SCRIPT' || c.tagName === 'STYLE') continue;
      const role = c.getAttribute ? c.getAttribute('role') : '';
      const txt = ownText(c);
      if (txt && role === 'listitem') {
        const r = c.getBoundingClientRect();
        if (r.width > 0 && r.height > 0) {
          const key = txt + '|' + Math.round(r.y);
          if (!seen.has(key)) { seen.add(key); out.push({text: txt, y: Math.round(r.top)}); }
        }
      }
      walk(c, d + 1);
    }
  };
  walk(document.body, 0);
  return out;
}
"""


@dataclass
class Comment:
    text: str
    username: str = ""
    y: float = 0.0

    @property
    def key(self) -> str:
        return f"{self.username}|{' '.join(self.text.split()).lower()}"


class CommentBot:
    """Reads and replies to comments on a post."""

    def __init__(self, page, cfg, selectors: dict, guardrails=None, rulebook=None):
        self.page = page
        self.cfg = cfg
        self.s = selectors
        self.guardrails = guardrails
        self.rulebook = rulebook

    def _sel(self, path: str, default: str = "") -> str:
        node = self.s
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node if isinstance(node, str) else default

    def open_post(self, url: str) -> bool:
        """Load a post and open the comment panel."""
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
            self.page.wait_for_timeout(3500)
        except (PlaywrightError, PlaywrightTimeout) as exc:
            log.warning("could not open the post: %s", exc)
            return False

        for label in ("Comment", "Comments"):
            try:
                button = self.page.locator(f'[aria-label="{label}" i]')
                if button.count():
                    button.first.click(timeout=8000)
                    self.page.wait_for_timeout(2500)
                    return True
            except (PlaywrightError, PlaywrightTimeout):
                continue
        # Some layouts show the comments panel by default.
        return True

    def read_comments(self) -> list[Comment]:
        """Comments currently rendered in the panel."""
        try:
            raw = self.page.evaluate(_COMMENT_TEXT)
        except PlaywrightError:
            return []
        comments: list[Comment] = []
        for item in raw or []:
            text = (item.get("text") or "").strip()
            if not text:
                continue
            # Instagram renders "<username> <comment>" in one list item.
            username = ""
            parts = text.split(" ", 1)
            if len(parts) == 2 and re.fullmatch(r"[A-Za-z0-9._]+", parts[0]):
                username, text = parts[0], parts[1].strip()
            comments.append(Comment(text=text, username=username, y=float(item.get("y") or 0)))
        comments.sort(key=lambda c: c.y)
        return comments

    def reply(self, comment: Comment, reply_text: str) -> tuple[bool, str]:
        """Post a reply under a comment."""
        try:
            field = self.page.locator(
                'textarea[aria-label*="Comment" i], div[contenteditable="true"][aria-label*="Comment" i]'
            )
            if not field.count():
                field = self.page.locator('textarea[placeholder*="Add a comment" i]')
            if not field.count():
                return False, "comment input not found"
            field.first.click(timeout=8000)
            field.first.fill(reply_text, delay=random.randint(25, 80))
            self.page.wait_for_timeout(800)

            post = self.page.locator('button:has-text("Post"), div[role="button"]:has-text("Post")')
            if not post.count():
                return False, "comment Post button not found"
            post.first.click(timeout=8000)
            self.page.wait_for_timeout(3000)
            return True, "posted"
        except (PlaywrightError, PlaywrightTimeout) as exc:
            return False, str(exc)
