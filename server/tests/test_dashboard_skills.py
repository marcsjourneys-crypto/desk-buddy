"""The dashboard's Skills API on a real loopback socket: the scan, approving (with a fake
`uv sync`, never the real one), turning a skill off, the usual Host/Origin/token checks,
no secret in any response, and apps or bookmarks that would take an approved skill's
spoken name."""

import asyncio
import contextlib
import json
import threading
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from test_dashboard import SECRETS, TOKEN, call, free_port

from desk_buddy.apps_store import AppsStore
from desk_buddy.dashboard import build_status, create_dashboard, start_dashboard
from desk_buddy.events import EventBus
from desk_buddy.personality_store import PersonalityStore
from desk_buddy.settings_store import LiveSettings, SettingsStore
from desk_buddy.skills_store import MAX_APPROVED, SkillsStore
from desk_buddy.tools.skills import SkillRunner, SyncResult

UV = r"C:\Tools\uv.exe"
SKILL_SECRET = "SECRET_SHOULD_NOT_LEAK=planted-skill-secret-0005"
WINDOWS_APPS = [{"Name": "Notepad", "AppID": "Notepad"}]


@pytest.fixture(autouse=True)
def _no_real_app_data(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))  # never the real one


def manifest(name="Hello time", spoken=("hello time",), **more):
    return {"name": name, "spoken_names": list(spoken), "description": "Says the time.", **more}


def make_skill(root: Path, slug: str, data=None, *, project=False) -> Path:
    folder = root / slug
    folder.mkdir(parents=True)
    raw = data if isinstance(data, str) else json.dumps(data or manifest())
    (folder / "skill.json").write_text(raw, encoding="utf-8")
    (folder / "main.py").write_text("print('hi')\n", encoding="utf-8")
    (folder / ".env").write_text(SKILL_SECRET + "\n", encoding="utf-8")
    if project:
        (folder / "pyproject.toml").write_text(
            '[project]\nname = "x"\nversion = "0"\n', encoding="utf-8"
        )
    return folder


@dataclass
class FakeSync:
    """Stands in for run_sync: records each call; writes uv.lock unless told not to."""

    ok: bool = True
    lock: bool = True
    output: str = ""
    gate: threading.Event | None = None
    during: object = None  # called while the sync runs
    started: threading.Event = field(default_factory=threading.Event)
    calls: list = field(default_factory=list)

    def __call__(self, uv, folder, env, **kwargs):
        self.calls.append((uv, Path(folder), dict(env)))
        self.started.set()
        if self.during is not None:
            self.during()
        if self.gate is not None:
            assert self.gate.wait(10)
        if not self.ok:
            return SyncResult(False, "The setup (uv sync) failed (exit 2).", self.output)
        if self.lock:
            (Path(folder) / "uv.lock").write_text("version = 1\n", encoding="utf-8")
        return SyncResult(True)


@dataclass
class Dash:
    port: int
    root: Path
    skills: SkillsStore
    apps: AppsStore
    sync: FakeSync
    live: LiveSettings
    runner: SkillRunner | None

    @property
    def base(self):
        return f"http://127.0.0.1:{self.port}"

    @property
    def origin(self):
        return f"http://127.0.0.1:{self.port}"

    def headers(self, **extra):
        return {"Origin": self.origin, "X-CARL-Token": TOKEN, **extra}

    async def get(self, **headers):
        status, _, body = await call(
            "GET", self.base + "/api/skills", headers=headers or {"X-CARL-Token": TOKEN}
        )
        return status, json.loads(body)

    async def post(self, slug, what="approve", headers=None):
        status, _, body = await call(
            "POST",
            f"{self.base}/api/skills/{slug}/{what}",
            body={},
            headers=self.headers() if headers is None else headers,
        )
        return status, json.loads(body), body


@contextlib.asynccontextmanager
async def running(tmp_path, *, sync=None, uv=True, skills_dir=True):
    root = tmp_path / "skills"
    root.mkdir(parents=True)
    env = tmp_path / ".env"
    lines = [
        f"ANTHROPIC_API_KEY={SECRETS[0]}",
        f"ROBOT_TOKEN={SECRETS[1]}",
        f"SPOTIFY_CLIENT_ID={SECRETS[2]}",
        f"YOUTUBE_API_KEY={SECRETS[3]}",
        "TTS_ENGINE=sapi",
    ]
    if skills_dir:
        lines.append(f"SKILLS_DIR={root}")
    env.write_text("\n".join(lines) + "\n", encoding="utf-8")
    settings = SettingsStore(tmp_path / "settings.json", env_path=env, environ={})
    live = LiveSettings(settings.settings())
    apps = AppsStore(
        tmp_path / "apps.json",
        tmp_path / "bookmarks.json",
        runner=lambda args, timeout: json.dumps(WINDOWS_APPS).encode(),
    )
    skills = SkillsStore(tmp_path / "skills.json")
    runner = SkillRunner(UV, tmp_path / "appdata" / "desk-buddy") if uv else None
    sync = sync or FakeSync()
    port = free_port()
    app = create_dashboard(
        port=port,
        token=TOKEN,
        settings=settings,
        live=live,
        personality=PersonalityStore(tmp_path / "personality.json"),
        bus=EventBus(),
        status=lambda: build_status(
            live.current, voice="sapi", tools=(), robot=None, state="idle", dashboard_port=port
        ),
        preview=lambda: "prompt",
        apps=apps,
        skills=skills,
        skill_runner=runner,
        skill_sync=sync,
    )
    server = await start_dashboard(app, port)
    assert server is not None
    try:
        yield Dash(port, root, skills, apps, sync, live, runner)
    finally:
        await asyncio.wait_for(server.stop(), 5)


def by_slug(view):
    return {s["slug"]: s for s in view["skills"]}


# --- the round trip ---


def test_scan_approve_change_and_disable(tmp_path):
    async def run():
        async with running(tmp_path) as d:
            make_skill(d.root, "hello-time")
            make_skill(
                d.root,
                "send-note",
                manifest(
                    "Send note",
                    ("send note", "email the team"),
                    needs_yes=True,
                    timeout_seconds=60,
                    inputs={
                        "choices": {"tone": {"options": ["short", "long"], "default": "short"}},
                        "text": {"description": "What to say", "max_length": 200},
                    },
                ),
                project=True,
            )
            status, view = await d.get()
            assert status == 200, view
            assert view["enabled"] is True and view["error"] is None
            assert view["uv_found"] is True and view["limits"] == {"approved": MAX_APPROVED}
            skills = by_slug(view)
            assert skills["hello-time"] == {
                "slug": "hello-time",
                "name": "Hello time",
                "description": "Says the time.",
                "spoken_names": ["hello time"],
                "needs_yes": False,
                "timeout_seconds": 30,
                "choices": [],
                "text": None,
                "status": "available",
                "approved": False,
                "errors": [],
            }
            send = skills["send-note"]
            assert send["needs_yes"] is True and send["timeout_seconds"] == 60
            assert send["choices"] == [
                {"name": "tone", "options": ["short", "long"], "default": "short"}
            ]
            assert send["text"] == {
                "description": "What to say",
                "max_length": 200,
                "required": False,
            }

            # No project: no sync.
            version = d.skills.version
            status, view, _ = await d.post("hello-time")
            assert status == 200, view
            assert by_slug(view)["hello-time"]["status"] == "approved"
            assert d.sync.calls == []
            assert d.skills.version == version + 1  # CARL rebuilds at the next question

            # A project: `uv sync` in its folder, with the clean environment.
            status, view, _ = await d.post("send-note")
            assert status == 200, view
            assert by_slug(view)["send-note"]["status"] == "approved"
            [(uv, folder, env)] = d.sync.calls
            assert uv == UV and folder == d.root / "send-note"
            assert env["UV_PROJECT_ENVIRONMENT"].endswith(r"skill-envs\send-note")
            assert "ANTHROPIC_API_KEY" not in env and "ROBOT_TOKEN" not in env
            # The fingerprint is taken after the sync: uv.lock is covered.
            assert (d.root / "send-note" / "uv.lock").is_file()
            assert by_slug((await d.get())[1])["send-note"]["status"] == "approved"

            # Edit a file: changed, until approved again.
            (d.root / "hello-time" / "main.py").write_text("print('new')\n", encoding="utf-8")
            skills = by_slug((await d.get())[1])
            assert skills["hello-time"]["status"] == "changed"
            assert skills["hello-time"]["approved"] is True
            status, view, _ = await d.post("hello-time")
            assert status == 200 and by_slug(view)["hello-time"]["status"] == "approved"
            status, body, _ = await d.post("hello-time")  # on already: nothing to do
            assert status == 400 and body["error"] == "It's already on."

            # Turn it off.
            status, view, _ = await d.post("hello-time", "disable")
            assert status == 200, view
            assert by_slug(view)["hello-time"]["status"] == "available"
            assert "hello-time" not in d.skills.approvals()
            status, view, _ = await d.post("hello-time", "disable")  # already off: fine
            assert status == 200

    asyncio.run(run())


def test_the_view_follows_a_skills_dir_saved_a_moment_ago(tmp_path):
    async def run():
        async with running(tmp_path, skills_dir=False) as d:
            status, view = await d.get()
            assert status == 200 and view["enabled"] is False and view["skills"] == []
            status, body, _ = await d.post("hello-time")
            assert status == 400 and body["error"] == "Set SKILLS_DIR on the Settings tab first."
            make_skill(d.root, "hello-time")
            status, _, body = await call(
                "PUT",
                d.base + "/api/settings",
                body={"SKILLS_DIR": str(d.root)},
                headers=d.headers(),
            )
            assert status == 200, body
            # Not applied yet (that's at the next question), but the page shows it.
            assert d.live.current.skills_dir == ""
            status, view = await d.get()
            assert view["enabled"] is True and by_slug(view)["hello-time"]

    asyncio.run(run())


def test_a_missing_skills_dir_is_a_banner(tmp_path):
    async def run():
        async with running(tmp_path) as d:
            d.root.rmdir()
            status, view = await d.get()
            assert status == 200
            assert view["enabled"] is True and "isn't an existing folder" in view["error"]
            status, body, _ = await d.post("hello-time")
            assert status == 400 and "isn't an existing folder" in body["error"]

    asyncio.run(run())


def test_a_corrupt_approvals_file_is_a_banner(tmp_path):
    (tmp_path / "skills.json").write_text("{broken", encoding="utf-8")

    async def run():
        async with running(tmp_path) as d:
            return (await d.get())[1]

    view = asyncio.run(run())
    assert "skills.json" in view["error"]


# --- what approving refuses ---


def test_a_failed_sync_shows_its_output_and_approves_nothing(tmp_path):
    output = "x" * 1500 + "error: no solution found for requests>=99"
    tail = output[-1000:]

    async def run():
        async with running(tmp_path, sync=FakeSync(ok=False, output=tail)) as d:
            make_skill(d.root, "send-note", project=True)
            status, body, _ = await d.post("send-note")
            assert status == 400
            assert body["error"] == "The setup (uv sync) failed (exit 2)."
            assert body["output"].endswith("no solution found for requests>=99")
            assert d.skills.approvals() == {}
            assert by_slug((await d.get())[1])["send-note"]["status"] == "available"

    asyncio.run(run())


def test_no_uv_lock_after_the_sync_is_refused(tmp_path):
    async def run():
        async with running(tmp_path, sync=FakeSync(lock=False)) as d:
            make_skill(d.root, "send-note", project=True)
            status, body, _ = await d.post("send-note")
            assert status == 400 and "uv.lock" in body["error"]
            assert d.skills.approvals() == {}

    asyncio.run(run())


def test_a_skill_inside_another_uv_project_is_refused_before_any_sync(tmp_path):
    async def run():
        async with running(tmp_path) as d:
            make_skill(d.root, "send-note", project=True)
            (d.root / "pyproject.toml").write_text(
                '[tool.uv.workspace]\nmembers = ["*"]\n', encoding="utf-8"
            )
            status, body, _ = await d.post("send-note")
            assert status == 400
            assert body["error"] == f"The skill is inside another uv project ({d.root})."
            assert d.sync.calls == [] and d.skills.approvals() == {}

    asyncio.run(run())


def test_an_invalid_skill_is_refused_and_shows_why(tmp_path):
    async def run():
        async with running(tmp_path) as d:
            make_skill(d.root, "broken", manifest(timeout_seconds=500, colour="red"))
            make_skill(d.root, "Bad_Name")
            view = (await d.get())[1]
            broken = by_slug(view)["broken"]
            assert broken["status"] == "invalid" and broken["name"] is None
            fields = {e["field"] for e in broken["errors"]}
            assert {"timeout_seconds", "colour"} <= fields
            assert by_slug(view)["Bad_Name"]["errors"][0]["field"] == "folder"
            status, body, _ = await d.post("broken")
            assert status == 400 and "timeout_seconds" in body["error"]
            # A bad slug is refused before the file system is touched.
            status, body, _ = await d.post("Bad_Name")
            assert status == 400 and "folder name" in body["error"]
            status, body, _ = await d.post("no-such-skill")
            assert status == 404
            assert d.skills.approvals() == {} and d.sync.calls == []

    asyncio.run(run())


def test_the_21st_approval_is_refused(tmp_path):
    async def run():
        async with running(tmp_path) as d:
            for i in range(MAX_APPROVED):
                d.skills.approve(f"other-{i}", "a" * 64)
            make_skill(d.root, "hello-time")
            status, body, _ = await d.post("hello-time")
            assert status == 400 and f"At most {MAX_APPROVED}" in body["error"]
            assert "hello-time" not in d.skills.approvals()
            # Re-approving one that's on but changed is fine.
            make_skill(d.root, "other-0", manifest("Other", ("other",)))
            status, body, _ = await d.post("other-0")
            assert status == 200, body

    asyncio.run(run())


def test_one_approval_at_a_time_and_no_disable_meanwhile(tmp_path):
    gate = threading.Event()
    sync = FakeSync(gate=gate)

    async def run():
        async with running(tmp_path, sync=sync) as d:
            make_skill(d.root, "send-note", project=True)
            make_skill(d.root, "hello-time", manifest("Hi", ("hi",)))
            make_skill(d.root, "other", manifest("Other", ("other",)))
            d.skills.approve("other", "c" * 64)
            first = asyncio.create_task(d.post("send-note"))
            try:
                assert await asyncio.to_thread(sync.started.wait, 5)
                # The event loop isn't blocked by the sync: other routes still answer.
                assert (await d.get())[0] == 200
                status, body, _ = await d.post("hello-time")
                assert status == 409 and "Another skill is being set up" in body["error"]
                status, body, _ = await d.post("other", "disable")
                assert status == 409 and "Another skill is being set up" in body["error"]
                assert "other" in d.skills.approvals()
            finally:
                gate.set()
            status, body, _ = await first
            assert status == 200, body
            assert set(d.skills.approvals()) == {"send-note", "other"}
            status, body, _ = await d.post("other", "disable")  # free again
            assert status == 200, body

    asyncio.run(run())


def test_no_setup_while_a_skill_is_running(tmp_path):
    async def run():
        async with running(tmp_path) as d:
            make_skill(d.root, "send-note", project=True)
            runner = d.runner
            assert runner._lock.acquire()  # a skill run in progress
            try:
                status, body, _ = await d.post("send-note")
            finally:
                runner._lock.release()
            assert status == 409 and "A skill is running" in body["error"]
            assert d.sync.calls == [] and d.skills.approvals() == {}
            status, body, _ = await d.post("send-note")
            assert status == 200, body

    asyncio.run(run())


def test_a_clash_or_edit_during_the_sync_is_refused(tmp_path):
    holder = {}

    def bookmark_takes_the_name():
        assert (
            holder["d"].apps.update_bookmarks([{"name": "send note", "url": "https://x.example"}])
            == {}
        )

    async def run():
        async with running(tmp_path, sync=FakeSync(during=bookmark_takes_the_name)) as d:
            holder["d"] = d
            make_skill(d.root, "send-note", manifest("Send", ("send note",)), project=True)
            status, body, _ = await d.post("send-note")
            assert status == 400 and "already used by an app or bookmark" in body["error"]
            assert d.skills.approvals() == {}

        def bad_edit():
            (holder["d"].root / "edit-me" / "skill.json").write_text("{", encoding="utf-8")

        async with running(tmp_path / "2", sync=FakeSync(during=bad_edit)) as d:
            holder["d"] = d
            make_skill(d.root, "edit-me", project=True)
            status, body, _ = await d.post("edit-me")
            assert status == 400 and "skill.json" in body["error"]
            assert d.skills.approvals() == {}

    asyncio.run(run())


def test_without_uv_nothing_can_be_approved(tmp_path):
    async def run():
        async with running(tmp_path, uv=False) as d:
            make_skill(d.root, "hello-time")
            status, view = await d.get()
            assert status == 200 and view["uv_found"] is False
            status, body, _ = await d.post("hello-time")
            assert status == 503 and "uv wasn't found" in body["error"]
            assert d.skills.approvals() == {}

    asyncio.run(run())


# --- Host, Origin and the token ---


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"X-CARL-Token": "wrong"},
        {"Origin": "ORIGIN"},
        {"Origin": "http://evil.example", "X-CARL-Token": TOKEN},
        {"Origin": "null", "X-CARL-Token": TOKEN},
        {"Host": "evil.example:PORT", "X-CARL-Token": TOKEN, "Origin": "ORIGIN"},
    ],
    ids=["none", "wrong-token", "origin-only", "evil-origin", "null-origin", "evil-host"],
)
def test_every_skills_route_needs_our_host_origin_and_token(tmp_path, headers):
    async def run():
        async with running(tmp_path) as d:
            make_skill(d.root, "hello-time")
            make_skill(d.root, "send-note", manifest("Send", ("send",)), project=True)
            d.skills.approve("send-note", "b" * 64)
            sent = {
                k: v.replace("ORIGIN", d.origin).replace("PORT", str(d.port))
                for k, v in headers.items()
            }
            results = [
                await call("GET", d.base + "/api/skills", headers=sent),
                await call(
                    "POST", d.base + "/api/skills/hello-time/approve", body={}, headers=sent
                ),
                await call("POST", d.base + "/api/skills/send-note/disable", body={}, headers=sent),
            ]
            assert [r[0] for r in results] == [403, 403, 403]
            for _, _, body in results:
                assert "hello time" not in body
            assert d.skills.approvals() == {"send-note": "b" * 64}
            assert d.sync.calls == []

    asyncio.run(run())


# --- no secrets ---


def test_no_secret_ever_appears_in_a_skills_response(tmp_path):
    async def run():
        bodies = []
        async with running(tmp_path) as d:
            make_skill(d.root, "hello-time")
            make_skill(d.root, "send-note", manifest("Send", ("send",)), project=True)
            make_skill(d.root, "broken", "{not json")
            (d.root / "hello-time" / "notes.txt").write_text(SKILL_SECRET, encoding="utf-8")
            bodies.append(json.dumps((await d.get())[1]))
            for slug in ("hello-time", "send-note", "broken"):
                bodies.append((await d.post(slug))[2])
            bodies.append((await d.post("hello-time", "disable"))[2])
            bodies.append(json.dumps((await d.get())[1]))
            fingerprints = list(d.skills.approvals().values())
        async with running(tmp_path / "2", sync=FakeSync(ok=False, output="failed")) as d:
            make_skill(d.root, "send-note", project=True)
            bodies.append((await d.post("send-note"))[2])
        return "\n".join(bodies), fingerprints

    text, fingerprints = asyncio.run(run())
    assert "Hello time" in text  # the responses did carry the skills
    for secret in (*SECRETS, "planted-skill-secret-0005", "SECRET_SHOULD_NOT_LEAK", "print("):
        assert secret not in text
    assert fingerprints
    for fp in fingerprints:
        assert fp not in text


# --- names apps and bookmarks can't take ---


def test_apps_and_bookmarks_cant_take_an_approved_skills_name(tmp_path):
    async def run():
        async with running(tmp_path) as d:
            make_skill(d.root, "order-check", manifest("Order check", ("orders", "order check")))
            make_skill(d.root, "hello-time")  # not approved: its name is free to take
            assert (await d.post("order-check"))[0] == 200
            d.apps.available()

            async def put(path, body):
                status, _, text = await call("PUT", d.base + path, body=body, headers=d.headers())
                return status, json.loads(text)

            status, body = await put(
                "/api/bookmarks",
                {
                    "bookmarks": [
                        {"name": "news", "url": "https://news.example"},
                        {"name": " Orders ", "url": "https://shop.example"},
                    ]
                },
            )
            assert status == 400
            assert set(body["errors"]) == {"bookmarks.1"}
            assert "Order check (order-check)" in body["errors"]["bookmarks.1"]
            assert d.apps.snapshot()[2] == ()  # nothing saved
            status, body = await put(
                "/api/apps", {"apps": [{"app_id": "Notepad", "names": ["pad", "Order Check"]}]}
            )
            assert status == 400 and "order-check" in body["errors"]["apps.0"]
            assert d.apps.snapshot()[1] == ()

            # Names no approved skill holds still save.
            status, body = await put(
                "/api/bookmarks",
                {"bookmarks": [{"name": "hello time", "url": "https://h.example"}]},
            )
            assert status == 200, body
            status, body = await put(
                "/api/apps", {"apps": [{"app_id": "Notepad", "names": ["pad"]}]}
            )
            assert status == 200, body

            # A changed skill still holds its names; once off, they're free.
            (d.root / "order-check" / "main.py").write_text("print(2)\n", encoding="utf-8")
            status, body = await put(
                "/api/apps", {"apps": [{"app_id": "Notepad", "names": ["orders"]}]}
            )
            assert status == 400
            assert (await d.post("order-check", "disable"))[0] == 200
            status, body = await put(
                "/api/apps", {"apps": [{"app_id": "Notepad", "names": ["orders"]}]}
            )
            assert status == 200, body

            # Now the app holds it: the skill shows the clash and can't be approved.
            skill = by_slug((await d.get())[1])["order-check"]
            assert skill["status"] == "invalid"
            assert any("already used by an app" in e["reason"] for e in skill["errors"])
            status, _, _ = await d.post("order-check")
            assert status == 400

    asyncio.run(run())
