"""The reply rulebook.

Deterministic, offline matching. Every rule is a list of trigger phrases and a
reply. Rules are evaluated most-specific-first so that "shipping and returns"
wins over "returns".

Matching is **word-boundary aware**. An earlier version matched raw substrings,
which meant the keyword ``hi`` fired on *"shipping"* and *"which"* — two
completely unrelated customer questions that would have been answered with a
greeting. That bug is pinned down by a regression test.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable


@dataclass
class Rule:
    """One entry in the rulebook."""

    match: list[str]
    reply: str
    name: str = ""
    priority: int = 0

    @property
    def label(self) -> str:
        return self.name or ", ".join(self.match[:2])

    def keywords(self) -> list[str]:
        return [m.strip().lower() for m in self.match if str(m).strip()]

    def specificity(self) -> int:
        """Longest keyword wins ties — longer phrases are more specific."""
        lengths = [len(k) for k in self.keywords()]
        return max(lengths) if lengths else 0

    def matches(self, text: str) -> bool:
        """True if any keyword appears in ``text`` on word boundaries."""
        haystack = (text or "").lower()
        if not haystack.strip():
            return False
        for keyword in self.keywords():
            pattern = r"(?<![\w])" + re.escape(keyword) + r"(?![\w])"
            if re.search(pattern, haystack):
                return True
        return False


def load_rules(raw: Iterable[dict]) -> list[Rule]:
    """Build rules from config, sorted most-specific-first."""
    rules: list[Rule] = []
    for index, entry in enumerate(raw or []):
        if not isinstance(entry, dict):
            continue
        match = entry.get("match") or entry.get("matches") or entry.get("keywords") or []
        if isinstance(match, str):
            match = [match]
        reply = entry.get("reply") or entry.get("response") or ""
        if not reply:
            continue
        rules.append(
            Rule(
                match=[str(m) for m in match],
                reply=str(reply),
                name=str(entry.get("name") or ""),
                priority=int(entry.get("priority") or 0),
            )
        )
    rules.sort(key=lambda r: (-r.priority, -r.specificity()))
    return rules


class Rulebook:
    """Matches an inbound message against the configured rules."""

    def __init__(self, rules: Iterable[Rule]):
        self.rules = list(rules)

    @classmethod
    def from_config(cls, cfg) -> "Rulebook":
        return cls(load_rules(cfg.get("reply.rules", []) or []))

    def match(self, text: str) -> Rule | None:
        """Return the first (most specific) matching rule, or None."""
        for rule in self.rules:
            if rule.matches(text):
                return rule
        return None

    def reply_for(self, text: str, default: str) -> tuple[str, str]:
        """Resolve a reply for ``text``.

        Returns ``(reply, source)`` where source is ``"rule:<label>"``,
        ``"default"``, or ``"none"``.
        """
        rule = self.match(text)
        if rule is not None:
            return rule.reply, f"rule:{rule.label}"
        if default:
            return default, "default"
        return "", "none"

    def __len__(self) -> int:
        return len(self.rules)
