"""Tool declarations for Gemini and their dispatcher.

Read-only tools run directly. Proposing tools (add_title, delete_title, propose_cleanup) never
change anything: they create a pending action and return ``awaiting_user_confirmation``. The
bot then shows a code-rendered confirm button that only the requesting user can press.
Arguments are validated here; anything malformed is returned to the model as an error.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from smarrtbot.clients.http import ServiceError
from smarrtbot.flows import add as add_flow
from smarrtbot.flows import cleanup as cleanup_flow
from smarrtbot.flows import delete as delete_flow
from smarrtbot.flows.actions import Proposal
from smarrtbot.flows.library import library, match_titles
from smarrtbot.flows.storage import storage_report

log = logging.getLogger(__name__)


def _obj(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"type": "object", "properties": props}
    if required:
        out["required"] = required
    return out


DECLARATIONS: list[dict[str, Any]] = [
    {
        "name": "search_movie",
        "description": "Search for a movie to add. Returns candidates with tmdb_id.",
        "parameters": _obj({"query": {"type": "string"}}, ["query"]),
    },
    {
        "name": "search_series",
        "description": "Search for a TV series to add. Returns candidates with tvdb_id.",
        "parameters": _obj({"query": {"type": "string"}}, ["query"]),
    },
    {
        "name": "add_title",
        "description": (
            "Propose adding a movie (by tmdb_id) or series (by tvdb_id) from search results. "
            "Creates a confirmation button for the user; it does not add anything by itself."
        ),
        "parameters": _obj(
            {
                "kind": {"type": "string", "enum": ["movie", "series"]},
                "id": {"type": "integer", "description": "tmdb_id for movies, tvdb_id for series"},
            },
            ["kind", "id"],
        ),
    },
    {
        "name": "list_library",
        "description": "List titles in the library with sizes, optionally filtered by a query.",
        "parameters": _obj(
            {
                "kind": {"type": "string", "enum": ["movie", "series", "any"]},
                "query": {"type": "string"},
                "sort": {"type": "string", "enum": ["size", "title"]},
                "limit": {"type": "integer"},
            }
        ),
    },
    {
        "name": "delete_title",
        "description": (
            "Propose deleting a library title by its library id (from list_library): a movie, "
            "a whole series, or one season (kind 'season' with season number). Frees space by "
            "also removing the matching torrent when it is not shared. Creates a confirmation "
            "button; it does not delete anything by itself."
        ),
        "parameters": _obj(
            {
                "kind": {"type": "string", "enum": ["movie", "series", "season"]},
                "id": {"type": "integer"},
                "season": {"type": "integer"},
            },
            ["kind", "id"],
        ),
    },
    {
        "name": "get_queue",
        "description": "Current downloads in progress.",
        "parameters": _obj({}),
    },
    {
        "name": "get_storage_report",
        "description": "Disk total/used/free, sizes of movies/tv/torrents, largest titles, trends.",
        "parameters": _obj({}),
    },
    {
        "name": "list_cleanup_candidates",
        "description": (
            "List things that could be cleaned up, with ids and sizes. Categories: orphans, "
            "unlinked_torrents, suspicious, duplicates, largest. Omit category for all."
        ),
        "parameters": _obj(
            {
                "category": {
                    "type": "string",
                    "enum": list(cleanup_flow.CATEGORIES),
                }
            }
        ),
    },
    {
        "name": "propose_cleanup",
        "description": (
            "Propose deleting cleanup candidates by their ids from list_cleanup_candidates. "
            "Creates a confirmation button showing the exact list; it deletes nothing itself."
        ),
        "parameters": _obj(
            {"candidate_ids": {"type": "array", "items": {"type": "string"}}},
            ["candidate_ids"],
        ),
    },
]

READ_ONLY = {
    "search_movie",
    "search_series",
    "list_library",
    "get_queue",
    "get_storage_report",
    "list_cleanup_candidates",
}
PROPOSING = {"add_title", "delete_title", "propose_cleanup"}
assert READ_ONLY | PROPOSING == {d["name"] for d in DECLARATIONS}  # noqa: S101


class _Query(BaseModel):
    query: str = Field(min_length=1, max_length=200)


class _Add(BaseModel):
    kind: str = Field(pattern="^(movie|series)$")
    id: int = Field(gt=0)


class _List(BaseModel):
    kind: str = Field(default="any", pattern="^(movie|series|any)$")
    query: str | None = Field(default=None, max_length=200)
    sort: str = Field(default="size", pattern="^(size|title)$")
    limit: int = Field(default=15, ge=1, le=50)


class _Delete(BaseModel):
    kind: str = Field(pattern="^(movie|series|season)$")
    id: int = Field(gt=0)
    season: int | None = Field(default=None, ge=0, le=200)


class _Category(BaseModel):
    category: str | None = None


class _Cleanup(BaseModel):
    candidate_ids: list[str] = Field(min_length=1, max_length=50)


@dataclass
class ToolContext:
    svc: Any
    requester_id: int
    chat_id: int
    proposals: list[Proposal] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)


async def call_tool(ctx: ToolContext, name: str, args: dict[str, Any] | None) -> dict[str, Any]:
    ctx.calls.append(name)
    args = args or {}
    try:
        return await _dispatch(ctx, name, args)
    except ValidationError as exc:
        return {"error": f"invalid arguments: {exc.errors()[0].get('msg', 'bad value')}"}
    except (
        add_flow.AddError,
        delete_flow.DeleteError,
        cleanup_flow.CleanupError,
        ServiceError,
    ) as exc:
        return {"error": str(exc)[:300]}
    except Exception:
        log.exception("tool %s failed", name)
        return {"error": "internal error"}


async def _dispatch(ctx: ToolContext, name: str, args: dict[str, Any]) -> dict[str, Any]:
    svc = ctx.svc
    if name == "search_movie":
        q = _Query(**args)
        return {"results": await add_flow.search(svc, "movie", q.query)}
    if name == "search_series":
        q = _Query(**args)
        return {"results": await add_flow.search(svc, "series", q.query)}
    if name == "add_title":
        a = _Add(**args)
        p = await add_flow.propose_add(svc, a.kind, a.id, ctx.requester_id, ctx.chat_id)  # type: ignore[arg-type]
        ctx.proposals.append(p)
        return p.summary
    if name == "list_library":
        a = _List(**args)
        titles = await library(svc, None if a.kind == "any" else a.kind)  # type: ignore[arg-type]
        if a.query:
            titles = match_titles(titles, a.query, limit=a.limit)
        key = (lambda t: -t.size) if a.sort == "size" else (lambda t: t.title.lower())
        titles = sorted(titles, key=key)[: a.limit]
        return {"titles": [t.as_tool() for t in titles]}
    if name == "delete_title":
        d = _Delete(**args)
        if d.kind == "season" and d.season is None:
            return {"error": "season number required for kind 'season'"}
        p, _plan = await delete_flow.propose_delete(
            svc,
            d.kind,  # type: ignore[arg-type]
            d.id,
            d.season if d.kind == "season" else None,
            ctx.requester_id,
            ctx.chat_id,
        )
        ctx.proposals.append(p)
        return p.summary
    if name == "get_queue":
        return {"queue": await queue_items(svc)}
    if name == "get_storage_report":
        return (await storage_report(svc)).as_tool()
    if name == "list_cleanup_candidates":
        c = _Category(**args)
        items = await cleanup_flow.list_candidates(svc, c.category)
        return {"candidates": [x.as_tool() for x in items[:40]], "total": len(items)}
    if name == "propose_cleanup":
        c2 = _Cleanup(**args)
        p = await cleanup_flow.propose_cleanup(svc, c2.candidate_ids, ctx.requester_id, ctx.chat_id)
        ctx.proposals.append(p)
        return p.summary
    return {"error": f"unknown tool {name}"}


async def queue_items(svc: Any) -> list[dict[str, Any]]:
    from smarrtbot.util import untrusted

    out: list[dict[str, Any]] = []
    if svc.radarr is not None:
        for q in await svc.radarr.queue(includeMovie=True):
            size, left = float(q.get("size") or 0), float(q.get("sizeleft") or 0)
            out.append(
                {
                    "title": untrusted((q.get("movie") or {}).get("title") or q.get("title"), 100),
                    "progress": round(100 * (1 - left / size), 1) if size else 0,
                    "status": q.get("status"),
                    "eta": q.get("timeleft"),
                }
            )
    if svc.sonarr is not None:
        for q in await svc.sonarr.queue(includeSeries=True, includeEpisode=True):
            size, left = float(q.get("size") or 0), float(q.get("sizeleft") or 0)
            ep = q.get("episode") or {}
            label = (q.get("series") or {}).get("title") or q.get("title")
            if ep:
                sn, en = int(ep.get("seasonNumber") or 0), int(ep.get("episodeNumber") or 0)
                label = f"{label} S{sn:02d}E{en:02d}"
            out.append(
                {
                    "title": untrusted(label, 100),
                    "progress": round(100 * (1 - left / size), 1) if size else 0,
                    "status": q.get("status"),
                    "eta": q.get("timeleft"),
                }
            )
    return out
