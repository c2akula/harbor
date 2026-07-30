"""Redeploy — SKU fallback, the orphan guard, and provider optionality."""
import json
import pathlib
import tempfile
import unittest
import unittest.mock

from harbor import lifecycle, provider as prov, redeploy
from harbor.config import Config


def cfg(**kw):
    base = dict(
        vm_name="box", provider="hyperstack", ssh_user="u",
        ssh_key=pathlib.Path("/x"), api="http://x",
        key_file=pathlib.Path("/x"), rate_per_hr=1.0,
        model_key_file=pathlib.Path("/x"), slot_context=1, effort="max",
        flow_concurrency=0, endpoint_url="http://box:8080",
        oracle_markers="x", oracle_model="",
        flavors=("gpu-a", "gpu-b"), image="img", keypair="kp", volume_id=7,
    )
    base.update(kw)
    return Config(**base)


class CapableProvider:
    def __init__(self, stock_by_flavor):
        self._stock = stock_by_flavor
        self.created: list[str] = []

    def state(self, c): return prov.ABSENT
    def start(self, c): pass
    def stop(self, c): pass
    def stock(self, c, flavor): return self._stock.get(flavor, 0)

    def create(self, c, flavor, user_data):
        self.created.append(flavor)
        return 4242

    def destroy(self, c): pass

    def public_ip(self, c): return "203.0.113.7"


class MinimalProvider:
    def state(self, c): return prov.ABSENT
    def start(self, c): pass
    def stop(self, c): pass


class CloudInitContract(unittest.TestCase):
    def test_no_third_party_transport(self):
        self.assertNotIn("tailscale", redeploy.user_data(cfg()).lower(),
                         "M6: the box's network is harbor's own")

    def test_regserve_ships_verbatim_from_the_package(self):
        import base64
        ud = redeploy.user_data(cfg())
        packed = ud.split("regserve.py\n")[1].split("content: ")[1].split()[0]
        self.assertEqual(base64.b64decode(packed).decode(),
                         redeploy.regserve_source(),
                         "the service on the box must be the one we unit-test")

    def test_firewall_defaults_closed_with_exactly_the_three_doors(self):
        import base64
        ud = redeploy.user_data(cfg())
        packed = ud.split("nftables.conf\n")[1].split("content: ")[1].split()[0]
        nft = base64.b64decode(packed).decode()
        self.assertIn("policy drop", nft)
        for door in ("tcp dport 22", "udp dport 51820", "tcp dport 8443"):
            self.assertIn(door, nft)
        self.assertNotIn("8080", nft,
                         "the model port must never open on the public side")

    def test_volume_mounts_before_the_hub_rises(self):
        ud = redeploy.user_data(cfg())
        self.assertLess(ud.index("mount -a"), ud.index("hub.sh\n", ud.index("runcmd")),
                        "the box's network identity lives on the volume")


class Capability(unittest.TestCase):
    def test_detection(self):
        self.assertTrue(prov.can_redeploy(CapableProvider({})))
        self.assertFalse(prov.can_redeploy(MinimalProvider()))

    def test_up_on_absent_without_capability_refuses_plainly(self):
        with unittest.mock.patch.object(prov, "load",
                                        return_value=MinimalProvider()):
            self.assertEqual(lifecycle.up(cfg()), 1)


class SkuFallback(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        p = unittest.mock.patch.dict("os.environ",
                                     {"LLM_WD_STATE_DIR": self.d})
        p.start()
        self.addCleanup(p.stop)
        # Stop after creation: the tailnet wait and re-render are the live
        # test's business.
        p2 = unittest.mock.patch.object(redeploy, "SSH_WAIT_TRIES", 0)
        p2.start()
        self.addCleanup(p2.stop)

    def _ud(self):
        return unittest.mock.patch.object(redeploy, "user_data",
                                          return_value="#cloud-config")

    def test_first_flavor_with_stock_wins(self):
        p = CapableProvider({"gpu-a": 0, "gpu-b": 3})
        with self._ud():
            redeploy.redeploy(cfg(), p)
        self.assertEqual(p.created, ["gpu-b"])

    def test_no_stock_anywhere_is_an_error_not_a_create(self):
        p = CapableProvider({"gpu-a": 0, "gpu-b": 0})
        with self._ud():
            self.assertEqual(redeploy.redeploy(cfg(), p), 1)
        self.assertEqual(p.created, [])

    def test_a_recorded_orphan_blocks_a_second_create(self):
        """A crash after create must never lead to a second billing VM."""
        (pathlib.Path(self.d) / "redeploy.json").write_text(
            json.dumps({"vm_id": 111, "flavor": "gpu-a"}))
        p = CapableProvider({"gpu-a": 5})
        with self._ud():
            redeploy.redeploy(cfg(), p)
        self.assertEqual(p.created, [], "must continue against the orphan")

    def test_an_intent_that_never_created_is_discarded(self):
        """A run killed before its create call leaves vm_id null; with the
        provider seeing nothing under our name, the next up must create
        fresh — not wait forever on a ghost (e.g. two concurrent `up` runs
        where the first dies during its volume wait)."""
        (pathlib.Path(self.d) / "redeploy.json").write_text(
            json.dumps({"vm_id": None, "flavor": "gpu-a"}))
        p = CapableProvider({"gpu-a": 5})
        with self._ud():
            redeploy.redeploy(cfg(), p)
        self.assertEqual(p.created, ["gpu-a"], "must create, not wait")

    def test_missing_redeploy_intent_refuses(self):
        p = CapableProvider({"gpu-a": 5})
        with self._ud():
            self.assertEqual(redeploy.redeploy(cfg(flavors=()), p), 1)
            self.assertEqual(redeploy.redeploy(cfg(volume_id=0), p), 1)
        self.assertEqual(p.created, [])


if __name__ == "__main__":
    unittest.main()
