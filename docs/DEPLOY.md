# Deploying smarrtbot

This guide is for the operator. It uses placeholders (`<SMARRTBOT_DIR>`, `<DATA_DIR>`, …). Keep
your real values in your private notes, never in this repository.

## 1. What you need

- A Docker host (Portainer or plain compose) that can reach Radarr, Sonarr, qBittorrent and
  (optionally) Plex on their published ports. Outbound HTTPS to `api.telegram.org` and
  `generativelanguage.googleapis.com`. **No inbound port** is needed (long polling).
- A Telegram bot token from @BotFather. Privacy mode stays **on** (the default).
- 1–4 Gemini API keys, each from a **separate** Google Cloud project (free tier).
- Radarr/Sonarr/qBittorrent with media and torrents on the **same filesystem** (hardlinks).

Image: `ghcr.io/bask209/smarrtbot:<VERSION>` (amd64, ~60 MB compressed, non-root UID 1000).
Always pin a version tag; there is no `latest`.

> Fragile disk? Pull the image once, on its own, and let IO settle before doing anything else.

## 2. Host folders

```
<SMARRTBOT_DIR>/
├── config/config.yaml     # from examples/config.example.yaml
├── secrets/               # chmod 700, files chmod 600, owned by UID 1000
└── state/                 # SQLite DB + scan cache, owned by UID 1000
```

Put `<SMARRTBOT_DIR>` on bulk storage (HDD), not on a fragile system SSD. Secret files: see
`examples/secrets-layout/README.md`.

## 3. Mounts and network (security-relevant)

| Container path | Host path | Mode | Why |
|---|---|---|---|
| `/config` | `<SMARRTBOT_DIR>/config` | ro | config.yaml |
| `/run/secrets` | `<SMARRTBOT_DIR>/secrets` | ro | secret files (mount the folder so key edits hot-reload) |
| `/data/state` | `<SMARRTBOT_DIR>/state` | rw | database |
| `/data/media` | `<DATA_DIR>/media` | **ro** | hardlink checks and scans; media is only ever deleted via Radarr/Sonarr |
| `/data/torrents` | `<DATA_DIR>/torrents` | rw | guarded delete of unmanaged leftovers only |

- Mount **only** the media and torrents folders, never their parent (which may hold other
  personal data). The bot also refuses any path outside `storage.*_root` in code.
- The container paths must match what Radarr/Sonarr/qBittorrent report (`/data/media/...`,
  `/data/torrents/...` in the usual setup). If they differ, set `storage.path_maps`.
- Run the bot **outside** any VPN network namespace (a normal bridge network), so Telegram and
  Gemini traffic does not depend on the VPN. `extra_hosts: host.docker.internal:host-gateway`
  lets it reach services on the host's published ports. If the host firewall blocks that, use
  the host's LAN address in your private `config.yaml` instead.
- Resource limits: `mem_limit: 256m`, `cpus: 0.5`, `read_only: true`, `cap_drop: [ALL]`,
  `no-new-privileges`. See `examples/stack.example.yml`.

## 4. First start (private chat, dry run)

1. Copy `examples/config.example.yaml` to `<SMARRTBOT_DIR>/config/config.yaml`. Keep
   `dry_run: true`, `owner_id: 0`, `allowed_group_ids: []`. Fill in service URLs.
2. Create the secret files.
3. Create the stack from `examples/stack.example.yml` (replace placeholders, pin the version).
4. Check the config without starting the bot:
   `smarrtbot check-config` (Portainer → container → Console, or `docker exec smarrtbot smarrtbot check-config`).
   It prints `ok`/`FAIL` per secret file, never the secret.
5. Open a DM with the bot and send `/whoami`. Put the returned user id in `telegram.owner_id`,
   restart the container, then send `/help` and `/status` in the DM.
6. Soak test in the DM with `dry_run: true`: ask for a title, try `/storage`, `/cleanup`,
   `/delete <title>`. Every change shows a button and, in dry run, only reports what it would do.
7. Set `dry_run: false` (or env `SMARRTBOT_DRY_RUN=false`), restart, test one real add and one
   real delete of a test title.

## 5. Enable the group

1. Add the bot to the group (privacy mode on: it only sees commands, @mentions and replies).
2. Send `/whoami` in the group; it shows the chat id (negative number).
3. Add it to `telegram.allowed_group_ids` (and `notify_chat_id` if alerts should go elsewhere),
   restart.
4. Each member sends `/whoami`; the owner gets an Approve/Deny DM and taps Approve.
   `/revoke <user_id>` (owner only) removes access; `/users` lists users.

## 6. Quality profiles (`setup-arr`)

```
smarrtbot setup-arr            # dry run: prints + / ~ / = per custom format and profile
smarrtbot setup-arr --apply    # creates/updates only "SB: …" formats and "smarrtbot …" profiles
```

It never edits existing titles or other profiles. New adds from the bot use
`smarrtbot 1080p` automatically (unless `radarr.quality_profile` / `sonarr.quality_profile`
pin another). Re-running is idempotent (everything shows `=`).

## 7. Download notices (Radarr/Sonarr built-in Telegram connection)

```
smarrtbot setup-notify --chat-id <GROUP_CHAT_ID>           # dry run
smarrtbot setup-notify --chat-id <GROUP_CHAT_ID> --apply   # create/update + test message
```

This creates a notification named `smarrtbot` (type Telegram) in each app with grab, import,
upgrade and health events. It stores the bot token inside Radarr/Sonarr (their own config).
Manual alternative: *Settings → Connect → + → Telegram*, same token and chat id.

## 8. Dashboard tile (e.g. Homepage)

```yaml
- smarrtbot:
    icon: telegram.svg
    description: Telegram media bot
    siteMonitor: http://<HOST>:<HEALTH_PORT>/healthz
```

`/healthz` returns 200 when polling runs and the scheduler heartbeat is fresh, else 503. It
exposes only status, version and uptime.

## 9. Day-2 operations

- **Add a Gemini key:** append a line to `secrets/gemini_keys`. Reloaded automatically.
- **Models:** `/models` shows ranking, status (`ok`, `untested`, `notfree`, `dead`, `gone`),
  cooldowns and per-key usage. Pin one with `gemini.pinned_model`.
- **When all keys are exhausted** the bot answers "AI busy, try /help or later"; commands keep
  working.
- **Logs** never contain secrets (they are redacted), but keep them private anyway.
- **Backup:** copy `state/` (SQLite in WAL mode; stop the container or copy all three files).

## 10. Upgrade and rollback

- Upgrade: change the image tag in the stack to the new version and redeploy. Pull one image at
  a time on fragile disks.
- Rollback: set the previous tag and redeploy. The database schema is versioned
  (`PRAGMA user_version`); a release that changes it says so in the changelog. Keep a copy of
  `state/` before upgrading to roll back across schema changes.
- Kill switch: `SMARRTBOT_DRY_RUN=true` and restart, or stop the container.
