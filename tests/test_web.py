"""Tests for the Render HTTP control surface."""
import json
import threading
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from igbot.web import AutomationManager, ControlHandler


def _serve(root, token=""):
    manager = AutomationManager(root)
    handler = type("TestHandler", (ControlHandler,), {
        "manager": manager,
        "control_token": token,
    })
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return manager, server, thread, f"http://127.0.0.1:{server.server_port}"


def test_health_and_status_endpoints(tmp_path, monkeypatch):
    monkeypatch.delenv("IGBOT_COOKIE", raising=False)
    monkeypatch.delenv("IGBOT_COOKIES_FILE", raising=False)
    (tmp_path / "config.yaml").write_text("behaviour:\n  baseline_only: true\n", encoding="utf-8")
    _, server, thread, base = _serve(tmp_path)
    try:
        with urlopen(base + "/health") as response:
            assert response.status == 200
            assert json.loads(response.read()) == {"status": "ok"}
        with urlopen(base + "/status") as response:
            status = json.loads(response.read())
            assert status["service"] == "online"
            assert status["automation"] == "stopped"
            assert status["session_configured"] is False
            assert status["baseline_only"] is True
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_control_routes_require_token(tmp_path):
    _, server, thread, base = _serve(tmp_path, token="secret-test-token")
    try:
        request = Request(base + "/automation/start", method="POST")
        try:
            urlopen(request)
            assert False, "expected authorization failure"
        except HTTPError as exc:
            assert exc.code == 401
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_control_routes_disabled_without_token(tmp_path):
    _, server, thread, base = _serve(tmp_path, token="")
    try:
        request = Request(base + "/automation/start", method="POST")
        try:
            urlopen(request)
            assert False, "expected disabled control API"
        except HTTPError as exc:
            assert exc.code == 503
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
