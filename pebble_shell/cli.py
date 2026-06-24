from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pebble", description="Pebble Shell command line interface.")
    subparsers = parser.add_subparsers(dest="command")

    serve = subparsers.add_parser("serve", help="Run Pebble Shell in the foreground.")
    serve.add_argument(
        "--env-file",
        default=None,
        help="Environment file to load before serving. Defaults to .env, then ~/.pebble-shell/.env.",
    )

    args = parser.parse_args(argv)
    if args.command == "serve":
        env_file = _resolve_env_file(args.env_file)
        if env_file:
            _load_env_file(env_file)
        from .__main__ import main as serve_main

        asyncio.run(serve_main())
        return 0

    parser.print_help()
    return 0


def _resolve_env_file(explicit: str | None) -> Path | None:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    env_file = os.environ.get("PEBBLE_ENV_FILE")
    if env_file:
        candidates.append(Path(env_file).expanduser())
    candidates.append(Path.cwd() / ".env")
    pebble_home = Path(os.environ.get("PEBBLE_HOME", "~/.pebble-shell")).expanduser()
    candidates.append(pebble_home / ".env")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _load_env_file(path: Path) -> None:
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


if __name__ == "__main__":
    raise SystemExit(main())
