"""Consult guard — the security control. Same-commit rule: any change to this
control ships with tests, because a scrubbed guard silently matches nothing
and the failure is invisible until a prompt leaks."""
import os
import unittest

from harbor import consult

# Synthetic markers mirroring the real shapes: word, org name, path prefix.
MARKERS = "seekrit|acmecorp|Playpen/|/home/testuser"


class GuardFailsClosed(unittest.TestCase):
    def setUp(self):
        os.environ.pop("ORACLE_UNSAFE", None)

    def test_no_markers_configured_refuses_everything(self):
        ok, reason = consult.guard("a perfectly innocent question", markers="")
        self.assertFalse(ok)
        self.assertIn("refusing", reason)

    def test_marker_in_question_is_refused_and_named(self):
        ok, reason = consult.guard("why does seekrit WidgetBuffer deadlock", MARKERS)
        self.assertFalse(ok)
        self.assertIn("seekrit", reason)

    def test_markers_match_case_insensitively(self):
        ok, _ = consult.guard("the SEEKRIT scheduler", MARKERS)
        self.assertFalse(ok)

    def test_path_marker_is_refused(self):
        ok, _ = consult.guard("file at /home/testuser/x.cpp", MARKERS)
        self.assertFalse(ok)

    def test_clean_question_passes(self):
        ok, reason = consult.guard(
            "how should a lock-free MPSC queue handle wraparound", MARKERS)
        self.assertTrue(ok, reason)

    def test_unsafe_override_bypasses(self):
        os.environ["ORACLE_UNSAFE"] = "1"
        try:
            ok, _ = consult.guard("mentions seekrit explicitly", MARKERS)
            self.assertTrue(ok)
        finally:
            del os.environ["ORACLE_UNSAFE"]


class QuestionComposition(unittest.TestCase):
    def test_level_annotation_prefixes_question(self):
        q = consult.compose_question("the question", 2)
        self.assertTrue(q.startswith("[abstraction level 2:"))
        self.assertTrue(q.endswith("the question"))

    def test_guard_sees_the_composed_question(self):
        """The guard must run on what is SENT, not what was typed."""
        composed = consult.compose_question("touches Playpen/foo", 3)
        ok, _ = consult.guard(composed, MARKERS)
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
