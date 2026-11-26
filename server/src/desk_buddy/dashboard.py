"""The dashboard: a local web page for CARL (live events, settings, personality, the apps
and websites CARL may open).

It listens on 127.0.0.1 only (DASHBOARD_PORT, default 8766), in CARL's own event loop.
Nothing off this PC can connect, so there's no password, but a web page in the user's own
browser could still try to reach it, so:

- every request must carry our Host (127.0.0.1:<port> or localhost:<port>), which stops
  DNS rebinding from reading anything;
- every change (PUT/POST/DELETE) must carry our Origin and the X-CARL-Token header, a random
  per-process token put into the page we serve (CSRF);
- listing Windows' apps (it starts PowerShell) and listing the skills (it reads the
  skill folders) need the X-CARL-Token header too;
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
import threading
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, Response

import desk_buddy
from desk_buddy import skills_setup
from desk_buddy.apps_store import AppsStore, ListError
from desk_buddy.config import ConfigError, Settings
from desk_buddy.events import EventBus
from desk_buddy.personality_store import PersonalityStore
from desk_buddy.server import RunningServer, ServerStartError, start_server
from desk_buddy.settings_store import LiveSettings, SettingsStore
from desk_buddy.skills_store import SkillsStore, taken_names
from desk_buddy.tools.skills import SkillRunner, run_sync

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
    computer_control: bool | None = None,
) -> dict[str, Any]:
    """What the page's status bar shows. Whether things are on, never their secrets.
    `computer_control` is what's in force now (turning it off applies at once)."""
    names = set(tools)
    if computer_control is None:
        computer_control = settings.computer_control
    return {
        "version": desk_buddy.__version__,
        "state": state,
        "voice": voice,
        "notes": "take_note" in names,
        "music": "play_music" in names and computer_control,
        "computer_control": computer_control,
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
    apps: AppsStore | None = None,
    skills: SkillsStore | None = None,
    skill_runner: SkillRunner | None = None,
    skill_sync: skills_setup.Sync = run_sync,
) -> FastAPI:
    """The dashboard app. Without `apps`, there is no Apps API; without `skills`, no
    Skills API. `skill_runner` is None when uv wasn't found (skills can't be approved);
    `skill_sync` runs `uv sync` (a fake in tests)."""
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
            if request.method in CHANGES:
                await _read_capped(request)  # read it, so the connection closes cleanly
            return _refused("Unknown host.")
        if request.method in CHANGES:
            try:
                length = int(request.headers.get("content-length") or 0)
            except ValueError:
                return JSONResponse({"error": "Bad Content-Length."}, status_code=400)
            if length > MAX_BODY or length < 0:
                return JSONResponse({"error": "Too big."}, status_code=413)
            sent = request.headers.get("x-carl-token", "").encode()
            ours = request.headers.get("origin") in origins
            if not (ours and hmac.compare_digest(sent, token.encode())):
                await _read_capped(request)  # read it, so the connection closes cleanly
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
        if isinstance(changes, Response):
            return changes
        try:
            new, errors = await asyncio.to_thread(settings.update, changes)
        except (ConfigError, OSError) as exc:
            return _store_failed(exc)
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
        except (ConfigError, OSError) as exc:
            return _store_failed(exc)
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
        if isinstance(changes, Response):
            return changes
        try:
            _, errors = await asyncio.to_thread(personality.update, changes)
        except OSError as exc:
            return _store_failed(exc)
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
        except OSError as exc:
            return _store_failed(exc)
        log.info("Dashboard: personality %s reset", item)
        return JSONResponse(await asyncio.to_thread(personality_view))

    # --- apps and bookmarks ---

    def other_names() -> frozenset[str]:
        """The spoken names apps and bookmarks use: a skill can't take them."""
        if apps is None:
            return frozenset()
        _, chosen, sites = apps.snapshot()
        return taken_names(chosen, sites)

    def skill_clashes(
        items: Any, names_of: Callable[[Any], list[Any]], prefix: str
    ) -> dict[str, str]:
        """Errors for apps or bookmarks that take an approved skill's spoken name."""
        if skills is None:
            return {}
        owners = skills_setup.approved_names(skills, live.latest.skills_dir, other_names())
        return skills_setup.name_clashes(items, names_of, prefix, owners)

    if apps is not None:

        @app.get("/api/apps/available")
        async def get_available(request: Request) -> Response:
            # It starts PowerShell, so it needs the page's token like a change does
            # (a foreign page can't send that header without a preflight we never allow).
            sent = request.headers.get("x-carl-token", "").encode()
            if not hmac.compare_digest(sent, token.encode()):
                return _refused("Only the dashboard page can list the apps.")
            try:
                listing = await asyncio.to_thread(apps.available)
            except ListError as exc:
                log.warning("Dashboard: couldn't list the apps: %s", exc)
                return JSONResponse({"error": str(exc)}, status_code=503)
            return JSONResponse({"apps": listing})

        @app.get("/api/apps")
        async def get_apps() -> dict[str, Any]:
            return apps.apps_view()

        @app.put("/api/apps")
        async def put_apps(request: Request) -> Response:
            body = await _json_object(request)
            if isinstance(body, Response):
                return body
            clashes = await asyncio.to_thread(skill_clashes, body.get("apps"), _app_names, "apps")
            if clashes:
                return JSONResponse({"errors": clashes}, status_code=400)
            try:
                errors = await asyncio.to_thread(apps.update_apps, body.get("apps"))
            except OSError as exc:
                return _store_failed(exc)
            if errors:
                return JSONResponse({"errors": errors}, status_code=400)
            return JSONResponse(apps.apps_view())

        @app.get("/api/bookmarks")
        async def get_bookmarks() -> dict[str, Any]:
            return apps.bookmarks_view()

        @app.put("/api/bookmarks")
        async def put_bookmarks(request: Request) -> Response:
            body = await _json_object(request)
            if isinstance(body, Response):
                return body
            clashes = await asyncio.to_thread(
                skill_clashes, body.get("bookmarks"), _bookmark_names, "bookmarks"
            )
            if clashes:
                return JSONResponse({"errors": clashes}, status_code=400)
            try:
                errors = await asyncio.to_thread(apps.update_bookmarks, body.get("bookmarks"))
            except OSError as exc:
                return _store_failed(exc)
            if errors:
                return JSONResponse({"errors": errors}, status_code=400)
            return JSONResponse(apps.bookmarks_view())

    # --- skills ---

    if skills is not None:
        # One setup at a time, held by the worker thread for the whole approval (its
        # `uv sync` included); turning a skill off waits for none and is refused meanwhile.
        setting_up = threading.Lock()

        def skills_view() -> dict[str, Any]:
            return skills_setup.skills_view(
                skills,
                live.latest.skills_dir,
                other_names(),
                uv_found=skill_runner is not None,
            )

        @app.get("/api/skills")
        async def get_skills(request: Request) -> Response:
            # It reads the skill folders, so it needs the page's token like a change.
            sent = request.headers.get("x-carl-token", "").encode()
            origin = request.headers.get("origin")
            if not hmac.compare_digest(sent, token.encode()) or (
                origin is not None and origin not in origins
            ):
                return _refused("Only the dashboard page can list the skills.")
            try:
                view = await asyncio.to_thread(skills_view)
            except OSError as exc:
                return JSONResponse(
                    {"error": f"Couldn't read the skills ({type(exc).__name__})."}, status_code=503
                )
            return JSONResponse(view)

        @app.post("/api/skills/{slug}/approve")
        async def approve_skill(slug: str, request: Request) -> Response:
            await _read_capped(request)  # no body is needed; read it, so it closes cleanly
            # Off the event loop: `uv sync` can take minutes, and the voice loop must
            # never wait for it.
            try:
                outcome = await asyncio.to_thread(
                    skills_setup.approve,
                    skills,
                    skill_runner,
                    live.latest.skills_dir,
                    slug,
                    other_names,
                    lock=setting_up,
                    sync=skill_sync,
                )
            except OSError as exc:
                return _store_failed(exc)
            return JSONResponse(outcome.body, status_code=outcome.status)

        @app.post("/api/skills/{slug}/disable")
        async def disable_skill(slug: str, request: Request) -> Response:
            await _read_capped(request)
            try:
                outcome = await asyncio.to_thread(
                    skills_setup.disable,
                    skills,
                    live.latest.skills_dir,
                    slug,
                    other_names,
                    lock=setting_up,
                    uv_found=skill_runner is not None,
                )
            except OSError as exc:
                return _store_failed(exc)
            return JSONResponse(outcome.body, status_code=outcome.status)

    # --- live events ---

    @app.websocket("/api/events")
    async def events(ws: WebSocket) -> None:
        if ws.headers.get("host") not in hosts or ws.headers.get("origin") not in origins:
            await ws.close(code=1008)  # before accept: the browser gets a 403
            return
        await ws.accept()
        queue, history = bus.subscribe()
        receiver = asyncio.create_task(_until_closed(ws))
        getter: asyncio.Task[Any] | None = None
        try:
            for event in history:
                await ws.send_json(event)
            while not receiver.done():
                getter = asyncio.create_task(queue.get())
                done, _ = await asyncio.wait(
                    {getter, receiver}, return_when=asyncio.FIRST_COMPLETED
                )
                if getter not in done:
                    break
                await ws.send_json(getter.result())
        except Exception:
            pass  # the page went away
        finally:
            bus.unsubscribe(queue)
            receiver.cancel()
            if getter is not None:
                getter.cancel()  # never leave a queue.get() pending
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


def _app_names(item: Any) -> list[Any]:
    names = item.get("names")
    return names if isinstance(names, list) else []


def _bookmark_names(item: Any) -> list[Any]:
    return [item.get("name")]


def _refused(message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=403)


def _store_failed(exc: Exception) -> JSONResponse:
    """A save that failed (the disk, or settings that no longer load): a JSON error the
    page can show, never a 500."""
    log.warning("Dashboard: couldn't save: %s", exc)
    if isinstance(exc, ConfigError):
        return JSONResponse({"error": f"Couldn't save: {exc}"}, status_code=400)
    return JSONResponse({"error": f"Couldn't save: {type(exc).__name__}"}, status_code=503)


async def _read_capped(request: Request) -> bytes | None:
    """The body, read as it streams in; None once it passes MAX_BODY (whatever
    Content-Length said, e.g. a chunked body)."""
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY:
            return None
    return bytes(body)


async def _json_object(request: Request) -> dict[str, Any] | JSONResponse:
    body = await _read_capped(request)
    if body is None:
        return JSONResponse({"error": "Too big."}, status_code=413)
    try:
        data = json.loads(body or b"null")
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return JSONResponse({"error": "Send a JSON object."}, status_code=400)
    return data


async def _until_closed(ws: WebSocket) -> None:
    """The page sends nothing; this ends when it goes away."""
    while True:
        message = await ws.receive()
        if message["type"] == "websocket.disconnect":
            return
