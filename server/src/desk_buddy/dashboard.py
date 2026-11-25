"""The dashboard: a local web page for CARL (live events, settings, personality).

It listens on 127.0.0.1 only (DASHBOARD_PORT, default 8766), in CARL's own event loop.
Nothing off this PC can connect, so there's no password, but a web page in the user's own
browser could still try to reach it, so:

- every request must carry our Host (127.0.0.1:<port> or localhost:<port>), which stops
  DNS rebinding from reading anything;
- every change (PUT/DELETE) must carry our Origin and the X-CARL-Token header, a random
  per-process token put into the page we serve (CSRF);
- the event WebSocket must carry our Origin;
- anything else gets a 403.

No secret ever goes out: the settings view only has editable, non-secret keys; the status
only says whether things are on. Text from CARL's world (transcripts, Spotify names) is
sent as JSON strings, and the page only ever renders it with textContent.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, Response

import desk_buddy
from desk_buddy.config import Settings
from desk_buddy.events import EventBus
from desk_buddy.personality_store import PersonalityStore
from desk_buddy.server import RunningServer, ServerStartError, start_server
from desk_buddy.settings_store import LiveSettings, SettingsStore

log = logging.getLogger(__name__)

HOST = "127.0.0.1"
STATIC_DIR = Path(__file__).with_name("dashboard_static")
STATIC_FILES = {"app.js": "text/javascript; charset=utf-8", "style.css": "text/css; charset=utf-8"}
TOKEN_PLACEHOLDER = "__CARL_TOKEN__"
MAX_BODY = 64 * 1024
CHANGES = ("PUT", "POST", "DELETE", "PATCH")


def build_status(
    settings: Settings,
    *,
    voice: str,
    tools: Iterable[str],
    robot: Any,
    state: str,
    dashboard_port: int,
) -> dict[str, Any]:
    """What the page's status bar shows. Whether things are on, never their secrets."""
    names = set(tools)
    return {
        "version": desk_buddy.__version__,
        "state": state,
        "voice": voice,
        "notes": "take_note" in names,
        "music": "play_music" in names,
        "computer_control": settings.computer_control,
        "robot": {
            "enabled": bool(settings.robot_token),
            "connected": robot is not None,
            "fw_version": getattr(robot, "fw_version", None) or None,
        },
        "dashboard": f"http://{HOST}:{dashboard_port}",
    }


def create_dashboard(
    *,
    port: int,
    token: str,
    settings: SettingsStore,
    live: LiveSettings,
    personality: PersonalityStore,
    bus: EventBus,
    status: Callable[[], dict[str, Any]],
    preview: Callable[[], str],
) -> FastAPI:
    app = FastAPI(title="CARL dashboard", docs_url=None, redoc_url=None, openapi_url=None)
    hosts = {f"{HOST}:{port}", f"localhost:{port}"}
    origins = {f"http://{h}" for h in hosts}
    csp = (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        f"connect-src 'self' ws://{HOST}:{port} ws://localhost:{port}; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'none'; object-src 'none'"
    )

    @app.middleware("http")
    async def guard(request: Request, call_next: Callable[[Request], Any]) -> Response:
        if request.headers.get("host") not in hosts:
            return _refused("Unknown host.")
        if request.method in CHANGES:
            if int(request.headers.get("content-length") or 0) > MAX_BODY:
                return JSONResponse({"error": "Too big."}, status_code=413)
            sent = request.headers.get("x-carl-token", "").encode()
            ours = request.headers.get("origin") in origins
            if not (ours and hmac.compare_digest(sent, token.encode())):
                await request.body()  # read it, so the connection closes cleanly
                return _refused("Changes must come from the dashboard page.")
        response: Response = await call_next(request)
        response.headers["Content-Security-Policy"] = csp
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    # --- the page ---

    @app.get("/")
    async def page() -> HTMLResponse:
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(html.replace(TOKEN_PLACEHOLDER, token))

    @app.get("/static/{name}")
    async def static(name: str) -> Response:
        if name not in STATIC_FILES:  # a fixed list: no paths, no traversal
            return JSONResponse({"error": "Not found."}, status_code=404)
        body = (STATIC_DIR / name).read_bytes()
        return Response(body, media_type=STATIC_FILES[name])

    # --- status ---

    @app.get("/api/status")
    async def get_status() -> dict[str, Any]:
        return status()

    # --- settings ---

    def settings_view() -> dict[str, Any]:
        return {"settings": settings.view(), "error": settings.error}

    @app.get("/api/settings")
    async def get_settings() -> dict[str, Any]:
        return await asyncio.to_thread(settings_view)

    @app.put("/api/settings")
    async def put_settings(request: Request) -> Response:
        changes = await _json_object(request)
        if changes is None:
            return JSONResponse({"error": "Send a JSON object."}, status_code=400)
        new, errors = await asyncio.to_thread(settings.update, changes)
        if errors:
            return JSONResponse({"errors": errors}, status_code=400)
        live.propose(new)
        log.info("Dashboard: settings changed (%s)", ", ".join(sorted(changes)))
        return JSONResponse(await asyncio.to_thread(settings_view))

    @app.delete("/api/settings/{key}")
    async def reset_setting(key: str) -> Response:
        try:
            new = await asyncio.to_thread(settings.reset, key)
        except KeyError:
            return JSONResponse({"error": "No such setting."}, status_code=404)
        live.propose(new)
        log.info("Dashboard: %s reset", key)
        return JSONResponse(await asyncio.to_thread(settings_view))

    # --- personality ---

    def personality_view() -> dict[str, Any]:
        return {**personality.view(), "preview": preview()}

    @app.get("/api/personality")
    async def get_personality() -> dict[str, Any]:
        return await asyncio.to_thread(personality_view)

    @app.put("/api/personality")
    async def put_personality(request: Request) -> Response:
        changes = await _json_object(request)
        if changes is None:
            return JSONResponse({"error": "Send a JSON object."}, status_code=400)
        _, errors = await asyncio.to_thread(personality.update, changes)
        if errors:
            return JSONResponse({"errors": errors}, status_code=400)
        log.info("Dashboard: personality changed")
        return JSONResponse(await asyncio.to_thread(personality_view))

    @app.delete("/api/personality/{item}")
    async def reset_personality(item: str) -> Response:
        try:
            await asyncio.to_thread(personality.reset, item)
        except KeyError:
            return JSONResponse({"error": "No such item."}, status_code=404)
        log.info("Dashboard: personality %s reset", item)
        return JSONResponse(await asyncio.to_thread(personality_view))

    # --- live events ---

    @app.websocket("/api/events")
    async def events(ws: WebSocket) -> None:
        if ws.headers.get("host") not in hosts or ws.headers.get("origin") not in origins:
            await ws.close(code=1008)  # before accept: the browser gets a 403
            return
        await ws.accept()
        queue, history = bus.subscribe()
        receiver = asyncio.create_task(_until_closed(ws))
        try:
            for event in history:
                await ws.send_json(event)
            while not receiver.done():
                getter = asyncio.create_task(queue.get())
                done, _ = await asyncio.wait(
                    {getter, receiver}, return_when=asyncio.FIRST_COMPLETED
                )
                if getter not in done:
                    getter.cancel()
                    break
                await ws.send_json(getter.result())
        except Exception:
            pass  # the page went away
        finally:
            bus.unsubscribe(queue)
            receiver.cancel()
            with contextlib.suppress(Exception):
                await ws.close()

    return app


async def start_dashboard(app: FastAPI, port: int) -> RunningServer | None:
    """Serve the dashboard on 127.0.0.1:port. Never fatal: if it can't listen, CARL runs
    without it."""
    try:
        server = await start_server(app, HOST, port, name="Dashboard")
    except ServerStartError as exc:
        log.warning("Dashboard: off (%s)", exc)
        return None
    log.info("Dashboard: http://%s:%d", HOST, server.port)
    return server


def _refused(message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=403)


async def _json_object(request: Request) -> dict[str, Any] | None:
    body = await request.body()
    if len(body) > MAX_BODY:
        return None
    try:
        data = json.loads(body or b"null")
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


async def _until_closed(ws: WebSocket) -> None:
    """The page sends nothing; this ends when it goes away."""
    while True:
        message = await ws.receive()
        if message["type"] == "websocket.disconnect":
            return
