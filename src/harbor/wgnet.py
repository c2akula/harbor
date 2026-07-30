"""The harbor-owned transport: WireGuard hub-and-spoke constants, the join
blob, and spoke config rendering.

The box is the hub; every client is a spoke whose AllowedIPs is the hub
address alone — a spoke has no route to any other spoke, so teammate
isolation is topology rather than policy.
"""
from __future__ import annotations

import base64
import binascii
import json
import subprocess

SUBNET = "10.77.0.0/24"
HUB_IP = "10.77.0.1"
OPERATOR_IP = "10.77.0.2"
TEAMMATE_FIRST = 10          # .2–.9 reserved for operator machines
WG_PORT = 51820
REG_PORT = 8443
ENDPOINT_URL = f"http://{HUB_IP}:8080"

_BLOB_FIELDS = ("ip", "port", "fp", "key")


class BlobError(ValueError):
    pass


class CacheMissing(RuntimeError):
    pass


def cache_path():
    from . import config as config_mod
    return config_mod.config_path().parent / "box.json"


def cache_read() -> dict:
    p = cache_path()
    if not p.exists():
        raise CacheMissing("no cached box address — run `harbor up` once "
                           "from this machine")
    return json.loads(p.read_text())


def cache_write(**fields) -> None:
    p = cache_path()
    current = json.loads(p.read_text()) if p.exists() else {}
    current.update(fields)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(current, indent=1))


def ssh_host(cfg) -> str:
    """Where SSH reaches the box: the cached floating IP."""
    return cache_read()["floating_ip"]


class WgMissing(RuntimeError):
    pass


def blob_compose(ip: str, port: int, fp: str, key: str) -> str:
    payload = {"v": 2, "ip": ip, "port": port, "fp": fp, "key": key}
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()


def blob_parse(blob: str) -> dict:
    try:
        payload = json.loads(base64.urlsafe_b64decode(blob.strip().encode()))
        assert isinstance(payload, dict)
    except (binascii.Error, ValueError, AssertionError):
        raise BlobError("not a harbor join blob — paste it exactly as sent")
    if payload.get("v") != 2:
        raise BlobError(f"blob version {payload.get('v')!r} — this harbor "
                        "speaks version 2; operator and joiner must upgrade "
                        "to matching releases")
    missing = [f for f in _BLOB_FIELDS if f not in payload]
    if missing:
        raise BlobError(f"blob is incomplete (missing {', '.join(missing)}) "
                        "— ask the operator to rerun `harbor share`")
    return payload


def keypair() -> tuple[str, str]:
    """A fresh WireGuard identity via the wg binary."""
    try:
        priv = subprocess.run(["wg", "genkey"], capture_output=True,
                              text=True, check=True).stdout.strip()
        pub = subprocess.run(["wg", "pubkey"], input=priv, capture_output=True,
                             text=True, check=True).stdout.strip()
    except FileNotFoundError:
        raise WgMissing("wireguard-tools is not installed (no `wg` binary)")
    return priv, pub


def spoke_conf(private_key: str, address: str, box_pubkey: str,
               endpoint_ip: str) -> str:
    return f"""[Interface]
PrivateKey = {private_key}
Address = {address}/24

[Peer]
PublicKey = {box_pubkey}
AllowedIPs = {HUB_IP}/32
Endpoint = {endpoint_ip}:{WG_PORT}
PersistentKeepalive = 25
"""


def wg_dir():
    d = cache_path().parent / "wg"
    d.mkdir(parents=True, exist_ok=True)
    return d


def own_keypair() -> tuple[str, str]:
    """This machine's spoke identity, minted on first use."""
    d = wg_dir()
    priv_f, pub_f = d / "key", d / "key.pub"
    if not priv_f.exists():
        priv, pub = keypair()
        priv_f.touch(mode=0o600)
        priv_f.write_text(priv)
        pub_f.write_text(pub)
    return priv_f.read_text().strip(), pub_f.read_text().strip()


def tunnel_up(conf: "pathlib.Path") -> None:
    """Raise (or refresh) the local spoke — the one sudo in harbor.

    stderr stays on the terminal: capturing it swallows sudo's password
    prompt, and the command then looks like a hang instead of a question.
    """
    subprocess.run(["sudo", "wg-quick", "down", str(conf)],
                   stdout=subprocess.DEVNULL, text=True)   # absent link is fine
    if subprocess.run(["sudo", "wg-quick", "up", str(conf)],
                      stdout=subprocess.DEVNULL, text=True).returncode != 0:
        raise RuntimeError("wg-quick up failed (see its output above)")


# The operator's peer entry is persisted in the same table the registration
# service owns, so a rebooted box replays it with everyone else's.
_INSTALL_PEER = """sudo python3 - <<'PY'
import json, pathlib, subprocess
pub = {pub!r}
f = pathlib.Path("/weights/wg/peers.json")
peers = json.loads(f.read_text()) if f.exists() else {{}}
peers[pub] = {ip!r}
f.write_text(json.dumps(peers, indent=1))
subprocess.run(["wg", "set", "wg0", "peer", pub,
                "allowed-ips", {ip!r} + "/32"], check=True)
PY
cat /weights/wg/box.pub
openssl x509 -in /weights/wg/reg-cert.pem -fingerprint -sha256 -noout
"""


def operator_link(cfg, provider=None) -> None:
    """Make this machine the hub's operator spoke. Idempotent; raises
    RuntimeError with an actionable message on failure."""
    if provider is not None and callable(getattr(provider, "public_ip", None)):
        ip = provider.public_ip(cfg)
        if ip:
            cache_write(floating_ip=ip)
    host = ssh_host(cfg)
    priv, pub = own_keypair()
    r = subprocess.run(
        ["ssh", "-i", str(cfg.ssh_key), "-o", "BatchMode=yes",
         "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10",
         f"{cfg.ssh_user}@{host}",
         _INSTALL_PEER.format(pub=pub, ip=OPERATOR_IP)],
        capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"operator peer install failed: "
                           f"{r.stderr.strip() or 'box unreachable'}")
    lines = [l.strip() for l in r.stdout.splitlines() if l.strip()]
    box_pub = lines[0]
    fp = lines[-1].split("=", 1)[1] if "=" in lines[-1] else ""
    cache_write(fp=fp, box_pub=box_pub)

    conf = wg_dir() / "harbor.conf"
    conf.touch(mode=0o600)
    conf.write_text(spoke_conf(priv, OPERATOR_IP, box_pub, host))
    tunnel_up(conf)
