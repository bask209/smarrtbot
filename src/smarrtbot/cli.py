"""Command-line entry point.

smarrtbot run                      start the bot (long polling)
smarrtbot check-config             validate config and secret files (prints no secrets)
smarrtbot setup-arr [--apply]      custom formats + quality profiles (dry run by default)
smarrtbot setup-notify --chat-id N [--apply]   *arr built-in Telegram notifications
smarrtbot healthcheck              exit 0 if the local health endpoint is OK
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence

from smarrtbot import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="smarrtbot", description="smarrtbot Telegram bot")
    parser.add_argument("--version", action="version", version=f"smarrtbot {__version__}")
    parser.add_argument("--config", help="path to config.yaml (default $SMARRTBOT_CONFIG)")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("run", help="start the bot")
    sub.add_parser("check-config", help="validate configuration and secret files")
    p = sub.add_parser("setup-arr", help="create custom formats and quality profiles")
    p.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    n = sub.add_parser("setup-notify", help="configure the *arr Telegram connections")
    n.add_argument("--chat-id", type=int, required=True)
    n.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    n.add_argument("--no-test", action="store_true", help="skip the test message")
    h = sub.add_parser("healthcheck", help="probe the local health endpoint")
    h.add_argument("--url", default=None)
    return parser


def _settings(args: argparse.Namespace):  # type: ignore[no-untyped-def]
    from smarrtbot.config import load_settings
    from smarrtbot.logsafe import setup_logging

    settings = load_settings(args.config)
    setup_logging(settings.log_level)
    return settings


def _arr_clients(settings) -> dict:  # type: ignore[no-untyped-def,type-arg]
    from smarrtbot.services import build_services

    svc = build_services(settings, with_agent=False)
    clients = {}
    if svc.radarr is not None:
        clients["radarr"] = svc.radarr
    if svc.sonarr is not None:
        clients["sonarr"] = svc.sonarr
    return clients


def cmd_check_config(args: argparse.Namespace) -> int:
    from smarrtbot.config import SecretError, read_secret

    s = _settings(args)
    problems: list[str] = []
    checks = [("telegram token", s.telegram.token_file, True)]
    checks += [("gemini keys", s.gemini.keys_file, True)]
    if s.radarr.enabled:
        checks.append(("radarr api key", s.radarr.api_key_file, True))
    if s.sonarr.enabled:
        checks.append(("sonarr api key", s.sonarr.api_key_file, True))
    if s.qbittorrent.enabled:
        checks.append(("qbittorrent password", s.qbittorrent.password_file, True))
    if s.mediaserver.kind == "plex":
        checks.append(("plex token", s.mediaserver.token_file, True))
    for name, path, _req in checks:
        try:
            read_secret(path, name=name)
            print(f"ok    {name}: {path}")
        except SecretError as exc:
            problems.append(str(exc))
            print(f"FAIL  {exc}")
    if not s.telegram.owner_id:
        problems.append("telegram.owner_id is not set")
        print("FAIL  telegram.owner_id is not set")
    print(f"dry_run={s.dry_run} groups={s.telegram.allowed_group_ids} data_dir={s.data_dir}")
    return 1 if problems else 0


def cmd_setup_arr(args: argparse.Namespace) -> int:
    from smarrtbot.setup_arr import plan_and_apply

    clients = _arr_clients(_settings(args))
    plan = asyncio.run(plan_and_apply(clients, apply=args.apply))
    print(plan.render())
    if not args.apply:
        n = len(plan.pending())
        print(f"\nDRY RUN: {n} change(s). Re-run with --apply to write them.")
    return 0


def cmd_setup_notify(args: argparse.Namespace) -> int:
    from smarrtbot.config import read_secret
    from smarrtbot.logsafe import register_secret
    from smarrtbot.setup_notify import plan_and_apply

    settings = _settings(args)
    token = read_secret(settings.telegram.token_file, name="telegram token")
    register_secret(token, "<telegram-token>")
    clients = _arr_clients(settings)
    lines = asyncio.run(
        plan_and_apply(clients, token, args.chat_id, apply=args.apply, test=not args.no_test)
    )
    print("\n".join(lines))
    if not args.apply:
        print("\nDRY RUN: re-run with --apply to write.")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from smarrtbot.bot.app import run
    from smarrtbot.services import build_services

    settings = _settings(args)
    run(build_services(settings))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd is None:
        build_parser().print_help()
        return 0
    if args.cmd == "healthcheck":
        from smarrtbot.health import probe

        return probe(args.url) if args.url else probe()
    handlers = {
        "run": cmd_run,
        "check-config": cmd_check_config,
        "setup-arr": cmd_setup_arr,
        "setup-notify": cmd_setup_notify,
    }
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
