"""Installed-Crush version floor.

harbor's config depends on Crush features (PreToolUse hooks, type:llamacpp,
disable_default_providers) that a stale binary silently ignores — we ran 31
releases behind once and built a proxy for a capability that already existed.
This fails loudly instead."""
import re
import shutil
import subprocess
import unittest

MINIMUM = (0, 86, 0)


class CrushVersionFloor(unittest.TestCase):
    def test_installed_crush_supports_what_harbor_configures(self):
        if not shutil.which("crush"):
            self.skipTest("crush not installed on this machine")
        out = subprocess.run(["crush", "--version"], capture_output=True,
                             text=True, timeout=20).stdout
        m = re.search(r"(\d+)\.(\d+)\.(\d+)", out)
        self.assertIsNotNone(m, f"cannot parse crush version from: {out!r}")
        version = tuple(int(g) for g in m.groups())
        self.assertGreaterEqual(
            version, MINIMUM,
            f"crush {'.'.join(map(str, version))} predates features harbor "
            f"configures (hooks, llamacpp provider) — upgrade to "
            f">= {'.'.join(map(str, MINIMUM))}")


if __name__ == "__main__":
    unittest.main()
