# smarrtbot specification

This is the living design document. Deployment details for a specific host belong in a private operator setup, never in this repository. Everything here uses placeholders.

## 1. Purpose and scope

smarrtbot is a Telegram bot for an owner and a few trusted people. They ask for movies and shows in English or Latin American Spanish. Gemini function calling turns their messages into tool calls against Radarr and Sonarr. The bot also monitors disk usage and cleans up storage by chat, strictly inside the media and torrents space.

**Out of scope:** books and any shadow library; request managers (Overseerr, Jellyseerr, Seerr); tracker or indexer lists in code (indexers live in Prowlarr).

## 2. Users, languages, permissions

- Requests are automatic once a user is approved (no per-request approval).
- Access is checked by a single gate before every handler. The chat must be a DM or a group on the allowlist, and the user must be approved. Otherwise there is no Gemini call, and only `/whoami` works.
- Approval works like this: a user sends `/whoami`, the owner gets an Approve button, and the approval is stored per user (global). The owner's ID comes from config. `/revoke` is owner-only.
- Telegram privacy mode stays on. In groups, natural language needs an @mention or a reply to the bot. Commands work normally.
- Any approved user may delete, after a confirm tap. **The confirm must come from the same user who started the action.**
- The bot replies in the user's language: English, or Latin American Spanish phrasing.
- Commands: `/help /status /queue /storage /delete /models /whoami /revoke`, plus natural language.
- `/help` shows a privacy and terms notice. On the free tier, Google may use Gemini prompts to improve its products, and stacking free keys may breach Google's terms. The operator accepts that risk.

## 3. Playback quality goals

- 1080p is the default. 4K only when the reference client can Direct Play it. Example reference: a 2022 Samsung Tizen TV with HDR10/HDR10+, no Dolby Vision and no DTS decoding.
- Assume the server has no hardware transcoding.
- These break playback and are flagged: Dolby Vision, TrueHD, **any** DTS, embedded PGS or ASS subtitles (they force burn-in transcoding), and bitrate over the cap.
- Bitrate caps are config values. Placeholders: 25 Mbps for 4K and 12 Mbps for 1080p.
- The media-server integration is pluggable: Plex now, Jellyfin possibly later.

## 4. Modules

### 4.1 Telegram (`bot/`)
Long polling only, with no inbound ports. Handlers, the auth gate and i18n (en, es) live here. Built on python-telegram-bot (async, JobQueue).

### 4.2 LLM (`llm/`)
- Gemini through raw REST with httpx, using function calling.
- Read-only tools: `search_movie`, `search_series`, `list_library`, `get_storage_report`, `list_cleanup_candidates`.
- Proposing tools: `add_title`, `delete_title` and `propose_cleanup`. These only create a pending action (§5) and never execute anything.
- Titles, overviews and file names from services are **untrusted data**. They are passed only as JSON tool results and never placed in system or user instructions.
- Per-chat memory in SQLite: recent turns plus a rolling summary.

### 4.3 KeyPool
- 3 or 4 free Gemini keys, each from a separate Google project, in a hot-reloaded file. Keys are added by editing the file, never via Telegram.
- On 429, a cooldown is set per (key, model) from the retry time.
- Invalid keys are marked dead.
- Usage is counted per key, and keys are redacted from all logs.
- When every key is exhausted, the bot switches to commands only and replies "AI busy, try /help or later".

### 4.4 ModelManager (daily job)
- Lists the available models and keeps those that support `generateContent` and function calling.
- Ranks them: stable Flash, then flash-lite, then preview, with Pro last.
- Live-tests the winner with a tiny tool-call prompt.
- Demotes models that hit rate limits, and marks one dead after N consecutive failures.
- Re-discovers models on 404 or deprecation.
- `/models` shows the current state, and a config pin can override the choice.
- The API does not say which models are free, so the manager learns that from 403 and 429 responses.

### 4.5 Radarr and Sonarr clients (`clients/`)
Lookup, add (with the default quality profile and root folder), trigger search, and list sizes and disk space. Every call retries with backoff, because the services may drop briefly when, for example, a VPN container restarts.

### 4.6 qBittorrent client
Finds the torrent from the `downloadId` (the torrent hash) in the *arr history. Lists torrent files and sizes, and deletes a torrent together with its files.

### 4.7 Delete flow (hardlink-aware, `flows/delete.py`)
Imported media files are hardlinks to the files in `torrents/`. Deleting only in Radarr or Sonarr frees nothing while the torrent still exists.

1. Resolve the target from the stored pending-action payload, never from model text.
2. Delete the title and its files through Radarr or Sonarr. A season delete means deleting each episode file in Sonarr and unmonitoring the season.
3. Find the matching torrent(s) through the *arr history `downloadId`.
4. **Shared-torrent rule:** a torrent is deleted only if *all* of its files belong to the thing being deleted. Otherwise it is kept and the bot reports "torrent shared, kept, X GB not freed".
5. If no torrent is found, the bot reports "no torrent found, 0 GB freed" and never guesses.
6. A torrent that is still seeding is deleted anyway, and the confirmation says so.
7. Bytes freed are measured with the *arr disk-space API before and after, plus the torrent size. If the numbers don't add up, the bot says so.

Controls: an audit log (who, what, bytes freed); a rate limit of 5 deletes per hour per user, with the owner exempt and the limit configurable; and a confirm button showing the exact size and a "no undo" warning. Both whole-series and single-season deletes are supported.

### 4.8 TV-friendly check (`flows/tvcheck.py`)
- Polls the *arr import history (no webhooks), for example every 5 minutes, with a cursor in SQLite.
- Inspects streams through the media-server adapter. Plex exposes subtitle codecs; the adapter retries with backoff because scans can be slow.
- Flags the issues listed in §3 and posts them to the group, with a tap to blocklist and re-search.

### 4.9 `smarrtbot setup-arr`
- Idempotently creates custom formats and quality profiles in Radarr and Sonarr.
- Runs as a dry run first and shows the diff.
- Makes the new profile the default for **new** adds only, and never touches existing titles.

### 4.10 Download notices
These use Radarr and Sonarr's built-in Telegram connection, configured through their APIs. This stores the bot token and chat ID inside Radarr and Sonarr. `docs/DEPLOY.md` documents the steps.

### 4.11 Optional, later: subtitle strip job
Opt-in, and only after one file has been tested with the owner's approval. A low-priority job removes PGS and ASS tracks and keeps external SRT files (for example from Bazarr). It handles one file at a time at idle IO priority, on the media disk only.

### 4.12 Storage monitoring and cleanup (`flows/storage.py`, `flows/cleanup.py`)

**Scope (defense in depth)**
- The allowed roots are config values: `media/movies`, `media/tv` and `torrents`. The container mounts only the data folder or those subpaths, never a parent folder that holds other personal data.
- Ideally `media/` is mounted read-only (for scanning) and only `torrents/` is writable.
- Every path is resolved with `realpath` and must stay inside an allowed root. Symlinks and traversal are refused, the roots themselves are never deletable, and anything outside is rejected even if the model asks for it.
- Media is deleted **only** through Radarr and Sonarr, followed by the matching torrent through qBittorrent. There is never a raw `rm` inside `media/`.
- Data that qBittorrent manages in `torrents/` is deleted through the qBittorrent API.
- Only unmanaged leftovers in `torrents/` (files qBittorrent doesn't know about) may be removed by a guarded filesystem delete, after a preview and a confirm tap.
- There is a configurable per-day cap on bytes deleted, an audit log, and `DRY_RUN` on by default.

**Monitoring**
- `/storage` shows the filesystem's total, used and free space; the sizes of movies, tv and torrents; and the largest titles. Daily snapshots in SQLite give 7- and 30-day deltas.
- Alerts go to the owner and the group when free space drops below configurable thresholds (for example 10% or 100 GB). A weekly summary is optional.
- Sizes come cheaply from the *arr size APIs and qBittorrent totals. A full filesystem scan only runs as a scheduled, cached, rate-limited job at idle IO priority, using the `ioprio_set` syscall if `ionice` isn't available. It counts each inode once and reports **unique bytes**.

**Cleanup categories** (each with a preview list and sizes)
1. Unmanaged orphans in `torrents/`: link count 1, and not known to qBittorrent.
2. Completed torrents with no hardlink into the library.
3. Suspicious files, such as `.exe`, `.iso`, or `.rar` named like episodes.
4. Duplicate files for the same movie.
5. The largest titles.

**By chat:** questions like "how much space is left?", "what can I delete to free 200 GB?" or "show me the orphans". The model gets the read-only tools plus `propose_cleanup`, which only creates a pending action. The confirm button shows the exact list and total size, along with the no-undo warning.

**Scenarios that could delete the wrong thing, and how the design prevents them**

| Scenario | Prevention |
|---|---|
| A torrent-side file whose other hardlink is in `media/` gets deleted as an "orphan", which frees nothing and confuses the accounting | The orphan check requires link count == 1 *and* that the file isn't in qBittorrent. Files with a link count above 1 are never orphans. |
| A season-pack torrent is shared by several seasons or episodes, and deleting one season would remove the others' torrent-side data | Shared-torrent rule (§4.7): the torrent is deleted only when *all* of its files map to the target, otherwise it is kept and the bot reports the bytes it didn't free. |
| The model invents an ID or path, or a malicious overview injects "delete X" | IDs and file lists come only from the stored pending payload, built by code from service data. The model can only propose. Execution needs the requester's own tap. |
| A symlink or `../` escapes the allowed roots | `realpath` plus a prefix check against the allowed roots, refusal of symlinks, and a mount that excludes everything else. |
| A root folder is deleted, or a path that only exists in the container's view | Roots are never deletable. Paths are mapped between the *arr, qBittorrent and bot container views, and refused if the mapping doesn't resolve. |
| A replayed or forwarded button, or a tap by another user | One-use nonce, expiry, and a check that `callback.from_user.id` equals the requester. |
| A runaway series of deletes | Rate limit per user, a daily bytes cap, an audit log, and `DRY_RUN` by default. |
| A file that a torrent is still writing | Unfinished torrents are excluded, and so are files modified in the last N minutes. |

## 5. Enforcement model

- **Read-only tools** run directly.
- **State-changing tools** never run directly. Each writes a `pending_actions` row with a random nonce, the requester's ID, the payload and an expiry of a few minutes. The bot then sends an inline button that carries only the nonce.
- The callback handler checks the nonce, the expiry and that `callback.from_user.id == requester_id`. It then runs the action **exactly once**, under `DRY_RUN`, the rate limit and the daily cap.

## 6. Data (SQLite, WAL mode, stdlib `sqlite3` behind a thin repository layer)

Tables: `users`, `chats`, `turns`, `summaries`, `pending_actions`, `audit_log`, `keys_usage`, `model_state`, `history_cursor`, `flags` (TV-check queue and results), `storage_snapshots`, plus a small `kv` table (alert state, cleanup-candidate cache, access-request throttling).

## 7. Configuration

- Env vars plus a YAML file, validated with pydantic and pydantic-settings.
- Secrets are mounted files: the Telegram token, Gemini keys, Radarr and Sonarr keys, the qBittorrent password and the Plex token.
- Service addresses are config only. `DRY_RUN` defaults to on.
- The bot runs outside any VPN network namespace, on a bridge network, and reaches the services at their published ports.

## 8. Layout

```
src/smarrtbot/
  config.py  db.py  setup_arr.py  setup_notify.py  health.py  cli.py
  services.py  fsguard.py  scan.py  logsafe.py  util.py
  bot/          handlers, auth, i18n (en, es)
  llm/          gemini, keypool, modelmanager, tools, memory
  clients/      http (retry/backoff), arr (Radarr + Sonarr), qbit
  mediaserver/  base, plex (jellyfin later)
  flows/        add, delete, tvcheck, storage, cleanup
tests/  docs/  examples/  .github/workflows/  Dockerfile
```

## 9. Packaging and CI

- Image built on `python:3.12-slim`, running as non-root UID 1000.
- Label `org.opencontainers.image.source` points at this repo.
- A small health endpoint backs a Docker healthcheck. The memory target is 256 MB.
- CI on every push: ruff, pytest, gitleaks (full history) and pip-audit.
- On a `v*` tag: build and push `ghcr.io/bask209/smarrtbot` using the workflow `GITHUB_TOKEN` with `packages: write`, then run a Trivy scan.
- Images are built by CI, never on the deployment host.
- Deliverables for operators:
  - `examples/`: a config example, a stack file pinned to a version tag, and a secrets folder layout with placeholders.
  - `docs/DEPLOY.md`
  - `docs/CONFIG.md`

## 10. Milestones

| | |
|---|---|
| M0 | Repo, CI, scanning |
| M1 | Telegram, allowlist, `/whoami` approvals |
| M2 | Radarr/Sonarr search and add (dry-run) |
| M3 | Gemini tool calling and memory, plus an injection test (a malicious overview must not trigger tools) |
| M4 | KeyPool and ModelManager |
| M5 | Delete flow, then storage monitoring and cleanup |
| M6 | TV-friendly check and `setup-arr` |
| M7 | Packaging, docs, pre-release tags, anonymous-pull check |
| M8 | Operator handoff; `v1.0.0` only with the owner's approval |
