"""``smarrtbot setup-arr``: idempotent custom formats and quality profiles for TV-friendly grabs.

* Dry run by default: prints what would be created or changed. ``--apply`` executes.
* Touches only objects whose names start with ``SB`` / ``smarrtbot`` that it owns. Existing
  titles are never edited; they keep their profiles.
* "Default for new adds" = the bot adds with ``smarrtbot 1080p`` when it exists (unless config
  pins another profile). Radarr/Sonarr have no server-side default profile setting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

CF_PREFIX = "SB: "
PROFILE_1080 = "smarrtbot 1080p"
PROFILE_4K = "smarrtbot 4K TV-safe"


def _title_spec(name: str, regex: str, negate: bool = False) -> dict[str, Any]:
    return {
        "name": name,
        "implementation": "ReleaseTitleSpecification",
        "negate": negate,
        "required": False,
        "fields": [{"name": "value", "value": regex}],
    }


# (name, regex, score in 1080p profile, score in 4K profile)
FORMATS: list[tuple[str, str, int, int]] = [
    ("Dolby Vision", r"\b(DV|DoVi|Dolby[ ._-]?Vision)\b", -10000, -10000),
    ("TrueHD", r"\bTrue[ ._-]?HD\b", -10000, -10000),
    ("DTS (any)", r"\bDTS(?:[ ._-]?(?:HD|MA|X|ES|HRA|Express))*\b", -10000, -10000),
    ("Remux", r"\bRemux\b", -10000, -10000),
    ("HDR10+", r"\bHDR10(\+|P|Plus)\b", 0, 50),
    ("HDR10", r"\bHDR(10)?\b", 0, 25),
    ("x265 / HEVC", r"\b(x265|HEVC|[Hh][ .]?265)\b", 20, 10),
    ("Compatible audio", r"\b(E-?AC-?3|DDP|DD\+|AC-?3|DD|AAC)\b", 10, 10),
]

# Quality names as they appear in each app's /qualityprofile/schema (groups or single).
ALLOWED = {
    "radarr": {
        PROFILE_1080: ["WEB 720p", "Bluray-720p", "HDTV-1080p", "WEB 1080p", "Bluray-1080p"],
        PROFILE_4K: ["WEB 1080p", "Bluray-1080p", "WEB 2160p", "Bluray-2160p"],
    },
    "sonarr": {
        PROFILE_1080: ["WEB 720p", "Bluray-720p", "HDTV-1080p", "WEB 1080p", "Bluray-1080p"],
        PROFILE_4K: ["WEB 1080p", "Bluray-1080p", "WEB 2160p", "Bluray-2160p"],
    },
}
CUTOFF = {PROFILE_1080: "Bluray-1080p", PROFILE_4K: "Bluray-2160p"}


@dataclass
class Change:
    app: str
    what: str  # "custom format" | "quality profile"
    name: str
    action: str  # create | update | unchanged
    detail: str = ""


@dataclass
class Plan:
    changes: list[Change] = field(default_factory=list)

    def pending(self) -> list[Change]:
        return [c for c in self.changes if c.action != "unchanged"]

    def render(self) -> str:
        lines = []
        for c in self.changes:
            mark = {"create": "+", "update": "~", "unchanged": "="}[c.action]
            lines.append(
                f"{mark} [{c.app}] {c.what}: {c.name}" + (f" ({c.detail})" if c.detail else "")
            )
        return "\n".join(lines)


def desired_formats() -> list[dict[str, Any]]:
    return [
        {
            "name": CF_PREFIX + name,
            "includeCustomFormatWhenRenaming": False,
            "specifications": [_title_spec(name, regex)],
        }
        for name, regex, _s1, _s4 in FORMATS
    ]


def _spec_sig(cf: dict[str, Any]) -> list[tuple[Any, ...]]:
    sig = []
    for s in cf.get("specifications", []):
        fields = s.get("fields")
        if isinstance(fields, list):
            value = next((f.get("value") for f in fields if f.get("name") == "value"), None)
        else:
            value = (fields or {}).get("value")
        sig.append((s.get("implementation"), bool(s.get("negate")), bool(s.get("required")), value))
    return sorted(sig)


def _item_name(item: dict[str, Any]) -> str:
    return str(item.get("name") or (item.get("quality") or {}).get("name") or "")


def _item_id(item: dict[str, Any]) -> int:
    return int(item.get("id") or (item.get("quality") or {}).get("id"))


def build_profile(
    schema: dict[str, Any], app: str, name: str, cf_ids: dict[str, int]
) -> dict[str, Any]:
    allowed = set(ALLOWED[app][name])
    items = []
    names_found = set()
    for it in schema["items"]:
        it = dict(it)
        n = _item_name(it)
        it["allowed"] = n in allowed
        if it["allowed"]:
            names_found.add(n)
        items.append(it)
    missing = allowed - names_found
    if missing:
        raise ValueError(f"{app}: qualities not found in schema: {sorted(missing)}")
    cutoff = next(_item_id(it) for it in items if _item_name(it) == CUTOFF[name])
    scores = {CF_PREFIX + n: (s1 if name == PROFILE_1080 else s4) for n, _r, s1, s4 in FORMATS}
    format_items = [
        {"format": cf_ids[cf], "name": cf, "score": score}
        for cf, score in scores.items()
        if cf in cf_ids
    ]
    body = dict(schema)
    body.update(
        {
            "name": name,
            "upgradeAllowed": True,
            "cutoff": cutoff,
            "items": items,
            "minFormatScore": 0,
            "cutoffFormatScore": 10000,
            "minUpgradeFormatScore": 1,
            "formatItems": format_items,
        }
    )
    body.pop("id", None)
    return body


def _profile_sig(p: dict[str, Any]) -> tuple[Any, ...]:
    allowed = sorted(_item_name(i) for i in p.get("items", []) if i.get("allowed"))
    scores = sorted(
        (str(f.get("name")), int(f.get("score") or 0))
        for f in p.get("formatItems", [])
        if str(f.get("name", "")).startswith(CF_PREFIX) and int(f.get("score") or 0) != 0
    )
    return (
        tuple(allowed),
        p.get("cutoff"),
        p.get("minFormatScore"),
        bool(p.get("upgradeAllowed")),
        tuple(scores),
    )


async def plan_and_apply(clients: dict[str, Any], apply: bool = False) -> Plan:
    plan = Plan()
    for app, client in clients.items():
        existing = {cf["name"]: cf for cf in await client.custom_formats()}
        cf_ids = {n: int(cf["id"]) for n, cf in existing.items()}
        for want in desired_formats():
            have = existing.get(want["name"])
            if have is None:
                plan.changes.append(Change(app, "custom format", want["name"], "create"))
                if apply:
                    created = await client.create_custom_format(want)
                    cf_ids[want["name"]] = int(created["id"])
            elif _spec_sig(have) != _spec_sig(want):
                plan.changes.append(Change(app, "custom format", want["name"], "update", "regex"))
                if apply:
                    await client.update_custom_format({**want, "id": have["id"]})
            else:
                plan.changes.append(Change(app, "custom format", want["name"], "unchanged"))
        schema = await client.quality_profile_schema()
        if not apply:
            # Pretend-create ids so the dry-run diff can be computed.
            for i, cf in enumerate(desired_formats()):
                cf_ids.setdefault(cf["name"], -(i + 1))
        # The schema lists formats that exist; add ours so scores can be set.
        schema = dict(schema)
        schema["formatItems"] = [
            {"format": fid, "name": n, "score": 0} for n, fid in sorted(cf_ids.items())
        ]
        profiles = {p["name"]: p for p in await client.quality_profiles()}
        for name in (PROFILE_1080, PROFILE_4K):
            want = build_profile(schema, app, name, cf_ids)
            have = profiles.get(name)
            if have is None:
                plan.changes.append(Change(app, "quality profile", name, "create"))
                if apply:
                    await client.create_quality_profile(want)
            elif _profile_sig(have) != _profile_sig(want):
                plan.changes.append(Change(app, "quality profile", name, "update"))
                if apply:
                    # Keep the id and any formatItems we don't own; update ours.
                    merged = dict(want)
                    merged["id"] = have["id"]
                    ours = {f["name"] for f in want["formatItems"]}
                    merged["formatItems"] = want["formatItems"] + [
                        f for f in have.get("formatItems", []) if f.get("name") not in ours
                    ]
                    await client.update_quality_profile(merged)
            else:
                plan.changes.append(Change(app, "quality profile", name, "unchanged"))
    return plan
