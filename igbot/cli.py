"""Command line interface.

    igbot login      -- connect using a session cookie
    igbot doctor     -- check everything is working
    igbot baseline   -- read the inbox and log messages, never reply
    igbot watch      -- the 24/7 loop
    igbot publish    -- post a photo
    igbot comments   -- read or reply to comments on a post
    igbot stats      -- show counters
    igbot selftest   -- verify the send path against your own account

Every command accepts ``--root`` to point at a different project directory.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from pathlib import Path

import yaml

from . import config as config_mod
from . import log as log_mod
from .ai import AIReplyEngine
from .dom import DOM
from .dm import DMBot
from .guardrails import Guardrails
from .publish import Publisher
from .rules import Rulebook
from .store import Store
from .transport import Transport

log = log_mod.get(__name__)

_STOP = {"flag": False}


def _install_signal_handlers() -> None:
    def handler(signum, frame):  # pragma: no cover - signal path
        log.info("signal %s received; finishing the current cycle", signum)
        _STOP["flag"] = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):  # pragma: no cover
            pass


def _settle(cfg) -> float:
    """Scale factor for the page-settling waits (1.0 = real-world timing)."""
    return max(0.05, float(cfg.get("behaviour.page_settle_ms", 1000) or 1000) / 1000.0)


def _load_selectors(root: Path) -> dict:
    path = root / "igbot" / "selectors.yaml"
    if not path.exists():
        log.warning("selectors.yaml not found at %s", path)
        return {}
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        log.error("could not read selectors.yaml: %s", exc)
        return {}


def _open_session(cfg, headless: bool | None = None):
    """Launch the browser and make sure we are logged in."""
    from .browser import Browser

    browser = Browser(cfg, headless=headless)
    page = browser.launch()

    cookie_file = cfg.resolve("account.cookies_file")
    cookie_source = None
    if cookie_file.exists():
        cookie_source = str(cookie_file)
    elif os.environ.get("IGBOT_COOKIE"):
        cookie_source = os.environ["IGBOT_COOKIE"]
    else:
        browser.close()
        raise SystemExit(
            "No session cookie found.\n"
            f"  Expected it at: {cookie_file}\n"
            "  Run 'igbot login --cookie-stdin' and paste the cookie, or set IGBOT_COOKIE."
        )

    count = browser.import_cookies(cookie_source)
    log.info("loaded %d cookies", count)

    if not browser.is_logged_in():
        browser.close()
        raise SystemExit(
            "Instagram did not accept that cookie (it may have expired).\n"
            "Grab a fresh cookie and run 'igbot login' again."
        )
    return browser, page


# ---------------------------------------------------------------------- login


def cmd_login(args) -> int:
    cfg = config_mod.load(args.root)
    log_mod.setup(cfg.get("logging.level", "INFO"), str(cfg.resolve("logging.file")))

    from .browser import Browser

    browser = Browser(cfg, headless=True)
    browser.launch()

    source = None
    if args.cookie:
        source = args.cookie
    elif args.cookie_file:
        source = str(Path(args.cookie_file).expanduser())
    elif args.cookie_stdin:
        source = "-"
    else:
        default = cfg.resolve("account.cookies_file")
        if default.exists():
            source = str(default)

    if not source:
        browser.close()
        print(
            "Provide the cookie one of these ways:\n"
            "  igbot login --cookie-file /path/to/cookies.txt\n"
            "  igbot login --cookie-stdin      (paste it, then press Ctrl-D)\n"
            "  igbot login --cookie 'csrftoken=...; sessionid=...'",
            file=sys.stderr,
        )
        return 2

    try:
        count = browser.import_cookies(source)
    except ValueError as exc:
        browser.close()
        print(f"Could not read the cookie: {exc}", file=sys.stderr)
        return 2

    print(f"Imported {count} cookies.")
    if not browser.is_logged_in():
        browser.close()
        print(
            "Instagram rejected that cookie. It may have expired — copy a fresh one.",
            file=sys.stderr,
        )
        return 1

    handle = browser.account_handle()
    user_id = browser.session_user_id()
    print(f"Logged in as {handle or '(handle unknown)'} (id {user_id or '?'}).")

    target = Path(args.save) if args.save else cfg.resolve("account.cookies_file")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(browser.export_cookies(), encoding="utf-8")
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    print(f"Saved the session cookie to {target}")
    browser.close()
    return 0


# --------------------------------------------------------------------- doctor


def cmd_doctor(args) -> int:
    cfg = config_mod.load(args.root)
    log_mod.setup(cfg.get("logging.level", "INFO"), str(cfg.resolve("logging.file")))
    print("igbot doctor\n" + "=" * 40)

    ok = True

    # --- config
    print(f"config        : {cfg.root}")
    rules = Rulebook.from_config(cfg)
    print(f"rules         : {len(rules)} rule(s)")
    print(f"baseline mode : {bool(cfg.get('behaviour.baseline_only', False))}")
    print(f"reply delay   : {cfg.get('behaviour.reply_delay_min')}-{cfg.get('behaviour.reply_delay_max')}s")
    print(f"tabs          : {cfg.get('behaviour.tabs')}")

    # --- state
    db_path = cfg.path("state", "igbot.db")
    store = Store(db_path)
    guard = Guardrails(store, cfg)
    print(f"state db      : {db_path}")
    for key, value in guard.stats().items():
        print(f"  {key:<16} {value}")

    # --- selectors
    selectors = _load_selectors(cfg.root)
    print(f"selectors     : {len(selectors)} section(s)")

    # --- session
    try:
        browser, page = _open_session(cfg, headless=args.headless)
    except SystemExit as exc:
        print(f"session       : FAILED — {exc}")
        return 1

    try:
        print(f"session       : OK (user id {browser.session_user_id()})")
        dom = DOM(page, selectors, str(cfg.get('urls.base', 'https://www.instagram.com')), settle=_settle(cfg))
        transport = Transport(page)
        transport.attach(page)

        dom.open_inbox()
        tabs = dom.tab_names()
        print(f"inbox tabs    : {tabs}")
        if not tabs:
            print("  !! no tabs found — Instagram may have changed the layout")
            ok = False

        rows = dom.list_rows()
        print(f"conversation rows on Primary: {len(rows)}")
        for row in rows[:8]:
            direction = "outgoing" if row.outgoing else "INBOUND "
            print(f"  [{direction}] {row.title[:28]:<28} {row.preview[:48]!r} ({row.time})")
        if not rows:
            print("  (no conversations in this tab — that is fine)")

        # --- request tab
        if dom.switch_tab("Requests"):
            request_rows = dom.list_rows()
            print(f"conversation rows on Requests: {len(request_rows)}")
            for row in request_rows[:5]:
                print(f"  [INBOUND ] {row.title[:28]:<28} {row.preview[:48]!r} ({row.time})")
        else:
            print("requests tab  : not present")

        # --- send path
        if args.deep:
            print("\nchecking the send path against your own account...")
            from .selftest import run_selftest

            result = run_selftest(browser, page, dom, selectors, cfg)
            for line in result.lines:
                print(f"  {line}")
            if not result.ok:
                ok = False
    finally:
        browser.close()

    print("\n" + ("All good." if ok else "Some checks failed — see above."))
    return 0 if ok else 1


# ------------------------------------------------------------------- baseline


def cmd_baseline(args) -> int:
    cfg = config_mod.load(args.root)
    log_mod.setup(cfg.get("logging.level", "INFO"), str(cfg.resolve("logging.file")))

    selectors = _load_selectors(cfg.root)
    store = Store(cfg.path("state", "igbot.db"))
    guard = Guardrails(store, cfg)
    rulebook = Rulebook.from_config(cfg)
    ai = AIReplyEngine(cfg)

    browser, page = _open_session(cfg, headless=args.headless)
    try:
        transport = Transport(page)
        transport.attach(page)
        dom = DOM(page, selectors, str(cfg.get('urls.base', 'https://www.instagram.com')), settle=_settle(cfg))
        bot = DMBot(dom, store, guard, rulebook, ai, cfg, transport)

        dom.open_inbox()
        tabs = cfg.get("behaviour.tabs", ["Primary", "Requests"]) or ["Primary"]
        for tab in tabs:
            if tab.lower() != "primary" and not dom.switch_tab(tab):
                continue
            rows = dom.list_rows()
            print(f"\n=== {tab} — {len(rows)} conversation(s) ===")
            for row in rows:
                direction = "sent by us" if row.outgoing else "INBOUND"
                print(f"  {row.title[:26]:<26} [{direction}] {row.preview[:60]!r} ({row.time})")

            # Show what the bot would answer.
            for row in rows:
                if row.outgoing:
                    continue
                reply, source = bot.choose_reply(row.preview)
                print(f"    -> would reply ({source}): {reply[:80]!r}")
    finally:
        browser.close()
    return 0


# ---------------------------------------------------------------------- watch


def cmd_watch(args) -> int:
    cfg = config_mod.load(args.root)
    log_mod.setup(cfg.get("logging.level", "INFO"), str(cfg.resolve("logging.file")))

    selectors = _load_selectors(cfg.root)
    store = Store(cfg.path("state", "igbot.db"))
    guard = Guardrails(store, cfg)
    rulebook = Rulebook.from_config(cfg)
    ai = AIReplyEngine(cfg)

    browser, page = _open_session(cfg, headless=args.headless)
    transport = Transport(page)
    transport.attach(page)
    dom = DOM(page, selectors, str(cfg.get('urls.base', 'https://www.instagram.com')), settle=_settle(cfg))
    bot = DMBot(dom, store, guard, rulebook, ai, cfg, transport)

    interval = float(args.interval or cfg.get("behaviour.poll_seconds", 45) or 45)
    _install_signal_handlers()

    baseline = bool(cfg.get("behaviour.baseline_only", False))
    if baseline:
        log.warning("baseline_only is ON — the bot will read but never reply")

    log.info("watching every %.0fs (Ctrl-C to stop)", interval)
    cycles = 0
    try:
        while not _STOP["flag"]:
            started = time.time()
            try:
                result = bot.poll_once()
                if result.inbound or result.replied:
                    log.info("poll: %s", result.summary())
                    for note in result.notes:
                        log.info("  note: %s", note)
            except Exception as exc:  # pragma: no cover - keep the loop alive
                log.exception("poll failed: %s", exc)

            cycles += 1
            if args.once:
                break
            elapsed = time.time() - started
            remaining = max(2.0, interval - elapsed)
            deadline = time.time() + remaining
            while not _STOP["flag"] and time.time() < deadline:
                time.sleep(0.5)
    finally:
        browser.close()
    log.info("stopped after %d cycle(s)", cycles)
    return 0


# -------------------------------------------------------------------- publish


def cmd_publish(args) -> int:
    cfg = config_mod.load(args.root)
    log_mod.setup(cfg.get("logging.level", "INFO"), str(cfg.resolve("logging.file")))
    selectors = _load_selectors(cfg.root)

    media = args.media or str(cfg.resolve("publishing.media_file"))
    caption = args.caption or ""
    if not caption:
        caption_file = args.caption_file or str(cfg.resolve("publishing.caption_file"))
        if caption_file and Path(caption_file).exists():
            caption = Path(caption_file).read_text(encoding="utf-8")

    if not media:
        print("No media file given. Use --media /path/to/photo.jpg", file=sys.stderr)
        return 2

    browser, page = _open_session(cfg, headless=args.headless)
    try:
        publisher = Publisher(page, cfg, selectors)
        result = publisher.publish(media, caption, dry_run=args.dry_run)
        for step in result.steps:
            print(f"  · {step}")
        print(result.message)
        return 0 if result.ok else 1
    finally:
        browser.close()


# ------------------------------------------------------------------- comments


def cmd_comments(args) -> int:
    cfg = config_mod.load(args.root)
    log_mod.setup(cfg.get("logging.level", "INFO"), str(cfg.resolve("logging.file")))
    selectors = _load_selectors(cfg.root)

    if not args.url:
        print("Give the post URL with --url", file=sys.stderr)
        return 2

    store = Store(cfg.path("state", "igbot.db"))
    guard = Guardrails(store, cfg)
    rulebook = Rulebook.from_config(cfg)

    browser, page = _open_session(cfg, headless=args.headless)
    try:
        from .comments import CommentBot

        bot = CommentBot(page, cfg, selectors, guard, rulebook)
        if not bot.open_post(args.url):
            print("Could not open the post.", file=sys.stderr)
            return 1
        comments = bot.read_comments()
        print(f"{len(comments)} comment(s):")
        for comment in comments:
            who = comment.username or "?"
            print(f"  @{who:<22} {comment.text[:70]!r}")
        if args.reply:
            default = str(cfg.get("reply.default_reply", "") or "")
            for comment in comments:
                if comment.username and comment.username == cfg.get("account.username"):
                    continue
                text, source = rulebook.reply_for(comment.text, default)
                if not text:
                    continue
                ok, reason = bot.reply(comment, text)
                print(f"  reply to @{comment.username} ({source}): {reason}")
                if not ok:
                    break
                time.sleep(float(cfg.get("behaviour.reply_delay_min", 12) or 12))
    finally:
        browser.close()
    return 0


# ---------------------------------------------------------------------- stats


def cmd_stats(args) -> int:
    cfg = config_mod.load(args.root)
    log_mod.setup(cfg.get("logging.level", "INFO"), str(cfg.resolve("logging.file")))
    store = Store(cfg.path("state", "igbot.db"))
    guard = Guardrails(store, cfg)

    print("igbot stats\n" + "=" * 40)
    for key, value in guard.stats().items():
        print(f"{key:<20} {value}")
    print("\nrecent replies:")
    for row in store.recent("reply", limit=10):
        print(f"  {row['ts']:.0f}  {row['thread_id'][:28]:<28} {str(row['detail'])[:40]}")
    return 0


# ------------------------------------------------------------------- selftest


def cmd_selftest(args) -> int:
    cfg = config_mod.load(args.root)
    log_mod.setup(cfg.get("logging.level", "INFO"), str(cfg.resolve("logging.file")))
    selectors = _load_selectors(cfg.root)

    browser, page = _open_session(cfg, headless=args.headless)
    try:
        from .selftest import run_selftest

        result = run_selftest(browser, page, DOM(page, selectors, str(cfg.get('urls.base', 'https://www.instagram.com')), settle=_settle(cfg)), selectors, cfg)
        for line in result.lines:
            print(line)
        return 0 if result.ok else 1
    finally:
        browser.close()


# ----------------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="igbot",
        description="Instagram DM auto-reply and publishing bot.",
    )
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]),
                        help="project directory (default: the package's parent)")
    parser.add_argument("--headless", action="store_true",
                        help="run without a visible window (less safe)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("login", help="connect using a session cookie")
    p.add_argument("--cookie", help="the cookie header as a string")
    p.add_argument("--cookie-file", help="a file holding the cookie header")
    p.add_argument("--cookie-stdin", action="store_true", help="read the cookie from stdin")
    p.add_argument("--save", help="where to save the cookie (default: account.cookies_file)")
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("doctor", help="check the whole setup")
    p.add_argument("--deep", action="store_true", help="also test the send path")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("baseline", help="read the inbox and log what is there")
    p.set_defaults(func=cmd_baseline)

    p = sub.add_parser("watch", help="run the 24/7 polling loop")
    p.add_argument("--once", action="store_true", help="run a single poll and exit")
    p.add_argument("--interval", type=float, help="seconds between polls")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("publish", help="post a photo")
    p.add_argument("--media", help="path to the image or video")
    p.add_argument("--caption", help="caption text")
    p.add_argument("--caption-file", help="file holding the caption")
    p.add_argument("--dry-run", action="store_true",
                   help="prepare the post but do not click Share")
    p.set_defaults(func=cmd_publish)

    p = sub.add_parser("comments", help="read or reply to comments on a post")
    p.add_argument("--url", help="the post URL")
    p.add_argument("--reply", action="store_true", help="reply to each comment")
    p.set_defaults(func=cmd_comments)

    p = sub.add_parser("stats", help="show counters")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("selftest", help="verify the send path against your own account")
    p.set_defaults(func=cmd_selftest)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
