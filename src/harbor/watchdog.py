"""Idle watchdog: runs from a systemd user timer every 10 min. If the box is
ACTIVE and served no requests for 3 consecutive checks, warn, then hibernate
one tick later unless held — an unattended box still parks, a present operator
gets a say.

Probes are overridable via LLM_WD_* env vars (shell commands) so behaviour is
testable without a VM — the same contract the shell version had."""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from . import state as state_mod
from .config import Config

STRIKE_LIMIT = 3


def _sh(cmd: str) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, shell=True, capture_output=True, text=True)


def _probe_state(cfg: Config) -> str:
    if cmd := os.environ.get("LLM_WD_STATE_CMD"):
        return _sh(cmd).stdout.strip()
    from . import hyperstack
    return hyperstack.vm_state(cfg)


def _tmux_alive_local() -> bool:
    cmd = os.environ.get("LLM_WD_LOCAL_TMUX_CMD", "tmux ls 2>/dev/null | grep -q .")
    return _sh(cmd).returncode == 0


def _tmux_alive_vm(cfg: Config) -> bool:
    if cmd := os.environ.get("LLM_WD_VM_TMUX_CMD"):
        return _sh(cmd).returncode == 0
    from . import wgnet
    try:
        host = wgnet.ssh_host(cfg)
    except wgnet.CacheMissing:
        return False        # never upped from here: nothing to keep alive
    cmd = (f"ssh -i {cfg.ssh_key} -o ConnectTimeout=10 -o BatchMode=yes "
           f"{cfg.ssh_user}@{host} 'tmux ls 2>/dev/null | grep -q .' 2>/dev/null")
    return _sh(cmd).returncode == 0


def _recent_traffic(cfg: Config) -> str:
    """Completions since the previous tick, or 'ERR' when unknowable.

    Reads the server's own request counter rather than grepping its logs: log
    markers are engine-specific (llama.cpp's 'print_timing' has no vLLM
    equivalent), and a probe that silently stops matching would let the
    watchdog park a box that is actively serving. A counter delta needs no ssh
    and works through the tunnel.
    """
    if cmd := os.environ.get("LLM_WD_TRAFFIC_CMD"):
        out = _sh(cmd).stdout.strip()
        return out if out else "ERR"
    try:
        import requests
        r = requests.get(f"{cfg.endpoint_url}/metrics", timeout=10)
        r.raise_for_status()
        total = 0.0
        for line in r.text.splitlines():
            if line.startswith("vllm:request_success_total"):
                total += float(line.rsplit(" ", 1)[-1])
        marker = state_mod.state_dir() / "request_total"
        try:
            previous = float(marker.read_text().strip())
        except (FileNotFoundError, ValueError):
            marker.write_text(f"{total}\n")
            return "ERR"          # first observation: no delta, decide nothing
        marker.write_text(f"{total}\n")
        return str(max(0, int(total - previous)))
    except Exception:
        return "ERR"


def _down(cfg: Config) -> None:
    if cmd := os.environ.get("LLM_WD_DOWN_CMD"):
        _sh(cmd)
        return
    from . import lifecycle
    lifecycle.down(cfg)


def _notify(message: str) -> None:
    if cmd := os.environ.get("LLM_WD_NOTIFY_CMD"):
        subprocess.run(cmd, shell=True, input=message, capture_output=True, text=True)
        return
    env = dict(os.environ)
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{os.getuid()}/bus")
    subprocess.run(["notify-send", "-u", "critical", "harbor", message],
                   capture_output=True, env=env)


def _track_stock(cfg: Config, d: Path) -> None:
    """While the box is parked, silently sample provider stock each tick and
    shout once when it returns from zero — a hibernated VM can only resume
    when stock exists."""
    try:
        if cmd := os.environ.get("LLM_WD_STOCK_CMD"):
            out = _sh(cmd).stdout.strip()
            stock, gpu = (int(out) if out else None), "GPU"
        else:
            from . import hyperstack
            _, gpu = hyperstack.vm_info(cfg)
            stock = hyperstack.gpu_stock(cfg, gpu) if gpu else None
        if stock is None:
            return
        marker = d / "stock_last"
        try:
            prev = int(marker.read_text().strip())
        except (FileNotFoundError, ValueError):
            prev = None
        marker.write_text(f"{stock}\n")
        if prev == 0 and stock > 0:
            _log(d, f"STOCK RETURNED ({gpu}: {stock})")
            _notify(f"{gpu} stock returned ({stock} available) — "
                    "'harbor up' can resume the box now.")
    except Exception:
        pass


def _log(state_dir: Path, line: str) -> None:
    stamp = time.strftime("%F %T")
    with open(state_dir / "decisions.log", "a") as f:
        f.write(f"{stamp} {line}\n")


def tick(cfg: Config) -> int:
    d = state_mod.state_dir()           # local: logs, traffic marker, stock

    vm = _probe_state(cfg)
    if vm != "ACTIVE":
        if vm:                          # parked (not API-down): watch stock
            _track_stock(cfg, d)
        return 0                        # a box that is off has no strikes

    # Holds, strikes and the warned marker are SHARED state on the box: any
    # user's hold must stop every user's watchdog. Unreachable shared state
    # means abstain — never park what you cannot see.
    try:
        expiry = state_mod.hold_expiry(cfg)
        if expiry is not None:
            if expiry > time.time():
                state_mod.clear_pending(cfg)
                state_mod.reset_strikes(cfg)
                return 0
            state_mod.release_hold(cfg)  # expired

        # Live sessions mean work — never count strikes while ANY tmux
        # session runs here or on the VM (downloads, swaps, batteries).
        if _tmux_alive_local() or _tmux_alive_vm(cfg):
            state_mod.reset_strikes(cfg)
            return 0

        recent = _recent_traffic(cfg)
        if recent == "ERR":
            return 0  # can't tell — do nothing rather than kill a session
        if recent.isdigit() and int(recent) > 0:
            state_mod.reset_strikes(cfg)
            return 0

        n = state_mod.bump_strikes(cfg)
        _log(d, f"state={vm} recent={recent} strikes={n}")

        if n >= STRIKE_LIMIT:
            if not state_mod.warned(cfg):
                state_mod.set_warned(cfg)
                _log(d, "WARNED (idle; hibernating next tick unless held)")
                _notify("GPU box idle — hibernating at the next check "
                        "(~10 min). Run 'harbor hold' to keep it awake.")
            else:
                state_mod.clear_warned(cfg)
                state_mod.reset_strikes(cfg)
                _log(d, "HIBERNATING (warned, no objection)")
                _notify("GPU box hibernating now (idle, no objection). "
                        "'harbor up' to resume.")
                _down(cfg)
    except state_mod.StateUnavailable as e:
        _log(d, f"shared state unreachable — abstaining ({e})")
        return 0
    return 0
