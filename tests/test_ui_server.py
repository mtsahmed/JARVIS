"""UI-2B: the local HUD server (a real server on a free port; no network beyond 127.0.0.1, no API calls)."""

import http.client
import json
import re
import time
from collections.abc import Iterator
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.sync.client import ClientConnection, connect

from jarvis.core.events import EventBus
from jarvis.ui import server as server_module
from jarvis.ui.server import STATIC_DIR, HudServer, UIError


@pytest.fixture
def hud() -> Iterator[HudServer]:
    server = HudServer(EventBus(), 0, hello_timeout=0.5)
    server.start()
    yield server
    server.stop()


def open_ws(hud: HudServer, origin: str | None = None) -> ClientConnection:
    return connect(f"ws://127.0.0.1:{hud.port}/ws", origin=origin or f"http://127.0.0.1:{hud.port}",
                   proxy=None, open_timeout=5)


def send(ws: ClientConnection, type: str, data: dict[str, Any] | None = None, **overrides: Any) -> None:
    message = {"v": 1, "type": type, "id": "1", "ts": "2026-09-28T09:41:05Z", "data": data or {}}
    message.update(overrides)
    ws.send(json.dumps(message))


def receive(ws: ClientConnection) -> dict[str, Any]:
    return json.loads(ws.recv(timeout=3))


def hello(ws: ClientConnection, hud: HudServer) -> dict[str, Any]:
    """Say hello; returns the snapshot."""
    send(ws, "hello", {"token": hud.token, "client": "hud"})
    ack = receive(ws)
    assert ack["type"] == "hello_ack" and ack["data"]["session"] and ack["data"]["server_version"]
    snapshot = receive(ws)
    assert snapshot["type"] == "snapshot"
    return snapshot


def refused(ws: ClientConnection) -> str:
    """The server's error code, after which it closes with 1008."""
    error = receive(ws)
    assert error["type"] == "error" and error["data"]["recoverable"] is False
    with pytest.raises(ConnectionClosed):
        ws.recv(timeout=3)
    assert ws.close_code == 1008
    return error["data"]["code"]


def get(hud: HudServer, path: str, host: str | None = None) -> http.client.HTTPResponse:
    conn = http.client.HTTPConnection("127.0.0.1", hud.port, timeout=5)
    conn.putrequest("GET", path, skip_host=True)
    conn.putheader("Host", host or f"127.0.0.1:{hud.port}")
    conn.endheaders()
    return conn.getresponse()


# --- the page and static files ----------------------------------------------------------------------


def test_page_has_this_runs_token_and_strict_headers(hud: HudServer) -> None:
    response = get(hud, "/")
    body = response.read().decode()
    assert response.status == 200 and hud.token in body and "{{JARVIS_SESSION_TOKEN}}" not in body
    csp = response.getheader("Content-Security-Policy")
    assert "default-src 'none'" in csp and "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert f"connect-src 'self' ws://127.0.0.1:{hud.port};" in csp
    assert response.getheader("X-Content-Type-Options") == "nosniff"
    assert response.getheader("Cache-Control") == "no-store"


def test_every_listed_file_is_served_with_its_type(hud: HudServer) -> None:
    for path, (name, content_type) in server_module.FILES.items():
        response = get(hud, path)
        assert response.status == 200, path
        assert response.getheader("Content-Type") == content_type
        assert response.read() != b""


def test_page_uses_only_listed_files_and_nothing_inline_or_remote() -> None:
    page = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    css = (STATIC_DIR / "hud.css").read_text(encoding="utf-8")
    script = (STATIC_DIR / "hud.js").read_text(encoding="utf-8")
    referenced = {"/" + ref for ref in re.findall(r'(?:href|src)="([^"#]+)"', page)}
    referenced |= {"/" + ref for ref in re.findall(r'url\("([^"]+)"\)', css)}
    assert referenced == set(server_module.FILES) - {"/"}  # every file used, nothing unlisted
    # The CSP (default-src 'none'; script-src/style-src 'self') would block these, so they must not exist.
    assert not re.search(r"<script(?![^>]*\bsrc=)|<style|\sstyle=|\son\w+=", page)
    assert "{{JARVIS_SESSION_TOKEN}}" in page
    for text in (page, css, script):
        assert not re.search(r"https?://(?!127\.0\.0\.1)", text.replace("http://www.w3.org/2000/svg", ""))
    assert "setAttribute(\"style\"" not in script and "eval(" not in script and "new Function" not in script


def test_each_run_has_a_new_token() -> None:
    assert HudServer(EventBus(), 0).token != HudServer(EventBus(), 0).token


@pytest.mark.parametrize("path", ["/../.env", "/..%2f..%2f.env", "/index.html/../../.env", "/static/index.html",
                                  "/fonts/../../server.py", "/server.py", "/%2e%2e/%2e%2e/.env", "//etc/passwd"])
def test_only_listed_files_are_served(hud: HudServer, path: str) -> None:
    assert get(hud, path).status == 404


def test_listed_but_missing_file_is_404(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<p>hi</p>", encoding="utf-8")
    server = HudServer(EventBus(), 0, static_dir=tmp_path)
    server.start()
    try:
        assert get(server, "/").status == 200 and get(server, "/hud.css").status == 404
    finally:
        server.stop()


@pytest.mark.parametrize("host", ["evil.example", "localhost:1", "evil.example:80", "127.0.0.1"])
def test_wrong_host_is_refused(hud: HudServer, host: str) -> None:
    assert get(hud, "/", host=host).status == 403  # DNS rebinding defence


def test_static_files_never_use_innerhtml() -> None:
    for path in STATIC_DIR.rglob("*"):
        if path.suffix in (".html", ".js"):
            text = path.read_text(encoding="utf-8")
            assert not re.search(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write", text), path


# --- the WebSocket: who may connect -----------------------------------------------------------------


@pytest.mark.parametrize("origin", ["http://evil.example", "http://localhost:47821", "null"])
def test_wrong_origin_is_refused(hud: HudServer, origin: str) -> None:
    with pytest.raises(InvalidStatus) as error:
        open_ws(hud, origin=origin)
    assert error.value.response.status_code == 403


def test_wrong_token_is_refused(hud: HudServer) -> None:
    with open_ws(hud) as ws:
        send(ws, "hello", {"token": "guess", "client": "hud"})
        assert refused(ws) == "unauthorized"


def test_first_message_must_be_hello(hud: HudServer) -> None:
    with open_ws(hud) as ws:
        send(ws, "ping")
        assert refused(ws) == "unauthorized"


def test_no_hello_in_time_is_refused(hud: HudServer) -> None:
    with open_ws(hud) as ws:
        assert refused(ws) == "hello_timeout"


def test_version_mismatch_is_refused(hud: HudServer) -> None:
    with open_ws(hud) as ws:
        send(ws, "hello", {"token": hud.token, "client": "hud"}, v=2)
        assert refused(ws) == "version_mismatch"


def test_malformed_hello_is_refused(hud: HudServer) -> None:
    with open_ws(hud) as ws:
        ws.send("not json")
        assert refused(ws) == "unauthorized"


def test_at_most_three_pages(hud: HudServer) -> None:
    with ExitStack() as pages:
        first = pages.enter_context(open_ws(hud))
        for ws in [first, pages.enter_context(open_ws(hud)), pages.enter_context(open_ws(hud))]:
            hello(ws, hud)
        with pytest.raises(InvalidStatus) as error:
            open_ws(hud)
        assert error.value.response.status_code == 503
        first.close()
        for _ in range(50):  # the server notices the closed page
            try:
                with open_ws(hud):
                    break
            except InvalidStatus:
                time.sleep(0.05)
        else:
            pytest.fail("a closed page's place was not freed")


def test_message_size_limit(hud: HudServer) -> None:
    with open_ws(hud) as ws:
        hello(ws, hud)
        ws.send("x" * (64 * 1024 + 1))
        with pytest.raises(ConnectionClosed):
            ws.recv(timeout=3)
        assert ws.close_code == 1009  # message too big


def test_flooding_is_cut_off(hud: HudServer, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server_module, "RATE_LIMIT", (5, 10.0))
    with open_ws(hud) as ws:
        hello(ws, hud)
        for _ in range(6):
            send(ws, "ping")
        codes = []
        with pytest.raises(ConnectionClosed):
            while True:
                message = receive(ws)
                codes.append(message["data"].get("code") if message["type"] == "error" else message["type"])
        assert codes[-1] == "rate_limited" and ws.close_code == 1008


# --- the WebSocket: after hello ---------------------------------------------------------------------


def test_ping_pong_bad_messages_and_later_types(hud: HudServer) -> None:
    with open_ws(hud) as ws:
        hello(ws, hud)
        send(ws, "ping")
        assert receive(ws)["type"] == "pong"
        ws.send("not json")  # reported, but the page stays connected
        assert receive(ws)["data"]["code"] == "bad_message"
        send(ws, "text_input", {"text": "delete everything"})  # UI-3 type: ignored in UI-2
        send(ws, "ptt", {"action": "start"})
        send(ws, "ping")
        assert receive(ws)["type"] == "pong"


def test_bus_events_reach_the_page(hud: HudServer) -> None:
    bus = hud._bus
    with open_ws(hud) as ws:
        hello(ws, hud)
        bus.state("listening")
        bus.transcript("what's the weather")
        bus.reply("<script>alert(1)</script> Sunny.", [{"title": "Weather", "url": "https://w.example"},
                                                        {"title": "Bad", "url": "javascript:alert(1)"}])
        bus.error("speech_recognition", "Could not reach speech recognition.")
        got = [receive(ws) for _ in range(4)]
    assert [m["type"] for m in got] == ["state", "transcript", "reply", "error"]
    assert got[0]["data"] == {"mode": "listening", "detail": ""}
    assert got[1]["data"] == {"text": "what's the weather", "final": True}
    # Text is passed on as text (the page shows it with textContent); only https links are kept.
    assert got[2]["data"] == {"text": "<script>alert(1)</script> Sunny.",
                              "sources": [{"title": "Weather", "url": "https://w.example"}], "kind": "answer"}
    assert got[3]["data"]["code"] == "speech_recognition"


def test_snapshot_catches_up_without_repeats(hud: HudServer) -> None:
    bus = hud._bus
    bus.state("thinking")
    bus.transcript("hello")
    bus.reply("Hi.")
    with open_ws(hud) as ws:
        snapshot = hello(ws, hud)["data"]
        assert snapshot["mode"] == "thinking" and snapshot["last_user"] == "hello"
        assert snapshot["last_reply"]["text"] == "Hi."
        assert [(e["role"], e["text"]) for e in snapshot["log"]] == [("user", "hello"), ("jarvis", "Hi.")]
        assert snapshot["reminders"] == [] and snapshot["system"] == {}  # UI-4
        bus.state("idle")
        assert receive(ws)["data"]["mode"] == "idle"  # the next message is new, not a repeat


def test_log_keeps_last_50(hud: HudServer) -> None:
    bus = hud._bus
    for i in range(60):
        bus.transcript(f"q{i}")
    with open_ws(hud) as ws:
        log = hello(ws, hud)["data"]["log"]
    assert len(log) == 50 and log[-1]["text"] == "q59"


def test_token_never_logged(hud: HudServer, caplog: pytest.LogCaptureFixture) -> None:
    with open_ws(hud) as ws:
        send(ws, "hello", {"token": hud.token + "x", "client": "hud"})
        refused(ws)
    with open_ws(hud) as ws:
        hello(ws, hud)
    assert hud.token not in caplog.text


# --- starting and stopping -------------------------------------------------------------------------


def test_port_in_use_is_a_clear_error(hud: HudServer) -> None:
    with pytest.raises(UIError, match=f"port {hud.port} is not available"):
        HudServer(EventBus(), hud.port).start()


def test_start_hud_from_config(capsys: pytest.CaptureFixture[str]) -> None:
    from jarvis import cli

    assert cli.start_hud(SimpleNamespace(ui_enabled=False, ui_port=47821), EventBus()) is None
    busy = HudServer(EventBus(), 0)
    busy.start()
    try:
        assert cli.start_hud(SimpleNamespace(ui_enabled=True, ui_port=busy.port), EventBus()) is None
        assert "HUD unavailable" in capsys.readouterr().out  # Jarvis carries on without it
    finally:
        busy.stop()


def test_ui_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from jarvis.core.config import _KEYS, ConfigError, load_config

    for key in _KEYS:
        monkeypatch.delenv(key, raising=False)
    base = ("OPENAI_API_KEY=sk-x\nJARVIS_LLM_PROVIDER=openai\nJARVIS_LLM_MODEL=m\nJARVIS_MAX_OUTPUT_TOKENS=1\n"
            "JARVIS_LLM_TIMEOUT_SECONDS=1\nJARVIS_HISTORY_TURNS=1\n")
    env = tmp_path / ".env"
    env.write_text(base, encoding="utf-8")
    config = load_config(project_root=tmp_path)
    assert (config.ui_enabled, config.ui_port) == (False, 47821)  # off unless turned on
    env.write_text(base + "JARVIS_UI_ENABLED=true\nJARVIS_UI_PORT=50000\n", encoding="utf-8")
    config = load_config(project_root=tmp_path)
    assert (config.ui_enabled, config.ui_port) == (True, 50000)
    for bad in ("JARVIS_UI_ENABLED=maybe", "JARVIS_UI_PORT=80", "JARVIS_UI_PORT=70000", "JARVIS_UI_PORT=abc"):
        env.write_text(base + bad + "\n", encoding="utf-8")
        with pytest.raises(ConfigError, match=bad.split("=")[0]):
            load_config(project_root=tmp_path)
