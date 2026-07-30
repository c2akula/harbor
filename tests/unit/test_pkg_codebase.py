"""The codebase guard: check the query against the repo, not against a list.

The private set is open — the token that actually leaked from this project
(ROTATION-TAG-9) sat in a README and no marker list would have named it. These
tests use that shape deliberately.
"""
import pathlib
import subprocess
import tempfile
import unittest

from harbor import codebase


def make_repo() -> pathlib.Path:
    d = pathlib.Path(tempfile.mkdtemp(prefix="cb_"))
    (d / "src").mkdir()
    (d / "src" / "buffer.h").write_text(
        "class WidgetBuffer {\n"
        "  void flushWithBackoffLimit(int budget);\n"
        "};\n")
    # Documentation and config matter as much as source — the real leak was a
    # token in a README.
    (d / "README.md").write_text(
        "# Console\n\nMaintenance window token ROTATION-TAG-9 rotates monthly.\n")
    (d / "deploy.cfg").write_text("upstream_host = metrics-sink.internal\n")
    subprocess.run(["git", "init", "-q", str(d)], check=True)
    subprocess.run(["git", "-C", str(d), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(d), "-c", "user.email=t@t",
                    "-c", "user.name=t", "commit", "-qm", "init"], check=True)
    return d


class IndexCoversTheWholeRepo(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo = make_repo()
        cls.idx = codebase.build(cls.repo)

    def test_indexes_documentation_and_config_not_only_source(self):
        self.assertGreaterEqual(self.idx.files, 3)
        self.assertIn("rotation", self.idx.symbols)          # from README
        # The internal hostname, not the bare word "metrics" — that is
        # ordinary English and would block innocent questions.
        self.assertIn("metrics-sink.internal", self.idx.symbols)

    def test_catches_the_token_no_marker_list_would_contain(self):
        hit = codebase.check("what does ROTATION-TAG-9 gate?", self.idx)
        self.assertIsNotNone(hit, "a token from the README must be caught")

    def test_catches_a_distinctive_identifier(self):
        hit = codebase.check("why does WidgetBuffer deadlock?", self.idx)
        self.assertIsNotNone(hit)

    def test_catches_a_verbatim_lift(self):
        """A genuine paste is contiguous. (An interleaved paraphrase is caught
        by the identifier tier instead — which is the correct division: the
        literal tier is for lifted blocks, not for reworded ones.)"""
        hit = codebase.check(
            "what deadlocks here: class WidgetBuffer { void "
            "flushWithBackoffLimit(int budget); };", self.idx)
        self.assertEqual(hit[0], "verbatim")

    def test_generic_question_passes(self):
        """The guard must not make ordinary escalation impossible."""
        self.assertIsNone(codebase.check(
            "how should a lock-free multi-producer queue handle wraparound "
            "when consumers stall for long periods?", self.idx))

    def test_result_never_echoes_the_matched_text(self):
        """This gets printed; echoing matches would leak into a terminal log."""
        hit = codebase.check("what does ROTATION-TAG-9 gate?", self.idx)
        self.assertNotIn("ROTATION", str(hit))
        self.assertIsInstance(hit[1], int)


class GuardIntegration(unittest.TestCase):
    def test_blocks_and_explains_without_naming_the_content(self):
        from harbor import consult
        repo = make_repo()
        ok, reason = consult.guard_against_codebase(
            "why does WidgetBuffer deadlock?", str(repo))
        self.assertFalse(ok)
        self.assertNotIn("WidgetBuffer", reason)
        self.assertIn("ORACLE_UNSAFE", reason, "must say how to override")

    def test_silent_outside_a_repo(self):
        from harbor import consult
        with tempfile.TemporaryDirectory() as d:
            ok, _ = consult.guard_against_codebase("anything at all", d)
            self.assertTrue(ok, "no repo to compare against: markers still apply")


class ComposedPrefixDoesNotSelfMatch(unittest.TestCase):
    """The abstraction-level annotation lives in consult.py, so guarding the
    COMPOSED question made every consult match harbor's own source. Caught
    live, not by the offline tests, which passed raw text.

    Asserted on the call contract rather than by asking a sample question:
    any example written here becomes indexed, and would then match itself.
    """

    def test_the_raw_question_is_what_gets_checked(self):
        import unittest.mock
        from harbor import consult
        from harbor.config import Config
        cfg = Config(vm_name="", provider="hyperstack", ssh_user="u",
                     ssh_key=pathlib.Path("/x"), api="http://x",
                     key_file=pathlib.Path("/x"), rate_per_hr=1.0,
                     model_key_file=pathlib.Path("/x"), slot_context=1,
                     effort="max", flow_concurrency=0,
                     endpoint_url="http://x", oracle_markers="zzz", oracle_model="")
        asked = "how do consensus protocols handle network partitions"
        with unittest.mock.patch.object(consult, "guard_against_codebase",
                                        return_value=(False, "blocked")) as g:
            consult.run(cfg, asked, 2, False, None)
        sent = g.call_args[0][0]
        self.assertEqual(sent, asked, "must check the raw question")
        self.assertNotIn("abstraction level", sent,
                         "composing first makes harbor's own boilerplate match")


if __name__ == "__main__":
    unittest.main()
