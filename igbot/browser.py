"""Browser session management.

Responsibilities:

* one persistent Chromium profile, reused across restarts, so Instagram sees a
  device that has been logging in for weeks rather than a brand new one;
* a first-class **cookie login** path — paste the browser's ``Cookie:`` header
  and you are in, no form automation and no second-factor prompt;
* ordinary Playwright browser operation without fingerprint masking;
* honest detection of whether we are actually logged in, and of the
  checkpoint / "confirm it's you" screens that must never be pushed through
  automatically.
"""

from __future__ import annotations

import json
import os
import random
import re
import time
from pathlib import Path
from urllib.parse import unquote

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

from . import log

log = log.get(__name__)

# Text that means Instagram has stopped and wants a human. We never click
# through these — the bot shuts the flow down and leaves the account alone.
CHECKPOINT_MARKERS = (
    "checkpoint required",
    "confirm it's you",
    "confirm it is you",
    "suspicious login attempt",
    "help us confirm",
    "we detected unusual activity",
    "your account has been temporarily limited",
    "secure your account",
    "change your password",
    "two-factor authentication",
    "enter the code we sent",
    "phone number verification",
    "security code",
)

_LOGIN_WALL_MARKERS = (
    "log in to instagram",
    "log in to follow",
    "see photos and videos",
    "create an account",
)

def parse_cookie_header(header: str) -> list[dict]:
    """Parse a raw ``Cookie:`` header into Playwright cookie dicts.

    Handles the exact shape a browser devtools copy produces:

        csrftoken=abc; sessionid=123%3Aabc; rur="LDC\\054123\\054456:...";

    * values are percent-decoded (Instagram's ``sessionid`` really contains
      colons, not ``%3A``);
    * quoted values are unquoted and ``\\,`` / ``\\"`` escapes are resolved;
    * the pair is split on the *first* ``=`` only, so base64-ish values that
      contain ``=`` survive intact.
    """
    cookies: list[dict] = []
    for chunk in (header or "").split(";"):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        name, _, raw_value = chunk.partition("=")
        name = name.strip()
        value = raw_value.strip()
        if not name:
            continue

        quoted = len(value) >= 2 and value[0] == '"' and value[-1] == '"'
        if quoted:
            value = value[1:-1]
        # Instagram's ``rur`` cookie uses octal escapes for punctuation, e.g.
        # ``LDC\05463154994371\0541826870414:...`` where \054 is a comma.
        # Some tools emit a plain ``\,`` instead — handle both.
        value = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), value)
        value = value.replace('\\"', '"').replace("\\,", ",").replace("\\\\", "\\")
        # Percent-decode; leave malformed sequences untouched.
        value = unquote(value)

        cookies.append({"name": name, "value": value})
    return cookies



def read_cookie_source(source: str) -> str:
    """Resolve a cookie string from a file path, a literal string, or stdin."""
    source = (source or "").strip()

    if not source:
        return ""

    if source == "-":
        import sys
        return sys.stdin.read().strip()

    # Treat cookie headers as literal strings, not filesystem paths.
    if ";" in source and "=" in source:
        return source

    # Handle a single cookie name=value pair.
    if "=" in source and "/" not in source and "\\" not in source:
        return source

    # Only check values that may actually be file paths.
    path = Path(source).expanduser()

    try:
        if path.is_file():
            return path.read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        pass

    return source



class SessionExpired(RuntimeError):
    """Raised when Instagram requires a fresh login."""


class CheckpointRequired(RuntimeError):
    """Raised when Instagram puts up a human-verification screen."""


class Browser:
    """Owns the Playwright browser context and the Instagram session."""

    def __init__(self, cfg, headless: bool | None = None):
        self.cfg = cfg
        self.headless = bool(cfg.get("browser.headless", False)) if headless is None else headless
        self._pw = None
        self._browser = None
        self.context = None
        self.page = None
        self.checkpoint_text = ""

    # ------------------------------------------------------------------ setup

    def _device(self) -> dict:
        device = str(self.cfg.get("account.device", "mobile") or "mobile").lower()
        if device == "desktop":
            return {
                "viewport": {"width": 1440, "height": 900},
                "device_scale_factor": 1,
                "is_mobile": False,
                "has_touch": False,
                "user_agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
            }
        vp = self.cfg.get("browser.viewport", {}) or {}
        return {
            "viewport": {
                "width": int(vp.get("width", 414)),
                "height": int(vp.get("height", 863)),
            },
            "device_scale_factor": float(vp.get("device_scale_factor", 2.608695652173913)),
            "is_mobile": bool(vp.get("is_mobile", True)),
            "has_touch": bool(vp.get("has_touch", True)),
            "user_agent": str(self.cfg.get("browser.user_agent", "") or "") or None,
        }

    def launch(self):
        """Start Chromium with a persistent profile. Returns the live page."""
        if self.page is not None:
            return self.page

        device = self._device()
        profile_dir = Path(self.cfg.resolve("browser.profile_dir"))
        profile_dir.mkdir(parents=True, exist_ok=True)

        self._pw = sync_playwright().start()
        args = [
            "--disable-dev-shm-usage",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        proxy = str(self.cfg.get("browser.proxy", "") or "")

        log.info(
            "launching chromium (headless=%s, profile=%s, device=%s)",
            self.headless,
            profile_dir,
            self.cfg.get("account.device", "mobile"),
        )

        # A persistent context keeps cookies, localStorage and the device
        # fingerprint across restarts, so Instagram sees a long-lived device
        # instead of a fresh one every night.
        context_kwargs = {
            "user_data_dir": str(profile_dir),
            "headless": self.headless,
            "args": args,
            "viewport": device["viewport"],
            "device_scale_factor": device["device_scale_factor"],
            "is_mobile": device["is_mobile"],
            "has_touch": device["has_touch"],
            "locale": str(self.cfg.get("browser.locale", "en-US") or "en-US"),
            "timezone_id": "Europe/Moscow",
            "slow_mo": int(self.cfg.get("browser.slow_mo_ms", 0) or 0),
        }
        if device.get("user_agent"):
            context_kwargs["user_agent"] = device["user_agent"]
        if proxy:
            context_kwargs["proxy"] = {"server": proxy}

        self.context = self._pw.chromium.launch_persistent_context(**context_kwargs)
        self.context.set_default_timeout(30000)

        # launch_persistent_context usually hands back one blank page.
        if self.context.pages:
            self.page = self.context.pages[0]
        else:
            self.page = self.context.new_page()
        return self.page

    def close(self) -> None:
        for closer in (self.context, self._browser):
            try:
                if closer is not None:
                    closer.close()
            except Exception:  # pragma: no cover - best effort teardown
                pass
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:  # pragma: no cover
            pass
        self.page = None
        self.context = None
        self._browser = None
        self._pw = None

    def __enter__(self) -> "Browser":
        self.launch()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---------------------------------------------------------------- cookies

    def import_cookies(self, source: str, domain: str | None = None) -> int:
        """Install cookies from a header string / file / stdin.

        Returns the number of cookies installed.
        """
        raw = read_cookie_source(source)
        if not raw:
            raise ValueError("no cookies supplied")
        parsed = parse_cookie_header(raw)
        if not parsed:
            raise ValueError("cookie source did not contain any name=value pairs")

        domain = domain or str(self.cfg.get("account.cookie_domain", ".instagram.com"))
        payload = []
        for cookie in parsed:
            entry = {
                "name": cookie["name"],
                "value": cookie["value"],
                "domain": domain,
                "path": "/",
                "httpOnly": False,
                "secure": True,
                "sameSite": "Lax",
            }
            payload.append(entry)

        self.context.add_cookies(payload)
        log.info("imported %d cookies for %s", len(payload), domain)
        return len(payload)

    def export_cookies(self) -> str:
        """Return the current session cookies as a ``Cookie:`` header value."""
        cookies = self.context.cookies("https://www.instagram.com")
        return "; ".join(f"{c['name']}={c['value']}" for c in cookies)

    def save_session_cookie(self, path: str | Path) -> Path:
        """Persist the live session cookies to a file (mode 600)."""
        target = Path(path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.export_cookies(), encoding="utf-8")
        try:
            os.chmod(target, 0o600)
        except OSError:  # pragma: no cover - platform dependent
            pass
        return target

    # ------------------------------------------------------------------ login

    def goto(self, url: str, wait: str = "domcontentloaded") -> None:
        """Navigate and surface any checkpoint immediately."""
        log.info("navigating to %s", url)
        self.page.goto(url, wait_until=wait, timeout=45000)
        self.page.wait_for_timeout(random.randint(700, 1400))
        self.assert_no_checkpoint()

    def assert_no_checkpoint(self) -> None:
        """Raise if the page is a checkpoint / verification screen."""
        try:
            text = (self.page.inner_text("body", timeout=5000) or "").lower()
        except PlaywrightTimeout:
            return
        except PlaywrightError:
            return
        for marker in CHECKPOINT_MARKERS:
            if marker in text:
                self.checkpoint_text = marker
                log.error("checkpoint detected: %s — stopping for a human", marker)
                raise CheckpointRequired(marker)

    def is_logged_in(self) -> bool:
        """Decide whether the session is authenticated.

        Uses the page's own embedded state where possible, and falls back to
        structural checks. A hard login wall counts as *not* logged in.
        """
        url = str(self.cfg.get("urls.base", "https://www.instagram.com")).rstrip("/")
        try:
            response = self.page.goto(url + "/", wait_until="domcontentloaded", timeout=45000)
        except (PlaywrightError, PlaywrightTimeout) as exc:
            log.warning("could not load instagram: %s", exc)
            return False

        if response is not None and response.status >= 400:
            log.warning("instagram returned HTTP %s", response.status)
            return False

        self.page.wait_for_timeout(random.randint(800, 1600))

        try:
            html = self.page.content()
        except PlaywrightError:
            return False

        if '"is_logged_in":true' in html or '"is_logged_in": true' in html:
            return True

        try:
            body = (self.page.inner_text("body", timeout=5000) or "").lower()
        except (PlaywrightError, PlaywrightTimeout):
            body = ""

        if any(marker in body for marker in _LOGIN_WALL_MARKERS):
            return False

        # The DM inbox link only renders for an authenticated session.
        try:
            if self.page.locator('a[href*="/direct/"]').count() > 0:
                return True
        except PlaywrightError:
            pass
        return False

    def session_user_id(self) -> str:
        """The account's own numeric id, from the ``ds_user_id`` cookie."""
        for cookie in self.context.cookies("https://www.instagram.com"):
            if cookie["name"] == "ds_user_id":
                return str(cookie["value"])
        return ""

    def account_handle(self) -> str:
        """Best-effort handle of the logged-in account."""
        try:
            html = self.page.content()
        except PlaywrightError:
            return ""
        match = re.search(r'"username"\s*:\s*"([^"]+)"', html)
        return match.group(1) if match else ""
