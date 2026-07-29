"""Lifecycle state — shared on the box when harbor manages one.

Holds, strikes and the warned marker govern a SHARED machine, so they live on
that machine; per-laptop copies let one user's watchdog park a box another
user is holding. Endpoint mode (no VM) keeps local files.
"""
import pathlib
import subprocess
import tempfile
import unittest
import unittest.mock

from harbor import state
from harbor.config import Config


def cfg(vm_ip="10.0.0.1"):
    return Config(
        vm_name="box" if vm_ip else "", vm_ip=vm_ip, provider="hyperstack",
        ssh_user="u", ssh_key=pathlib.Path("/x"), api="http://x",
        key_file=pathlib.Path("/x"), rate_per_hr=1.0,
        model_key_file=pathlib.Path("/x"), slot_context=1, effort="max",
        flow_concurrency=0, endpoint_url="http://x",
        oracle_markers="x", oracle_model="",
    )


def isolate_env(tc: unittest.TestCase) -> None:
    """Shared mode requires the env override absent; other tests set it."""
    import os
    p = unittest.mock.patch.dict(os.environ)
    p.start()
    os.environ.pop("LLM_WD_STATE_DIR", None)
    tc.addCleanup(p.stop)


class FakeBox:
    """Executes the state scripts against a local directory, standing in for
    the box's ~/.harbor-state over SSH."""

    def __init__(self, root):
        self.root = root
        self.calls = 0

    def run(self, cfg_, script):
        self.calls += 1
        # cwd IS the box state dir — the scripts use relative paths exactly
        # as they do after the real transport's `cd`.
        r = subprocess.run(["bash", "-c", script], cwd=self.root,
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise state.StateUnavailable(r.stderr.strip() or "script failed")
        return r.stdout


class DeadBox:
    def run(self, cfg_, script):
        raise state.StateUnavailable("ssh: connect timed out")


class SharedHold(unittest.TestCase):
    def setUp(self):
        isolate_env(self)
        self.d = tempfile.mkdtemp()
        self.box = FakeBox(self.d)
        self.patch = unittest.mock.patch.object(state, "_box_run", self.box.run)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()

    def test_hold_set_on_one_laptop_is_seen_from_another(self):
        state.set_hold(2, cfg())
        # a "different laptop" is just a different process — same box state
        expiry = state.hold_expiry(cfg())
        self.assertIsNotNone(expiry)
        self.assertGreater(expiry, 0)

    def test_release_clears_the_shared_hold(self):
        state.set_hold(2, cfg())
        state.release_hold(cfg())
        self.assertIsNone(state.hold_expiry(cfg()))

    def test_endpoint_mode_stays_local(self):
        with unittest.mock.patch.dict("os.environ",
                                      {"LLM_WD_STATE_DIR": self.d}):
            state.set_hold(1, cfg(vm_ip=""))
        self.assertEqual(self.box.calls, 0, "no VM — nothing to SSH to")


class WindowedStrikes(unittest.TestCase):
    def setUp(self):
        isolate_env(self)
        self.d = tempfile.mkdtemp()
        self.box = FakeBox(self.d)
        self.patch = unittest.mock.patch.object(state, "_box_run", self.box.run)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()

    def test_two_watchdogs_in_one_window_count_one_strike(self):
        """N laptops tick independently; one idle period must not earn N
        strikes."""
        self.assertEqual(state.bump_strikes(cfg()), 1)
        self.assertEqual(state.bump_strikes(cfg()), 1, "same window — no bump")

    def test_reset_zeroes_the_shared_count(self):
        state.bump_strikes(cfg())
        state.reset_strikes(cfg())
        self.assertEqual(state.bump_strikes(cfg()), 1)

    def test_warned_marker_is_shared_and_clearable(self):
        self.assertFalse(state.warned(cfg()))
        state.set_warned(cfg())
        self.assertTrue(state.warned(cfg()))
        state.clear_warned(cfg())
        self.assertFalse(state.warned(cfg()))


class UnreachableBox(unittest.TestCase):
    def test_state_errors_are_typed_not_generic(self):
        isolate_env(self)
        with unittest.mock.patch.object(state, "_box_run", DeadBox().run):
            with self.assertRaises(state.StateUnavailable):
                state.hold_expiry(cfg())
            with self.assertRaises(state.StateUnavailable):
                state.bump_strikes(cfg())


if __name__ == "__main__":
    unittest.main()
