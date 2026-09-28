"""Local HUD server: the page and one WebSocket (/ws) on 127.0.0.1, in its own thread.

Security (docs/ui-protocol.md, docs/threat-model.md):
- Binds to 127.0.0.1 only; the port is held exclusively so no other program can take it over.
- Host must be 127.0.0.1:<port> (defends against DNS rebinding: a website that points its own name at
  127.0.0.1 still sends its own Host) and the WebSocket Origin must be this page.
- A random session token is put in the served page; the first message must be hello with it, within
  5 s. Wrong token, bad version, or no hello: an error message, then the connection is closed.
- Only a fixed list of files is served (no paths from the request reach the file system), with a
  strict Content-Security-Policy. Messages are limited to 64 KB; at most MAX_CLIENTS pages at once.
- The token and message contents are never logged.
"""

from __future__ import annotations

import asyncio
import hmac
import http
import logging
import secrets
import socket
import threading
import time
import uuid
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from websockets.asyncio.server import ServerConnection, serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response

from jarvis.core.events import EventBus
from jarvis.ui.hub import Hub, send_to
from jarvis.ui.protocol import MAX_MESSAGE_BYTES, ProtocolError, envelope, error_message, parse_client

logger = logging.getLogger(__name__)

HOST = "127.0.0.1"
STATIC_DIR = Path(__file__).parent / "static"
MAX_CLIENTS = 3
HELLO_TIMEOUT_SECONDS = 5.0
RATE_LIMIT = (100, 10.0)  # at most 100 messages per 10 s from one page (it pings every 15 s)
TOKEN_PLACEHOLDER = "{{JARVIS_SESSION_TOKEN}}"
POLICY_VIOLATION = 1008  # WebSocket close code

# URL path -> (file under static/, content type). Nothing else is ever served.
FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/hud.css": ("hud.css", "text/css; charset=utf-8"),
    "/hud.js": ("hud.js", "text/javascript; charset=utf-8"),
    "/fonts/rajdhani-600.woff2": ("fonts/rajdhani-600.woff2", "font/woff2"),
    "/fonts/rajdhani-700.woff2": ("fonts/rajdhani-700.woff2", "font/woff2"),
    "/fonts/ibm-plex-mono-400.woff2": ("fonts/ibm-plex-mono-400.woff2", "font/woff2"),
    "/fonts/ibm-plex-mono-500.woff2": ("fonts/ibm-plex-mono-500.woff2", "font/woff2"),
}


class UIError(Exception):
    """The HUD could not start (e.g. the port is in use). Jarvis carries on without it."""


def _server_version() -> str:
    try:
        return version("jarvis")
    except PackageNotFoundError:
        return "0"


class HudServer:
    def __init__(self, bus: EventBus, port: int, static_dir: Path = STATIC_DIR,
                 hello_timeout: float = HELLO_TIMEOUT_SECONDS) -> None:
        self._bus, self._requested_port, self._static_dir = bus, port, static_dir
        self._hello_timeout = hello_timeout
        self._token = secrets.token_urlsafe(32)
        self._session = uuid.uuid4().hex  # shown to pages; not a secret (unlike the token)
        self._hub: Hub | None = None
        self._port = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._ready = threading.Event()
        self._startup_error: Exception | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self._port

    @property
    def url(self) -> str:
        return f"http://{HOST}:{self._port}/"

    @property
    def token(self) -> str:
        return self._token

    # --- lifecycle (called from Jarvis's main thread) ----------------------------------------------

    def start(self, timeout: float = 5.0) -> None:
        """Start serving in a background thread. Raises UIError if it cannot."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # Windows: no other program may bind this port too
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            sock.bind((HOST, self._requested_port))
        except OSError as e:
            sock.close()
            raise UIError(f"port {self._requested_port} is not available ({e.strerror or e}); "
                          "set another JARVIS_UI_PORT") from None
        self._port = sock.getsockname()[1]
        self._hub = Hub(self._bus)
        self._thread = threading.Thread(target=self._run, args=(sock,), name="hud-server", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout) or self._startup_error is not None:
            self.stop()
            raise UIError(f"the HUD server did not start ({self._startup_error or 'timed out'})")
        logger.info("HUD server listening on %s", self.url)

    def stop(self) -> None:
        if self._loop is not None and self._stop is not None and not self._loop.is_closed():
            try:
                self._loop.call_soon_threadsafe(self._stop.set)
            except RuntimeError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=5)
        if self._hub is not None:
            self._hub.close()

    def _run(self, sock: socket.socket) -> None:
        try:
            asyncio.run(self._main(sock))
        except Exception as e:  # never take Jarvis down
            logger.exception("HUD server stopped with an error")
            self._startup_error = self._startup_error or e
            self._ready.set()

    async def _main(self, sock: socket.socket) -> None:
        assert self._hub is not None
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        self._hub.attach(self._loop)
        async with serve(self._handle, sock=sock, origins=[self._origin], process_request=self._process_request,
                         max_size=MAX_MESSAGE_BYTES, compression=None, server_header=None):
            self._ready.set()
            await self._stop.wait()

    # --- HTTP: page, static files, and checks before the WebSocket upgrade --------------------------

    @property
    def _origin(self) -> str:
        return f"http://{HOST}:{self._port}"

    def _process_request(self, connection: ServerConnection, request: Request) -> Response | None:
        try:
            host = request.headers.get("Host")
            origin = request.headers.get("Origin")
        except Exception:  # e.g. the header sent twice
            return connection.respond(http.HTTPStatus.BAD_REQUEST, "Bad request\n")
        if host != f"{HOST}:{self._port}":
            logger.warning("HUD request refused: unexpected Host header")
            return connection.respond(http.HTTPStatus.FORBIDDEN, "Forbidden\n")
        path = request.path.split("?", 1)[0]
        if path == "/ws":
            if origin != self._origin:
                logger.warning("HUD WebSocket refused: unexpected Origin")
                return connection.respond(http.HTTPStatus.FORBIDDEN, "Forbidden\n")
            if self._hub is not None and self._hub.client_count >= MAX_CLIENTS:
                return connection.respond(http.HTTPStatus.SERVICE_UNAVAILABLE, "Too many HUD pages open\n")
            return None  # go on with the WebSocket handshake
        if path not in FILES:
            return connection.respond(http.HTTPStatus.NOT_FOUND, "Not found\n")
        return self._file_response(*FILES[path])

    def _file_response(self, name: str, content_type: str) -> Response:
        try:
            body = (self._static_dir / name).read_bytes()
        except OSError:
            return Response(404, "Not Found", Headers({"Content-Type": "text/plain"}), b"Not found\n")
        if name == "index.html":
            body = body.replace(TOKEN_PLACEHOLDER.encode(), self._token.encode())
        headers = Headers({
            "Content-Type": content_type,
            "Content-Length": str(len(body)),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": (
                "default-src 'none'; script-src 'self'; style-src 'self'; font-src 'self'; img-src 'self'; "
                # 'self': the page re-reads its token from / after Jarvis restarts (a new token each run).
                f"connect-src 'self' ws://{HOST}:{self._port}; base-uri 'none'; form-action 'none'; "
                "frame-ancestors 'none'"
            ),
        })
        return Response(200, "OK", headers, body)

    # --- WebSocket ---------------------------------------------------------------------------------

    async def _handle(self, connection: ServerConnection) -> None:
        assert self._hub is not None
        if not await self._hello(connection):
            return
        page, snapshot = self._hub.add_client()
        try:
            await connection.send(envelope("hello_ack", {"session": self._session,
                                                         "server_version": _server_version()}))
            await connection.send(snapshot)
            sender = asyncio.create_task(self._send_from(page, connection))
            try:
                await self._receive(page, connection)
            finally:
                sender.cancel()
        except ConnectionClosed:
            pass
        finally:
            self._hub.remove_client(page)

    async def _hello(self, connection: ServerConnection) -> bool:
        """The first message must be a valid hello with this run's token."""
        try:
            raw = await asyncio.wait_for(connection.recv(), self._hello_timeout)
            message = parse_client(raw)
            if message is None or message.type != "hello":
                raise ProtocolError("unauthorized", "The first message must be hello.", fatal=True)
            if not hmac.compare_digest(message.data["token"].encode(), self._token.encode()):
                raise ProtocolError("unauthorized", "Wrong session token. Reload the page.", fatal=True)
            return True
        except TimeoutError:
            await self._refuse(connection, "hello_timeout", "No hello received in time.")
        except ProtocolError as e:
            await self._refuse(connection, e.code if e.code != "bad_message" else "unauthorized", e.message)
        except ConnectionClosed:
            pass
        return False

    async def _refuse(self, connection: ServerConnection, code: str, message: str) -> None:
        logger.warning("HUD connection refused: %s", code)
        try:
            await connection.send(error_message(code, message, recoverable=False))
            await connection.close(POLICY_VIOLATION, code)
        except ConnectionClosed:
            pass

    async def _receive(self, page: asyncio.Queue[str], connection: ServerConnection) -> None:
        limit, window = RATE_LIMIT
        times: list[float] = []
        async for raw in connection:
            now = time.monotonic()
            times = [t for t in times if now - t < window] + [now]
            if len(times) > limit:
                await self._refuse(connection, "rate_limited", "Too many messages.")
                return
            try:
                message = parse_client(raw)
            except ProtocolError as e:
                logger.warning("HUD message rejected: %s", e.code)
                if e.fatal:
                    await self._refuse(connection, e.code, e.message)
                    return
                send_to(page, error_message(e.code, e.message))
                continue
            if message is None:
                logger.info("HUD message ignored (type not handled in this phase)")
            elif message.type == "ping":
                send_to(page, envelope("pong"))
            # a second hello is harmless and ignored

    @staticmethod
    async def _send_from(page: asyncio.Queue[str], connection: ServerConnection) -> None:
        try:
            while True:
                await connection.send(await page.get())
        except ConnectionClosed:
            pass
