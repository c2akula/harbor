"""harbor init — the interactive scaffold must produce a loadable config."""
import os
import pathlib
import tempfile
import unittest
import unittest.mock

from typer.testing import CliRunner

from harbor import cli, config as config_mod


class InitScaffold(unittest.TestCase):
    ANSWERS = "\n".join([
        "hyperstack",         # harbor manages a Hyperstack box
        "boxy",               # vm name
        "10.1.2.3",           # ip
        "",                   # ssh user (default)
        "",                   # ssh key (default)
        "",                   # api key file (default)
        "",                   # rate (default)
        "",                   # model key file (default)
        "seekrit|acmecorp",   # markers — no default, must be typed
        "",                   # oracle model (default)
    ]) + "\n"

    def _init(self, conf: pathlib.Path, input_text: str):
        os.environ["HARBOR_CONF"] = str(conf)
        try:
            # No crush/brew on the imaginary machine: the wizard prints the
            # install hint instead of prompting, keeping stdin fixtures flat.
            with unittest.mock.patch("harbor.units.install_units"), \
                 unittest.mock.patch("harbor.init_cmd.shutil.which",
                                     return_value=None):
                return CliRunner().invoke(cli.app, ["init"], input=input_text)
        finally:
            del os.environ["HARBOR_CONF"]

    def test_writes_a_loadable_config_with_prompted_values(self):
        with tempfile.TemporaryDirectory() as d:
            conf = pathlib.Path(d) / "config.toml"
            r = self._init(conf, self.ANSWERS)
            self.assertEqual(r.exit_code, 0, r.output)
            cfg = config_mod.load(conf)
            self.assertEqual(cfg.vm_name, "boxy")
            self.assertEqual(cfg.vm_ip, "10.1.2.3")
            self.assertEqual(cfg.ssh_user, "ubuntu")
            self.assertEqual(cfg.endpoint_url, "http://10.1.2.3:8080",
                             "a managed box serves on its own address")
            self.assertEqual(cfg.oracle_markers, "seekrit|acmecorp")
            self.assertEqual(cfg.oracle_model, "opus")

    def test_markers_have_no_default(self):
        """The guard must be typed deliberately — a default marker list is the
        scrubbed-guard regression in a new shape."""
        # the prompt supplies no default= for markers: empty input re-prompts
        with tempfile.TemporaryDirectory() as d:
            conf = pathlib.Path(d) / "config.toml"
            r = self._init(conf, self.ANSWERS.replace("seekrit|acmecorp\n", "\nx|y\n"))
            self.assertEqual(r.exit_code, 0, r.output)
            self.assertEqual(config_mod.load(conf).oracle_markers, "x|y")

    def test_refuses_to_clobber_without_confirmation(self):
        with tempfile.TemporaryDirectory() as d:
            conf = pathlib.Path(d) / "config.toml"
            conf.write_text("# existing\n")
            r = self._init(conf, "n\n")
            self.assertNotEqual(r.exit_code, 0)
            self.assertEqual(conf.read_text(), "# existing\n", "must not overwrite on 'n'")


class BringYourOwnEndpointMode(unittest.TestCase):
    """The `endpoint` mode is the documented smallest way in; it must actually work."""

    ANSWERS = "\n".join([
        "endpoint",                 # you run the server, harbor drives it
        "http://gpu.example:9000",  # endpoint url
        "",                         # key file (default)
        "",                         # slot context (default)
        "seekrit|acmecorp",         # markers — no default, typed deliberately
        "",                         # oracle model (default)
        "",                         # flow concurrency (default)
    ]) + "\n"

    def test_modes_are_named_not_numbered(self):
        """A digit means nothing in a bug report or a README; a name carries
        its own definition."""
        from harbor import init_cmd
        self.assertEqual(set(init_cmd.MODES), {"hyperstack", "provider", "endpoint"})

    def test_tty_prompts_go_through_questionary(self):
        """Interactive sessions get the arrow-key/validator UX; the plain
        path stays for piped stdin. Both must produce the same answers."""
        from harbor import init_cmd
        fake_q = unittest.mock.MagicMock()
        fake_q.select.return_value.ask.return_value = "endpoint"
        fake_q.text.return_value.ask.return_value = "abc"
        fake_q.confirm.return_value.ask.return_value = True
        with unittest.mock.patch.object(init_cmd, "_interactive",
                                        return_value=True), \
             unittest.mock.patch.dict("sys.modules",
                                      {"questionary": fake_q}):
            self.assertEqual(
                init_cmd._ask_select("m", ["a", "endpoint"], "a"), "endpoint")
            self.assertEqual(init_cmd._ask_text("t", default="x"), "abc")
            self.assertTrue(init_cmd._ask_confirm("c", default=False))
        fake_q.select.assert_called_once()
        fake_q.text.assert_called_once()
        fake_q.confirm.assert_called_once()

    def test_marker_verdict_rejects_empty_and_bad_regex(self):
        from harbor import init_cmd
        self.assertIsNot(init_cmd._marker_verdict(""), True)
        self.assertIsNot(init_cmd._marker_verdict("("), True)
        self.assertIs(init_cmd._marker_verdict("acme|proj"), True)

    def test_writes_a_config_with_no_vm_section(self):
        import os
        with tempfile.TemporaryDirectory() as d:
            conf = pathlib.Path(d) / "config.toml"
            os.environ["HARBOR_CONF"] = str(conf)
            try:
                with unittest.mock.patch("harbor.init_cmd.shutil.which",
                                         return_value=None):
                    r = CliRunner().invoke(cli.app, ["init"], input=self.ANSWERS)
            finally:
                del os.environ["HARBOR_CONF"]
            self.assertEqual(r.exit_code, 0, r.output)
            # A section HEADER, not the substring — the file explains itself
            # in a comment that mentions [vm].
            headers = [l.strip() for l in conf.read_text().splitlines()
                       if l.strip().startswith("[")]
            self.assertNotIn("[vm]", headers,
                             "the `endpoint` mode must not write a [vm] section")
            cfg = config_mod.load(conf)
            self.assertFalse(cfg.manages_vm)
            self.assertEqual(cfg.endpoint_url, "http://gpu.example:9000")
            self.assertEqual(cfg.oracle_markers, "seekrit|acmecorp")

    def test_does_not_try_to_install_systemd_units(self):
        """There is no machine to manage, so there are no units to render."""
        import os
        with tempfile.TemporaryDirectory() as d:
            conf = pathlib.Path(d) / "config.toml"
            os.environ["HARBOR_CONF"] = str(conf)
            try:
                with unittest.mock.patch("harbor.units.install_units") as iu, \
                     unittest.mock.patch("harbor.init_cmd.shutil.which",
                                         return_value=None):
                    r = CliRunner().invoke(cli.app, ["init"], input=self.ANSWERS)
            finally:
                del os.environ["HARBOR_CONF"]
            self.assertEqual(r.exit_code, 0, r.output)
            iu.assert_not_called()


if __name__ == "__main__":
    unittest.main()
