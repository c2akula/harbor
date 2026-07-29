"""Systemd unit installation — what gets rendered, and on what kind of host."""
import pathlib
import subprocess
import tempfile
import unittest
import unittest.mock

from harbor import units
from harbor.config import Config


def cfg(*, vm_ip: str, tmp: pathlib.Path) -> Config:
    return Config(
        vm_name="box" if vm_ip else "", vm_ip=vm_ip, provider="hyperstack",
        ssh_user="ubuntu", ssh_key=pathlib.Path("/x"),
        api="http://x", key_file=tmp / "k", rate_per_hr=1.0,
        model_key_file=tmp / "mk", slot_context=131072,
        effort="max", flow_concurrency=0,
        endpoint_url="http://127.0.0.1:8081", oracle_markers="x",
        oracle_model="",
    )


class UnitsPerMode(unittest.TestCase):
    """The box serves on the tailnet directly — the only laptop unit is the
    watchdog, and only when harbor manages a machine."""

    def test_no_units_without_a_vm(self):
        """A watchdog with no box to park is a timer that fires forever and
        can never act; `endpoint` mode installs nothing rather than that."""
        with tempfile.TemporaryDirectory() as d:
            tmp = pathlib.Path(d)
            with unittest.mock.patch.object(units, "UNIT_DIR", tmp / "units"), \
                 unittest.mock.patch.object(units, "_daemon_reload"):
                units.install_units(cfg(vm_ip="", tmp=tmp))
            self.assertFalse((tmp / "units").exists())

    def test_managed_mode_installs_the_watchdog_and_no_tunnel(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = pathlib.Path(d)
            with unittest.mock.patch.object(units, "UNIT_DIR", tmp / "units"), \
                 unittest.mock.patch.object(units, "_daemon_reload"):
                units.install_units(cfg(vm_ip="10.0.0.1", tmp=tmp))
            self.assertTrue((tmp / "units" / "harbor-watchdog.timer").exists())
            self.assertFalse((tmp / "units" / "harbor-tunnel.service").exists())

    def test_a_stale_tunnel_unit_from_an_earlier_install_is_removed(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = pathlib.Path(d)
            (tmp / "units").mkdir()
            (tmp / "units" / "harbor-tunnel.service").write_text("stale")
            with unittest.mock.patch.object(units, "UNIT_DIR", tmp / "units"), \
                 unittest.mock.patch.object(units, "_daemon_reload"):
                units.install_units(cfg(vm_ip="10.0.0.1", tmp=tmp))
            self.assertFalse((tmp / "units" / "harbor-tunnel.service").exists())


class HostsWithoutUserSystemd(unittest.TestCase):
    """macOS, WSL without systemd, and containers have no user session bus.
    Installing should still leave a usable tool rather than abort."""

    def test_install_survives_a_missing_systemctl(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = pathlib.Path(d)
            with unittest.mock.patch.object(units, "UNIT_DIR", tmp / "units"), \
                 unittest.mock.patch("subprocess.run",
                                     side_effect=FileNotFoundError("systemctl")):
                units.install_units(cfg(vm_ip="10.0.0.1", tmp=tmp))
            self.assertTrue((tmp / "units" / "harbor-watchdog.timer").exists(),
                            "units should still be on disk for a later reload")

    def test_install_survives_a_failing_daemon_reload(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = pathlib.Path(d)
            err = subprocess.CalledProcessError(1, "systemctl")
            with unittest.mock.patch.object(units, "UNIT_DIR", tmp / "units"), \
                 unittest.mock.patch("subprocess.run", side_effect=err):
                units.install_units(cfg(vm_ip="10.0.0.1", tmp=tmp))


class InstallerOrdering(unittest.TestCase):
    """A fresh adopter has no config; install.sh must not try to render units
    from one."""

    def test_installer_points_a_fresh_adopter_at_init(self):
        inst = (pathlib.Path(__file__).resolve().parents[2] / "install.sh").read_text()
        self.assertIn("harbor init", inst,
                      "the no-config path must name the command that fixes it")

    def test_installer_does_not_reference_the_retired_engine(self):
        inst = (pathlib.Path(__file__).resolve().parents[2] / "install.sh").read_text()
        self.assertNotIn("llama-server", inst)


if __name__ == "__main__":
    unittest.main()
