"""Logging setup that survives being piped into another process.

The bot is meant to run under systemd / nohup for days at a time. If stderr is
a closed pipe (``systemctl ... | head``, or the parent shell exits), a plain
StreamHandler raises EPIPE on write and can take the whole process down. We
swallow broken-pipe errors and keep a rotating file log as the source of truth.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path

_CONFIGURED = False

_FMT = "%(asctime)s %(levelname)-7s %(name)-18s %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


class _SafeStreamHandler(logging.StreamHandler):
    """StreamHandler that never raises on a broken pipe."""

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        try:
            super().emit(record)
        except (BrokenPipeError, ValueError):
            # Downstream reader went away (e.g. `| head`). Drop the record.
            pass
        except OSError:
            pass


def setup(level: str = "INFO", logfile: str | None = None) -> None:
    """Configure root logging. Safe to call more than once."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    root = logging.getLogger()
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))

    for handler in list(root.handlers):
        root.removeHandler(handler)

    stream = _SafeStreamHandler(sys.stderr)
    stream.setFormatter(logging.Formatter(_FMT, _DATEFMT))
    root.addHandler(stream)

    if logfile:
        path = Path(logfile).expanduser()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(
                str(path), maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
            )
            fh.setFormatter(logging.Formatter(_FMT, _DATEFMT))
            root.addHandler(fh)
        except OSError as exc:  # pragma: no cover - disk problems
            root.warning("cannot open log file %s: %s", path, exc)

    # Third-party chatter down a notch.
    for noisy in ("asyncio", "urllib3", "playwright._impl._browser_context"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True


def get(name: str) -> logging.Logger:
    """Return a namespaced logger."""
    return logging.getLogger(name)


def set_level(level: str) -> None:
    """Adjust level at runtime (used by the CLI's ``--verbose`` flag)."""
    logging.getLogger().setLevel(getattr(logging, str(level).upper(), logging.INFO))
    os.environ["IGBOT_LEVEL"] = str(level).upper()
