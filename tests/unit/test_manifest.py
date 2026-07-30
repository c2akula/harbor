"""What may exist in the shared repo — an ALLOWLIST, not a leak scan.

The private set is open: proprietary snippets, tokens, internal paths, things
nobody thought to name. a maintenance token sat in a public repo for nine days
precisely because no marker list would have contained it. You cannot blacklist
what you cannot enumerate.

What you CAN enumerate is what this repo is supposed to hold. Anything else
fails regardless of its content — which catches material that arrived by
accident (`git add -A`) or by a rule about where things live, without anyone
having to guess what is sensitive.
"""
import pathlib
import subprocess
import unittest

REPO = pathlib.Path(__file__).resolve().parents[2]

# Directories whose contents are the tool itself.
ALLOWED_TREES = (
    "src/", "tests/", "config/", "flows/", "cloud/", "systemd/",
    "docker/", ".github/", ".githooks/",
)

# Individual files that belong at the root or are deliberate examples.
ALLOWED_FILES = {
    "README.md", "LICENSE", "install.sh", "pyproject.toml", "uv.lock",
    ".gitignore",
    "deployments/README.md", "deployments/config.toml.example",
    # Documentation is a curated set, enumerated page by page — a blanket
    # docs/ grant would re-admit the path class of a real past leak.
    "docs/security.md", "docs/workflows.md", "docs/extending.md",
    "docs/setup-endpoint.md", "docs/setup-managed.md", "docs/operating.md",
    "docs/setup-team.md",
}


class SharedRepoManifest(unittest.TestCase):
    def test_only_expected_paths_are_tracked(self):
        tracked = subprocess.run(["git", "ls-files"], cwd=REPO,
                                 capture_output=True, text=True).stdout.split()
        unexpected = [f for f in tracked
                      if f not in ALLOWED_FILES
                      and not any(f.startswith(t) for t in ALLOWED_TREES)]
        self.assertEqual(
            unexpected, [],
            "paths not on the shared-repo allowlist. If they belong here, add "
            "them to ALLOWED_FILES/ALLOWED_TREES deliberately; if they are "
            "operational or private material, they belong in the private "
            "record instead:\n  " + "\n  ".join(unexpected))

    def test_allowlist_would_have_caught_the_real_incidents(self):
        """Regression against real incident path CLASSES: a stray memory
        file, an experiment answer key, a personal deployment config, an
        operational roadmap."""
        for path in ("docs/claude-code-memory-notes.md",
                     "gauntlet/2026-01-01-answer-key.md",  # hygiene-ok: fixture path
                     "deployments/alice/config.toml",
                     "TODO.md"):
            allowed = (path in ALLOWED_FILES
                       or any(path.startswith(t) for t in ALLOWED_TREES))
            self.assertFalse(allowed, f"{path} would have slipped through")


if __name__ == "__main__":
    unittest.main()
