# Configuration reference

Sources, highest precedence first:

1. environment variables: `SMARRTBOT_<KEY>`, nested with `__`
   (e.g. `SMARRTBOT_TELEGRAM__OWNER_ID=123`, `SMARRTBOT_DRY_RUN=false`);
2. the YAML file at `$SMARRTBOT_CONFIG` (default `/config/config.yaml`).

Secrets are never config values; config holds only the **path** of each secret file.

## Top level

| Key | Default | Meaning |
|---|---|---|
| `dry_run` | `true` | When true, confirmed actions only report what they would do. |
| `log_level` | `INFO` | `DEBUG`, `INFO`, `WARNING`… Secrets are redacted at every level. |
| `data_dir` | `/data/state` | SQLite database and scan cache. |
| `health_host` / `health_port` | `0.0.0.0` / `8080` | Health endpoint (`/healthz`). |

## `telegram`

| Key | Default | Meaning |
|---|---|---|
| `token_file` | `/run/secrets/telegram_token` | Bot token file. |
| `owner_id` | `0` | Owner's Telegram user id. Required; nothing but `/whoami` works until set. |
| `allowed_group_ids` | `[]` | Groups the bot answers in. DMs from approved users always work. |
| `notify_chat_id` | first group | Target for TV-check flags, alerts and weekly summaries. |

## `radarr`, `sonarr`

| Key | Default | Meaning |
|---|---|---|
| `url` | `""` (disabled) | Base URL, e.g. `http://host.docker.internal:7878`. |
| `api_key_file` | – | API key file. |
| `quality_profile` | auto | Name or id for new adds. Auto: `smarrtbot 1080p`, else `HD-1080p`, else first. |
| `root_folder` | first | Root folder path for new adds. |
| `timeout` | `20` | Seconds per request (requests retry with backoff when the service drops). |

## `qbittorrent`

| Key | Default | Meaning |
|---|---|---|
| `url` | `""` (disabled) | Web UI base URL. |
| `username` | `admin` | Web UI user. |
| `password_file` | – | Web UI password file. |

## `mediaserver`

| Key | Default | Meaning |
|---|---|---|
| `kind` | `none` | `plex` or `none` (TV check disabled without a media server). |
| `url` | `""` | e.g. `http://host.docker.internal:32400`. |
| `token_file` | – | X-Plex-Token file. |

## `gemini`

| Key | Default | Meaning |
|---|---|---|
| `keys_file` | `/run/secrets/gemini_keys` | One key per line, `#` comments allowed, hot-reloaded. |
| `base_url` | Google endpoint | Override only for testing. |
| `pinned_model` | `null` | Use this model first while it works. |
| `max_consecutive_failures` | `5` | A model is marked `dead` after this many; revived daily. |
| `timeout` | `45` | Seconds per request. |
| `max_tool_rounds` | `4` | Tool-call rounds per message. |
| `temperature` | `0.2` | Sampling temperature. |
| `memory_turns` | `12` | Recent turns sent as context. |
| `summarize_after_turns` | `24` | Older turns are folded into a rolling summary. |

## `limits`

| Key | Default | Meaning |
|---|---|---|
| `deletes_per_hour` | `5` | Per-user title deletes per hour (real deletes only). |
| `owner_exempt_from_rate_limit` | `true` | Owner bypasses the hourly limit. |
| `daily_delete_cap_gb` | `500` | Total bytes deleted (delete + cleanup) per 24 h, for everyone. `0` disables. |
| `pending_ttl_seconds` | `300` | Confirm buttons expire after this. |

## `storage`

| Key | Default | Meaning |
|---|---|---|
| `movies_root` / `tv_root` / `torrents_root` | `/data/media/movies` … | Allowed read roots (container paths). |
| `writable_roots` | `[/data/torrents]` | The only place the guarded filesystem delete may act. Must be inside a read root. |
| `path_maps` | `[]` | `[{src, dst}]` to translate service paths to the bot's view. |
| `alert_free_percent` / `alert_free_gb` | `10` / `100` | Alert once when free space drops below either. |
| `alert_check_minutes` | `30` | How often thresholds are checked. |
| `weekly_summary` | `false` | Post a storage summary on Mondays. |
| `scan_interval_hours` | `24` | Full scan (idle IO priority, cached). |
| `scan_min_interval_minutes` | `60` | Never scan more often than this. |
| `orphan_min_age_minutes` | `120` | Files newer than this are never orphan candidates. |
| `ignore_dirs` | `[incomplete, .incomplete]` | Folder names skipped by the scan. |
| `suspicious_extensions` | `.exe .scr .bat .cmd .com .lnk .msi .iso` | Flagged as suspicious. |

## `tvcheck`

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Poll imports and inspect them through the media server. |
| `interval_minutes` | `5` | Poll interval. |
| `max_bitrate_mbps_4k` / `max_bitrate_mbps_1080p` | `25` / `12` | Bitrate caps (placeholders until tested). |
| `max_attempts` | `6` | Inspection retries while the media server scans. |
| `first_retry_minutes` | `5` | First retry delay; doubles each attempt. |

## Commands

`/help /status /queue /storage /cleanup [category] /delete <title> [season N] /models /whoami
/forget`, owner-only `/revoke <id>` and `/users`, plus natural language (EN / Latam ES).

CLI: `smarrtbot run | check-config | setup-arr [--apply] | setup-notify --chat-id N [--apply] |
healthcheck`.
