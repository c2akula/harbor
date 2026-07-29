"""Per-user API keys for the model server.

One file per person under /weights/keys — on the volume, so a redeployed box
carries the team's access with the weights. A token is shown ONCE at issue
time; the server accepts any listed token (vLLM repeatable --api-key), so
revocation is deletion plus a unit re-render.
"""
from __future__ import annotations

import re
import secrets
import subprocess

from . import state
from .config import Config

KEYS_DIR = "/weights/keys"
_NAME = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


class KeyExists(RuntimeError):
    pass


def _run(cfg: Config, script: str) -> str:
    r = subprocess.run(
        ["ssh", "-i", str(cfg.ssh_key), "-o", "BatchMode=yes",
         "-o", "StrictHostKeyChecking=accept-new",
         "-o", "ConnectTimeout=5", f"{cfg.ssh_user}@{cfg.vm_ip}",
         f"mkdir -p {KEYS_DIR} && cd {KEYS_DIR} && {script}"],
        capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise state.StateUnavailable(r.stderr.strip() or "box unreachable")
    return r.stdout


def _check(name: str) -> str:
    if not _NAME.match(name or ""):
        raise ValueError(f"key name {name!r}: letters, digits, - and _ only")
    return name


def add(cfg: Config, name: str) -> str:
    """Issue a key for `name`; returns the token — the only time it is shown."""
    _check(name)
    if name in list_names(cfg):
        raise KeyExists(f"a key named {name!r} already exists — revoke first")
    token = f"hbr-{secrets.token_urlsafe(24)}"
    _run(cfg, f"umask 077 && echo '{token}' > '{name}.key'")
    return token


def revoke(cfg: Config, name: str) -> None:
    _check(name)
    _run(cfg, f"rm -f '{name}.key'")


def list_names(cfg: Config) -> list[str]:
    out = _run(cfg, "ls -1 2>/dev/null || true")
    return sorted(f[:-4] for f in out.split() if f.endswith(".key"))


def tokens(cfg: Config) -> list[str]:
    """Every valid token, for the serving unit's --api-key list."""
    out = _run(cfg, "cat -- *.key 2>/dev/null || true")
    return [t for t in (line.strip() for line in out.splitlines()) if t]
