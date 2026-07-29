"""The provider seam: harbor manages a box without naming a cloud."""
import pathlib
import unittest
import unittest.mock

from harbor import provider as prov
from harbor.config import Config


def cfg(provider_spec: str) -> Config:
    return Config(
        vm_name="box", vm_ip="0.0.0.0", provider=provider_spec, ssh_user="u",
        ssh_key=pathlib.Path("/x"), api="http://x", key_file=pathlib.Path("/x"),
        rate_per_hr=1.0, model_key_file=pathlib.Path("/x"),
        slot_context=1, effort="max", flow_concurrency=0,
        endpoint_url="http://x", oracle_markers="x", oracle_model="",
    )


class Loading(unittest.TestCase):
    def test_default_is_the_builtin(self):
        from harbor.hyperstack import HyperstackProvider
        self.assertIsInstance(prov.load(cfg("hyperstack")), HyperstackProvider)
        self.assertIsInstance(prov.load(cfg("")), HyperstackProvider)

    def test_import_path_loads_a_third_party_provider(self):
        """A team's provider only has to be importable — no packaging."""
        import types, sys
        mod = types.ModuleType("fake_cloud")

        class MyProvider:
            def state(self, cfg): return prov.ACTIVE
            def start(self, cfg): pass
            def stop(self, cfg): pass

        mod.MyProvider = MyProvider
        sys.modules["fake_cloud"] = mod
        try:
            p = prov.load(cfg("fake_cloud:MyProvider"))
            self.assertIsInstance(p, MyProvider)
            self.assertEqual(p.state(None), prov.ACTIVE)
        finally:
            del sys.modules["fake_cloud"]

    def test_bad_spec_explains_itself(self):
        with self.assertRaises(prov.ProviderError) as e:
            prov.load(cfg("gcp"))
        self.assertIn("import path", str(e.exception))

    def test_missing_module_names_the_problem(self):
        with self.assertRaises(prov.ProviderError) as e:
            prov.load(cfg("no_such_module:Thing"))
        self.assertIn("no_such_module", str(e.exception))


class HyperstackNormalisation(unittest.TestCase):
    """Hyperstack has TWO off-states with different endpoints AND different
    billing. Both must present as OFF so harbor's logic never has to know."""

    def _state(self, raw):
        from harbor import hyperstack
        with unittest.mock.patch.object(hyperstack, "vm_state", return_value=raw):
            return hyperstack.HyperstackProvider().state(cfg("hyperstack"))

    def test_both_off_states_normalise_to_off(self):
        self.assertEqual(self._state("HIBERNATED"), prov.OFF)
        self.assertEqual(self._state("SHUTOFF"), prov.OFF)

    def test_active_and_transitional_and_unknown(self):
        self.assertEqual(self._state("ACTIVE"), prov.ACTIVE)
        self.assertEqual(self._state("HIBERNATING"), prov.TRANSITIONING)
        self.assertEqual(self._state(""), prov.UNKNOWN)

    def test_start_picks_the_endpoint_matching_the_off_state(self):
        """SHUTOFF needs /start; HIBERNATED needs /hibernate-restore. Using the
        wrong one leaves the box off while it bills."""
        from harbor import hyperstack
        for raw, expected in (("SHUTOFF", "start"), ("HIBERNATED", "resume")):
            with unittest.mock.patch.object(hyperstack, "vm_state", return_value=raw), \
                 unittest.mock.patch.object(hyperstack, "start") as st, \
                 unittest.mock.patch.object(hyperstack, "resume") as rs:
                hyperstack.HyperstackProvider().start(cfg("hyperstack"))
                called = "start" if st.called else "resume" if rs.called else None
                self.assertEqual(called, expected, raw)

    def test_capacity_outage_becomes_an_actionable_provider_error(self):
        from harbor import hyperstack
        err = hyperstack.HyperstackError(400, "Not Enough Stock of L40.")
        with unittest.mock.patch.object(hyperstack, "vm_state", return_value="HIBERNATED"), \
             unittest.mock.patch.object(hyperstack, "resume", side_effect=err):
            with self.assertRaises(prov.ProviderError) as e:
                hyperstack.HyperstackProvider().start(cfg("hyperstack"))
        self.assertIn("no free capacity", str(e.exception))


if __name__ == "__main__":
    unittest.main()
