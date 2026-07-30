"""Team access: one shared key, one paste-able message, one join command.

The operator's `harbor share` prints a message whose blob carries everything
a teammate's machine needs: where the box is, how to prove the box is really
it (TLS fingerprint), and the team key that both registers the tunnel and
authenticates model calls. Whoever holds the blob holds access — it travels
like a password, and `harbor keys rotate` kills it everywhere at once.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import secrets
import ssl
import sys

from . import config as config_mod
from . import wgnet

INSTALL_URL = ("https://raw.githubusercontent.com/c2akula/harbor/master/"
               "install.sh")

JOIN_CONFIG = f"""\
# harbor configuration — written by `harbor join`; the operator's box serves
# on the team network's hub address.

[endpoint]
url = "{wgnet.ENDPOINT_URL}"
model_key_file = "{{key_file}}"
slot_context = 262144
effort = "high"
"""


def team_key_path():
    return config_mod.config_path().parent / "team.key"


def ensure_team_key() -> str:
    f = team_key_path()
    if not f.exists():
        f.parent.mkdir(parents=True, exist_ok=True)
        f.touch(mode=0o600)
        f.write_text(f"hbr-{secrets.token_urlsafe(24)}\n")
    return f.read_text().strip()


def rotate_local() -> str:
    team_key_path().unlink(missing_ok=True)
    return ensure_team_key()


def share_message() -> str:
    cache = wgnet.cache_read()          # raises CacheMissing before first up
    blob = wgnet.blob_compose(cache["floating_ip"], wgnet.REG_PORT,
                              cache.get("fp", ""), ensure_team_key())
    return f"""\
── send this to your teammates ─────────────────────────────────────
Access to our team's private model server. The command installs
harbor and a WireGuard-based VPN link that reaches ONLY the model
box — nothing else on anyone's machine is exposed.

    curl -fsSL {INSTALL_URL} | sh -s -- --join '{blob}'

Already have harbor installed?  harbor join '{blob}'
────────────────────────────────────────────────────────────────────
This message IS access — send it the way you'd send a password.
"""


def push_team_key(cfg) -> str:
    """Make sure the box accepts the team key: 'present', 'pushed', or
    'unreachable' (share still works — the blob activates at next up)."""
    from . import keys as keys_mod
    from . import model as model_mod
    from . import state as state_mod
    token = ensure_team_key()
    try:
        if "team" in keys_mod.list_names(cfg):
            return "present"
        keys_mod._run(cfg, f"umask 077 && echo '{token}' > team.key")
        model_mod.rerender(cfg)
        return "pushed"
    except state_mod.StateUnavailable:
        return "unreachable"


def fp_match(a: str, b: str) -> bool:
    norm = lambda s: s.replace(":", "").strip().lower()
    return bool(a) and norm(a) == norm(b)


def register(ip: str, port: int, fp: str, token: str, pubkey: str) -> dict:
    """POST /join over TLS pinned to the box's certificate fingerprint —
    no CA involved; the blob itself says who the box is."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE           # replaced by the pin below
    conn = http.client.HTTPSConnection(ip, port, timeout=15, context=ctx)
    conn.connect()
    seen = hashlib.sha256(conn.sock.getpeercert(binary_form=True)).hexdigest()
    if not fp_match(fp, seen):
        conn.close()
        raise RuntimeError(
            "the server at the blob's address does not present the expected "
            "certificate — the blob is stale (operator redeployed?) or the "
            "address is not your box. Ask for a fresh `harbor share` message.")
    conn.request("POST", "/join", body=json.dumps({"pubkey": pubkey}),
                 headers={"Authorization": f"Bearer {token}",
                          "Content-Type": "application/json"})
    r = conn.getresponse()
    body = r.read()
    conn.close()
    if r.status == 401:
        raise RuntimeError("the box rejected the key — it was rotated. Ask "
                           "for a fresh `harbor share` message.")
    if r.status != 200:
        raise RuntimeError(f"registration failed ({r.status})")
    return json.loads(body)


def _probe(token: str) -> bool:
    import requests
    try:
        r = requests.get(f"{wgnet.ENDPOINT_URL}/v1/models", timeout=5,
                         headers={"Authorization": f"Bearer {token}"})
        return r.status_code == 200
    except requests.RequestException:
        return False


def _crush_write(cfg) -> None:
    from . import crush as crush_mod
    live = (json.loads(crush_mod.LIVE_PATH.read_text())
            if crush_mod.LIVE_PATH.exists() else {})
    merged = crush_mod.merge(live, cfg)
    crush_mod.LIVE_PATH.parent.mkdir(parents=True, exist_ok=True)
    crush_mod.LIVE_PATH.write_text(json.dumps(merged, indent=2) + "\n")


def join(blob: str, force: bool = False) -> int:
    """The whole teammate flow. Every step is check-then-fix: rerunning after
    any abort continues where it stopped."""
    try:
        payload = wgnet.blob_parse(blob)
    except wgnet.BlobError as e:
        print(f"harbor join: {e}", file=sys.stderr)
        return 2

    conf_path = config_mod.config_path()
    if conf_path.exists() and conf_path.read_text().strip() and not force:
        print(f"harbor join: {conf_path} already exists — this machine is "
              "set up. Rerun with --force to overwrite.", file=sys.stderr)
        return 2

    print("registering with the box...")
    priv, pub = wgnet.own_keypair()
    try:
        granted = register(payload["ip"], payload["port"], payload["fp"],
                           payload["key"], pub)
    except (OSError, RuntimeError) as e:
        print(f"harbor join: {e}", file=sys.stderr)
        return 1

    conf = wgnet.wg_dir() / "harbor.conf"
    conf.touch(mode=0o600)
    conf.write_text(wgnet.spoke_conf(priv, granted["ip"],
                                     granted["box_pubkey"], payload["ip"]))
    print("raising the private link (sudo may prompt)...")
    try:
        wgnet.tunnel_up(conf)
    except wgnet.WgMissing as e:
        print(f"harbor join: {e} — install it (apt install wireguard-tools / "
              "brew install wireguard-tools) and rerun this command.",
              file=sys.stderr)
        return 1
    except RuntimeError as e:
        print(f"harbor join: {e}", file=sys.stderr)
        return 1

    key_file = conf_path.parent / "model-key"
    key_file.parent.mkdir(parents=True, exist_ok=True)
    key_file.touch(mode=0o600)
    key_file.write_text(payload["key"] + "\n")
    conf_path.write_text(JOIN_CONFIG.format(key_file=key_file))
    print(f"wrote {conf_path}")

    if _probe(payload["key"]):
        print("model answers — you're in.")
    else:
        print("note: the model isn't answering right now (the box may be "
              "parked — ask your operator). Your setup is complete.")

    _crush_write(config_mod.load())
    print('next:  crush run "hello"')
    return 0
