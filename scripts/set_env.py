r"""Update /opt/copilot/.env from a JSON file, safely.

Why this exists: every attempt to edit secrets with inline ``sed`` over SSH lost
to shell quoting (backticks, '#', quotes). Reading the values from a JSON file
removes the shell from the equation entirely.

Values are written single-quoted so both our loader (app/config.py) and systemd's
EnvironmentFile parse them identically; existing quotes are stripped first, so
re-running is idempotent.

Usage on the server:
    python set_env.py '{"OSS_BUCKET": "copilot-artifacts-ly"}'
    python set_env.py --masked          # show current OSS/DB keys, values masked
"""
from __future__ import annotations

import json
import os
import pathlib
import sys

ENV_PATH = pathlib.Path("/opt/copilot/.env")
MASK_SUFFIXES = ("_PASSWORD", "_SECRET", "_KEY", "_ACCESS_KEY_ID", "_API_KEY")


def strip_quotes(value: str) -> str:
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        return v[1:-1]
    return v


def load_lines() -> list[str]:
    if not ENV_PATH.exists():
        return []
    return ENV_PATH.read_text(encoding="utf-8").splitlines()


def update(pairs: dict[str, str]) -> list[str]:
    lines = load_lines()
    seen: set[str] = set()
    for i, line in enumerate(lines):
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key = line.split("=", 1)[0].strip()
        if key in pairs:
            lines[i] = f"{key}='{pairs[key]}'"
            seen.add(key)
    for key, value in pairs.items():
        if key not in seen:
            lines.append(f"{key}='{value}'")
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(ENV_PATH, 0o600)
    return sorted(seen), sorted(set(pairs) - seen)


def mask(key: str, value: str) -> str:
    if any(key.endswith(s) or key == s for s in MASK_SUFFIXES):
        return f"<len={len(value)}>" if value else "<empty>"
    return value or "<empty>"


def show() -> None:
    for line in load_lines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, _, raw = line.partition("=")
        key = key.strip()
        if not (key.startswith(("OSS_", "DB_"))):
            continue
        print(f"  {key:24} {mask(key, strip_quotes(raw))}")


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--masked":
        show()
        return 0
    if len(sys.argv) < 2:
        print("usage: set_env.py --file <path.json> | '{json}' | --masked")
        return 2

    # Prefer a file: passing JSON on the command line means every layer of
    # shell (pwsh -> ssh -> bash) gets a chance to mangle quotes and $().
    if sys.argv[1] == "--file" and len(sys.argv) > 2:
        pairs = json.loads(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))
    else:
        pairs = json.loads(sys.argv[1])

    updated, added = update(pairs)
    print(f"updated: {updated or '(none)'}")
    print(f"added  : {added or '(none)'}")
    show()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
