"""Thrash detection — outcome-aware, fail-open.

The contract: deny only when the SAME command produced the SAME result twice
already (a third run is pointless); changing results mean a poll loop and are
left alone; every failure path is silent — a broken supervisor must never
break the session.
"""
import importlib.machinery
import importlib.util
import json
import os
import pathlib
import sqlite3
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[2]
HOOK = REPO / "config" / "hooks" / "bash-policy.py"

spec = importlib.util.spec_from_loader("bash_policy_thrash",
    importlib.machinery.SourceFileLoader("bash_policy_thrash", str(HOOK)))
P = importlib.util.module_from_spec(spec)
spec.loader.exec_module(P)

SESSION = "sess-1"


def make_project(history, provider="harbor-execute"):
    """Project dir with a .crush/crush.db holding [(command, result)] history,
    oldest first — mirrors Crush's schema for the columns the hook reads."""
    d = pathlib.Path(tempfile.mkdtemp(prefix="thrash_"))
    (d / ".crush").mkdir()
    con = sqlite3.connect(d / ".crush" / "crush.db")
    con.execute("CREATE TABLE messages (id TEXT, session_id TEXT, role TEXT, "
                "parts TEXT, provider TEXT, created_at INTEGER)")
    for i, (cmd, result) in enumerate(history):
        call_id = f"call_{i}"
        con.execute("INSERT INTO messages VALUES (?, ?, 'assistant', ?, ?, ?)",
                    (f"a{i}", SESSION, json.dumps([{"type": "tool_call",
                     "data": {"id": call_id, "name": "bash",
                              "input": json.dumps({"command": cmd})}}]),
                     provider, i * 10))
        con.execute("INSERT INTO messages VALUES (?, ?, 'tool', ?, ?, ?)",
                    (f"t{i}", SESSION, json.dumps([{"type": "tool_result",
                     "data": {"tool_call_id": call_id, "name": "bash",
                              "content": result}}]), provider, i * 10 + 1))
    con.commit()
    con.close()
    return d


class ThrashContract(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="hookstate_")
        os.environ["HARBOR_HOOK_STATE"] = self.state

    def tearDown(self):
        del os.environ["HARBOR_HOOK_STATE"]

    def verdict(self, cmd, project):
        return P.thrash_verdict(cmd, SESSION, str(project))

    def test_third_identical_call_with_identical_results_is_denied(self):
        d = make_project([("make test", "FAIL x"), ("make test", "FAIL x")])
        self.assertIsNotNone(self.verdict("make test", d))

    def test_changing_results_is_a_poll_loop_and_left_alone(self):
        d = make_project([("harbor status", "vm STARTING"),
                          ("harbor status", "vm ACTIVE")])
        self.assertIsNone(self.verdict("harbor status", d))

    def test_different_command_resets_the_streak(self):
        d = make_project([("make test", "FAIL x"), ("ls", "files"),
                          ("make test", "FAIL x")])
        self.assertIsNone(self.verdict("make test", d),
                          "streak must be consecutive")

    def test_one_nudge_per_streak_then_normal_prompt(self):
        d = make_project([("make test", "FAIL x"), ("make test", "FAIL x")])
        self.assertIsNotNone(self.verdict("make test", d), "first: nudge")
        self.assertIsNone(self.verdict("make test", d),
                          "second: silent — the model must not be wedged")

    def test_short_history_is_silent(self):
        d = make_project([("make test", "FAIL x")])
        self.assertIsNone(self.verdict("make test", d))

    # --- fail-open -----------------------------------------------------------
    def test_no_db_is_silent(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(self.verdict("make test", d))

    def test_corrupt_db_is_silent(self):
        d = pathlib.Path(tempfile.mkdtemp())
        (d / ".crush").mkdir()
        (d / ".crush" / "crush.db").write_text("not a database")
        self.assertIsNone(self.verdict("make test", d))

    def test_missing_session_is_silent(self):
        d = make_project([("make test", "FAIL x"), ("make test", "FAIL x")])
        self.assertIsNone(P.thrash_verdict("make test", "", str(d)))

    def test_db_discovered_from_subdirectory_cwd(self):
        d = make_project([("make test", "FAIL x"), ("make test", "FAIL x")])
        sub = d / "src" / "deep"
        sub.mkdir(parents=True)
        self.assertIsNotNone(self.verdict("make test", sub))

    def test_thrash_never_allows_what_policy_denies(self):
        """Safety asymmetry: a thrash-free destructive command still denies."""
        self.assertEqual(P.classify("rm -rf /tmp/x")[0], "deny")


class ExploreBashCoupling(unittest.TestCase):
    """In explore mode, unproven bash is denied — closing the prompt loophole
    the edit-tool gate alone leaves open."""

    def verdict(self, cmd, project):
        return P.explore_verdict(cmd, P.classify(cmd)[0], SESSION, str(project))

    def test_write_pattern_command_is_denied_in_explore(self):
        d = make_project([("ls", "files")], provider="harbor-explore")
        self.assertIsNotNone(self.verdict("echo x > notes.txt", d))

    def test_read_only_command_stays_allowed_in_explore(self):
        d = make_project([("ls", "files")], provider="harbor-explore")
        self.assertIsNone(self.verdict("git status", d),
                          "allow-class must not be touched by explore")

    def test_harbor_commands_keep_the_prompt(self):
        """consult's permission prompt IS the leak-review gate — never deny it."""
        d = make_project([("ls", "files")], provider="harbor-explore")
        self.assertIsNone(self.verdict("harbor consult 'a hard question'", d))

    def test_execute_mode_is_unaffected(self):
        d = make_project([("ls", "files")], provider="harbor-execute")
        self.assertIsNone(self.verdict("echo x > notes.txt", d))

    def test_no_db_fails_open(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(
                P.explore_verdict("echo x > f", "silent", SESSION, d))


class MarkerPruning(unittest.TestCase):
    def test_old_markers_are_pruned_fresh_kept(self):
        import os as _os
        import time as _time
        with tempfile.TemporaryDirectory() as root:
            _os.environ["HARBOR_HOOK_STATE"] = root
            try:
                old = pathlib.Path(root) / "nudged-s1-aaaa"
                fresh = pathlib.Path(root) / "nudged-s2-bbbb"
                old.touch(); fresh.touch()
                past = _time.time() - 8 * 86400
                _os.utime(old, (past, past))
                P._nudged_marker("s3", "cccc")      # any access prunes
                self.assertFalse(old.exists(), "week-old marker must be pruned")
                self.assertTrue(fresh.exists(), "fresh marker must survive")
            finally:
                del _os.environ["HARBOR_HOOK_STATE"]


if __name__ == "__main__":
    unittest.main()
