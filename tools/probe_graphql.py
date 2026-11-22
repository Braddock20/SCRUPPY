#!/usr/bin/env python3
"""Correlate /api/graphql request payloads with their response bodies."""
from __future__ import annotations
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from igbot import config
from igbot import log as logmod
from igbot.browser import Browser

logmod.setup("WARNING")
out = Path(sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parents[1] / "probe"))
out.mkdir(parents=True, exist_ok=True)

cfg = config.load(str(Path(__file__).resolve().parents[1]))
b = Browser(cfg, headless=False)
page = b.launch()
b.import_cookies(str(cfg.resolve("account.cookies_file")))
b.is_logged_in()

pending = {}
records = []

def on_request(req):
    if "/graphql" in req.url:
        try:
            pd = req.post_data or ""
        except Exception:
            pd = ""
        pending[id(req)] = pd

def on_response(resp):
    if "/graphql" not in resp.url:
        return
    try:
        body = resp.text()
    except Exception:
        return
    records.append({"url": resp.url[:200], "len": len(body), "body": body})

page.on("request", on_request)
page.on("response", on_response)

page.goto("https://www.instagram.com/direct/inbox/", wait_until="domcontentloaded", timeout=60000)
page.wait_for_timeout(10000)

print(f"captured {len(records)} graphql responses\n")
for i, r in enumerate(records):
    print(f"--- [{i}] {r['len']} bytes  {r['url'][:110]}")
    try:
        d = json.loads(r["body"])
    except Exception as e:
        print("     not json:", e); continue
    data = d.get("data") or {}
    if not isinstance(data, dict):
        print("     data:", type(data).__name__, str(data)[:200]); continue
    for k, v in data.items():
        if isinstance(v, dict):
            sub = list(v.keys())[:9]
            print(f"     data.{k}: dict keys={sub}")
        elif isinstance(v, list):
            print(f"     data.{k}: list len={len(v)}")
            if v and isinstance(v[0], dict):
                print(f"        [0] keys={list(v[0].keys())[:12]}")
                print(f"        [0] = {json.dumps(v[0], default=str)[:260]}")
        else:
            print(f"     data.{k} = {str(v)[:120]}")

(out/"graphql_responses.json").write_text(json.dumps(records, indent=1), encoding="utf-8")
b.close()
