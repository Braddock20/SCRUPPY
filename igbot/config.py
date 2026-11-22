"""Configuration loading.

Precedence, lowest to highest:

    built-in defaults
    config.yaml            (shipped example, user-edited)
    config.local.yaml      (gitignored, machine-specific overrides)
    environment variables  (IGBOT_*)
    explicit CLI flags

Everything the user is likely to want to change lives in ``config.yaml``.
"""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml

DEFAULTS: dict[str, Any] = {
    "account": {
        "username": "",
        "cookies_file": "secrets/cookies.txt",
        "cookie_domain": ".instagram.com",
        "device": "mobile",
        "delay_days": 0,
    },
    "browser": {
        "headless": False,
        "slow_mo_ms": 35,
        "profile_dir": "state/profile",
        "proxy": "",
        "locale": "en-US",
        "viewport": {
            "width": 414,
            "height": 863,
            "device_scale_factor": 2.608695652173913,
            "is_mobile": True,
            "has_touch": True,
        },
        "user_agent": (
            "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
        ),
    },
    "urls": {
        "base": "https://www.instagram.com",
        "feed": "/",
        "inbox": "/direct/inbox/",
    },
    "behaviour": {
        "poll_seconds": 45,
        "page_settle_ms": 1000,
        "reply_delay_min": 12,
        "reply_delay_max": 55,
        "cooldown_seconds": 900,
        "hourly_cap": 8,
        "daily_cap": 25,
        "global_daily_cap": 120,
        "baseline_only": False,
        "accept_requests": False,
        "tabs": ["Primary", "Request", "General"],
    },
    "reply": {
        "default_reply": "Thanks for reaching out! We've got your message and will reply shortly.",
        "rules": [],
        "ai": {
            "enabled": False,
            "endpoint": "",
            "api_key_env": "OPENAI_API_KEY",
            "model": "gpt-4o-mini",
            "timeout": 20,
            "max_tokens": 160,
        },
    },
    "publishing": {
        "caption_file": "",
        "media_file": "",
        "media_url": "",
    },
    "logging": {
        "level": "INFO",
        "file": "logs/igbot.log",
    },
}


def _deep_merge(base: dict, over: dict) -> dict:
    """Recursively merge ``over`` into a copy of ``base``."""
    out = copy.deepcopy(base)
    for key, value in (over or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _apply_env(cfg: dict) -> dict:
    """Overlay ``IGBOT_*`` environment variables."""
    env = os.environ

    mapping = {
        "IGBOT_COOKIES_FILE": ("account", "cookies_file"),
        "IGBOT_DEVICE": ("account", "device"),
        "IGBOT_HEADLESS": ("browser", "headless"),
        "IGBOT_PROFILE_DIR": ("browser", "profile_dir"),
        "IGBOT_PROXY": ("browser", "proxy"),
        "IGBOT_POLL_SECONDS": ("behaviour", "poll_seconds"),
        "IGBOT_DEFAULT_REPLY": ("reply", "default_reply"),
        "IGBOT_AI_ENDPOINT": ("reply", "ai", "endpoint"),
        "IGBOT_AI_KEY": ("reply", "ai", "api_key"),
        "IGBOT_AI_MODEL": ("reply", "ai", "model"),
        "IGBOT_LOG_LEVEL": ("logging", "level"),
        "IGBOT_LOG_FILE": ("logging", "file"),
    }
    for var, path in mapping.items():
        if var not in env:
            continue
        raw = env[var]
        node = cfg
        for part in path[:-1]:
            node = node.setdefault(part, {})
        node[path[-1]] = _coerce(raw, node.get(path[-1]))
    return cfg


def _coerce(raw: str, current: Any) -> Any:
    """Coerce a string env value to the type of the current value."""
    if isinstance(current, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(current, int) and not isinstance(current, bool):
        try:
            return int(raw)
        except ValueError:
            return current
    if isinstance(current, float):
        try:
            return float(raw)
        except ValueError:
            return current
    return raw


class Config:
    """Dotted-access wrapper around the merged config dict."""

    def __init__(self, data: dict[str, Any], root: Path):
        self._data = data
        self.root = root

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def get(self, path: str, default: Any = None) -> Any:
        """Fetch a nested value with a dotted path: ``cfg.get('reply.ai.enabled')``."""
        node: Any = self._data
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, path: str, value: Any) -> None:
        """Set a nested value with a dotted path."""
        parts = path.split(".")
        node = self._data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    def resolve(self, path: str) -> Path:
        """Resolve a *configured* path value relative to the project root."""
        p = Path(str(self.get(path, "") or "")).expanduser()
        if not str(p) or str(p) == ".":
            return self.root
        return p if p.is_absolute() else (self.root / p)

    def path(self, *parts: str) -> Path:
        """Build a path under the project root.

        Use this for paths the bot itself owns (the state database, the log
        file) rather than values that come from the config file.
        """
        return self.root.joinpath(*[str(p) for p in parts])

    @property
    def data(self) -> dict[str, Any]:
        return self._data

    def dump(self) -> str:
        """Redacted YAML dump — secrets are masked."""
        import re

        text = yaml.safe_dump(self._data, sort_keys=False, allow_unicode=True)
        return re.sub(r"(?im)^(\s*[^#\n]*(?:key|token|secret|password)[^:\n]*:\s*).+$", r"\1<redacted>", text)


def load(root: str | Path = ".", overrides: dict[str, Any] | None = None) -> Config:
    """Load config from ``root``, merging files and environment."""
    root_path = Path(root).expanduser().resolve()
    data = copy.deepcopy(DEFAULTS)

    for name in ("config.yaml", "config.local.yaml"):
        candidate = root_path / name
        if candidate.exists():
            loaded = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
            if not isinstance(loaded, dict):
                raise ValueError(f"{candidate} must contain a YAML mapping")
            data = _deep_merge(data, loaded)

    data = _apply_env(data)

    if overrides:
        for key, value in overrides.items():
            if value is None:
                continue
            if "." in key:
                cfg = Config(data, root_path)
                cfg.set(key, value)
            else:
                data[key] = value

    return Config(data, root_path)
