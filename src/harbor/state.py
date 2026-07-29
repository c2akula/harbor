"""Watchdog state shared by hold, status, and the watchdog itself."""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "harbor"
LEGACY_STATE_DIR = Path.home() / ".local" / "state" / "llm-watchdog"


def state_dir() -> Path:
    override = os.environ.get("LLM_WD_STATE_DIR")
    if override:
        d = Path(override)
        d.mkdir(parents=True, exist_ok=True)
        return d
    # One-time migration: live state (hold expiry, strike count, decision log)
    # moves with the rename so an active hold survives it.
    if LEGACY_STATE_DIR.exists() and not DEFAULT_STATE_DIR.exists():
        LEGACY_STATE_DIR.rename(DEFAULT_STATE_DIR)
    DEFAULT_STATE_DIR.mkdir(parents=True, exist_ok=True)
    return DEFAULT_STATE_DIR


class StateUnavailable(RuntimeError):
    """The shared state on the box cannot be reached. Callers abstain from
    destructive action — an unreachable box costs money, never work."""


# Holds, strikes and the warned marker govern a SHARED machine, so they live
# on that machine (~/.harbor-state, root disk — survives hibernation). Two
# watchdogs on one idle period must count one strike, so the bump is windowed
# and runs server-side as a single script rather than read-modify-write.
BOX_STATE_DIR = "~/.harbor-state"
STRIKE_WINDOW = 480     # seconds; slightly under the watchdog tick


def _box_run(cfg, script: str) -> str:
    r = subprocess.run(
        ["ssh", "-i", str(cfg.ssh_key), "-o", "BatchMode=yes",
         "-o", "StrictHostKeyChecking=accept-new",
         "-o", "ConnectTimeout=5", f"{cfg.ssh_user}@{cfg.vm_ip}",
         f"mkdir -p {BOX_STATE_DIR} && cd {BOX_STATE_DIR} && {script}"],
        capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise StateUnavailable(r.stderr.strip() or "box state unreachable")
    return r.stdout


def _shared(cfg) -> bool:
    # Env override keeps tests and single-box debugging on local files.
    return (cfg is not None and cfg.manages_vm
            and not os.environ.get("LLM_WD_STATE_DIR"))


def hold_expiry(cfg=None) -> int | None:
    """Epoch seconds the current hold expires at, or None if no hold."""
    if _shared(cfg):
        out = _box_run(cfg, "cat hold 2>/dev/null || true").strip()
        return int(out) if out.isdigit() else (None if not out else 0)
    f = state_dir() / "hold"
    if not f.exists():
        return None
    try:
        return int(f.read_text().strip())
    except ValueError:
        return 0


def set_hold(hours: float, cfg=None) -> int:
    expiry = int(time.time() + hours * 3600)
    if _shared(cfg):
        _box_run(cfg, f"echo {expiry} > hold && rm -f pending_hibernate")
        return expiry
    (state_dir() / "hold").write_text(f"{expiry}\n")
    clear_pending()
    return expiry


def release_hold(cfg=None) -> None:
    if _shared(cfg):
        _box_run(cfg, "rm -f hold")
        return
    (state_dir() / "hold").unlink(missing_ok=True)


def clear_pending(cfg=None) -> None:
    if _shared(cfg):
        _box_run(cfg, "rm -f pending_hibernate")
        return
    (state_dir() / "pending_hibernate").unlink(missing_ok=True)


def bump_strikes(cfg=None) -> int:
    """One windowed increment; returns the count. Concurrent watchdogs within
    a window observe the same idle period and must not multiply it."""
    script = (
        'now=$(date +%s); read cnt ts < idle_count 2>/dev/null || '
        '{ cnt=0; ts=0; }; '
        f'if [ $((now - ts)) -ge {STRIKE_WINDOW} ]; then '
        'cnt=$((cnt + 1)); echo "$cnt $now" > idle_count; fi; echo "$cnt"'
    )
    if _shared(cfg):
        out = _box_run(cfg, script).strip()
        return int(out) if out.isdigit() else 0
    d = state_dir()
    now = int(time.time())
    try:
        cnt, ts = (int(x) for x in (d / "idle_count").read_text().split())
    except (FileNotFoundError, ValueError):
        cnt, ts = 0, 0
    if now - ts >= STRIKE_WINDOW:
        cnt += 1
        (d / "idle_count").write_text(f"{cnt} {now}\n")
    return cnt


def reset_strikes(cfg=None) -> None:
    if _shared(cfg):
        _box_run(cfg, "rm -f idle_count")
        return
    (state_dir() / "idle_count").unlink(missing_ok=True)


def warned(cfg=None) -> bool:
    if _shared(cfg):
        return _box_run(cfg, "test -f pending_hibernate && echo y || true"
                        ).strip() == "y"
    return (state_dir() / "pending_hibernate").exists()


def set_warned(cfg=None) -> None:
    if _shared(cfg):
        _box_run(cfg, "touch pending_hibernate")
        return
    (state_dir() / "pending_hibernate").touch()


def clear_warned(cfg=None) -> None:
    clear_pending(cfg)
