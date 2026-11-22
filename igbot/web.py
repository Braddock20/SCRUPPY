"""Small HTTP control surface for running igbot on Render as a Web Service.

Routes:
  GET  /health             public process health check
  GET  /status             public, secret-free automation status
  POST /automation/start   requires Bearer IGBOT_CONTROL_TOKEN
  POST /automation/stop    requires Bearer IGBOT_CONTROL_TOKEN

The automation process runs separately from the HTTP server so Render can
probe the web service while the CLI watch loop remains active.
"""
from __future__ import annotations

import hmac
import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import config as config_mod


class AutomationManager:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self._lock = threading.RLock()
        self._process: subprocess.Popen | None = None
        self._started_at: str | None = None
        self._last_exit_code: int | None = None
        self._last_error: str | None = None

    def has_cookie_source(self) -> bool:
        if os.environ.get("IGBOT_COOKIE", "").strip():
            return True
        cfg = config_mod.load(self.root)
        return cfg.resolve("account.cookies_file").is_file()

    def start(self) -> tuple[bool, str]:
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                return True, "already_running"
            if not self.has_cookie_source():
                self._last_error = "No Instagram session cookie is configured."
                return False, "missing_session_cookie"

            command = [
                sys.executable, "-m", "igbot",
                "--root", str(self.root),
                "--headless", "watch",
            ]
            try:
                # Keep the worker's output in the configured project log.
                cfg = config_mod.load(self.root)
                log_path = cfg.resolve("logging.file")
                log_path.parent.mkdir(parents=True, exist_ok=True)
                log_handle = log_path.open("a", encoding="utf-8")
                try:
                    self._process = subprocess.Popen(
                        command,
                        cwd=str(self.root),
                        stdin=subprocess.DEVNULL,
                        stdout=log_handle,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                        close_fds=True,
                    )
                finally:
                    log_handle.close()
                self._started_at = datetime.now(timezone.utc).isoformat()
                self._last_exit_code = None
                self._last_error = None
                return True, "started"
            except Exception as exc:
                self._last_error = f"Could not start automation: {type(exc).__name__}: {exc}"
                self._process = None
                return False, "start_failed"

    def stop(self) -> tuple[bool, str]:
        with self._lock:
            proc = self._process
            if proc is None or proc.poll() is not None:
                if proc is not None:
                    self._last_exit_code = proc.returncode
                return True, "already_stopped"
            try:
                # The worker has its own process group; stop its browser children too.
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(timeout=5)
                self._last_exit_code = proc.returncode
                return True, "stopped"
            except ProcessLookupError:
                self._last_exit_code = proc.poll()
                return True, "already_stopped"
            except Exception as exc:
                self._last_error = f"Could not stop automation: {type(exc).__name__}: {exc}"
                return False, "stop_failed"

    def status(self) -> dict[str, Any]:
        with self._lock:
            running = self._process is not None and self._process.poll() is None
            if self._process is not None and not running:
                self._last_exit_code = self._process.returncode
            cfg = config_mod.load(self.root)
            return {
                "service": "online",
                "automation": "running" if running else "stopped",
                "session_configured": self.has_cookie_source(),
                "baseline_only": bool(cfg.get("behaviour.baseline_only", True)),
                "started_at": self._started_at,
                "last_exit_code": self._last_exit_code,
                "last_error": self._last_error,
            }


class ControlHandler(BaseHTTPRequestHandler):
    manager: AutomationManager
    control_token: str

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if not self.control_token:
            self._send(503, {"error": "control_api_disabled", "message": "Set IGBOT_CONTROL_TOKEN to enable remote controls."})
            return False
        supplied = self.headers.get("Authorization", "")
        expected = f"Bearer {self.control_token}"
        if not hmac.compare_digest(supplied, expected):
            self._send(401, {"error": "unauthorized"})
            return False
        return True

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path.rstrip("/") or "/"
        if path in ("/health", "/healthz"):
            self._send(200, {"status": "ok"})
        elif path == "/status":
            self._send(200, self.manager.status())
        elif path == "/":
            self._send(200, {
                "name": "SCRUPPY",
                "status": "online",
                "endpoints": ["/health", "/status", "/automation/start", "/automation/stop"],
            })
        else:
            self._send(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path.rstrip("/")
        if path not in ("/automation/start", "/automation/stop"):
            self._send(404, {"error": "not_found"})
            return
        if not self._authorized():
            return
        if path.endswith("/start"):
            ok, result = self.manager.start()
        else:
            ok, result = self.manager.stop()
        self._send(202 if ok else 409, {"result": result, **self.manager.status()})

    def log_message(self, fmt: str, *args: Any) -> None:
        # Render captures stdout; keep request logs concise and avoid headers.
        print(f"[http] {self.address_string()} {fmt % args}", flush=True)


def main() -> int:
    root = Path(os.environ.get("IGBOT_ROOT", Path(__file__).resolve().parents[1])).resolve()
    host = "0.0.0.0"
    port = int(os.environ.get("PORT", "10000"))
    manager = AutomationManager(root)

    handler = type("SCRUPPYControlHandler", (ControlHandler,), {
        "manager": manager,
        "control_token": os.environ.get("IGBOT_CONTROL_TOKEN", ""),
    })
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True

    autostart = os.environ.get("IGBOT_AUTOSTART", "true").strip().lower() not in ("0", "false", "no", "off")
    print(f"SCRUPPY web service listening on {host}:{port}", flush=True)
    if autostart:
        ok, result = manager.start()
        print(f"automation startup: {result}", flush=True)
        if not ok:
            print("Automation is not running; configure a valid session cookie, then POST /automation/start.", flush=True)

    def request_shutdown(signum, frame):  # pragma: no cover - exercised by Render
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        manager.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
