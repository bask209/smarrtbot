# Secrets folder layout

Create these files in `<SMARRTBOT_DIR>/secrets/` on the host and mount that folder at
`/run/secrets` (read-only). Each file holds only the secret, on one line. Make the folder `700`
and each file `600`, owned by UID 1000 (the container user).

```
secrets/
├── telegram_token          # from @BotFather
├── gemini_keys             # one Gemini API key per line (separate Google projects); # comments ok
├── radarr_api_key          # Radarr → Settings → General → API Key
├── sonarr_api_key          # Sonarr → Settings → General → API Key
├── qbittorrent_password    # qBittorrent Web UI password
└── plex_token              # X-Plex-Token (only if mediaserver.kind is plex)
```

The `*.example` files next to this README only show the format. Never copy real values into
this repository.

Add a Gemini key: append a line to `gemini_keys`. The bot reloads the file automatically
(no restart, never via Telegram).
