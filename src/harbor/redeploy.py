"""Recreate the box from the volume: SKU fallback, wg hub up, then serve.

The volume is the system — weights, venv, chat template, per-user keys, the
current-model marker AND the box's network identity (wg keypair, TLS cert,
peer table) all live on it — so a fresh VM only needs: mount the volume,
raise the hub, carry a >=580 driver, and re-render the serving unit. A stock
outage on one flavor becomes a fallback to the next, not an outage.
"""
from __future__ import annotations

import base64
import json
import pathlib
import subprocess
import sys
import time

from . import model as model_mod
from . import provider as prov
from . import state as state_mod
from . import wgnet
from .config import Config

SSH_WAIT_TRIES = 60          # the fresh box installs a driver and reboots
SSH_WAIT_SECONDS = 15

# The hub setup that runs on the box. Identity is create-if-missing: a
# redeployed box finds its keys, cert and peers on the volume and comes back
# as the same network self.
_HUB_SH = f"""#!/bin/bash
set -e
mkdir -p /weights/wg /weights/keys
cd /weights/wg
[ -f box.key ] || (umask 077; wg genkey > box.key; wg pubkey < box.key > box.pub)
[ -f reg-cert.pem ] || openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 \\
  -keyout reg-key.pem -out reg-cert.pem -days 3650 -nodes -subj /CN=harbor-box
chmod 600 reg-key.pem box.key
umask 077
cat > /etc/wireguard/wg0.conf <<EOF
[Interface]
Address = {wgnet.HUB_IP}/24
ListenPort = {wgnet.WG_PORT}
PrivateKey = $(cat box.key)
EOF
systemctl enable --now wg-quick@wg0
systemctl enable --now harbor-reg
systemctl enable --now nftables
"""

_NFT_CONF = f"""#!/usr/sbin/nft -f
flush ruleset
table inet harbor {{
  chain input {{
    type filter hook input priority 0; policy drop;
    iif lo accept
    ct state established,related accept
    iifname "wg0" accept
    tcp dport 22 accept
    udp dport {wgnet.WG_PORT} accept
    tcp dport {wgnet.REG_PORT} accept
    icmp type echo-request accept
    icmpv6 type {{ nd-neighbor-solicit, nd-neighbor-advert, nd-router-advert, echo-request }} accept
  }}
}}
"""

_REG_UNIT = """[Unit]
Description=harbor peer registration
RequiresMountsFor=/weights
After=wg-quick@wg0.service

[Service]
ExecStart=/usr/bin/python3 /opt/harbor/regserve.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
"""


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def regserve_source() -> str:
    """The registration service ships VERBATIM from this package — one
    source, unit-tested here, executed there."""
    return (pathlib.Path(__file__).parent / "regserve.py").read_text()


def user_data(cfg: Config) -> str:
    """cloud-init: mount the volume FIRST (the box's identity lives on it),
    then raise the wg hub + registration + firewall, then the >=580 driver
    with a reboot only if the image is older."""
    return f"""#cloud-config
write_files:
  - path: /opt/harbor/regserve.py
    encoding: b64
    content: {_b64(regserve_source())}
  - path: /opt/harbor/hub.sh
    permissions: '0755'
    encoding: b64
    content: {_b64(_HUB_SH)}
  - path: /etc/nftables.conf
    encoding: b64
    content: {_b64(_NFT_CONF)}
  - path: /etc/systemd/system/harbor-reg.service
    encoding: b64
    content: {_b64(_REG_UNIT)}
runcmd:
  - DEBIAN_FRONTEND=noninteractive apt-get install -y wireguard-tools nftables || (apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y wireguard-tools nftables)
  - mkdir -p /weights
  - bash -c 'for i in $(seq 60); do blkid -L harbor-weights >/dev/null && break; sleep 5; done'
  - bash -c 'echo "LABEL=harbor-weights /weights ext4 defaults,nofail 0 2" >> /etc/fstab && mount -a'
  - bash /opt/harbor/hub.sh
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

    print("waiting for the box's public address", end="", flush=True)
    host = ""
    for _ in range(SSH_WAIT_TRIES):
        host = p.public_ip(cfg)
        if host:
            wgnet.cache_write(floating_ip=host)
            break
        print(".", end="", flush=True)
        time.sleep(SSH_WAIT_SECONDS)
    else:
        blob = json.loads(_orphan_file().read_text())
        print(f"\nharbor up: no public address appeared. The VM EXISTS and "
              f"BILLS (id {blob['vm_id']}) — check the provider console or "
              "destroy it with 'harbor down --release'.", file=sys.stderr)
        return 1

    print(" got it\nwaiting for the box (driver install may reboot it once)",
          end="", flush=True)
    for _ in range(SSH_WAIT_TRIES):
        r = subprocess.run(
            ["ssh", "-i", str(cfg.ssh_key), "-o", "BatchMode=yes",
             "-o", "StrictHostKeyChecking=accept-new",
             "-o", "ConnectTimeout=5", f"{cfg.ssh_user}@{host}",
             # mount -a first: the volume may have attached after cloud-init's
             # mount pass, and the fstab entry is already in place.
             "sudo mount -a 2>/dev/null; "
             "mountpoint -q /weights && ip link show wg0 >/dev/null 2>&1 && "
             "v=$(nvidia-smi --query-gpu="
             "driver_version --format=csv,noheader | cut -d. -f1) && "
             "[ \"${v:-0}\" -ge 580 ] && echo READY"],
            capture_output=True, text=True)
        if "READY" in r.stdout:
            break
        print(".", end="", flush=True)
        time.sleep(SSH_WAIT_SECONDS)
    else:
        blob = json.loads(_orphan_file().read_text())
        print(f"\nharbor up: box never became reachable. It EXISTS and "
              f"BILLS (VM id {blob['vm_id']}) — inspect it at {host} or "
              "destroy it with 'harbor down --release'.", file=sys.stderr)
        return 1
    print(" READY")

    rc = model_mod.rerender(cfg)
    if rc != 0:
        return rc
    _orphan_file().unlink(missing_ok=True)
    return 0


def release(cfg: Config, p) -> int:
    """Delete the VM, keep the volume. The root disk dies with the VM — by
    design nothing durable lives there; the box's network identity rides the
    volume and comes back with the successor."""
    p.destroy(cfg)
    try:
        host = wgnet.ssh_host(cfg)
        # The successor gets a new address anyway, but a stale known_hosts
        # entry for a reused address would bite later.
        subprocess.run(["ssh-keygen", "-R", host], capture_output=True)
    except wgnet.CacheMissing:
        pass
    _orphan_file().unlink(missing_ok=True)
    print(f"released: VM deleted, volume {cfg.volume_id} retained. "
          "'harbor up' recreates the box from it (new public address — "
          "rerun 'harbor share' and have teammates rerun their join).")
    return 0
