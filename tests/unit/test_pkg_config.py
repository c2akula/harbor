"""Package config loading — TOML, HARBOR_CONF override, fail-closed defaults."""
import pathlib
import tempfile
import unittest

from harbor import config as config_mod

MINIMAL = b"""
[vm]
name = "box"
ip = "10.0.0.1"
"""


class ConfigLoad(unittest.TestCase):
    def _load(self, body: bytes) -> config_mod.Config:
        with tempfile.NamedTemporaryFile(suffix=".toml", delete=False) as f:
            f.write(body)
        try:
            return config_mod.load(pathlib.Path(f.name))
        finally:
            pathlib.Path(f.name).unlink()

    def test_minimal_config_gets_documented_defaults(self):
        cfg = self._load(MINIMAL)
        self.assertEqual(cfg.slot_context, 131072)
        self.assertEqual(cfg.ssh_user, "ubuntu")

    def test_oracle_markers_default_is_empty_so_consult_fails_closed(self):
        """No baked-in marker default: an unset guard must refuse, not half-work."""
        cfg = self._load(MINIMAL)
        self.assertEqual(cfg.oracle_markers, "")

    def test_example_deployment_parses_and_configures_the_guard(self):
        """The tracked example must be loadable — real deployments are
        gitignored, so this is what a fresh clone actually has."""
        repo = pathlib.Path(__file__).resolve().parents[2]
        cfg = config_mod.load(repo / "deployments" / "config.toml.example")
        self.assertTrue(cfg.oracle_markers, "example must show the guard configured")
        self.assertTrue(cfg.manages_vm)
        self.assertTrue(cfg.flavors, "example must show the redeploy intent")

    def test_missing_config_raises(self):
        with self.assertRaises(FileNotFoundError):
            config_mod.load(pathlib.Path("/nonexistent/config.toml"))


class StatusWarnsOnCostlyStates(unittest.TestCase):
    def test_shutoff_is_flagged_as_billing(self):
        """SHUTOFF looks parked but bills full rate — the one state worth
        shouting about. Vendor-specific, so it comes from the provider detail
        path rather than harbor's generic states."""
        import contextlib
        import io
        import unittest.mock
        from harbor import hyperstack, status as status_mod
        from harbor.config import Config
        cfg = Config(vm_name="box", provider="hyperstack",
                     ssh_user="u", ssh_key=pathlib.Path("/x"), api="http://x",
                     key_file=pathlib.Path("/x"), rate_per_hr=1.0,
                     model_key_file=pathlib.Path("/x"), slot_context=1,
                     effort="max", flow_concurrency=0,
                     endpoint_url="http://127.0.0.1:1",
                     oracle_markers="x", oracle_model="")
        buf = io.StringIO()
        with unittest.mock.patch.object(hyperstack, "vm_state", return_value="SHUTOFF"), \
             unittest.mock.patch.object(hyperstack, "vm_info", return_value=("SHUTOFF", "L40")), \
             unittest.mock.patch.object(hyperstack, "gpu_stock", return_value=3), \
             unittest.mock.patch.object(status_mod, "model_serving", return_value=None), \
             contextlib.redirect_stdout(buf):
            rc = status_mod.run(cfg)
        self.assertEqual(rc, 1)
        self.assertIn("BILLING AT FULL RATE", buf.getvalue())


class StateDirMigration(unittest.TestCase):
    def test_legacy_dir_is_moved_with_live_state_intact(self):
        """An active hold must survive the llm-watchdog -> harbor rename."""
        import os
        import unittest.mock
        from harbor import state
        with tempfile.TemporaryDirectory() as d:
            legacy = pathlib.Path(d) / "llm-watchdog"
            new = pathlib.Path(d) / "harbor"
            legacy.mkdir()
            (legacy / "hold").write_text("9999999999\n")
            os.environ.pop("LLM_WD_STATE_DIR", None)
            with unittest.mock.patch.object(state, "LEGACY_STATE_DIR", legacy), \
                 unittest.mock.patch.object(state, "DEFAULT_STATE_DIR", new):
                got = state.state_dir()
            self.assertEqual(got, new)
            self.assertFalse(legacy.exists(), "legacy dir must be gone after migration")
            self.assertEqual((new / "hold").read_text().strip(), "9999999999")


class HoldState(unittest.TestCase):
    def test_hold_set_and_release_roundtrip(self):
        import os
        from harbor import state
        with tempfile.TemporaryDirectory() as d:
            os.environ["LLM_WD_STATE_DIR"] = d
            try:
                expiry = state.set_hold(2)
                self.assertEqual(state.hold_expiry(), expiry)
                state.release_hold()
                self.assertIsNone(state.hold_expiry())
            finally:
                del os.environ["LLM_WD_STATE_DIR"]

    def test_hold_clears_a_pending_hibernate(self):
        """A hold placed after the warning must cancel the queued hibernate."""
        import os
        from harbor import state
        with tempfile.TemporaryDirectory() as d:
            os.environ["LLM_WD_STATE_DIR"] = d
            try:
                pending = pathlib.Path(d) / "pending_hibernate"
                pending.touch()
                state.set_hold(1)
                self.assertFalse(pending.exists())
            finally:
                del os.environ["LLM_WD_STATE_DIR"]


if __name__ == "__main__":
    unittest.main()


class BringYourOwnEndpoint(unittest.TestCase):
    """T3: harbor must work for a team that already runs a model server."""

    BYO = b'[endpoint]\nurl = "http://gpu.example:9000"\n'

    def _load(self, body: bytes) -> config_mod.Config:
        with tempfile.NamedTemporaryFile(suffix=".toml", delete=False) as f:
            f.write(body)
        try:
            return config_mod.load(pathlib.Path(f.name))
        finally:
            pathlib.Path(f.name).unlink()

    def test_config_without_vm_section_loads(self):
        cfg = self._load(self.BYO)
        self.assertFalse(cfg.manages_vm)
        self.assertEqual(cfg.endpoint_url, "http://gpu.example:9000")

    def test_vm_section_means_managed(self):
        cfg = self._load(b'[vm]\nname = "box"\nip = "10.0.0.1"\n')
        self.assertTrue(cfg.manages_vm)

    def test_endpoint_url_defaults_to_the_box_address(self):
        """A managed box serves on the hub address of harbor's own network —
        the same constant for every client."""
        cfg = self._load(b'[vm]\nname = "box"\n')
        self.assertEqual(cfg.endpoint_url, "http://10.77.0.1:8080")

    def test_trailing_slash_is_normalised(self):
        cfg = self._load(b'[endpoint]\nurl = "http://gpu.example:9000/"\n')
        self.assertEqual(cfg.endpoint_url, "http://gpu.example:9000")

    def test_lifecycle_commands_refuse_clearly_without_a_vm(self):
        """A sentence the user can act on, not a traceback."""
        import os
        from typer.testing import CliRunner
        from harbor import cli
        with tempfile.NamedTemporaryFile("wb", suffix=".toml", delete=False) as f:
            f.write(self.BYO)
        os.environ["HARBOR_CONF"] = f.name
        try:
            for cmd in (["up"], ["down"], ["model", "qwen35"]):
                r = CliRunner().invoke(cli.app, cmd)
                self.assertEqual(r.exit_code, 2, cmd)
                self.assertIn("no [vm] section", r.output + str(r.stderr or ""), cmd)
        finally:
            del os.environ["HARBOR_CONF"]
            pathlib.Path(f.name).unlink()
