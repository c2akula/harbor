"""Registration service — auth, allocation, and what it never discloses."""
import http.client
import json
import pathlib
import tempfile
import threading
import unittest
import unittest.mock

from harbor import regserve

PUB_A = "A" * 43 + "="
PUB_B = "B" * 43 + "="


def make_state(tmp):
    root = pathlib.Path(tmp)
    (root / "keys").mkdir()
    (root / "keys" / "team.key").write_text("hbr-team-token\n")
    (root / "wg").mkdir()
    (root / "wg" / "box.pub").write_text("BOXPUB\n")
    return regserve.State(keys_dir=root / "keys", wg_dir=root / "wg")


class Auth(unittest.TestCase):
    def test_any_key_on_disk_is_accepted(self):
        with tempfile.TemporaryDirectory() as d:
            st = make_state(d)
            (pathlib.Path(d) / "keys" / "alice.key").write_text("hbr-alice\n")
            self.assertTrue(st.authorized("hbr-team-token"))
            self.assertTrue(st.authorized("hbr-alice"))

    def test_unknown_and_empty_are_refused(self):
        with tempfile.TemporaryDirectory() as d:
            st = make_state(d)
            self.assertFalse(st.authorized("hbr-wrong"))
            self.assertFalse(st.authorized(""))


class Allocation(unittest.TestCase):
    def test_teammates_start_at_ten_and_are_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            st = make_state(d)
            self.assertEqual(st.allocate(PUB_A), "10.77.0.10")
            self.assertEqual(st.allocate(PUB_B), "10.77.0.11")
            self.assertEqual(st.allocate(PUB_A), "10.77.0.10",
                             "same device rejoining must keep its address")

    def test_allocations_survive_restart(self):
        with tempfile.TemporaryDirectory() as d:
            st = make_state(d)
            st.allocate(PUB_A)
            st2 = regserve.State(keys_dir=st.keys_dir, wg_dir=st.wg_dir)
            self.assertEqual(st2.allocate(PUB_B), "10.77.0.11")


class BootReplay(unittest.TestCase):
    def test_apply_all_replays_the_persisted_table(self):
        with tempfile.TemporaryDirectory() as d:
            st = make_state(d)
            st.allocate(PUB_A)
            st.allocate(PUB_B)
            applied = []
            st.apply_peer = lambda pub, ip: applied.append((pub, ip))
            st.apply_all()
            self.assertEqual(sorted(applied),
                             [(PUB_A, "10.77.0.10"), (PUB_B, "10.77.0.11")])


class Endpoint(unittest.TestCase):
    """The HTTP contract, exercised against a real server on loopback
    (plain HTTP here; TLS wrapping is deployment, not logic)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state = make_state(self._tmp.name)
        self.wg_calls = []
        self.state.apply_peer = lambda pub, ip: self.wg_calls.append((pub, ip))
        self.server = regserve.make_server("127.0.0.1", 0, self.state)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self._tmp.cleanup()

    def _post(self, body, token=None, path="/join"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        conn.request("POST", path, body=json.dumps(body), headers=headers)
        r = conn.getresponse()
        return r.status, json.loads(r.read() or b"{}")

    def test_valid_join_returns_own_allocation_only(self):
        status, got = self._post({"pubkey": PUB_A}, token="hbr-team-token")
        self.assertEqual(status, 200)
        self.assertEqual(got, {"box_pubkey": "BOXPUB", "ip": "10.77.0.10"})
        self.assertEqual(self.wg_calls, [(PUB_A, "10.77.0.10")])

    def test_bad_and_missing_tokens_get_identical_401s(self):
        s1, b1 = self._post({"pubkey": PUB_A}, token="hbr-wrong")
        s2, b2 = self._post({"pubkey": PUB_A}, token=None)
        self.assertEqual((s1, b1), (s2, b2))
        self.assertEqual(s1, 401)
        self.assertEqual(self.wg_calls, [])

    def test_malformed_pubkey_is_a_400(self):
        status, _ = self._post({"pubkey": "short"}, token="hbr-team-token")
        self.assertEqual(status, 400)

    def test_other_paths_do_not_exist(self):
        status, _ = self._post({"pubkey": PUB_A}, token="hbr-team-token",
                               path="/peers")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
