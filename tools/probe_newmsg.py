#!/usr/bin/env python3
"""Open a self-thread via the 'New message' composer."""
from __future__ import annotations
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from igbot import config
from igbot import log as logmod
from igbot.browser import Browser

logmod.setup("INFO")
out = Path(sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parents[1] / "probe"))
out.mkdir(parents=True, exist_ok=True)
cfg = config.load(str(Path(__file__).resolve().parents[1]))
b = Browser(cfg, headless=False)
page = b.launch()
b.import_cookies(str(cfg.resolve("account.cookies_file")))
b.is_logged_in()
handle = b.account_handle() or "typical_me.11"

page.goto("https://www.instagram.com/direct/inbox/", wait_until="domcontentloaded", timeout=60000)
page.wait_for_timeout(7000)
for lbl in ["Not Now", "Later", "Close"]:
    try:
        btn = page.get_by_role("button", name=lbl, exact=True)
        if btn.count() and btn.first.is_visible():
            btn.first.click(timeout=2000); page.wait_for_timeout(1200); break
    except Exception: pass

print("=== click 'New message' ===")
nm = page.locator('[aria-label="New message"]')
print("count:", nm.count())
nm.first.click(timeout=6000)
page.wait_for_timeout(3000)
page.screenshot(path=str(out / "newmsg.png"))
print("url:", page.url)

# Find the recipient input
inputs = page.locator('input[placeholder], textarea[placeholder], [contenteditable="true"], [role="textbox"]')
print("inputs:", inputs.count())
for i in range(inputs.count()):
    try:
        ph = inputs.nth(i).get_attribute("placeholder") or ""
        al = inputs.nth(i).get_attribute("aria-label") or ""
        print(f"   [{i}] tag={inputs.nth(i).evaluate('e=>e.tagName')} ph={ph!r} aria={al!r}")
    except Exception as e:
        print("   err", e)

target = inputs.first
target.fill(handle)
page.wait_for_timeout(3500)
page.screenshot(path=str(out / "newmsg_results.png"))
print("\nresults:")
cands = page.locator('div[role="button"], li[role="option"], [role="option"], a[role="link"]')
for i in range(min(cands.count(), 20)):
    try:
        t = cands.nth(i).inner_text(timeout=1200).strip()
    except Exception:
        continue
    if t:
        print(f"   [{i}] {t[:70]!r}")

# pick the one that is literally our handle
for i in range(cands.count()):
    try:
        t = cands.nth(i).inner_text(timeout=1200).strip()
    except Exception:
        continue
    if any(ln.strip().lower() == handle.lower() for ln in t.split("\n")):
        print(f"\nclicking [{i}] {t[:50]!r}")
        cands.nth(i).click(timeout=6000)
        break
else:
    print("exact handle not found; clicking first visible result")
    try:
        cands.first.click(timeout=6000)
    except Exception as e:
        print("click failed:", e)

page.wait_for_timeout(3000)
try:
    nxt = page.get_by_role("button", name="Next", exact=False)
    if nxt.count() and nxt.first.is_visible():
        print("clicking Next")
        nxt.first.click(timeout=6000)
        page.wait_for_timeout(6000)
except Exception as e:
    print("next failed:", e)
print("url:", page.url)
page.screenshot(path=str(out / "selfthread.png"))

DEEP = r"""
() => {
  const out=[]; const seen=new Set();
  function walk(n,d){ if(d>40||out.length>3000) return;
    for (const c of (n.children||[])) { if(seen.has(c))continue; seen.add(c);
      const r=c.getBoundingClientRect();
      const own=Array.from(c.childNodes).filter(x=>x.nodeType===3).map(x=>x.nodeValue.trim()).join(' ').trim();
      out.push({d,tag:c.tagName.toLowerCase(),role:c.getAttribute('role')||'',ce:c.getAttribute('contenteditable')||'',
        aria:(c.getAttribute&&(c.getAttribute('aria-label')||''))||'',ph:(c.getAttribute&&(c.getAttribute('placeholder')||''))||'',
        href:(c.getAttribute&&(c.getAttribute('href')||''))||'',own:own.slice(0,60),txt:(c.innerText||'').replace(/\s+/g,' ').trim().slice(0,80),
        x:Math.round(r.x),y:Math.round(r.y),w:Math.round(r.width),h:Math.round(r.height)});
      walk(c,d+1); if(c.shadowRoot)walk(c.shadowRoot,d+1);} }
  walk(document.body,0); return out; }
"""
data = page.evaluate(DEEP)
(out/"selfthread.json").write_text(json.dumps(data, indent=1), encoding="utf-8")
print("\n--- editable / textbox ---")
for x in data:
    if x["ce"] in ("true","plaintext-only") or x["tag"]=="textarea" or x["role"]=="textbox":
        print(f"  d={x['d']:<3} {x['tag']:<8} role={x['role']:<9} ce={x['ce']:<14} x={x['x']:<5} y={x['y']:<5} w={x['w']:<5} h={x['h']:<4} aria={x['aria'][:34]!r} ph={x['ph'][:24]!r}")
print("\n--- aria labels ---")
for l in sorted({x["aria"] for x in data if x["aria"]}): print("   ", l)
b.close()
