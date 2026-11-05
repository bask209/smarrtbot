"""Search and add titles. The add itself only runs from a confirmed pending action."""

from __future__ import annotations

from typing import Any, Literal

from smarrtbot.flows.actions import Proposal
from smarrtbot.util import untrusted

DEFAULT_PROFILE_NAMES = ("smarrtbot 1080p", "HD-1080p")


class AddError(Exception):
    pass


def _year(item: dict[str, Any]) -> int | None:
    y = item.get("year")
    return int(y) if y else None


async def search(svc: Any, kind: Literal["movie", "series"], query: str) -> list[dict[str, Any]]:
    """Lookup results, normalised for the model. All text fields are untrusted data."""
    out: list[dict[str, Any]] = []
    if kind == "movie":
        if svc.radarr is None:
            raise AddError("Radarr is not configured")
        for r in (await svc.radarr.lookup(query))[:8]:
            if not r.get("tmdbId"):
                continue
            out.append(
                {
                    "tmdb_id": int(r["tmdbId"]),
                    "title": untrusted(r.get("title"), 120),
                    "year": _year(r),
                    "in_library": bool(r.get("id")),
                    "overview": untrusted(r.get("overview"), 240),
                }
            )
    else:
        if svc.sonarr is None:
            raise AddError("Sonarr is not configured")
        for r in (await svc.sonarr.lookup(query))[:8]:
            if not r.get("tvdbId"):
                continue
            out.append(
                {
                    "tvdb_id": int(r["tvdbId"]),
                    "title": untrusted(r.get("title"), 120),
                    "year": _year(r),
                    "seasons": len([s for s in r.get("seasons", []) if s.get("seasonNumber")]),
                    "in_library": bool(r.get("id")),
                    "overview": untrusted(r.get("overview"), 240),
                }
            )
    return out


async def _resolve_profile(client: Any, configured: str | int | None) -> int:
    profiles = await client.quality_profiles()
    if not profiles:
        raise AddError("no quality profiles found")
    if configured is not None and str(configured).strip():
        for p in profiles:
            if str(p["id"]) == str(configured) or p["name"] == str(configured):
                return int(p["id"])
        raise AddError(f"quality profile {configured!r} not found")
    by_name = {p["name"]: int(p["id"]) for p in profiles}
    for name in DEFAULT_PROFILE_NAMES:
        if name in by_name:
            return by_name[name]
    return int(profiles[0]["id"])


async def _resolve_root(client: Any, configured: str | None) -> str:
    roots = await client.root_folders()
    if configured:
        if any(r["path"].rstrip("/") == configured.rstrip("/") for r in roots):
            return configured
        raise AddError(f"root folder {configured!r} not found")
    if not roots:
        raise AddError("no root folders found")
    return str(roots[0]["path"])


async def _fetch(svc: Any, kind: str, ext_id: int) -> dict[str, Any] | None:
    if kind == "movie":
        if svc.radarr is None:
            raise AddError("Radarr is not configured")
        return await svc.radarr.lookup_tmdb(ext_id)
    if svc.sonarr is None:
        raise AddError("Sonarr is not configured")
    return await svc.sonarr.lookup_tvdb(ext_id)


async def propose_add(
    svc: Any, kind: Literal["movie", "series"], ext_id: int, requester_id: int, chat_id: int
) -> Proposal:
    item = await _fetch(svc, kind, int(ext_id))
    if not item:
        raise AddError("title not found by that id")
    title, year = untrusted(item.get("title"), 120), _year(item)
    if item.get("id"):
        raise AddError(f"{title} is already in the library")
    payload = {"kind": kind, "ext_id": int(ext_id), "title": title, "year": year}
    nonce = svc.pending.create("add", requester_id, chat_id, payload)
    label = f"{title} ({year})" if year else title
    return Proposal(
        nonce=nonce,
        kind="add",
        label=label,
        details=["movie" if kind == "movie" else "series"],
        summary={"status": "awaiting_user_confirmation", "title": title, "year": year},
    )


async def execute_add(svc: Any, payload: dict[str, Any], dry_run: bool) -> str:
    kind, ext_id = payload["kind"], int(payload["ext_id"])
    item = await _fetch(svc, kind, ext_id)  # fresh lookup by the stored id, never by model text
    if not item:
        raise AddError("title no longer found")
    label = payload["title"] + (f" ({payload['year']})" if payload.get("year") else "")
    if item.get("id"):
        return f"{label}: already in the library"
    client = svc.radarr if kind == "movie" else svc.sonarr
    cfg = svc.settings.radarr if kind == "movie" else svc.settings.sonarr
    profile = await _resolve_profile(client, cfg.quality_profile)
    root = await _resolve_root(client, cfg.root_folder)
    if dry_run:
        return f"[DRY RUN] would add {label} (profile {profile}) and start a search"
    if kind == "movie":
        await svc.radarr.add_movie(item, profile, root, search=True)
    else:
        await svc.sonarr.add_series(item, profile, root, search=True)
    return f"Added {label}; searching now"
