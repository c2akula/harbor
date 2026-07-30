"""Team access — the share message, the join flow, key rotation."""
import json
import os
import pathlib
import tempfile
import unittest
import unittest.mock

from harbor import team, wgnet


class Isolated(unittest.TestCase):
    """Every test here runs against its own config dir."""

    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._d.name)
        (self.root / "config.toml").write_text("")
        self._prior = os.environ.get("HARBOR_CONF")
        os.environ["HARBOR_CONF"] = str(self.root / "config.toml")

    def tearDown(self):
        if self._prior is None:
            os.environ.pop("HARBOR_CONF", None)
        else:
            os.environ["HARBOR_CONF"] = self._prior
        self._d.cleanup()


class TeamKey(Isolated):
    def test_minted_once_and_file_is_private(self):
        t1 = team.ensure_team_key()
        t2 = team.ensure_team_key()
        self.assertEqual(t1, t2, "share must not rotate silently")
        self.assertTrue(t1.startswith("hbr-"))
        f = team.team_key_path()
        self.assertEqual(f.stat().st_mode & 0o777, 0o600)

    def test_rotate_changes_the_token(self):
        t1 = team.ensure_team_key()
        t2 = team.rotate_local()
        self.assertNotEqual(t1, t2)
        self.assertEqual(team.ensure_team_key(), t2)


class ShareMessage(Isolated):
    def test_blob_round_trips_and_message_discloses_the_vpn(self):
        wgnet.cache_write(floating_ip="203.0.113.7", fp="AA:BB")
        msg = team.share_message()
        blob = msg.split("--join '")[1].split("'")[0]
        got = wgnet.blob_parse(blob)
        self.assertEqual(got["ip"], "203.0.113.7")
        self.assertEqual(got["fp"], "AA:BB")
        self.assertEqual(got["key"], team.ensure_team_key())
        self.assertIn("VPN", msg, "the message must say what it installs")

    def test_share_without_an_upped_box_names_the_fix(self):
        with self.assertRaises(wgnet.CacheMissing):
            team.share_message()


class JoinFlow(Isolated):
    def _blob(self):
        return wgnet.blob_compose("203.0.113.7", 8443, "AA:BB", "hbr-team")

    def test_join_writes_config_key_and_tunnel(self):
        conf_dir = self.root
        with unittest.mock.patch.object(
                team, "register",
                return_value={"box_pubkey": "BOXPUB", "ip": "10.77.0.11"}), \
             unittest.mock.patch.object(wgnet, "keypair",
                                        return_value=("PRIV", "PUB")), \
             unittest.mock.patch.object(wgnet, "tunnel_up") as up, \
             unittest.mock.patch.object(team, "_crush_write") as crush, \
             unittest.mock.patch.object(team, "_probe", return_value=True):
            rc = team.join(self._blob())
        self.assertEqual(rc, 0)
        cfg_text = (conf_dir / "config.toml").read_text()
        self.assertIn(wgnet.ENDPOINT_URL, cfg_text)
        key_file = conf_dir / "model-key"
        self.assertEqual(key_file.read_text().strip(), "hbr-team")
        self.assertEqual(key_file.stat().st_mode & 0o777, 0o600)
        wg_conf = (conf_dir / "wg" / "harbor.conf").read_text()
        self.assertIn("Address = 10.77.0.11/24", wg_conf)
        self.assertIn(f"AllowedIPs = {wgnet.HUB_IP}/32", wg_conf)
        up.assert_called_once()
        crush.assert_called_once()

    def test_existing_config_refused_without_force(self):
        (self.root / "config.toml").write_text("[endpoint]\nurl='x'\n")
        rc = team.join(self._blob())
        self.assertEqual(rc, 2)

    def test_garbage_blob_is_a_clear_error(self):
        rc = team.join("garbage!!!")
        self.assertEqual(rc, 2)


class FingerprintPinning(unittest.TestCase):
    def test_normalisation(self):
        self.assertTrue(team.fp_match("AA:BB:CC", "aabbcc"))
        self.assertFalse(team.fp_match("AA:BB:CC", "aabbcd"))


if __name__ == "__main__":
    unittest.main()
