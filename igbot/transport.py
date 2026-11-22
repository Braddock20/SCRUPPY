"""Transport — reads Instagram's own GraphQL responses as they flow past.

Why this exists
---------------
Scraping the DOM for message text is brittle and slow. But Instagram's web
app *already* fetches structured JSON describing the mailbox and the threads it
opens. Intercepting those responses costs nothing: no extra requests, no extra
footprint, nothing that looks unusual to the server.

Crucially we never *construct* a GraphQL request, so we do not need to know
Instagram's internal ``doc_id`` values — those change often and would break a
hand-rolled client. We simply read what the page fetches for itself.

Two payloads matter:

``get_slide_mailbox_for_iris_subscription``
    The mailbox index: thread ids, unread flags and last-activity timestamps.

``get_slide_thread_nullable``
    Metadata for the thread that was just opened: title, participants, and the
    viewer id.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from typing import Any

from . import log

log = log.get(__name__)


@dataclass
class ThreadMeta:
    """Metadata about a thread, harvested from Instagram's own responses."""

    thread_key: str = ""
    title: str = ""
    thread_id: str = ""
    viewer_id: str = ""
    participants: list[dict] = field(default_factory=list)
    users: list[dict] = field(default_factory=list)

    def usernames(self) -> list[str]:
        out = []
        for user in self.users or []:
            name = user.get("username")
            if name:
                out.append(str(name))
        return out


@dataclass
class MailboxEntry:
    """One entry in the mailbox index."""

    thread_id: str
    unread: bool = False
    last_activity_ms: int = 0
    muted: bool = False


class Transport:
    """Captures and indexes the GraphQL payloads the page fetches."""

    def __init__(self, page=None):
        self.page = page
        self._lock = threading.RLock()
        self.mailbox: dict[str, MailboxEntry] = {}
        self.threads: dict[str, ThreadMeta] = {}
        self._current: ThreadMeta | None = None
        self.seen_urls: set[str] = set()

    # ------------------------------------------------------------------ setup

    def attach(self, page) -> None:
        """Start capturing responses from ``page``."""
        self.page = page
        page.on("response", self._on_response)

    def detach(self) -> None:
        if self.page is not None:
            try:
                self.page.remove_listener("response", self._on_response)
            except Exception:  # pragma: no cover - best effort
                pass

    # --------------------------------------------------------------- capture

    def _on_response(self, response) -> None:
        url = response.url
        if "/graphql" not in url:
            return
        try:
            if response.status != 200:
                return
            body = response.text()
        except Exception:
            return
        if len(body) < 80:
            return
        try:
            payload = json.loads(body)
        except ValueError:
            return
        if not isinstance(payload, dict):
            return
        data = payload.get("data")
        if not isinstance(data, dict):
            return
        with self._lock:
            self._absorb(data)

    def _absorb(self, data: dict) -> None:
        for key, value in data.items():
            if not isinstance(value, dict):
                continue
            if key == "get_slide_mailbox_for_iris_subscription":
                self._absorb_mailbox(value)
            elif key == "get_slide_thread_nullable":
                self._absorb_thread(value)

    def _absorb_mailbox(self, node: dict) -> None:
        for folder_key in ("threads_by_folder", "threads_by_system_folder_and_ig_inbox_folder"):
            folder = node.get(folder_key)
            if not isinstance(folder, dict):
                continue
            for edge in folder.get("edges") or []:
                if not isinstance(edge, dict):
                    continue
                thread_node = edge.get("node")
                if not isinstance(thread_node, dict):
                    continue
                direct = thread_node.get("as_ig_direct_thread")
                if not isinstance(direct, dict):
                    direct = thread_node
                thread_id = str(direct.get("id") or thread_node.get("id") or "")
                if not thread_id:
                    continue
                try:
                    activity = int(direct.get("last_activity_timestamp_ms") or 0)
                except (TypeError, ValueError):
                    activity = 0
                entry = MailboxEntry(
                    thread_id=thread_id,
                    unread=bool(direct.get("marked_as_unread")),
                    last_activity_ms=activity,
                    muted=bool(direct.get("is_muted")),
                )
                with self._lock:
                    existing = self.mailbox.get(thread_id)
                    if existing and existing.last_activity_ms >= entry.last_activity_ms:
                        existing.unread = existing.unread or entry.unread
                    else:
                        self.mailbox[thread_id] = entry

    def _absorb_thread(self, node: dict) -> None:
        direct = node.get("as_ig_direct_thread")
        if not isinstance(direct, dict):
            return
        key = str(direct.get("thread_key") or "")
        meta = ThreadMeta(
            thread_key=key,
            title=str(direct.get("thread_title") or ""),
            thread_id=str(direct.get("id") or ""),
            viewer_id=str(direct.get("viewer_id") or ""),
            users=[u for u in (direct.get("users") or []) if isinstance(u, dict)],
        )
        with self._lock:
            self.threads[key] = meta
            self._current = meta

    # ----------------------------------------------------------------- access

    def current_thread(self) -> ThreadMeta | None:
        """Metadata for the most recently opened thread."""
        with self._lock:
            return self._current

    def last_activity_ms(self, thread_id: str) -> int:
        with self._lock:
            entry = self.mailbox.get(thread_id)
            return entry.last_activity_ms if entry else 0

    def unread_threads(self) -> list[str]:
        with self._lock:
            return [tid for tid, entry in self.mailbox.items() if entry.unread]

    def reset(self) -> None:
        with self._lock:
            self.mailbox.clear()
            self.threads.clear()
            self._current = None
