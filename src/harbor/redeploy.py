"""Recreate the box from the volume: SKU fallback, tailnet join, then serve.

The volume is the system — weights, venv, chat template, per-user keys and
the current-model marker all live on it — so a fresh VM only needs: join the
tailnet, mount the volume, carry a >=580 driver, and re-render the serving
unit. A stock outage on one flavor becomes a fallback to the next, not an
outage.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time

from . import model as model_mod
from . import provider as prov
from . import state as state_mod
from .config import Config

SSH_WAIT_TRIES = 60          # the fresh box installs a driver and reboots
SSH_WAIT_SECONDS = 15


def user_data(cfg: Config) -> str:
    """cloud-init: tailnet first (so the box is reachable even if later steps
    fail), volume mount by label, driver >=580 with a reboot only if the
    image is older. The auth key is read at build time and travels in the
    create request only."""
    ts_key = cfg.ts_authkey_file.read_text().strip()
    return f"""#cloud-config
runcmd:
  - curl -fsSL https://tailscale.com/install.sh | sh
  - tailscale up --authkey={ts_key} --hostname={cfg.vm_name}
  - mkdir -p /weights
  - bash -c 'for i in $(seq 60); do blkid -L harbor-weights >/dev/null && break; sleep 5; done'
  - bash -c 'echo "LABEL=harbor-weights /weights ext4 defaults,nofail 0 2" >> /etc/fstab && mount -a'
  - bash -c 'cp /var/log/cloud-init-output.log /weights/redeploy-diag.log 2>/dev/null || true'
  - bash -c 'v=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | cut -d. -f1); if [ "${{v:-0}}" -lt 580 ]; then apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y -o Dpkg::Options::=--force-overwrite cuda-drivers-580 && reboot; fi'
"""


def _orphan_file():
    return state_mod.state_dir() / "redeploy.json"


def has_orphan() -> bool:
    """An unfinished redeploy exists; `up` must finish it, whatever the
    provider reports — the VM's existence is exactly what makes the state
    look normal."""
    return _orphan_file().exists()


def redeploy(cfg: Config, p) -> int:
    """Create a fresh box on the first flavor with stock and bring it to
    serving. Returns 0 on success."""
    if not cfg.flavors:
        print("harbor up: no [vm] flavors configured — cannot create a box",
              file=sys.stderr)
        return 1
    if not (cfg.image and cfg.keypair and cfg.volume_id):
        print("harbor up: redeploy needs [vm] image, keypair and volume",
              file=sys.stderr)
        return 1
    if _orphan_file().exists():
        blob = json.loads(_orphan_file().read_text())
        print(f"note: an earlier redeploy left VM id {blob.get('vm_id')} "
              "(flavor {0}) — continuing against it rather than creating "
              "another".format(blob.get("flavor")), file=sys.stderr)
        # The create may have died between VM build and volume attachment;
        # make the attachment true before waiting on the mount.
        if callable(getattr(p, "attach", None)):
            p.attach(cfg)
    else:
        chosen = None
        for flavor in cfg.flavors:
            n = p.stock(cfg, flavor)
            print(f"stock {flavor}: {n}")
            if n > 0:
                chosen = flavor
                break
        if chosen is None:
            print("harbor up: no configured flavor has stock — retry later",
                  file=sys.stderr)
            return 1
        print(f"creating {cfg.vm_name} on {chosen}...")
        # Intent recorded BEFORE the create call: even a create that dies
        # mid-way may have made a billing VM, and a retry must continue
        # against it (by name) rather than mint a second one.
        _orphan_file().write_text(json.dumps({"vm_id": None,
                                              "flavor": chosen}))
        vm_id = p.create(cfg, chosen, user_data(cfg))
        _orphan_file().write_text(json.dumps({"vm_id": vm_id,
                                              "flavor": chosen}))

    print("waiting for the box to join the tailnet (driver install may "
          "reboot it once)", end="", flush=True)
    for _ in range(SSH_WAIT_TRIES):
        r = subprocess.run(
            ["ssh", "-i", str(cfg.ssh_key), "-o", "BatchMode=yes",
             "-o", "StrictHostKeyChecking=accept-new",
             "-o", "ConnectTimeout=5", f"{cfg.ssh_user}@{cfg.vm_ip}",
             # mount -a first: the volume may have attached after cloud-init's
             # mount pass, and the fstab entry is already in place.
             "sudo mount -a 2>/dev/null; "
             "mountpoint -q /weights && v=$(nvidia-smi --query-gpu="
             "driver_version --format=csv,noheader | cut -d. -f1) && "
             "[ \"${v:-0}\" -ge 580 ] && echo READY"],
            capture_output=True, text=True)
        if "READY" in r.stdout:
            break
        print(".", end="", flush=True)
        time.sleep(SSH_WAIT_SECONDS)
    else:
        blob = json.loads(_orphan_file().read_text())
        print(f"\nharbor up: box never became reachable over the tailnet. "
              f"It EXISTS and BILLS (VM id {blob['vm_id']}) — check the "
              "tailnet admin for a stale or suffixed node, or destroy it "
              "with 'harbor down --release'.", file=sys.stderr)
        return 1
    print(" READY")

    rc = model_mod.rerender(cfg)
    if rc != 0:
        return rc
    _orphan_file().unlink(missing_ok=True)
    return 0


def release(cfg: Config, p) -> int:
    """Delete the VM, keep the volume. The root disk dies with the VM — by
    design nothing durable lives there."""
    # Leaving the tailnet before deletion frees the hostname for the
    # successor node. The logout severs the SSH transport carrying it, so
    # this cannot return cleanly — the timeout IS the expected exit.
    try:
        subprocess.run(
            ["ssh", "-i", str(cfg.ssh_key), "-o", "BatchMode=yes",
             "-o", "StrictHostKeyChecking=accept-new",
             "-o", "ConnectTimeout=5", "-o", "ServerAliveInterval=3",
             "-o", "ServerAliveCountMax=2",
             f"{cfg.ssh_user}@{cfg.vm_ip}", "sudo tailscale logout"],
            capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        pass
    p.destroy(cfg)
    # The successor presents a new host key under the same name.
    subprocess.run(["ssh-keygen", "-R", cfg.vm_ip], capture_output=True)
    _orphan_file().unlink(missing_ok=True)
    print(f"released: VM deleted, volume {cfg.volume_id} retained. "
          "'harbor up' recreates the box from it.")
    return 0
