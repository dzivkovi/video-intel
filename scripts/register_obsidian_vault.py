#!/usr/bin/env python3
"""Register a folder as an Obsidian vault by writing it into Obsidian's registry.

Obsidian keeps every vault it knows in one JSON file (per platform):

    Windows  %APPDATA%\\obsidian\\obsidian.json
    macOS    ~/Library/Application Support/obsidian/obsidian.json
    Linux    ~/.config/obsidian/obsidian.json

The in-app "Change vault..." switcher only *filters* that list; it cannot add a
folder. The only built-in GUI action that adds one is the easy-to-miss "Open
folder as vault" button. This script appends the entry for you (idempotent) so
the vault appears in the list, or opens directly with --open.

IMPORTANT: Obsidian rewrites this file from memory when it quits, so it must be
CLOSED while the file is edited. This script refuses to run if Obsidian appears
to be running (override with --force). Every run writes a timestamped backup and
replaces the registry atomically (temp file + os.replace).

Convenience for the generated Lead-Lag Creator Atlas (scripts/wiki_atlas.py),
but works for any folder. Runbook: docs/intelligence-layer.md.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path


def registry_path() -> Path:
    system = platform.system()
    if system == "Windows":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return base / "obsidian" / "obsidian.json"
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "obsidian" / "obsidian.json"
    return Path.home() / ".config" / "obsidian" / "obsidian.json"


def _norm(path: str | Path) -> str:
    """Compare-safe form of a path: collapses `.`/`..`/redundant separators and,
    on case-insensitive platforms, case. So `X/_wiki`, `X/_wiki/`, and `X/a/../_wiki`
    all match one registered vault instead of duplicating it."""
    return os.path.normcase(os.path.normpath(str(path)))


def _vault_id(folder: Path) -> str:
    """Stable 16-hex id derived from the normalized path only (Obsidian's id width).
    Deriving from the path, not time, means re-registering the same folder always
    lands on the same id rather than accumulating duplicates."""
    return hashlib.sha1(_norm(folder).encode()).hexdigest()[:16]


def load_registry(registry: Path) -> dict:
    try:
        return json.loads(registry.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        sys.exit(
            f"Obsidian registry at {registry} is not valid JSON ({e}). "
            "Do not overwrite it blindly - back it up, then either fix the JSON or let "
            "Obsidian recreate it by opening a vault through the app once."
        )
    except OSError as e:
        sys.exit(f"could not read Obsidian registry {registry}: {e}")


def write_registry_atomic(registry: Path, cfg: dict) -> None:
    tmp = registry.with_name(f"{registry.name}.tmp-{os.getpid()}")
    tmp.write_text(json.dumps(cfg), encoding="utf-8")
    os.replace(tmp, registry)  # atomic on the same filesystem


def register_vault(cfg: dict, folder: Path, make_open: bool) -> tuple[str, bool]:
    """Add-or-update the vault entry in cfg. Returns (id, was_new). Pure over the
    dict, so it is unit-testable without touching a real registry or Obsidian."""
    vaults = cfg.setdefault("vaults", {})
    target = _norm(folder)

    if make_open:
        for v in vaults.values():
            v.pop("open", None)

    ts = int(time.time() * 1000)
    existing_id = next((vid for vid, v in vaults.items() if _norm(v.get("path", "")) == target), None)
    if existing_id is not None:
        vaults[existing_id]["ts"] = ts
        if make_open:
            vaults[existing_id]["open"] = True
        return existing_id, False

    new_id = _vault_id(folder)
    # only collides if a *different* path already hashed here; walk off deterministically
    while new_id in vaults and _norm(vaults[new_id].get("path", "")) != target:
        new_id = hashlib.sha1((new_id + "x").encode()).hexdigest()[:16]
    entry: dict[str, object] = {"path": str(folder), "ts": ts}
    if make_open:
        entry["open"] = True
    vaults[new_id] = entry
    return new_id, True


def obsidian_running() -> bool:
    """Best-effort process check; returns False if it cannot tell."""
    system = platform.system()
    try:
        if system == "Windows":
            out = subprocess.run(["tasklist"], capture_output=True, text=True, timeout=10).stdout
            return "obsidian.exe" in out.lower()
        out = subprocess.run(["pgrep", "-i", "obsidian"], capture_output=True, text=True, timeout=10)
        return out.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def launch_obsidian() -> bool:
    system = platform.system()
    try:
        if system == "Windows":
            exe = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Obsidian" / "Obsidian.exe"
            if exe.exists():
                subprocess.Popen([str(exe)])
                return True
        elif system == "Darwin":
            subprocess.Popen(["open", "-a", "Obsidian"])
            return True
        elif shutil.which("obsidian"):
            subprocess.Popen(["obsidian"])
            return True
    except (OSError, subprocess.SubprocessError):
        pass
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Register a folder as an Obsidian vault (idempotent).")
    parser.add_argument("path", type=Path, help="folder to register (e.g. <output_dir>/_wiki)")
    parser.add_argument("--open", action="store_true", help="make this the vault Obsidian opens on next launch")
    parser.add_argument("--launch", action="store_true", help="start Obsidian after registering")
    parser.add_argument("--force", action="store_true", help="edit even if Obsidian looks like it is running")
    args = parser.parse_args()

    folder = args.path.expanduser()
    if not folder.is_dir():
        sys.exit(f"folder not found: {folder} (create it first, or fix the path)")
    folder = folder.resolve()

    registry = registry_path()
    if not registry.exists():
        sys.exit(f"Obsidian registry not found at {registry} (run Obsidian at least once first)")

    if obsidian_running() and not args.force:
        sys.exit(
            "Obsidian appears to be running. Quit it fully (tray/menu -> Quit), then re-run.\n"
            "A running Obsidian rewrites its registry on exit and would wipe this edit. "
            "Pass --force to override."
        )

    backup = registry.with_name(f"{registry.name}.{time.strftime('%Y%m%d-%H%M%S')}.bak")
    shutil.copy2(registry, backup)

    cfg = load_registry(registry)
    vault_id, was_new = register_vault(cfg, folder, args.open)
    write_registry_atomic(registry, cfg)

    print(
        f"registered new vault (id {vault_id}): {folder}"
        if was_new
        else f"already registered (id {vault_id}); path unchanged"
    )
    print(f"backup: {backup}")
    print("\nObsidian now knows these vaults:")
    for vid, v in cfg["vaults"].items():
        flag = "  <- opens on launch" if v.get("open") else ""
        print(f"  {vid}  {v.get('path', '?')}{flag}")

    if args.launch:
        print("\nlaunching Obsidian..." if launch_obsidian() else "\ncould not launch Obsidian; start it yourself.")
    else:
        print("\nStart Obsidian normally; the vault is in the list (and opens on launch if you used --open).")


if __name__ == "__main__":
    main()
