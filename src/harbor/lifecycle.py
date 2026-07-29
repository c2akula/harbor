"""up / down: make the box usable, or park it.

Speaks the provider protocol, never a vendor's vocabulary — the provider maps
its own states onto ACTIVE / OFF / TRANSITIONING / UNKNOWN and decides which
call revives a box. Retries live here because "not ready yet" is universal;
"which off-state is this" is not.
"""
from __future__ import annotations

import subprocess
import sys
import time

import requests

from . import provider as prov
from . import state as state_mod
from .config import Config

POLL_SECONDS = 5
POLL_TRIES = 60
START_RETRIES = 5
# Wait ticks between re-issuing a start to a box still reporting OFF.
REISSUE_TICKS = 6


def up(cfg: Config) -> int:
    p = prov.load(cfg)

    # One flaky API response must not abort the convergence command: retry
    # an UNKNOWN probe briefly before treating it as real.
    state = p.state(cfg)
    for _ in range(3):
        if state != prov.UNKNOWN:
            break
        time.sleep(POLL_SECONDS)
        state = p.state(cfg)

    if state == prov.TRANSITIONING:
        # A box mid-change cannot be started; let it settle. (Hyperstack 400s
        # if you try to restore a hibernation that is still in flight.)
        print("box is mid-transition — waiting for it to settle", end="", flush=True)
        for _ in range(POLL_TRIES):
            state = p.state(cfg)
            if state != prov.TRANSITIONING:
                break
            print(".", end="", flush=True)
            time.sleep(POLL_SECONDS)
        print(f" {state}")

    from . import redeploy
    if state == prov.ABSENT or redeploy.has_orphan():
        # Either no machine exists under the configured name, or an earlier
        # redeploy is unfinished (its half-made VM is what makes the state
        # look normal). Both are redeploy's to finish — if the provider can.
        if not prov.can_redeploy(p):
            print("harbor up: no VM named for this config exists and the "
                  "provider cannot create one — create it manually, then "
                  "retry.", file=sys.stderr)
            return 1
        rc = redeploy.redeploy(cfg, p)
        if rc != 0:
            return rc
        state = p.state(cfg)

    if state == prov.OFF:
        print("starting the box...")
        for attempt in range(START_RETRIES):
            try:
                p.start(cfg)
                break
            except prov.ProviderError as e:
                # Already phrased for a human by the provider; don't reword it.
                print(f"harbor up: {e}", file=sys.stderr)
                return 1
            except Exception:
                if attempt == START_RETRIES - 1:
                    raise
                # Providers often report "off" slightly before they will accept
                # a start; a short retry avoids a false stop.
                print(f"not ready yet, retrying in {POLL_SECONDS * 3}s...")
                time.sleep(POLL_SECONDS * 3)
    elif state == prov.UNKNOWN:
        print("harbor up: cannot determine the box's state — check the "
              "provider console", file=sys.stderr)
        return 1

    print("waiting for the box", end="", flush=True)
    for tick in range(POLL_TRIES):
        state = p.state(cfg)
        if state == prov.ACTIVE:
            break
        # A start returning success proves the request was ACCEPTED, not that
        # the box will start — a request issued while a previous transition is
        # settling can be dropped. A box still OFF this deep into the wait is
        # not coming up on its own.
        if state == prov.OFF and tick and tick % REISSUE_TICKS == 0:
            print("\nbox still off — re-issuing start", end="", flush=True)
            try:
                p.start(cfg)
            except Exception:
                pass  # the next tick, or the timeout below, decides
        print(".", end="", flush=True)
        time.sleep(POLL_SECONDS)
    else:
        print(" gave up")
        print("harbor up: box never reached ACTIVE — check the provider "
              "console", file=sys.stderr)
        return 1
    print(" ACTIVE")

    print("waiting for model", end="", flush=True)
    for _ in range(POLL_TRIES):
        try:
            if requests.get(f"{cfg.endpoint_url}/health", timeout=2).status_code == 200:
                print(f" READY — model at {cfg.endpoint_url}")
                # Clean slate: strikes and warnings from before the park must
                # not carry into this boot, or the first idle tick inherits
                # them and parks a freshly resumed box.
                try:
                    state_mod.reset_strikes(cfg)
                    state_mod.clear_pending(cfg)
                except state_mod.StateUnavailable:
                    pass
                return 0
        except requests.RequestException:
            pass
        print(".", end="", flush=True)
        time.sleep(POLL_SECONDS)
    print(f" TIMEOUT — check: ssh -i {cfg.ssh_key} {cfg.ssh_user}@{cfg.vm_ip}"
          " 'systemctl status vllm'", file=sys.stderr)
    return 1


def down(cfg: Config, release: bool = False) -> int:
    p = prov.load(cfg)
    if release:
        if not prov.can_redeploy(p):
            print("harbor down: this provider cannot release (no "
                  "create/destroy capability) — parking instead.",
                  file=sys.stderr)
        else:
            from . import redeploy
            return redeploy.release(cfg, p)
    try:
        p.stop(cfg)
    except prov.ProviderError as e:
        print(f"harbor down: {e}", file=sys.stderr)
        return 1
    print("parked")
    return 0
