#!/usr/bin/env python3
"""Live probe: inspect the real Instagram DOM and the GraphQL traffic behind it.

Run this when selectors need re-pinning after an Instagram redesign. It only
*reads* — it never clicks, never sends, never follows.

    python -m tools.probe_live --out ../probe
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from igbot import config  # noqa: E402
from igbot import log as logmod  # noqa: E402
from igbot.browser import Browser  # noqa: E402

log = logmod.get("probe")

OUTLINE_JS = r"""
() => {
  const interesting = ['a','button','input','textarea','div','span','section','nav','main','header','ul','li','h1','h2','h3','p'];
  const roles = new Set(['button','tab','tablist','grid','list','listitem','textbox','link','img','dialog','menu','gridcell','row','navigation','main','banner','region','article','separator','presentation','none']);
  const out = [];
  function walk(el, depth) {
    if (depth > 14 || out.length > 1400) return;
    for (const child of el.children) {
      const tag = child.tagName.toLowerCase();
      const role = child.getAttribute('role') || '';
      const hasRole = roles.has(role);
      const isInteractive = ['a','button','input','textarea'].includes(tag);
      if (hasRole || isInteractive) {
        const r = child.getBoundingClientRect();
        const text = (child.innerText || child.getAttribute('aria-label') || child.getAttribute('placeholder') || '')
          .replace(/\s+/g,' ').trim().slice(0, 70);
        const href = child.getAttribute('href') || '';
        out.push({
          d: depth,
          tag,
          role: role || '',
          cls: (child.className && typeof child.className === 'string' ? child.className : '').split(/\s+/).filter(Boolean).slice(0,2).join('.'),
          href: href.slice(0, 70),
          text,
          x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height),
          vis: r.width > 0 && r.height > 0,
        });
        walk(child, depth + 1);
      } else {
        walk(child, depth);
      }
    }
  }
  walk(document.body, 0);
  return out;
}
"""

TAG_COUNTER_JS = r"""
() => {
  const tags = {}, roles = {};
  document.querySelectorAll('*').forEach(el => {
    const t = el.tagName.toLowerCase();
    tags[t] = (tags[t] || 0) + 1;
    const r = el.getAttribute('role');
    if (r) roles[r] = (roles[r] || 0) + 1;
  });
  return { tags, roles };
}
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    ap.add_argument("--out", default=str(Path(__file__).resolve().parents[1] / "probe"))
    ap.add_argument("--cookies", default=None)
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--wait", type=float, default=8.0)
    args = ap.parse_args()

    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)

    cfg = config.load(args.root)
    logmod.setup("INFO")

    browser = Browser(cfg, headless=args.headless or None)
    page = browser.launch()

    captured: list[dict] = []

    def on_response(response):
        try:
            url = response.url
            if "/graphql" not in url and "/api/v1" not in url:
                return
            if response.status != 200:
                return
            body = response.text()
            captured.append({"url": url[:400], "len": len(body), "body": body[:400000]})
        except Exception:
            pass

    page.on("response", on_response)

    try:
        cookie_source = args.cookies or str(cfg.resolve("account.cookies_file"))
        count = browser.import_cookies(cookie_source)
        print(f"[probe] imported {count} cookies")

        logged_in = browser.is_logged_in()
        print(f"[probe] is_logged_in = {logged_in}")
        print(f"[probe] ds_user_id   = {browser.session_user_id()}")
        print(f"[probe] handle       = {browser.account_handle()}")

        inbox = str(cfg.get("urls.base", "https://www.instagram.com")).rstrip("/") + "/direct/inbox/"
        page.goto(inbox, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(int(args.wait * 1000))
        print(f"[probe] inbox url    = {page.url}")

        page.screenshot(path=str(out / "inbox.png"), full_page=False)
        print(f"[probe] screenshot   -> {out / 'inbox.png'}")

        counter = page.evaluate(TAG_COUNTER_JS)
        outline = page.evaluate(OUTLINE_JS)

        (out / "outline.json").write_text(json.dumps(outline, indent=1), encoding="utf-8")
        (out / "tags.json").write_text(json.dumps(counter, indent=1), encoding="utf-8")

        print("\n[probe] role histogram:")
        for role, n in sorted(counter["roles"].items(), key=lambda kv: -kv[1]):
            print(f"    {role:<14} {n}")

        print("\n[probe] key structural facts:")
        for fact, value in {
            "has <header>": bool(counter["tags"].get("header")),
            "div[role=grid]": counter["roles"].get("grid", 0),
            "role=tab": counter["roles"].get("tab", 0),
            "role=tablist": counter["roles"].get("tablist", 0),
            "a[href*=/direct/t/]": sum(1 for o in outline if "/direct/t/" in (o.get("href") or "")),
            "role=button": counter["roles"].get("button", 0),
            "role=textbox": counter["roles"].get("textbox", 0),
        }.items():
            print(f"    {fact:<24} {value}")

        print("\n[probe] tabs:")
        for o in outline:
            if o.get("role") == "tab":
                print(f"    text={o['text']!r} vis={o['vis']} x={o['x']} y={o['y']} w={o['w']}")

        print("\n[probe] captured api responses:")
        (out / "responses.json").write_text(json.dumps(captured, indent=1), encoding="utf-8")
        for c in captured:
            print(f"    {c['len']:>8} bytes  {c['url'][:120]}")

        # Find message-shaped payloads.
        print("\n[probe] looking for message-shaped GraphQL payloads...")
        for c in captured:
            try:
                data = json.loads(c["body"])
            except ValueError:
                continue
            found = []

            def walk(node, path=""):
                if isinstance(node, dict):
                    if "text" in node and isinstance(node.get("text"), str):
                        keys = set(node.keys())
                        if keys & {"thread", "thread_id", "user_id", "timestamp", "item_type", "client_context"}:
                            found.append((path, node))
                    for k, v in node.items():
                        walk(v, f"{path}.{k}")
                elif isinstance(node, list):
                    for i, v in enumerate(node[:60]):
                        walk(v, f"{path}[{i}]")

            walk(data)
            if found:
                print(f"    {c['url'][:100]} -> {len(found)} message-shaped nodes")
                for path, node in found[:6]:
                    snippet = {
                        k: node.get(k)
                        for k in ("thread_id", "item_type", "user_id", "timestamp", "text")
                        if k in node
                    }
                    thread = node.get("thread")
                    if isinstance(thread, dict):
                        snippet["thread.id"] = thread.get("id") or thread.get("thread_id")
                    print(f"        {path[:70]} {json.dumps(snippet, default=str)[:190]}")

        return 0
    finally:
        browser.close()


if __name__ == "__main__":
    raise SystemExit(main())
