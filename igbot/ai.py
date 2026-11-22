"""Optional AI reply generation.

Deliberately vendor-neutral: point ``reply.ai.endpoint`` at any
OpenAI-compatible ``/chat/completions`` endpoint and it works.

Design rule: **the AI may improve a reply but may never be the reason a
message goes unanswered.** If the endpoint is slow, rate-limited, down, or
returns nonsense, we fall back to the deterministic rulebook and then to the
configured default reply. A customer message is never dropped because a model
was unavailable.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass

from . import log

log = log.get(__name__)

# A real browser UA. Some CDNs (including Cloudflare's bot management) reject
# the default `Python-urllib/3.11` UA outright with a 403 that looks like rate
# limiting, so we always send an explicit one.
_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

_SYSTEM_PROMPT = (
    "You are the customer-support assistant for a small business on Instagram. "
    "Reply to the customer's message in at most two short sentences. "
    "Be warm and specific. Never invent prices, availability, policies or links "
    "that are not given to you. If you do not know, say someone will follow up. "
    "Return only the reply text, no quotes, no preamble."
)


@dataclass
class AIResult:
    """Outcome of an AI reply attempt."""

    text: str
    ok: bool
    error: str = ""


class AIReplyEngine:
    """Thin client for an OpenAI-compatible chat endpoint."""

    def __init__(self, cfg):
        self.enabled = bool(cfg.get("reply.ai.enabled", False))
        self.endpoint = str(cfg.get("reply.ai.endpoint", "") or "").strip()
        self.model = str(cfg.get("reply.ai.model", "gpt-4o-mini") or "gpt-4o-mini")
        self.timeout = float(cfg.get("reply.ai.timeout", 20) or 20)
        self.max_tokens = int(cfg.get("reply.ai.max_tokens", 160) or 160)
        key_env = str(cfg.get("reply.ai.api_key_env", "OPENAI_API_KEY") or "OPENAI_API_KEY")
        self.api_key = str(cfg.get("reply.ai.api_key", "") or "") or os.environ.get(key_env, "")

    def available(self) -> bool:
        """True when the engine is switched on and fully configured."""
        return bool(self.enabled and self.endpoint and self.api_key)

    def generate(self, message: str, context: str = "") -> AIResult:
        """Ask the model for a reply. Never raises."""
        if not self.available():
            return AIResult("", False, "ai backend not configured")

        prompt = message.strip()
        if context:
            prompt = f"{prompt}\n\nConversation so far:\n{context}"

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": self.max_tokens,
            "temperature": 0.4,
        }
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "User-Agent": _UA,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            log.warning("ai request failed: %s", exc)
            return AIResult("", False, str(exc))
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            log.warning("ai response could not be parsed: %s", exc)
            return AIResult("", False, f"bad response: {exc}")

        try:
            text = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            log.warning("ai response missing choices[0].message.content")
            return AIResult("", False, "unexpected response shape")
        text = (text or "").strip()
        if not text:
            return AIResult("", False, "empty completion")
        return AIResult(text, True)
