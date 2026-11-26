"""The dashboard's side of skills: the view of SKILLS_DIR for the page, approving a skill
(with its `uv sync`) and turning one off, and the spoken names approved skills hold.

Everything here blocks (it reads files, and approving may run `uv sync` for minutes):
the dashboard calls it on a worker thread, never on the event loop. Nothing here reads a
skill's .env, and no response carries file contents or fingerprints; the only output
shown is the tail of a failed `uv sync`.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from desk_buddy.apps_store import check_name
from desk_buddy.skills_store import (
    APPROVED,
    CHANGED,
    INVALID,
    MAX_APPROVED,
    Skill,
    SkillsStore,
    check_slug,
)
from desk_buddy.tools.skills import (
    LOCK_FILE,
    SkillRunner,
    SyncResult,
    has_project,
    outside_project,
    run_sync,
)

log = logging.getLogger(__name__)

NO_FOLDER = "Set SKILLS_DIR on the Settings tab first."
NO_UV = "uv wasn't found on PATH, so skills are off. Install uv and restart CARL."

# Runs `uv sync` for one skill (run_sync's signature); a fake in tests.
Sync = Callable[..., SyncResult]


@dataclass(frozen=True)
class Outcome:
    """A route's answer: the HTTP status and the JSON body."""

    status: int
    body: dict[str, Any]


BUSY = Outcome(409, {"error": "Another skill is being set up; try again when it's done."})


def skill_view(s: Skill) -> dict[str, Any]:
    """One skill for the page: what the manifest says (nothing for an invalid one), its
    status and problems. Never its files, its .env or its fingerprint."""
    m = s.manifest
    return {
        "slug": s.slug,
        "name": m.name if m else None,
        "description": m.description if m else "",
        "spoken_names": list(m.spoken_names) if m else [],
        "needs_yes": m.needs_yes if m else False,
        "timeout_seconds": m.timeout_seconds if m else None,
        "choices": [
            {"name": c.name, "options": list(c.options), "default": c.default}
            for c in (m.choices if m else ())
        ],
        "text": (
            {
                "description": m.text.description,
                "max_length": m.text.max_length,
                "required": m.text.required,
            }
            if m and m.text
            else None
        ),
        "status": s.status,
        "approved": s.approved,
        "errors": [{"field": e.field, "reason": e.reason} for e in s.errors],
    }


def skills_view(
    store: SkillsStore, skills_dir: str, other_names: Iterable[str], *, uv_found: bool
) -> dict[str, Any]:
    """GET /api/skills: SKILLS_DIR as it is now."""
    scan = store.scan(skills_dir, other_names)
    error = " ".join(e for e in (scan.error, store.error) if e) or None
    return {
        "enabled": scan.enabled,
        "error": error,
        "skills": [skill_view(s) for s in scan.skills],
        "uv_found": uv_found,
        "limits": {"approved": MAX_APPROVED},
    }


def approve(
    store: SkillsStore,
    runner: SkillRunner | None,
    skills_dir: str,
    slug: str,
    other_names: Callable[[], Iterable[str]],
    *,
    lock: threading.Lock,
    sync: Sync = run_sync,
) -> Outcome:
    """Approve one skill: it must be valid (clashes included), not on already and, with a
    project, not inside another uv project; then `uv sync` (a project only), uv.lock must
    be there, and the skill is checked again: still valid with the names apps and
    bookmarks use at that moment, and its fingerprint is taken from that state. Any
    failure leaves the skill off. `lock` allows one setup at a time (409 otherwise)."""
    try:
        check_slug(slug)
    except ValueError as exc:
        return Outcome(400, {"error": str(exc)})
    if not lock.acquire(blocking=False):
        return BUSY
    try:
        return _approve(store, runner, skills_dir, slug, other_names, sync)
    finally:
        lock.release()


def _approve(
    store: SkillsStore,
    runner: SkillRunner | None,
    skills_dir: str,
    slug: str,
    other_names: Callable[[], Iterable[str]],
    sync: Sync,
) -> Outcome:
    if not skills_dir:
        return Outcome(400, {"error": NO_FOLDER})
    if runner is None:
        return Outcome(503, {"error": NO_UV})
    scan = store.scan(skills_dir, other_names())
    if scan.error:
        return Outcome(400, {"error": scan.error})
    skill = scan.get(slug)
    if skill is None:
        return Outcome(404, {"error": "There's no such skill in SKILLS_DIR."})
    present = frozenset(s.slug for s in scan.skills)
    if skill.status == INVALID:
        return _invalid(skill)
    if skill.status == APPROVED and skill.fingerprint is not None:
        # Its files match the approval, so there's nothing to set up; saving it again
        # bumps the version so CARL rescans (say, after its files were restored).
        store.approve(slug, skill.fingerprint, present)
        return Outcome(200, skills_view(store, skills_dir, other_names(), uv_found=True))
    # Only approvals of skills in this SKILLS_DIR count, not orphans.
    counted = [k for k in store.approvals() if k in present]
    if slug not in counted and len(counted) >= MAX_APPROVED:
        return Outcome(400, {"error": f"At most {MAX_APPROVED} skills can be on. Turn one off."})
    if has_project(skill.folder):
        reason = outside_project(skill.folder)
        if reason is not None:
            return Outcome(400, {"error": reason})
        if skill.approved:
            # The sync rewrites its environment before the checks below can fail: off
            # until it's approved again, so a failed re-approval leaves nothing on.
            store.disable(slug)
        log.info("Skills: setting up %s (uv sync)", slug)
        result = runner.sync(skill.folder, slug, sync=sync)
        if result.busy:
            return Outcome(409, {"error": result.error})
        if not result.ok:
            log.warning("Skills: %s setup failed: %s", slug, result.error)
            return Outcome(400, {"error": result.error, "output": result.output})
        if not (skill.folder / LOCK_FILE).is_file():
            return Outcome(
                400, {"error": f"The setup didn't leave a {LOCK_FILE} in the skill's folder."}
            )
    # Again after the sync: an edit or a new app or bookmark name in the meantime is
    # refused, and the fingerprint (uv.lock may be new) comes from this state.
    now = store.scan(skills_dir, other_names()).get(slug)
    if now is None:
        return Outcome(404, {"error": "The skill's folder is gone."})
    if now.status == INVALID or now.fingerprint is None:
        return _invalid(now)
    try:
        store.approve(slug, now.fingerprint, present)
    except ValueError as exc:
        return Outcome(400, {"error": str(exc)})
    return Outcome(200, skills_view(store, skills_dir, other_names(), uv_found=True))


def disable(
    store: SkillsStore,
    skills_dir: str,
    slug: str,
    other_names: Callable[[], Iterable[str]],
    *,
    lock: threading.Lock,
    uv_found: bool,
) -> Outcome:
    """Turn one skill off; refused (409) while any skill is being set up."""
    try:
        check_slug(slug)
    except ValueError as exc:
        return Outcome(400, {"error": str(exc)})
    if not lock.acquire(blocking=False):
        return BUSY
    try:
        store.disable(slug)
    finally:
        lock.release()
    return Outcome(200, skills_view(store, skills_dir, other_names(), uv_found=uv_found))


def _invalid(skill: Skill) -> Outcome:
    reasons = "; ".join(f"{e.field}: {e.reason}" for e in skill.errors)
    return Outcome(400, {"error": f"The skill isn't valid: {reasons or 'unknown problem'}"})


def approved_names(
    store: SkillsStore, skills_dir: str, other_names: Iterable[str]
) -> dict[str, str]:
    """The spoken names approved skills hold (approved, or changed and waiting to be
    approved again): {name: "<skill name> (<slug>)"}. Apps and bookmarks can't take them.
    It scans with the app and bookmark names of now, like every other scan, so the
    scan's problems (and so its log lines) don't flip back and forth."""
    if not skills_dir or not store.approvals():
        return {}
    owners: dict[str, str] = {}
    for s in store.scan(skills_dir, other_names).skills:
        if s.status in (APPROVED, CHANGED) and s.manifest is not None:
            for name in s.manifest.spoken_names:
                owners.setdefault(name, f"{s.manifest.name} ({s.slug})")
    return owners


def name_clashes(
    items: Any,
    names_of: Callable[[Mapping[str, Any]], list[Any]],
    prefix: str,
    owners: Mapping[str, str],
) -> dict[str, str]:
    """Errors ("<prefix>.<i>") for the entries of a PUT body whose spoken names an approved
    skill holds. Anything malformed is left for the store's own checks."""
    errors: dict[str, str] = {}
    if not owners or not isinstance(items, list):
        return errors
    for i, item in enumerate(items):
        if not isinstance(item, Mapping):
            continue
        for raw in names_of(item):
            try:
                name = check_name(raw)
            except ValueError:
                continue
            if name in owners:
                errors[f"{prefix}.{i}"] = (
                    f"The name {name!r} is used by the skill {owners[name]}. "
                    "Pick another name, or turn the skill off first."
                )
                break
    return errors
