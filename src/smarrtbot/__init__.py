"""smarrtbot: chat-driven Radarr/Sonarr requests and storage cleanup over Telegram."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("smarrtbot")
except PackageNotFoundError:  # running from a source tree without install
    __version__ = "0.0.0+local"
