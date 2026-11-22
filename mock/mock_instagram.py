#!/usr/bin/env python3
"""A mock Instagram, faithful enough to exercise the whole bot.

It reproduces the parts of instagram.com the bot actually depends on, using
the structure verified against the live site on 2026-11-22:

* the DM inbox with ``role="tablist"`` / ``role="tab"`` tabs
  (Primary | General | Requests);
* conversation rows as ``div[role="button"]`` whose text is
  ``title\\npreview\\n·\\ntime`` — with no ``href``, exactly like the real one;
* the "What's new" carousel, which uses the same element type but has no ``·``;
* a first-run notification modal;
* conversation pages at ``/direct/t/<key>/`` with message bubbles built as
  ``div[role="presentation"]`` coloured blue (outgoing) or grey (incoming),
  a ``div[role="textbox"][contenteditable="true"]`` composer and a
  ``div[role="button"][aria-label="Send"]`` that only exists once there is text;
* message-request threads that have Accept / Block / Delete and **no composer**
  until accepted.

A small control API lets tests inject inbound messages and read back what was
actually sent.

    python mock/mock_instagram.py --port 8080
"""

from __future__ import annotations

import argparse
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------


class MockState:
    """Everything the mock knows."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.threads: dict[str, dict] = {}
        self.modal_shown = False
        self.seed()

    def seed(self) -> None:
        """Default fixture: one empty Primary tab and one message request."""
        self.threads = {
            "1001": {
                "title": "Auren | Mint",
                "request": False,
                "accepted": True,
                "messages": [
                    {"text": "Hello", "outgoing": True},
                    {"text": "Is the blue one still available?", "outgoing": False},
                ],
            },
            "2002": {
                "title": "🍁ضعني🍁",
                "request": True,
                "accepted": False,
                "messages": [{"text": "Hi", "outgoing": False}],
            },
        }

    def reset(self) -> None:
        with self.lock:
            self.seed()
            self.modal_shown = False

    def rows_for(self, tab: str) -> list[dict]:
        with self.lock:
            out = []
            for key, thread in self.threads.items():
                if thread["request"] and tab != "Requests":
                    continue
                if not thread["request"] and tab == "Requests":
                    continue
                messages = thread["messages"]
                if not messages:
                    continue
                last = messages[-1]
                preview = ("You: " if last["outgoing"] else "") + last["text"]
                out.append(
                    {
                        "key": key,
                        "title": thread["title"],
                        "preview": preview,
                        "time": "2m",
                    }
                )
            return out


# --------------------------------------------------------------------------
# HTML rendering
# --------------------------------------------------------------------------

BASE_CSS = """
* { box-sizing: border-box; }
body { margin:0; font-family: -apple-system, "Segoe UI", Roboto, sans-serif;
       background:#fff; color:#000; }
.tabs { display:flex; border-bottom:1px solid #dbdbdb; }
.tabs button { flex:1; padding:14px 0; border:0; background:none; font-size:15px; cursor:pointer; }
.tabs button[aria-selected="true"] { border-bottom:2px solid #000; font-weight:600; }
.row { display:block; width:100%; padding:12px 16px; cursor:pointer; }
.row:hover { background:#fafafa; }
.carousel { display:flex; gap:12px; padding:16px; overflow:hidden; }
.carousel div[role="button"] { min-width:90px; }
.muted { color:#737373; font-size:13px; }
.msg-area { padding:16px; min-height:60vh; }
.bubble-row { display:flex; margin:6px 0; }
.bubble-row.out { justify-content:flex-end; }
.bubble-row.in { justify-content:flex-start; }
.bubble { max-width:70%; padding:9px 13px; border-radius:18px; font-size:14px; }
.out .bubble { background: rgb(74, 93, 249); color:#fff; }
.in .bubble { background: rgb(243, 245, 247); color:#000; }
.composer { position:sticky; bottom:0; display:flex; gap:8px; padding:10px 16px;
             border-top:1px solid #dbdbdb; background:#fff; }
.composer [role="textbox"] { flex:1; min-height:18px; padding:9px 12px;
                              border:1px solid #dbdbdb; border-radius:20px; outline:none; }
.composer .placeholder { position:absolute; pointer-events:none; color:#8e8e8e; }
.send { display:none; padding:9px 16px; border:0; border-radius:20px;
        background:#0095f6; color:#fff; cursor:pointer; }
.send.show { display:inline-block; }
.modal { position:fixed; inset:0; background:rgba(0,0,0,.6); display:flex;
         align-items:center; justify-content:center; }
.modal .card { background:#fff; border-radius:12px; padding:24px; max-width:360px; }
.modal button { margin-top:16px; padding:10px 18px; border:0; border-radius:8px;
                background:#0095f6; color:#fff; cursor:pointer; }
.modal button.secondary { background:#efefef; color:#000; margin-left:8px; }
.request-actions { position:sticky; bottom:0; display:flex; gap:12px;
                   padding:14px 16px; border-top:1px solid #dbdbdb; }
.request-actions div[role="button"] { padding:10px 18px; border-radius:8px;
                                      background:#efefef; cursor:pointer; }
"""


def esc(text: str) -> str:
    return (
        (text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def render_inbox(state: MockState, tab: str = "Primary") -> str:
    rows = state.rows_for(tab)
    row_html = []
    for row in rows:
        row_html.append(
            f'<div role="button" class="row" onclick="openThread({row["key"]})">'
            f"{esc(row['title'])}<br>{esc(row['preview'])}<br>&nbsp;<br>·<br>{esc(row['time'])}"
            f"</div>"
        )
    if not rows:
        row_html.append(
            '<div class="muted" style="padding:20px 16px">'
            "Chats will appear here after you send or receive a message</div>"
        )

    carousel = (
        '<div class="carousel">'
        '<div role="button">Friday feeling...<br>Your note</div>'
        '<div role="button">Final<br>Abi _de _dancer</div>'
        "</div>"
    )

    modal = ""
    if not state.modal_shown:
        modal = (
            '<div class="modal" id="modal">'
            '<div class="card"><h2>Turn on Notifications</h2>'
            "<p>Know right away when people follow you or like and comment on "
            "your photos.</p>"
            '<button onclick="dismissModal()">Turn On</button>'
            '<button class="secondary" onclick="dismissModal()">Not Now</button>'
            "</div></div>"
        )

    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Instagram</title><style>{BASE_CSS}</style></head>
<body>
<div id="root">
  <headerish></headerish>
  <div class="tabs" role="tablist">
    <button role="tab" aria-selected="{'true' if tab=='Primary' else 'false'}"
            onclick="switchTab('Primary')">Primary</button>
    <button role="tab" aria-selected="{'true' if tab=='General' else 'false'}"
            onclick="switchTab('General')">General</button>
    <button role="tab" aria-selected="{'true' if tab=='Requests' else 'false'}"
            onclick="switchTab('Requests')">Requests</button>
  </div>
  {carousel}
  <div id="rows">{''.join(row_html)}</div>
</div>
{modal}
<script>
function switchTab(name) {{
  window.location.href = '/direct/inbox/?tab=' + encodeURIComponent(name);
}}
function openThread(key) {{ window.location.href = '/direct/t/' + key + '/'; }}
function dismissModal() {{
  var m = document.getElementById('modal'); if (m) m.remove();
  fetch('/_control/modal-seen', {{method:'POST'}});
}}
</script>
</body></html>"""


def render_thread(state: MockState, key: str) -> str:
    with state.lock:
        thread = state.threads.get(key)
        if thread is None:
            return "<html><body>not found</body></html>"
        thread = dict(thread)
        thread["messages"] = list(thread["messages"])

    bubbles = []
    for message in thread["messages"]:
        side = "out" if message["outgoing"] else "in"
        bubbles.append(
            f'<div class="bubble-row {side}">'
            f'<div role="presentation" class="bubble">'
            f"<span><div class=\"bubble\">{esc(message['text'])}</div></span>"
            f"</div></div>"
        )

    if thread["request"] and not thread["accepted"]:
        actions = (
            '<div class="request-actions">'
            '<div role="button">Block</div><div role="button">Delete</div>'
            '<div role="button" onclick="acceptRequest()">Accept</div>'
            "</div>"
        )
        composer = ""
    else:
        actions = ""
        composer = (
            '<div class="composer">'
            '<div style="position:relative; flex:1;">'
            '<div role="textbox" contenteditable="true" id="composer"></div>'
            '<div class="placeholder" id="ph">Message...</div>'
            "</div>"
            '<div role="button" class="send" aria-label="Send" id="send">Send</div>'
            "</div>"
        )

    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Instagram</title><style>{BASE_CSS}</style></head>
<body>
<div id="root">
  <div class="msg-area" id="msgs">{''.join(bubbles)}</div>
  {composer}
  {actions}
</div>
<script>
var THREAD = {key};
var composer = document.getElementById('composer');
var ph = document.getElementById('ph');
var send = document.getElementById('send');

function syncComposer() {{
  var text = composer ? (composer.innerText || '').replace(/\\n/g, ' ').trim() : '';
  if (ph) ph.style.display = text ? 'none' : 'block';
  if (send) send.className = text ? 'send show' : 'send';
}}
if (composer) {{
  composer.addEventListener('input', syncComposer);
  composer.addEventListener('keyup', syncComposer);
}}
if (send) {{
  send.addEventListener('click', function () {{
    var text = (composer.innerText || '').replace(/\\n/g, ' ').trim();
    if (!text) return;
    fetch('/_control/send', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{thread: THREAD, text: text}})
    }}).then(function () {{
      var area = document.getElementById('msgs');
      var row = document.createElement('div');
      row.className = 'bubble-row out';
      row.innerHTML = '<div role="presentation" class="bubble">'
        + '<span><div class="bubble">' + text.replace(/&/g,'&amp;').replace(/</g,'&lt;')
        + '</div></span></div>';
      area.appendChild(row);
      composer.innerText = '';
      syncComposer();
    }});
  }});
}}
function acceptRequest() {{
  fetch('/_control/accept', {{
    method:'POST', headers:{{'Content-Type':'application/json'}},
    body: JSON.stringify({{thread: THREAD}})
  }}).then(function () {{ window.location.reload(); }});
}}
</script>
</body></html>"""


# --------------------------------------------------------------------------
# server
# --------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    state: MockState = MockState()

    def log_message(self, *args) -> None:  # silence the default stderr spam
        pass

    # -------------------------------------------------------------- helpers

    def _send_html(self, html: str, status: int = 200) -> None:
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except ValueError:
            return {}

    # ------------------------------------------------------------------ GET

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path in ("/", "/direct/inbox/"):
            tab = (query.get("tab") or ["Primary"])[0]
            self._send_html(render_inbox(self.state, tab))
            return

        match = re.fullmatch(r"/direct/t/([0-9]+)/?", path)
        if match:
            self._send_html(render_thread(self.state, match.group(1)))
            return

        if path == "/_control/state":
            with self.state.lock:
                self._send_json(
                    {
                        "threads": self.state.threads,
                        "modal_shown": self.state.modal_shown,
                    }
                )
            return

        self._send_html("<html><body>not found</body></html>", status=404)

    # ----------------------------------------------------------------- POST

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        payload = self._body()

        if path == "/_control/reset":
            self.state.reset()
            self._send_json({"ok": True})
            return

        if path == "/_control/modal-seen":
            self.state.modal_shown = True
            self._send_json({"ok": True})
            return

        if path == "/_control/inject":
            key = str(payload.get("thread") or "")
            text = str(payload.get("text") or "")
            outgoing = bool(payload.get("outgoing", False))
            with self.state.lock:
                thread = self.state.threads.setdefault(
                    key, {"title": key, "request": False, "accepted": True, "messages": []}
                )
                thread["messages"].append({"text": text, "outgoing": outgoing})
            self._send_json({"ok": True})
            return

        if path == "/_control/send":
            key = str(payload.get("thread") or "")
            text = str(payload.get("text") or "")
            with self.state.lock:
                thread = self.state.threads.setdefault(
                    key, {"title": key, "request": False, "accepted": True, "messages": []}
                )
                thread["messages"].append({"text": text, "outgoing": True})
            self._send_json({"ok": True})
            return

        if path == "/_control/accept":
            key = str(payload.get("thread") or "")
            with self.state.lock:
                thread = self.state.threads.get(key)
                if thread is not None:
                    thread["accepted"] = True
                    thread["request"] = False
            self._send_json({"ok": True})
            return

        self._send_json({"ok": False, "error": "unknown control endpoint"}, status=404)


def start(port: int = 0, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    """Start the mock server. Returns the server (already running)."""
    server = ThreadingHTTPServer((host, port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    args = ap.parse_args()
    server = start(args.port)
    print(f"mock instagram on http://127.0.0.1:{server.server_address[1]}")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
