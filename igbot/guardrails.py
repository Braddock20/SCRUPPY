"""Guardrails: the rules that decide whether a reply may be sent right now.

Two jobs:

1. **Protection** — cooldowns and caps stop the bot from replying to every
   message the moment it arrives, which is exactly the behaviour that makes an
   account look automated.
2. **Honesty** — when a reply is blocked the reason is recorded, so the log
   says *why* nothing was sent instead of leaving you guessing.

The reply is only counted once it has been *confirmed* sent. A failed send does
not burn the user's quota and does not start the cooldown.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .store import Store, day_start, hour_start

KIND_REPLY = "reply"
KIND_INBOUND = "inbound"
KIND_PUBLISH = "publish"
KIND_SEEN = "seen"


@dataclass
class Decision:
    """Outcome of a guardrail check."""

    allowed: bool
    reason: str = ""
    wait_seconds: int = 0
    details: dict = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.allowed

    def __str__(self) -> str:  # pragma: no cover - display helper
        if self.allowed:
            return "allowed"
        return f"blocked: {self.reason}"


class Guardrails:
    """Applies cooldown and rate-cap policy to outgoing replies."""

    def __init__(self, store: Store, cfg) -> None:
        self.store = store
        self.cfg = cfg

    # ------------------------------------------------------------------ policy

    def _cooldown(self) -> int:
        return int(self.cfg.get("behaviour.cooldown_seconds", 900) or 0)

    def _hourly_cap(self) -> int:
        return int(self.cfg.get("behaviour.hourly_cap", 8) or 0)

    def _daily_cap(self) -> int:
        return int(self.cfg.get("behaviour.daily_cap", 25) or 0)

    def _global_daily_cap(self) -> int:
        return int(self.cfg.get("behaviour.global_daily_cap", 120) or 0)

    # ------------------------------------------------------------------ checks

    def check(self, thread_id: str) -> Decision:
        """Decide whether we may reply to ``thread_id`` right now."""
        now = time.time()

        if bool(self.cfg.get("behaviour.baseline_only", False)):
            return Decision(False, "baseline mode is on (no replies are sent)")

        cooldown = self._cooldown()
        last = self.store.last_ts(KIND_REPLY, thread_id)
        if last is not None and cooldown > 0:
            elapsed = now - last
            if elapsed < cooldown:
                wait = int(cooldown - elapsed)
                return Decision(
                    False,
                    f"conversation is in cooldown for another {wait}s",
                    wait_seconds=wait,
                    details={"last_reply_at": last},
                )

        hour_cap = self._hourly_cap()
        if hour_cap > 0:
            used = self.store.count(KIND_REPLY, hour_start(now), thread_id)
            if used >= hour_cap:
                return Decision(
                    False,
                    f"hourly cap reached for this conversation ({used}/{hour_cap})",
                    wait_seconds=int(3600 - (now - hour_start(now))),
                    details={"used": used, "cap": hour_cap},
                )

        day_cap = self._daily_cap()
        if day_cap > 0:
            used = self.store.count(KIND_REPLY, day_start(now), thread_id)
            if used >= day_cap:
                return Decision(
                    False,
                    f"daily cap reached for this conversation ({used}/{day_cap})",
                    wait_seconds=int(86400 - (now - day_start(now))),
                    details={"used": used, "cap": day_cap},
                )

        global_cap = self._global_daily_cap()
        if global_cap > 0:
            used = self.store.count(KIND_REPLY, day_start(now), None)
            if used >= global_cap:
                return Decision(
                    False,
                    f"global daily cap reached ({used}/{global_cap})",
                    wait_seconds=int(86400 - (now - day_start(now))),
                    details={"used": used, "cap": global_cap},
                )

        return Decision(True)

    def record_reply(self, thread_id: str, detail: str | None = None) -> None:
        """Record a *confirmed* outgoing reply."""
        self.store.record(KIND_REPLY, thread_id=thread_id, detail=detail)

    def record_inbound(self, thread_id: str, fingerprint: str, detail: str | None = None) -> bool:
        """Record that an inbound message was seen.

        Returns False when the fingerprint was already known, which is how
        duplicate suppression works across restarts.
        """
        return self.store.record(
            KIND_INBOUND, thread_id=thread_id, fingerprint=fingerprint, detail=detail
        )

    # ------------------------------------------------------------------- stats

    def stats(self) -> dict:
        """Snapshot of current usage, for the CLI and the dashboard."""
        now = time.time()
        return {
            "replies_today": self.store.count(KIND_REPLY, day_start(now)),
            "replies_this_hour": self.store.count(KIND_REPLY, hour_start(now)),
            "inbound_today": self.store.count(KIND_INBOUND, day_start(now)),
            "global_daily_cap": self._global_daily_cap(),
            "daily_cap": self._daily_cap(),
            "hourly_cap": self._hourly_cap(),
            "cooldown_seconds": self._cooldown(),
            "baseline_only": bool(self.cfg.get("behaviour.baseline_only", False)),
        }
