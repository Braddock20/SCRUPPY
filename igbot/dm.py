"""The DM orchestrator — poll, decide, reply.

The loop, once per poll:

1. Open the inbox and walk each configured tab.
2. Read the conversation rows. A row whose preview is *not* prefixed with
   ``You:`` is an inbound message.
3. Skip anything already handled (dedupe by fingerprint, persisted).
4. Ask the guardrails whether a reply is allowed right now.
5. Open the thread, read the real message text, and pick a reply.
6. Wait a human amount of time, type, send, and **confirm the send**.

The confirmation step matters more than anything else here. Instagram clears the
composer even when a message is rejected, so "the box is empty" is not proof of
delivery. The only accepted proof is a new outgoing bubble appearing in the
thread. If that never happens the message is recorded as unconfirmed and
flagged for a human — the bot never blindly retries, because a duplicate
reply to a customer is worse than a missed one.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field

from . import log
from .ai import AIReplyEngine
from .dom import DOM, Row
from .guardrails import Guardrails
from .rules import Rulebook
from .store import Store, fingerprint_for

log = log.get(__name__)

KIND_UNCONFIRMED = "send_unconfirmed"
KIND_SKIPPED = "skipped"


@dataclass
class PollResult:
    """What happened during one poll."""

    rows_seen: int = 0
    inbound: int = 0
    replied: int = 0
    blocked: int = 0
    failed: int = 0
    skipped: int = 0
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"rows={self.rows_seen} inbound={self.inbound} replied={self.replied} "
            f"blocked={self.blocked} failed={self.failed} skipped={self.skipped}"
        )


class DMBot:
    """Coordinates reading the inbox and replying to messages."""

    def __init__(
        self,
        dom: DOM,
        store: Store,
        guardrails: Guardrails,
        rulebook: Rulebook,
        ai: AIReplyEngine,
        cfg,
        transport=None,
    ):
        self.dom = dom
        self.store = store
        self.guardrails = guardrails
        self.rulebook = rulebook
        self.ai = ai
        self.cfg = cfg
        self.transport = transport

    # ------------------------------------------------------------------ poll

    def poll_once(self) -> PollResult:
        """Run a single poll over every configured tab."""
        result = PollResult()
        baseline = bool(self.cfg.get("behaviour.baseline_only", False))

        self.dom.open_inbox()

        tabs = self.cfg.get("behaviour.tabs", ["Primary", "Request"]) or ["Primary"]
        for tab in tabs:
            if tab.lower() == "primary" and not self.dom.switch_tab("Primary"):
                log.info("no Primary tab found")
            elif tab.lower() not in ("primary",) and not self.dom.switch_tab(tab):
                log.info("no %r tab found; skipping", tab)
                result.notes.append(f"tab {tab!r} not present")

            for row in self.dom.list_rows():
                result.rows_seen += 1
                if row.outgoing:
                    continue  # our own message echoing back
                result.inbound += 1
                self._handle(row, result, baseline)

        return result

    # ---------------------------------------------------------------- handle

    def _fingerprint(self, row: Row) -> str:
        """A stable id for this inbound message.

        Built from the thread title, the preview text and — when the transport
        has seen it — the thread's last-activity timestamp. Including the
        timestamp means the same person sending the same words twice in a row
        is still recognised as two messages.
        """
        activity = ""
        if self.transport is not None:
            key = self._thread_key_for(row)
            ms = self.transport.last_activity_ms(key) if key else 0
            if ms:
                activity = str(ms)
        return fingerprint_for(row.title, "", f"{row.preview}|{activity}")

    def _thread_key_for(self, row: Row) -> str:
        """Best-effort mapping from an inbox row to a transport thread id."""
        if self.transport is None:
            return ""
        for key, meta in self.transport.threads.items():
            if meta.title and meta.title.strip() == row.title.strip():
                return key
        return ""

    def _handle(self, row: Row, result: PollResult, baseline: bool) -> None:
        fingerprint = self._fingerprint(row)

        if self.store.has_fingerprint(fingerprint):
            result.skipped += 1
            return

        log.info(
            "inbound message from %r: %r", row.title, row.preview[:120]
        )

        if baseline:
            self.guardrails.record_inbound(row.title, fingerprint, detail="baseline")
            self.store.record(KIND_SKIPPED, thread_id=row.title, detail="baseline mode")
            result.notes.append(f"baseline: logged message from {row.title}")
            return

        decision = self.guardrails.check(row.title)
        if not decision.allowed:
            log.info("not replying to %r: %s", row.title, decision.reason)
            result.blocked += 1
            result.notes.append(f"blocked {row.title}: {decision.reason}")
            # Deliberately NOT recorded as handled, so the message is retried
            # once the cooldown or cap allows it.
            return

        try:
            self._reply_to(row, result, fingerprint)
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("failed handling message from %r: %s", row.title, exc)
            result.failed += 1

    # ----------------------------------------------------------------- reply

    def _reply_to(self, row: Row, result: PollResult, fingerprint: str) -> None:
        info = self.dom.open_row(row)
        if not info.thread_key:
            log.warning("could not open thread for %r", row.title)
            result.failed += 1
            return

        messages = self.dom.read_messages()
        inbound = None
        for message in reversed(messages):
            if not message.outgoing:
                inbound = message
                break

        if inbound is None:
            log.info("no inbound message found in thread %s", info.thread_key)
            result.skipped += 1
            self.dom.back_to_inbox()
            return

        # Sanity check: the thread we opened should contain the message the
        # preview advertised. If it does not, we are looking at the wrong
        # conversation and must not reply.
        if not self._preview_matches(row.preview, inbound.text):
            log.warning(
                "preview %r does not match thread content %r; skipping",
                row.preview[:60],
                inbound.text[:60],
            )
            result.skipped += 1
            self.dom.back_to_inbox()
            return

        actual_text = inbound.text

        # A message request has no composer until it is accepted, and
        # accepting is visible to the sender — so it is opt-in.
        if not self.dom.composer():
            if not self.cfg.get("behaviour.accept_requests", False):
                log.info(
                    "thread %s is a message request and accept_requests is off; "
                    "logging for manual handling",
                    info.thread_key,
                )
                self.guardrails.record_inbound(row.title, fingerprint, detail="request-pending")
                self.store.record(
                    KIND_SKIPPED, thread_id=row.title, detail="message request needs manual accept"
                )
                result.notes.append(f"request from {row.title} needs manual acceptance")
                result.skipped += 1
                self.dom.back_to_inbox()
                return
            if not self.dom.accept_request():
                log.warning("could not accept request from %r", row.title)
                result.failed += 1
                self.dom.back_to_inbox()
                return
            log.info("accepted message request from %r", row.title)

        reply_text, source = self.choose_reply(actual_text)
        if not reply_text:
            log.info("no reply text for %r; skipping", row.title)
            result.skipped += 1
            self.dom.back_to_inbox()
            return

        log.info("replying to %r using %s: %r", row.title, source, reply_text[:100])

        # Human pause before answering.
        delay_min = float(self.cfg.get("behaviour.reply_delay_min", 12) or 0)
        delay_max = float(self.cfg.get("behaviour.reply_delay_max", 55) or 0)
        if delay_max > delay_min:
            delay = random.uniform(delay_min, delay_max)
        else:
            delay = delay_min
        log.info("waiting %.0fs before replying", delay)
        time.sleep(delay)

        before = self.dom.read_messages()
        before_count = len(before)

        if not self.dom.type_message(reply_text):
            log.warning("could not type the reply for %r", row.title)
            result.failed += 1
            self.dom.back_to_inbox()
            return

        if not self.dom.click_send():
            log.warning("no send button appeared for %r", row.title)
            result.failed += 1
            self.dom.back_to_inbox()
            return

        confirmed, reason = self.dom.wait_for_send_confirmation(reply_text, before_count)

        if confirmed:
            self.guardrails.record_reply(row.title, detail=f"reply via {source}")
            self.guardrails.record_inbound(row.title, fingerprint, detail=source)
            log.info("reply confirmed to %r (%s)", row.title, reason)
            result.replied += 1
        else:
            # Recorded so we never double-send, and surfaced loudly so a human
            # can check the conversation.
            self.guardrails.record_inbound(row.title, fingerprint, detail=f"unconfirmed: {reason}")
            self.store.record(
                KIND_UNCONFIRMED,
                thread_id=row.title,
                fingerprint=None,
                detail=reason[:300],
            )
            log.error(
                "reply to %r could NOT be confirmed (%s) — recorded as unconfirmed, "
                "please check the thread manually",
                row.title,
                reason,
            )
            result.failed += 1
            result.notes.append(f"unconfirmed reply to {row.title}: {reason}")

        self.dom.back_to_inbox()

    # ----------------------------------------------------------------- reply

    def choose_reply(self, text: str) -> tuple[str, str]:
        """Pick the reply for a message.

        Order: a matching rule, then the AI backend, then the default reply.
        The AI is only consulted for messages the rulebook does not cover, and
        its failure falls through to the default — a customer is never left
        without an answer because a model was unavailable.
        """
        default = str(self.cfg.get("reply.default_reply", "") or "")
        rule_text, source = self.rulebook.reply_for(text, "")

        if rule_text:
            return rule_text, source

        if self.ai.available():
            result = self.ai.generate(text)
            if result.ok:
                return result.text, "ai"
            log.warning("AI backend failed (%s); falling back to default reply", result.error)

        return default, "default"

    # ----------------------------------------------------------------- utils

    @staticmethod
    def _preview_matches(preview: str, actual: str) -> bool:
        """Is the thread content the same message the preview advertised?"""
        p = " ".join((preview or "").split()).lower().rstrip("…").strip()
        a = " ".join((actual or "").split()).lower()
        if not p or not a:
            return False
        if p == a:
            return True
        # Previews are truncated, so one is a prefix of the other.
        if a.startswith(p):
            return True
        if p.startswith(a):
            return True
        # Compare a stable prefix, in case of odd whitespace or ellipsis.
        head = p[:24]
        return bool(head) and a.startswith(head)
