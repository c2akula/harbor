"""Watchdog behaviour — the observable contract, no VM required.

Probes are injected via LLM_WD_* env vars (same contract the shell version
had), so these exercise the real code path rather than a copy of its logic.
"""
import os
import pathlib
import tempfile
import time
import unittest
import unittest.mock

from typer.testing import CliRunner

from harbor import cli, watchdog
from harbor.config import Config

FAKE = Config(
    vm_name="", vm_ip="0.0.0.0", provider="hyperstack", ssh_user="nobody", ssh_key=pathlib.Path("/nonexistent"),
    api="http://127.0.0.1:1", key_file=pathlib.Path("/nonexistent"), rate_per_hr=1.0,
    model_key_file=pathlib.Path("/nonexistent"), slot_context=1,
    effort="max", flow_concurrency=0, endpoint_url="http://127.0.0.1:8081", oracle_markers="x", oracle_model="",
)


def tick(state_dir, *, vm_state="ACTIVE", traffic="0", local_tmux=False,
         vm_tmux=False, stock=None):
    """Run one watchdog check with everything stubbed. Returns (rc, hibernated)."""
    marker = pathlib.Path(state_dir) / "DID_HIBERNATE"
    stubs = {
        "LLM_WD_STATE_DIR": str(state_dir),
        "LLM_WD_STATE_CMD": f"echo {vm_state}",
        "LLM_WD_TRAFFIC_CMD": f"echo {traffic}",
        "LLM_WD_LOCAL_TMUX_CMD": "true" if local_tmux else "false",
        "LLM_WD_VM_TMUX_CMD": "true" if vm_tmux else "false",
        "LLM_WD_DOWN_CMD": f"touch {marker}",
        "LLM_WD_STOCK_CMD": f"echo {stock}" if stock is not None else "echo",
        "LLM_WD_NOTIFY_CMD": f"cat >> {state_dir}/NOTIFIED",
    }
    old = {k: os.environ.get(k) for k in stubs}
    os.environ.update(stubs)
    try:
        rc = watchdog.tick(FAKE)
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return rc, marker.exists()


def strikes(state_dir):
    f = pathlib.Path(state_dir) / "idle_count"
    return int(f.read_text().split()[0]) if f.exists() else 0


def pending(state_dir):
    return (pathlib.Path(state_dir) / "pending_hibernate").exists()


class WatchdogContract(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="wdtest_")
        # These tests assert strike LOGIC with back-to-back ticks; the
        # window is a wall-clock concern, disabled here.
        p = unittest.mock.patch.object(watchdog.state_mod, "STRIKE_WINDOW", 0)
        p.start()
        self.addCleanup(p.stop)

    # --- it must never park a box that is doing work -----------------------
    def test_traffic_resets_strikes(self):
        tick(self.d); tick(self.d)
        tick(self.d, traffic="7")
        self.assertEqual(strikes(self.d), 0, "traffic must reset the idle count")

    def test_local_tmux_blocks(self):
        _, hib = tick(self.d, local_tmux=True)
        self.assertFalse(hib)
        self.assertEqual(strikes(self.d), 0)

    def test_vm_tmux_blocks(self):
        _, hib = tick(self.d, vm_tmux=True)
        self.assertFalse(hib)
        self.assertEqual(strikes(self.d), 0)

    def test_probe_error_is_fail_safe(self):
        """An unreachable VM must never cause a blind hibernate."""
        for _ in range(5):
            _, hib = tick(self.d, traffic="ERR")
            self.assertFalse(hib, "must not hibernate when traffic is unknown")

    def test_non_active_vm_does_nothing(self):
        _, hib = tick(self.d, vm_state="HIBERNATED")
        self.assertFalse(hib)

    # --- warn before parking ----------------------------------------------
    def test_warns_before_hibernating(self):
        for _ in range(3):
            _, hib = tick(self.d)
            self.assertFalse(hib, "must not hibernate before warning")
        self.assertTrue(pending(self.d), "third idle check must raise a warning")

    def test_hibernates_after_warning(self):
        for _ in range(3):
            tick(self.d)
        _, hib = tick(self.d)
        self.assertTrue(hib, "must hibernate on the check after the warning")
        self.assertFalse(pending(self.d), "pending marker cleared after acting")

    # --- holds -------------------------------------------------------------
    def test_active_hold_blocks_and_clears_pending(self):
        for _ in range(3):
            tick(self.d)
        self.assertTrue(pending(self.d))
        (pathlib.Path(self.d) / "hold").write_text(str(int(time.time()) + 3600))
        _, hib = tick(self.d)
        self.assertFalse(hib, "an active hold must prevent hibernation")
        self.assertFalse(pending(self.d), "hold must cancel a pending hibernate")
        self.assertEqual(strikes(self.d), 0)

    def test_unreachable_shared_state_means_abstain(self):
        """Never park what you cannot see: costs money, never work."""
        with unittest.mock.patch.object(
                watchdog.state_mod, "hold_expiry",
                side_effect=watchdog.state_mod.StateUnavailable("ssh down")):
            for _ in range(5):
                rc, hib = tick(self.d)
                self.assertEqual(rc, 0)
                self.assertFalse(hib, "must not hibernate blind")

    def test_expired_hold_is_ignored_and_removed(self):
        h = pathlib.Path(self.d) / "hold"
        h.write_text(str(int(time.time()) - 60))
        tick(self.d)
        self.assertFalse(h.exists(), "expired hold must be removed, not honoured")
        self.assertEqual(strikes(self.d), 1, "expired hold must not block strikes")


class StockWatch(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="stock_")

    def notified(self):
        p = pathlib.Path(self.d) / "NOTIFIED"
        return p.read_text().count("stock returned") if p.exists() else 0

    def test_zero_to_positive_notifies_once(self):
        tick(self.d, vm_state="HIBERNATED", stock=0)
        tick(self.d, vm_state="HIBERNATED", stock=0)
        self.assertEqual(self.notified(), 0, "no news while stock stays zero")
        tick(self.d, vm_state="HIBERNATED", stock=3)
        self.assertEqual(self.notified(), 1, "0 -> positive must notify")
        tick(self.d, vm_state="HIBERNATED", stock=3)
        self.assertEqual(self.notified(), 1, "steady stock must not re-notify")

    def test_first_sample_never_notifies(self):
        tick(self.d, vm_state="HIBERNATED", stock=5)
        self.assertEqual(self.notified(), 0, "no prior zero sample: no news")

    def test_active_vm_does_not_probe_stock(self):
        tick(self.d, vm_state="ACTIVE", stock=0)
        self.assertFalse((pathlib.Path(self.d) / "stock_last").exists())


class HoldCommand(unittest.TestCase):
    def _hold(self, state_dir, *args):
        os.environ["LLM_WD_STATE_DIR"] = state_dir
        try:
            r = CliRunner().invoke(cli.app, ["hold", *args])
            self.assertEqual(r.exit_code, 0, r.output)
        finally:
            del os.environ["LLM_WD_STATE_DIR"]

    def test_default_is_two_hours(self):
        d = tempfile.mkdtemp(prefix="holdtest_")
        self._hold(d)
        expiry = int((pathlib.Path(d) / "hold").read_text())
        hours = (expiry - time.time()) / 3600
        self.assertGreater(hours, 1.9)
        self.assertLess(hours, 2.1, "default hold must be 2h (12h cost a day of idle billing)")

    def test_accepts_custom_duration(self):
        d = tempfile.mkdtemp(prefix="holdtest_")
        self._hold(d, "5")
        expiry = int((pathlib.Path(d) / "hold").read_text())
        self.assertAlmostEqual((expiry - time.time()) / 3600, 5, delta=0.1)


if __name__ == "__main__":
    unittest.main()


class TrafficProbeIsEngineAgnostic(unittest.TestCase):
    """The old probe grepped llama.cpp's log marker. Under vLLM it returned
    neither 'ERR' nor a digit, fell through, and would have hibernated a box
    that was actively serving. The counter delta cannot fail that way."""

    METRICS = ('# HELP vllm:request_success_total Count\n'
               'vllm:request_success_total{finished_reason="stop"} 42.0\n'
               'vllm:request_success_total{finished_reason="length"} 8.0\n')

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="traffic_")
        os.environ["LLM_WD_STATE_DIR"] = self.d
        os.environ.pop("LLM_WD_TRAFFIC_CMD", None)

    def tearDown(self):
        os.environ.pop("LLM_WD_STATE_DIR", None)

    def _probe(self, body):
        import unittest.mock
        resp = unittest.mock.Mock()
        resp.text = body
        resp.raise_for_status = lambda: None
        with unittest.mock.patch("requests.get", return_value=resp):
            return watchdog._recent_traffic(FAKE)

    def test_first_observation_decides_nothing(self):
        self.assertEqual(self._probe(self.METRICS), "ERR")

    def test_delta_is_reported_between_ticks(self):
        self._probe(self.METRICS)                       # 50 total, seeds state
        more = self.METRICS.replace("42.0", "45.0")     # +3 requests
        self.assertEqual(self._probe(more), "3")

    def test_no_new_requests_reads_as_idle(self):
        self._probe(self.METRICS)
        self.assertEqual(self._probe(self.METRICS), "0")

    def test_unreachable_endpoint_is_fail_safe(self):
        import unittest.mock
        with unittest.mock.patch("requests.get", side_effect=RuntimeError("down")):
            self.assertEqual(watchdog._recent_traffic(FAKE), "ERR")
