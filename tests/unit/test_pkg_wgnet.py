"""Transport primitives — the blob contract and WireGuard config rendering."""
import unittest
import unittest.mock

from harbor import wgnet


class BlobContract(unittest.TestCase):
    def test_roundtrip(self):
        blob = wgnet.blob_compose("203.0.113.7", 8443, "AA:BB:CC", "hbr-abc")
        got = wgnet.blob_parse(blob)
        self.assertEqual(got, {"v": 2, "ip": "203.0.113.7", "port": 8443,
                               "fp": "AA:BB:CC", "key": "hbr-abc"})

    def test_garbage_is_rejected_with_a_cause(self):
        with self.assertRaises(wgnet.BlobError) as ctx:
            wgnet.blob_parse("not-a-blob!!!")
        self.assertIn("not a harbor join blob", str(ctx.exception))

    def test_wrong_version_names_the_mismatch(self):
        import base64, json
        stale = base64.urlsafe_b64encode(
            json.dumps({"v": 1, "url": "x", "key": "y"}).encode()).decode()
        with self.assertRaises(wgnet.BlobError) as ctx:
            wgnet.blob_parse(stale)
        self.assertIn("version", str(ctx.exception))

    def test_missing_field_is_rejected(self):
        import base64, json
        partial = base64.urlsafe_b64encode(
            json.dumps({"v": 2, "ip": "1.2.3.4"}).encode()).decode()
        with self.assertRaises(wgnet.BlobError):
            wgnet.blob_parse(partial)


class SpokeConfinement(unittest.TestCase):
    """A spoke's tunnel must only route to the hub — teammate isolation is
    this one line, so it gets its own test."""

    def conf(self):
        return wgnet.spoke_conf(private_key="PRIV", address="10.77.0.11",
                                box_pubkey="BOXPUB", endpoint_ip="203.0.113.7")

    def test_allowed_ips_is_exactly_the_hub(self):
        lines = [l.strip() for l in self.conf().splitlines()]
        self.assertIn(f"AllowedIPs = {wgnet.HUB_IP}/32", lines)
        self.assertNotIn("0.0.0.0/0", self.conf(),
                         "a spoke must never become a full-tunnel VPN")

    def test_endpoint_and_keepalive(self):
        conf = self.conf()
        self.assertIn(f"Endpoint = 203.0.113.7:{wgnet.WG_PORT}", conf)
        self.assertIn("PersistentKeepalive = 25", conf,
                      "spokes sit behind NAT; without keepalive the box "
                      "cannot reach them back and sessions stall")

    def test_no_dns_takeover(self):
        self.assertNotIn("DNS", self.conf(),
                         "the tunnel must not touch the machine's DNS")


class BoxCache(unittest.TestCase):
    """The floating IP and cert fingerprint are discovered at `up` and cached
    beside the config — everything `share` needs works offline."""

    def setUp(self):
        import os, tempfile, pathlib
        self._d = tempfile.TemporaryDirectory()
        conf = pathlib.Path(self._d.name) / "config.toml"
        conf.write_text("")
        self._prior = os.environ.get("HARBOR_CONF")
        os.environ["HARBOR_CONF"] = str(conf)

    def tearDown(self):
        import os
        if self._prior is None:
            os.environ.pop("HARBOR_CONF", None)
        else:
            os.environ["HARBOR_CONF"] = self._prior
        self._d.cleanup()

    def test_write_then_read_merges(self):
        wgnet.cache_write(floating_ip="203.0.113.7")
        wgnet.cache_write(fp="AA:BB")
        got = wgnet.cache_read()
        self.assertEqual(got["floating_ip"], "203.0.113.7")
        self.assertEqual(got["fp"], "AA:BB")

    def test_missing_cache_names_the_fix(self):
        with self.assertRaises(wgnet.CacheMissing) as ctx:
            wgnet.cache_read()
        self.assertIn("harbor up", str(ctx.exception))


class OperatorLink(unittest.TestCase):
    """`up` installs the operator as a spoke: peer on the box (persisted),
    fingerprint cached, local tunnel raised — all idempotent."""

    def setUp(self):
        import os, tempfile, pathlib
        self._d = tempfile.TemporaryDirectory()
        conf = pathlib.Path(self._d.name) / "config.toml"
        conf.write_text("")
        self._prior = os.environ.get("HARBOR_CONF")
        os.environ["HARBOR_CONF"] = str(conf)
        wgnet.cache_write(floating_ip="203.0.113.7")

    def tearDown(self):
        import os
        if self._prior is None:
            os.environ.pop("HARBOR_CONF", None)
        else:
            os.environ["HARBOR_CONF"] = self._prior
        self._d.cleanup()

    def _cfg(self):
        import pathlib
        from harbor.config import Config
        return Config(vm_name="box", provider="hyperstack", ssh_user="u",
                      ssh_key=pathlib.Path("/x"), api="http://x",
                      key_file=pathlib.Path("/x"), rate_per_hr=1.0,
                      model_key_file=pathlib.Path("/x"), slot_context=1,
                      effort="max", flow_concurrency=0, endpoint_url="http://x",
                      oracle_markers="x", oracle_model="")

    def test_link_writes_spoke_conf_and_caches_the_fingerprint(self):
        import pathlib
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            out = ""
            if cmd[0] == "ssh":
                out = "BOXPUB\nsha256 Fingerprint=AA:BB:CC\n"
            if cmd[:2] == ["wg", "genkey"]:
                out = "PRIV\n"
            if cmd[:2] == ["wg", "pubkey"]:
                out = "PUB\n"
            return unittest.mock.Mock(returncode=0, stdout=out, stderr="")

        with unittest.mock.patch("harbor.wgnet.subprocess.run",
                                 side_effect=fake_run):
            wgnet.operator_link(self._cfg(), provider=None)

        conf = pathlib.Path(self._d.name) / "wg" / "harbor.conf"
        self.assertTrue(conf.exists())
        text = conf.read_text()
        self.assertIn(f"Address = {wgnet.OPERATOR_IP}/24", text)
        self.assertIn("PublicKey = BOXPUB", text)
        self.assertEqual(conf.stat().st_mode & 0o777, 0o600)
        self.assertEqual(wgnet.cache_read()["fp"], "AA:BB:CC")
        self.assertTrue(any("wg-quick" in " ".join(map(str, c))
                            for c in calls), "the tunnel must come up")


class SudoIsVisible(unittest.TestCase):
    def test_tunnel_up_never_captures_stderr(self):
        """sudo asks for a password on stderr. Capturing it turns a question
        into a silent hang, so the terminal must keep stderr."""
        import pathlib
        calls = []
        with unittest.mock.patch("harbor.wgnet.subprocess.run") as run:
            run.side_effect = lambda cmd, **kw: (
                calls.append(kw), unittest.mock.Mock(returncode=0))[1]
            wgnet.tunnel_up(pathlib.Path("/tmp/x.conf"))
        for kw in calls:
            self.assertFalse(kw.get("capture_output"))
            self.assertNotIn("stderr", kw)


class Keypair(unittest.TestCase):
    def test_uses_wg_binary(self):
        with unittest.mock.patch("harbor.wgnet.subprocess.run") as run:
            run.side_effect = [
                unittest.mock.Mock(returncode=0, stdout="PRIV\n"),
                unittest.mock.Mock(returncode=0, stdout="PUB\n"),
            ]
            self.assertEqual(wgnet.keypair(), ("PRIV", "PUB"))

    def test_missing_binary_says_what_to_install(self):
        with unittest.mock.patch("harbor.wgnet.subprocess.run",
                                 side_effect=FileNotFoundError):
            with self.assertRaises(wgnet.WgMissing) as ctx:
                wgnet.keypair()
            self.assertIn("wireguard-tools", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
