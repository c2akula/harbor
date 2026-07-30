"""up/down against the PROVIDER PROTOCOL, not a vendor's API.

These assert the contract any provider must satisfy — which is the point of
the seam: harbor's lifecycle logic should be testable without naming a cloud.
"""
import pathlib
import unittest
import unittest.mock

from harbor import lifecycle
from harbor import provider as prov
from harbor.config import Config

FAKE = Config(
    vm_name="box", provider="hyperstack", ssh_user="u",
    ssh_key=pathlib.Path("/x"), api="http://x", key_file=pathlib.Path("/x"),
    rate_per_hr=1.0, model_key_file=pathlib.Path("/x"),
    slot_context=1, effort="max", flow_concurrency=0,
    endpoint_url="http://127.0.0.1:1", oracle_markers="x", oracle_model="",
)


class FakeProvider:
    """States are consumed in order, so a test can describe a trajectory."""

    def __init__(self, states, start_raises=None):
        self._states = iter(states)
        self._start_raises = start_raises
        self.started = 0
        self.stopped = 0

    def state(self, cfg):
        try:
            return next(self._states)
        except StopIteration:
            return prov.ACTIVE

    def start(self, cfg):
        self.started += 1
        if self._start_raises:
            raise self._start_raises

    def stop(self, cfg):
        self.stopped += 1


def run_up(fake):
    import os
    import tempfile
    from harbor import wgnet
    with tempfile.TemporaryDirectory() as d, \
         unittest.mock.patch.dict(os.environ, {"LLM_WD_STATE_DIR": d}), \
         unittest.mock.patch.object(prov, "load", return_value=fake), \
         unittest.mock.patch.object(lifecycle.subprocess, "run"), \
         unittest.mock.patch.object(lifecycle.time, "sleep"), \
         unittest.mock.patch.object(wgnet, "operator_link"), \
         unittest.mock.patch.object(lifecycle.requests, "get") as get:
        get.return_value.status_code = 200
        return lifecycle.up(FAKE)


class UpContract(unittest.TestCase):
    def test_off_box_is_started(self):
        f = FakeProvider([prov.OFF, prov.ACTIVE])
        self.assertEqual(run_up(f), 0)
        self.assertEqual(f.started, 1)

    def test_active_box_is_left_alone(self):
        f = FakeProvider([prov.ACTIVE, prov.ACTIVE])
        self.assertEqual(run_up(f), 0)
        self.assertEqual(f.started, 0, "must not start an already-running box")

    def test_transitioning_is_waited_out_then_started(self):
        """A box mid-change cannot be started; acting early is how we got a
        400 from a hibernation still in flight."""
        f = FakeProvider([prov.TRANSITIONING, prov.TRANSITIONING,
                          prov.OFF, prov.ACTIVE])
        self.assertEqual(run_up(f), 0)
        self.assertEqual(f.started, 1)

    def test_unknown_state_refuses_to_act(self):
        # Enough UNKNOWNs to outlast the transient-probe retries.
        f = FakeProvider([prov.UNKNOWN] * 8)
        self.assertEqual(run_up(f), 1)
        self.assertEqual(f.started, 0, "must never act on an unknown state")

    def test_a_transient_unknown_is_retried_not_fatal(self):
        """One flaky API response must not abort the convergence command."""
        f = FakeProvider([prov.UNKNOWN, prov.ACTIVE, prov.ACTIVE])
        self.assertEqual(run_up(f), 0)

    def test_provider_error_is_surfaced_verbatim_and_stops(self):
        """e.g. a capacity outage — the provider phrased it; harbor relays it
        rather than retrying into a wall."""
        f = FakeProvider([prov.OFF],
                         start_raises=prov.ProviderError("no free capacity"))
        self.assertEqual(run_up(f), 1)
        self.assertEqual(f.started, 1, "one attempt, not a retry storm")


class DroppyProvider(FakeProvider):
    """Accepts a start request and silently drops it — the box stays OFF
    until a second request lands."""

    def state(self, cfg):
        return prov.ACTIVE if self.started >= 2 else prov.OFF

    def start(self, cfg):
        self.started += 1


class NeverStartsProvider(FakeProvider):
    def state(self, cfg):
        return prov.OFF

    def start(self, cfg):
        self.started += 1


class LostStartContract(unittest.TestCase):
    """A start call returning success proves the request was accepted, not
    that the box will start. The wait loop owns the outcome."""

    def test_a_lost_start_is_reissued(self):
        f = DroppyProvider([])
        self.assertEqual(run_up(f), 0)
        self.assertGreaterEqual(f.started, 2, "the dropped start must be retried")

    def test_a_box_that_never_starts_is_an_error_not_a_false_active(self):
        import os
        import tempfile
        f = NeverStartsProvider([])
        with tempfile.TemporaryDirectory() as d, \
             unittest.mock.patch.dict(os.environ, {"LLM_WD_STATE_DIR": d}), \
             unittest.mock.patch.object(prov, "load", return_value=f), \
             unittest.mock.patch.object(lifecycle.subprocess, "run") as sub, \
             unittest.mock.patch.object(lifecycle.time, "sleep"), \
             unittest.mock.patch.object(lifecycle.requests, "get"):
            self.assertEqual(lifecycle.up(FAKE), 1)
        sub.assert_not_called()


class DownContract(unittest.TestCase):
    def test_stop_is_delegated_to_the_provider(self):
        f = FakeProvider([prov.ACTIVE])
        with unittest.mock.patch.object(prov, "load", return_value=f), \
             unittest.mock.patch.object(lifecycle.subprocess, "run"):
            self.assertEqual(lifecycle.down(FAKE), 0)
        self.assertEqual(f.stopped, 1)


if __name__ == "__main__":
    unittest.main()
