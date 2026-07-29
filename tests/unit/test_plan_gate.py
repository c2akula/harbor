"""Plan gate — no edits without a fresh plan artifact; self-correcting loop."""
import importlib.machinery
import importlib.util
import json
import os
import pathlib
import subprocess
import tempfile
import time
import unittest

REPO = pathlib.Path(__file__).resolve().parents[2]
HOOK = REPO / "config" / "hooks" / "plan-gate.py"

spec = importlib.util.spec_from_loader("plan_gate",
    importlib.machinery.SourceFileLoader("plan_gate", str(HOOK)))
G = importlib.util.module_from_spec(spec)
spec.loader.exec_module(G)


def project(plan: str | None = None, plan_age_s: int = 0) -> pathlib.Path:
    d = pathlib.Path(tempfile.mkdtemp(prefix="gate_"))
    (d / ".crush").mkdir()
    if plan is not None:
        p = d / ".crush" / "plan.md"
        p.write_text(plan)
        if plan_age_s:
            past = time.time() - plan_age_s
            os.utime(p, (past, past))
    return d


class GateContract(unittest.TestCase):
    def setUp(self):
        os.environ.pop("HARBOR_PLAN_GATE", None)

    def test_edit_without_plan_is_denied_with_teaching_reason(self):
        d = project()
        reason = G.gate("edit", str(d / "src/x.py"), str(d))
        self.assertIsNotNone(reason)
        self.assertIn("QUICK-FIX", reason, "the hatch must be taught, not hidden")
        self.assertIn(str(d / ".crush" / "plan.md"), reason)

    def test_all_edit_class_tools_are_gated(self):
        d = project()
        for tool in ("edit", "multiedit", "write", "download"):
            self.assertIsNotNone(G.gate(tool, str(d / "f"), str(d)), tool)

    def test_fresh_plan_opens_the_gate(self):
        d = project(plan="# plan\nproblem, acceptance, approach\n")
        self.assertIsNone(G.gate("edit", str(d / "src/x.py"), str(d)))

    def test_quick_fix_line_is_a_valid_plan(self):
        d = project(plan="QUICK-FIX: typo in comment\n")
        self.assertIsNone(G.gate("edit", str(d / "src/x.py"), str(d)))

    def test_stale_plan_closes_the_gate(self):
        d = project(plan="# old plan\n", plan_age_s=7 * 3600)
        self.assertIsNotNone(G.gate("edit", str(d / "src/x.py"), str(d)))

    def test_writing_the_plan_itself_is_always_allowed(self):
        """The self-correcting loop: satisfying the gate must not be gated."""
        d = project()
        self.assertIsNone(G.gate("write", str(d / ".crush" / "plan.md"), str(d)))

    def test_gate_applies_from_subdirectory_cwd(self):
        d = project()
        sub = d / "src" / "deep"
        sub.mkdir(parents=True)
        self.assertIsNotNone(G.gate("edit", str(d / "src/x.py"), str(sub)))

    def test_kill_switch(self):
        d = project()
        os.environ["HARBOR_PLAN_GATE"] = "off"
        try:
            self.assertIsNone(G.gate("edit", str(d / "src/x.py"), str(d)))
        finally:
            del os.environ["HARBOR_PLAN_GATE"]

    def test_non_crush_directory_gets_no_opinion(self):
        import unittest.mock
        with tempfile.TemporaryDirectory() as d, \
             unittest.mock.patch.object(G, "find_project_root", return_value=None):
            self.assertIsNone(G.gate("edit", f"{d}/x.py", d))

    def test_ungated_tools_pass(self):
        d = project()
        self.assertIsNone(G.gate("view", str(d / "x.py"), str(d)))


class ExploreModeEnforcement(unittest.TestCase):
    """The Explore prefix promises edits will fail; the gate keeps it true."""

    SESSION = "sess-x"

    def with_db(self, provider: str) -> pathlib.Path:
        import sqlite3
        d = project(plan="# fresh plan\n")     # fresh plan: explore still denies
        con = sqlite3.connect(d / ".crush" / "crush.db")
        con.execute("CREATE TABLE messages (id TEXT, session_id TEXT, role TEXT,"
                    " parts TEXT, provider TEXT, created_at INTEGER)")
        con.execute("INSERT INTO messages VALUES ('a1', ?, 'assistant', '[]', ?, 1)",
                    (self.SESSION, provider))
        con.commit(); con.close()
        return d

    def test_explore_mode_denies_source_edit_despite_fresh_plan(self):
        d = self.with_db("harbor-explore")
        reason = G.gate("write", str(d / "x.py"), str(d), self.SESSION)
        self.assertIsNotNone(reason)
        self.assertIn("read-only", reason)

    def test_explore_mode_still_allows_the_plan_file(self):
        d = self.with_db("harbor-explore")
        self.assertIsNone(
            G.gate("write", str(d / ".crush" / "plan.md"), str(d), self.SESSION))

    def test_execute_mode_is_unaffected(self):
        d = self.with_db("harbor-execute")
        self.assertIsNone(G.gate("edit", str(d / "x.py"), str(d), self.SESSION))

    def test_unknown_provider_fails_toward_execute(self):
        """No db / no session row → permissive mode, plan rule still applies."""
        d = project(plan="# fresh plan\n")     # .crush exists, no crush.db
        self.assertIsNone(G.gate("edit", str(d / "x.py"), str(d), "nosuch"))


class HookContract(unittest.TestCase):
    def _run(self, payload):
        r = subprocess.run(["python3", str(HOOK)], input=json.dumps(payload),
                           capture_output=True, text=True, timeout=20)
        return r.returncode, json.loads(r.stdout or "{}")

    def test_deny_carries_reason_via_stdin_contract(self):
        d = project()
        rc, out = self._run({"tool_name": "edit", "cwd": str(d),
                             "tool_input": {"file_path": str(d / "x.py")}})
        self.assertEqual(rc, 0)
        self.assertEqual(out.get("decision"), "deny")
        self.assertTrue(out.get("reason"))

    def test_malformed_input_is_silent(self):
        r = subprocess.run(["python3", str(HOOK)], input="not json",
                           capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(json.loads(r.stdout or "{}"), {})


if __name__ == "__main__":
    unittest.main()
