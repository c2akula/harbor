"""Per-user API keys — issued once, stored on the volume, revocable.

The key list lives with the weights (/weights/keys) so an M5-style redeploy
carries the team's access along with the model. Serving reads the list at
unit-render time; revocation is a re-render away.
"""
import pathlib
import subprocess
import tempfile
import unittest
import unittest.mock

from harbor import keys, state
from harbor.config import Config


def cfg():
    return Config(
        vm_name="box", vm_ip="10.0.0.1", provider="hyperstack", ssh_user="u",
        ssh_key=pathlib.Path("/x"), api="http://x",
        key_file=pathlib.Path("/x"), rate_per_hr=1.0,
        model_key_file=pathlib.Path("/x"), slot_context=1, effort="max",
        flow_concurrency=0, endpoint_url="http://10.0.0.1:8080",
        oracle_markers="x", oracle_model="",
    )


class FakeBox:
    def __init__(self, root):
        self.root = root

    def run(self, cfg_, script):
        r = subprocess.run(["bash", "-c", script], cwd=self.root,
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise state.StateUnavailable(r.stderr.strip() or "failed")
        return r.stdout


class KeyLifecycle(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        (pathlib.Path(self.d)).mkdir(exist_ok=True)
        self.patch = unittest.mock.patch.object(
            keys, "_run", FakeBox(self.d).run)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_add_generates_a_distinct_prefixed_token_per_name(self):
        a = keys.add(cfg(), "alice")
        b = keys.add(cfg(), "bob")
        self.assertTrue(a.startswith("hbr-") and b.startswith("hbr-"))
        self.assertNotEqual(a, b)
        self.assertEqual(set(keys.list_names(cfg())), {"alice", "bob"})

    def test_add_twice_refuses_rather_than_rotating_silently(self):
        keys.add(cfg(), "alice")
        with self.assertRaises(keys.KeyExists):
            keys.add(cfg(), "alice")

    def test_revoke_removes_the_key(self):
        keys.add(cfg(), "alice")
        keys.revoke(cfg(), "alice")
        self.assertEqual(keys.list_names(cfg()), [])

    def test_all_tokens_feed_the_serving_list(self):
        a = keys.add(cfg(), "alice")
        b = keys.add(cfg(), "bob")
        self.assertEqual(set(keys.tokens(cfg())), {a, b})

    def test_names_are_confined_to_the_keys_directory(self):
        for bad in ("../escape", "a/b", "a b", ""):
            with self.assertRaises(ValueError):
                keys.add(cfg(), bad)


if __name__ == "__main__":
    unittest.main()
