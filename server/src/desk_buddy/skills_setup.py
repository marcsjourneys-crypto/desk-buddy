"""The dashboard's side of skills: the view of SKILLS_DIR for the page, approving a skill
(with its `uv sync`) and turning one off, and the spoken names approved skills hold.

Everything here blocks (it reads files, and approving may run `uv sync` for minutes):
the dashboard calls it on a worker thread, never on the event loop. Nothing here reads a
skill's .env, and no response carries file contents or fingerprints; the only output
shown is the tail of a failed `uv sync`.
"""

from __future__ import annotations

import logging
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
    other_names: Iterable[str],
    *,
    sync: Sync = run_sync,
) -> Outcome:
    """Approve one skill: it must be valid (clashes included) and, with a project, not
    inside another uv project; then `uv sync` (a project only), uv.lock must be there, and
    the fingerprint is taken after the sync. Any failure leaves the skill off."""
    other = frozenset(other_names)
    try:
        check_slug(slug)
    except ValueError as exc:
        return Outcome(400, {"error": str(exc)})
    if not skills_dir:
        return Outcome(400, {"error": NO_FOLDER})
    if runner is None:
        return Outcome(503, {"error": NO_UV})
    scan = store.scan(skills_dir, other)
    if scan.error:
        return Outcome(400, {"error": scan.error})
    skill = scan.get(slug)
    if skill is None:
        return Outcome(404, {"error": "There's no such skill in SKILLS_DIR."})
    if skill.status == INVALID:
        return _invalid(skill)
    approvals = store.approvals()
    if slug not in approvals and len(approvals) >= MAX_APPROVED:
        return Outcome(400, {"error": f"At most {MAX_APPROVED} skills can be on. Turn one off."})
    if has_project(skill.folder):
        reason = outside_project(skill.folder)
        if reason is not None:
            return Outcome(400, {"error": reason})
        log.info("Skills: setting up %s (uv sync)", slug)
        result = sync(runner.uv, skill.folder, runner.clean_env(slug))
        if not result.ok:
            log.warning("Skills: %s setup failed: %s", slug, result.error)
            return Outcome(400, {"error": result.error, "output": result.output})
        if not (skill.folder / LOCK_FILE).is_file():
            return Outcome(
                400, {"error": f"The setup didn't leave a {LOCK_FILE} in the skill's folder."}
            )
    # The fingerprint after the sync (uv.lock may be new), from the files as they are now.
    now = store.check(skills_dir, slug)
    if now is None:
        return Outcome(404, {"error": "The skill's folder is gone."})
    if now.status == INVALID or now.fingerprint is None:
        return _invalid(now)
    try:
        store.approve(slug, now.fingerprint)
    except ValueError as exc:
        return Outcome(400, {"error": str(exc)})
    return Outcome(200, skills_view(store, skills_dir, other, uv_found=True))


def _invalid(skill: Skill) -> Outcome:
    reasons = "; ".join(f"{e.field}: {e.reason}" for e in skill.errors)
    return Outcome(400, {"error": f"The skill isn't valid: {reasons or 'unknown problem'}"})


def approved_names(store: SkillsStore, skills_dir: str) -> dict[str, str]:
    """The spoken names approved skills hold (approved, or changed and waiting to be
    approved again): {name: "<skill name> (<slug>)"}. Apps and bookmarks can't take them."""
    if not skills_dir or not store.approvals():
        return {}
    owners: dict[str, str] = {}
    # No other names: an app's name mustn't hide which skill holds it.
    for s in store.scan(skills_dir).skills:
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
