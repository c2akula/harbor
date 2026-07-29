"""Config coherence and repo-hygiene contracts.

These encode failures we actually shipped: a stale context_window that silently
throttled Crush to a quarter of the real window, and private identifiers leaking
into files meant to be shared.
"""
import json
import pathlib
import re
import subprocess
import tempfile
import unittest

from typer.testing import CliRunner

from harbor import cli

REPO = pathlib.Path(__file__).resolve().parents[2]
LIVE_CRUSH = pathlib.Path.home() / ".config/crush/crush.json"


class RepoHygiene(unittest.TestCase):
    # The pattern itself must not be committed — that would publish exactly the
    # identifiers it exists to catch. Sourced from the local config's oracle
    # markers, which is where the canonical list already lives.
    @staticmethod
    def _private_pattern():
        """The identifiers to hunt for, from any source EXCEPT this repo — the
        pattern must never be committed, or it publishes what it protects.
        FAILS CLOSED: a control that skips when it cannot check lets private
        material sit in a public repo unnoticed."""
        import os
        from harbor import config as config_mod
        markers = os.environ.get("HARBOR_PRIVATE_MARKERS", "")
        if not markers:
            local = REPO / ".private-markers"          # gitignored
            if local.exists():
                markers = local.read_text().strip()
        if not markers and config_mod.DEFAULT_PATH.exists():
            markers = config_mod.load().oracle_markers
        if markers.strip().lower() == "none":
            return None                       # deliberate, explicit opt-out
        if not markers:
            raise AssertionError(
                "cannot verify the repo is free of private identifiers: no "
                "markers configured. Set HARBOR_PRIVATE_MARKERS to a regex of "
                "identifiers that must never be committed (or to 'none' to "
                "opt out deliberately), or create .private-markers "
                "(gitignored), or configure harbor. Refusing to pass an "
                "unperformed check.")
        return re.compile(markers, re.I)

    def _shareable(self):
        out = subprocess.run(["git", "ls-files"], cwd=REPO,
                             capture_output=True, text=True).stdout.split()
        for rel in out:
            if rel.startswith("deployments/"):
                continue
            if rel == "tests/unit/test_config.py":  # holds the pattern itself
                continue
            yield rel

    def test_tracked_files_carry_no_private_identifiers(self):
        """Scans EVERYTHING tracked — a subset-scan lets the unscanned subset
        publish exactly what the scan protects."""
        pattern = self._private_pattern()
        if pattern is None:
            self.skipTest("HARBOR_PRIVATE_MARKERS=none: scan opted out")
        out = subprocess.run(["git", "ls-files"], cwd=REPO,
                             capture_output=True, text=True).stdout.split()
        for rel in out:
            p = REPO / rel
            if not p.is_file() or rel == "tests/unit/test_config.py":
                continue
            hits = sorted(set(m.group(0) for m in pattern.finditer(
                p.read_text(errors="ignore"))))
            # Report the COUNT, never the matches. This runs in CI, and the
            # matched substrings are the very identifiers being protected —
            # printing them writes them into a build log in clear text.
            # (GitHub masks the exact secret value, not its alternatives.)
            self.assertEqual(
                len(hits), 0,
                f"{rel} contains {len(hits)} private identifier(s). "
                "Run the scan locally to see which.")

    def test_no_api_keys_in_repo(self):
        """A 32-hex string in a tracked file is almost certainly a key."""
        out = subprocess.run(["git", "ls-files"], cwd=REPO,
                             capture_output=True, text=True).stdout.split()
        keyish = re.compile(r"\b[0-9a-f]{32}\b")
        for rel in out:
            p = REPO / rel
            if not p.is_file() or p.suffix in (".png", ".jpg"):
                continue
            try:
                text = p.read_text(errors="ignore")
            except Exception:
                continue
            for m in keyish.finditer(text):
                self.fail(f"possible secret in {rel}: {m.group(0)[:8]}…")

    def test_hook_script_is_syntactically_valid(self):
        hook = REPO / "config" / "hooks" / "bash-policy.py"
        r = subprocess.run(["python3", "-m", "py_compile", str(hook)],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_installer_installs_the_package(self):
        inst = (REPO / "install.sh").read_text()
        self.assertIn("uv tool install", inst, "install.sh must install the package")
        self.assertIn("install-units", inst, "install.sh must render systemd units")


class CrushConfigCoherence(unittest.TestCase):
    """The live config must agree with how the server is actually run."""

    def setUp(self):
        if not LIVE_CRUSH.exists():
            self.skipTest("no live crush.json on this machine")
        self.cfg = json.loads(LIVE_CRUSH.read_text())

    def test_selected_models_exist_in_their_provider(self):
        for role, sel in self.cfg.get("models", {}).items():
            prov = self.cfg["providers"][sel["provider"]]
            ids = {m["id"] for m in prov.get("models", [])}
            self.assertIn(sel["model"], ids,
                          f"models.{role} points at an id the provider does not define")

    def test_context_window_matches_server_slot_size(self):
        """Crush must not be told a window larger than a llama.cpp slot: -c is
        divided by --parallel, and overflowing the slot fails at request time."""
        from harbor import config as config_mod
        if not config_mod.DEFAULT_PATH.exists():
            self.skipTest("no harbor config on this machine")
        slot = config_mod.load().slot_context
        for pname, prov in self.cfg.get("providers", {}).items():
            if "cloud" not in pname:
                continue
            for mdl in prov.get("models", []):
                self.assertLessEqual(
                    mdl.get("context_window", 0), slot,
                    f"{mdl['id']} claims a window larger than one server slot")


class ConsultGuardCli(unittest.TestCase):
    """The marker guard at CLI level: it must fail CLOSED (exit 3, nothing
    sent). A guard whose pattern is scrubbed or empty must refuse, not
    silently match nothing. Guard unit tests live in test_pkg_consult.py;
    this verifies the wiring end to end."""

    def _consult(self, toml_body: str, *args) -> int:
        import os
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write(toml_body)
        os.environ["HARBOR_CONF"] = f.name
        try:
            return CliRunner().invoke(cli.app, ["consult", *args]).exit_code
        finally:
            del os.environ["HARBOR_CONF"]
            pathlib.Path(f.name).unlink()

    NO_MARKERS = '[vm]\nid = 1\nip = "10.0.0.1"\n'
    WITH_MARKERS = NO_MARKERS + '[oracle]\nmarkers = "seekrit|acmecorp"\n'

    def test_refuses_when_markers_unset(self):
        rc = self._consult(self.NO_MARKERS, "anything at all")
        self.assertEqual(rc, 3, "with no markers configured the guard must refuse, not send")

    def test_refuses_configured_markers(self):
        rc = self._consult(self.WITH_MARKERS, "does acmecorp handle this correctly")
        self.assertEqual(rc, 3, "a configured marker must be refused")


if __name__ == "__main__":
    unittest.main()
