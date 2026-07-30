"""Peer registration for the WireGuard hub — runs ON the box, stdlib only.

This file is shipped verbatim to the box by cloud-init and executed there
with the system python3; it must not import anything from harbor.

One endpoint: POST /join. Bearer token must match a key on the volume
(constant-time); the reply contains the caller's own allocation and the hub
public key — never anything about other peers. Every authentication failure
is the same 401.
"""
from __future__ import annotations

import hmac
import http.server
import json
import pathlib
import re
import ssl
import subprocess
import sys

SUBNET_PREFIX = "10.77.0."
FIRST_IP = 10
LAST_IP = 250
_PUBKEY = re.compile(r"^[A-Za-z0-9+/]{43}=$")
_401 = json.dumps({"error": "unauthorized"}).encode()


class State:
    def __init__(self, keys_dir, wg_dir):
        self.keys_dir = pathlib.Path(keys_dir)
        self.wg_dir = pathlib.Path(wg_dir)
        self.peers_file = self.wg_dir / "peers.json"

    def authorized(self, token: str) -> bool:
        ok = False
        for f in sorted(self.keys_dir.glob("*.key")):
            expect = f.read_text().strip()
            # No early exit: every stored key is compared every time.
            ok = hmac.compare_digest(token, expect) or ok
        return ok

    def _peers(self) -> dict:
        if self.peers_file.exists():
            return json.loads(self.peers_file.read_text())
        return {}

    def allocate(self, pubkey: str) -> str:
        peers = self._peers()
        if pubkey in peers:
            return peers[pubkey]
        taken = set(peers.values())
        for n in range(FIRST_IP, LAST_IP):
            ip = f"{SUBNET_PREFIX}{n}"
            if ip not in taken:
                peers[pubkey] = ip
                self.peers_file.write_text(json.dumps(peers, indent=1))
                return ip
        raise RuntimeError("peer address space exhausted")

    def box_pubkey(self) -> str:
        return (self.wg_dir / "box.pub").read_text().strip()

    def apply_peer(self, pubkey: str, ip: str) -> None:
        subprocess.run(["wg", "set", "wg0", "peer", pubkey,
                        "allowed-ips", f"{ip}/32"], check=True)

    def apply_all(self) -> None:
        """Boot-time replay: the JSON table is the source of truth; the live
        wg interface is a projection of it."""
        for pubkey, ip in self._peers().items():
            self.apply_peer(pubkey, ip)


class _Handler(http.server.BaseHTTPRequestHandler):
    state: State = None  # set by make_server

    def log_message(self, *a):  # tokens must never reach a log line
        pass

    def _reply(self, status: int, body: bytes):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        auth = self.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        if not self.state.authorized(token):
            return self._reply(401, _401)
        if self.path != "/join":
            return self._reply(404, b"{}")
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            pubkey = body["pubkey"]
            assert _PUBKEY.match(pubkey)
        except Exception:
            return self._reply(400, json.dumps(
                {"error": "body must be {\"pubkey\": <wireguard key>}"}).encode())
        ip = self.state.allocate(pubkey)
        self.state.apply_peer(pubkey, ip)
        self._reply(200, json.dumps(
            {"box_pubkey": self.state.box_pubkey(), "ip": ip}).encode())

    do_GET = do_POST  # same auth wall, and /join only answers POST anyway


def make_server(host: str, port: int, state: State,
                certfile=None, keyfile=None):
    handler = type("Handler", (_Handler,), {"state": state})
    server = http.server.ThreadingHTTPServer((host, port), handler)
    if certfile:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile, keyfile)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
    return server


def main():
    state = State(keys_dir="/weights/keys", wg_dir="/weights/wg")
    state.apply_all()
    server = make_server("0.0.0.0", 8443, state,
                         certfile="/weights/wg/reg-cert.pem",
                         keyfile="/weights/wg/reg-key.pem")
    print("regserve: listening on 8443", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    sys.exit(main())
