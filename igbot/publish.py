"""Publishing — posts a photo, reel or carousel through the web composer.

This is the most fragile part of the bot, because Instagram's upload modal is
a multi-step React flow whose class names change with every redesign.

Two design choices keep it survivable:

* every selector lives in ``selectors.yaml``;
* the flow is written as a sequence of named steps, each of which reports
  where it failed, so a redesign is a five-minute fix rather than an
  afternoon of guesswork.

``--dry-run`` stops before the final Share click, which lets the whole upload
path be rehearsed without publishing anything.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from . import log

log = log.get(__name__)


@dataclass
class PublishResult:
    ok: bool
    message: str
    steps: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.steps is None:
            self.steps = []


class Publisher:
    """Drives Instagram's post composer."""

    def __init__(self, page, cfg, selectors: dict):
        self.page = page
        self.cfg = cfg
        self.s = selectors

    def _sel(self, path: str, default: str = "") -> str:
        node = self.s
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node if isinstance(node, str) else default

    def _base(self) -> str:
        return str(self.cfg.get("urls.base", "https://www.instagram.com")).rstrip("/")

    # ------------------------------------------------------------------ steps

    def _open_composer(self, result: PublishResult) -> bool:
        """Click Create -> Post."""
        self.page.goto(self._base() + "/", wait_until="domcontentloaded", timeout=60000)
        self.page.wait_for_timeout(3000)

        try:
            create = self.page.locator(self._sel("publish.create_button", '[aria-label="Create"]'))
            if not create.count():
                create = self.page.locator('[aria-label="New post"], [role="button"]:has-text("Create")')
            if not create.count():
                result.message = "could not find the Create button"
                return False
            create.first.click(timeout=10000)
            result.steps.append("opened Create menu")
            self.page.wait_for_timeout(2500)
        except (PlaywrightError, PlaywrightTimeout) as exc:
            result.message = f"could not open the Create menu: {exc}"
            return False

        for label in ("Post", "Post photo", "Reel"):
            try:
                option = self.page.locator(f'[role="menu"] :text-is("{label}")')
                if not option.count():
                    option = self.page.locator(f'div[role="button"]:has-text("{label}")')
                if option.count() and option.first.is_visible():
                    option.first.click(timeout=8000)
                    result.steps.append(f"selected {label}")
                    self.page.wait_for_timeout(3500)
                    return True
            except (PlaywrightError, PlaywrightTimeout):
                continue

        result.message = "could not find the 'Post' option in the Create menu"
        return False

    def _attach_media(self, media: Path, result: PublishResult) -> bool:
        """Hand the file to Instagram's hidden file input."""
        try:
            file_input = self.page.locator('input[type="file"]')
            count = file_input.count()
        except PlaywrightError:
            count = 0
        if count == 0:
            # Sometimes the input only appears after the "Upload" tab.
            try:
                upload = self.page.locator('text=/Upload from computer/i')
                if upload.count():
                    upload.first.click(timeout=5000)
                    self.page.wait_for_timeout(1500)
                    file_input = self.page.locator('input[type="file"]')
                    count = file_input.count()
            except (PlaywrightError, PlaywrightTimeout):
                pass
        if count == 0:
            result.message = "no file input found in the upload dialog"
            return False
        try:
            file_input.first.set_input_files(str(media))
            result.steps.append(f"attached {media.name}")
            self.page.wait_for_timeout(4000)
            return True
        except (PlaywrightError, PlaywrightTimeout) as exc:
            result.message = f"could not attach the media file: {exc}"
            return False

    def _fill_caption(self, caption: str, result: PublishResult) -> bool:
        if not caption:
            return True
        try:
            field = self.page.locator(
                self._sel("publish.caption_input", 'div[aria-label="Caption"][contenteditable="true"]')
            )
            if not field.count():
                field = self.page.locator('textarea[aria-label="Caption"], div[contenteditable="true"][aria-label*="aption"]')
            if not field.count():
                result.message = "caption field not found (the upload may not have finished)"
                return False
            field.first.click(timeout=8000)
            field.first.type(caption, delay=random.randint(20, 70))
            result.steps.append(f"wrote caption ({len(caption)} chars)")
            self.page.wait_for_timeout(1200)
            return True
        except (PlaywrightError, PlaywrightTimeout) as exc:
            result.message = f"could not write the caption: {exc}"
            return False

    def _share(self, result: PublishResult) -> bool:
        try:
            share = self.page.locator(
                self._sel("publish.share_button", 'button:has-text("Share")')
            )
            if not share.count():
                share = self.page.locator('div[role="button"]:has-text("Share")')
            if not share.count():
                result.message = "Share button not found"
                return False
            share.first.click(timeout=12000)
            result.steps.append("clicked Share")
            self.page.wait_for_timeout(6000)
            return True
        except (PlaywrightError, PlaywrightTimeout) as exc:
            result.message = f"could not click Share: {exc}"
            return False

    # ------------------------------------------------------------------- api

    def publish(
        self,
        media: str | Path,
        caption: str = "",
        dry_run: bool = False,
        timeout: float = 120.0,
    ) -> PublishResult:
        """Publish ``media``. With ``dry_run`` stops before clicking Share."""
        result = PublishResult(ok=False, message="")
        media_path = Path(media).expanduser()
        if not media_path.exists():
            result.message = f"media file not found: {media_path}"
            return result

        if not self._open_composer(result):
            return result
        if not self._attach_media(media_path, result):
            return result
        if not self._fill_caption(caption, result):
            return result

        if dry_run:
            result.ok = True
            result.message = (
                "dry run: media attached and caption written; Share was NOT clicked"
            )
            return result

        if not self._share(result):
            return result

        result.ok = True
        result.message = "published"
        return result
