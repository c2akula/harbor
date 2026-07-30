"""Render and install harbor's systemd user units from package templates."""
from __future__ import annotations

import subprocess
from importlib import resources
from pathlib import Path

from .config import Config

UNIT_DIR = Path.home() / ".config" / "systemd" / "user"


def _daemon_reload() -> None:
    """Reload if this host has a user systemd; carry on if it does not (macOS,
    WSL, containers). The units are on disk either way for a later reload."""
    try:
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    except (OSError, subprocess.SubprocessError):
        print("no user systemd here — units written but not loaded")


def install_units(cfg: Config) -> None:
    # Both units act on a managed machine: one tunnels to it, the other parks
    # it when idle. With a bring-your-own endpoint there is neither.
    if not cfg.manages_vm:
        print("no machine to manage — no units to install")
        return

    UNIT_DIR.mkdir(parents=True, exist_ok=True)
    pkg = resources.files("harbor") / "systemd"

    # The box serves on the hub address directly; the only laptop-side unit
    # left is the watchdog. Retire a tunnel from an earlier install if present.
    (UNIT_DIR / "harbor-tunnel.service").unlink(missing_ok=True)
    for name in ("harbor-watchdog.timer", "harbor-watchdog.service"):
        (UNIT_DIR / name).write_text((pkg / name).read_text())

    _daemon_reload()
    print(f"systemd units rendered to {UNIT_DIR}")
