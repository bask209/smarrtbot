# smarrtbot

[![CI](https://github.com/bask209/smarrtbot/actions/workflows/ci.yml/badge.svg)](https://github.com/bask209/smarrtbot/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

> **Status: pre-release (0.x).** Feature-complete for the spec; being soak-tested. Design: [SPEC.md](SPEC.md) · Deploy: [docs/DEPLOY.md](docs/DEPLOY.md) · Config: [docs/CONFIG.md](docs/CONFIG.md)

A Telegram bot for a small, trusted group. People ask for movies and shows in English or Latin American Spanish, and Gemini function calling turns those messages into Radarr and Sonarr actions. It also reports disk usage and helps clean up storage by chat, strictly inside the media and torrents folders.

## Highlights

- **Chat requests** in EN / Latam-ES, routed through Gemini function calling to Radarr and Sonarr.
- **Confirmations enforced in code**: adds, deletes and cleanups need an inline-button tap from the same person who asked, with a nonce and an expiry. The model can only *propose*.
- **Hardlink-aware deletes**: removes the title through Radarr/Sonarr, *then* the matching torrent through qBittorrent, so the space is actually freed. Torrents shared with other titles are kept and reported.
- **Storage dashboard and cleanup**: free space, trends, orphans and duplicates, with every path confined to configured roots.
- **TV-friendly check**: flags imports a client can't Direct Play (Dolby Vision, TrueHD, DTS, PGS/ASS subtitles, bitrate over a cap) through a pluggable media-server layer (Plex first).
- **Free-tier friendly**: a pool of Gemini keys with per-key/model cooldowns and a daily model picker; falls back to plain commands when the AI is busy.
- **Safe by default**: `DRY_RUN` on, long polling only (no inbound ports), secrets as mounted files, non-root container.

## Quick start

1. Read [docs/DEPLOY.md](docs/DEPLOY.md).
2. Copy [`examples/config.example.yaml`](examples/config.example.yaml) and
   [`examples/stack.example.yml`](examples/stack.example.yml), create the secret files
   ([layout](examples/secrets-layout/README.md)).
3. Start with `dry_run: true` in a private chat, then enable your group.

```
smarrtbot run | check-config | setup-arr [--apply] | setup-notify --chat-id N [--apply] | healthcheck
```

## Not in scope

Books or shadow libraries, request managers such as Overseerr/Jellyseerr/Seerr, and indexer or tracker lists. Indexers stay in Prowlarr.

## Development

```sh
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
git config core.hooksPath scripts/hooks   # gitleaks pre-commit hook (needs gitleaks on PATH)
ruff check . && ruff format --check . && pytest
```

Or use the [pre-commit](https://pre-commit.com) framework with `.pre-commit-config.yaml`.

## Privacy note

On the free tier, Google may use Gemini prompts to improve its products. Using several free keys may breach Google's terms; operators accept that risk. The bot's `/help` repeats this notice.

## License

[MIT](LICENSE)
