"""Flow upgrades: journal-replay resume, slot pool, JSON repair, worktree."""
import json
import os
import pathlib
import subprocess
import tempfile
import threading
import time
import unittest
import unittest.mock

from harbor import flow

SCHEMA = {"type": "object", "required": ["verdict"],
          "properties": {"verdict": {"type": "string"}}}


def reset_flow_state(state_dir: str):
    flow._journal.clear()
    flow._journal_seen.clear()
    flow._journal_path = None
    flow._pool = None
    os.environ["LLM_WD_STATE_DIR"] = state_dir      # unused, hygiene
    os.environ[flow.SLOTS_ENV] = "2"


class JsonRepair(unittest.TestCase):
    def test_truncated_object_is_repaired(self):
        self.assertEqual(flow._extract_json('{"verdict": "ok", "items": [1, 2'),
                         {"verdict": "ok", "items": [1, 2]})

    def test_unclosed_string_is_repaired(self):
        self.assertEqual(flow._extract_json('{"verdict": "ok'),
                         {"verdict": "ok"})

    def test_escaped_quotes_do_not_confuse_the_tracker(self):
        obj = flow._extract_json('{"verdict": "say \\"hi\\"", "n": [3')
        self.assertEqual(obj["verdict"], 'say "hi"')
        self.assertEqual(obj["n"], [3])

    def test_hopeless_text_still_raises(self):
        with self.assertRaises(flow.FlowError):
            flow._extract_json("no json here at all")


class SlotPool(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        reset_flow_state(self.d)

    def tearDown(self):
        os.environ.pop(flow.SLOTS_ENV, None)

    def test_leaves_never_exceed_slot_count_even_with_wide_parallel(self):
        live, peak, lock = 0, [0], threading.Lock()

        def probe(messages, max_tokens):
            nonlocal live
            with lock:
                live += 1
                peak[0] = max(peak[0], live)
            time.sleep(0.05)
            with lock:
                live -= 1
            return "out"

        with unittest.mock.patch.object(flow, "_complete", side_effect=probe):
            # _complete is mocked, so gate at the journal layer's runner via
            # generate -> _generate_live -> _complete... the pool lives IN
            # _complete normally; emulate by wrapping probe with the pool.
            def gated(messages, max_tokens):
                with flow._slots():
                    return probe(messages, max_tokens)
            with unittest.mock.patch.object(flow, "_complete", side_effect=gated):
                flow.parallel([lambda i=i: flow.generate(f"p{i}") for i in range(8)])
        self.assertLessEqual(peak[0], 2, "slot pool must cap real concurrency")

    def test_config_concurrency_beats_llamacpp_autodetect(self):
        """vLLM has no /props; the operator's number must win (and be used)."""
        import unittest.mock
        from harbor.config import Config
        os.environ.pop(flow.SLOTS_ENV, None)
        cfg = Config(vm_name="", provider="hyperstack", ssh_user="u", ssh_key=pathlib.Path("/x"),
                     api="x", key_file=pathlib.Path("/x"), rate_per_hr=1.0,
                     model_key_file=pathlib.Path("/x"),
                     slot_context=1, effort="max", flow_concurrency=16, endpoint_url="http://127.0.0.1:8081",
                     oracle_markers="x", oracle_model="")
        with unittest.mock.patch.object(flow.config_mod, "load", return_value=cfg), \
             unittest.mock.patch.object(flow.requests, "get") as get:
            self.assertEqual(flow._slot_count(), 16)
            get.assert_not_called()   # must not even probe when told explicitly
        os.environ[flow.SLOTS_ENV] = "2"

    def test_unreachable_server_falls_back_timidly(self):
        import unittest.mock
        from harbor.config import Config
        os.environ.pop(flow.SLOTS_ENV, None)
        cfg = Config(vm_name="", provider="hyperstack", ssh_user="u", ssh_key=pathlib.Path("/x"),
                     api="x", key_file=pathlib.Path("/x"), rate_per_hr=1.0,
                     model_key_file=pathlib.Path("/x"),
                     slot_context=1, effort="max", flow_concurrency=0, endpoint_url="http://127.0.0.1:8081",
                     oracle_markers="x", oracle_model="")
        with unittest.mock.patch.object(flow.config_mod, "load", return_value=cfg), \
             unittest.mock.patch.object(flow.requests, "get",
                                        side_effect=RuntimeError("no server")):
            self.assertEqual(flow._slot_count(), flow.DEFAULT_WORKERS)
        os.environ[flow.SLOTS_ENV] = "2"

    def test_env_override_sizes_the_pool(self):
        os.environ[flow.SLOTS_ENV] = "3"
        flow._pool = None
        self.assertEqual(flow._slot_count(), 3)


class JournalReplay(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        reset_flow_state(self.d)
        self.calls = []

    def tearDown(self):
        os.environ.pop(flow.SLOTS_ENV, None)

    def _live(self, prompt, system, schema, max_tokens):
        self.calls.append(prompt)
        return f"answer:{prompt}"

    def run_flow(self, run_id, prompts):
        with unittest.mock.patch.object(flow, "_generate_live", side_effect=self._live), \
             unittest.mock.patch.object(flow, "RUNS_DIR", pathlib.Path(self.d)):
            flow._journal.clear(); flow._journal_seen.clear()
            flow._load_journal(run_id)
            return [flow.generate(p) for p in prompts]

    def test_resume_replays_identical_calls_without_model_time(self):
        first = self.run_flow("run1", ["a", "b"])
        self.assertEqual(len(self.calls), 2)
        second = self.run_flow("run1", ["a", "b"])
        self.assertEqual(len(self.calls), 2, "resume must not re-pay model time")
        self.assertEqual(first, second)

    def test_changed_call_runs_live_others_replay(self):
        self.run_flow("run2", ["a", "b"])
        self.calls.clear()
        self.run_flow("run2", ["a", "CHANGED"])
        self.assertEqual(self.calls, ["CHANGED"], "only the changed call re-runs")

    def test_best_of_n_occurrences_journal_separately(self):
        """Same prompt N times = N distinct journal entries, not one."""
        with unittest.mock.patch.object(flow, "_generate_live",
                                        side_effect=["r1", "r2"]), \
             unittest.mock.patch.object(flow, "RUNS_DIR", pathlib.Path(self.d)):
            flow._journal.clear(); flow._journal_seen.clear()
            flow._load_journal("run3")
            out = [flow.generate("same"), flow.generate("same")]
        self.assertEqual(out, ["r1", "r2"])
        replayed = self.run_flow("run3", ["same", "same"])
        self.assertEqual(replayed, ["r1", "r2"], "occurrence order must replay")

    def test_torn_journal_tail_is_tolerated(self):
        p = pathlib.Path(self.d) / "run4.jsonl"
        p.write_text(json.dumps({"k": flow._call_key("generate",
            json.dumps({"p": "a", "s": "", "j": None, "m": 32768})),
            "n": 0, "r": "cached"}) + "\n" + '{"k": "torn...')
        got = self.run_flow("run4", ["a"])
        self.assertEqual(got, ["cached"])
        self.assertEqual(self.calls, [], "intact entries must still replay")


class Worktree(unittest.TestCase):
    def test_worktree_isolates_and_cleans_up(self):
        repo = tempfile.mkdtemp()
        subprocess.run(["git", "init", "-q", repo], check=True)
        subprocess.run(["git", "-C", repo, "-c", "user.email=t@t",
                        "-c", "user.name=t", "commit", "-q",
                        "--allow-empty", "-m", "init"], check=True)
        with flow.worktree(repo) as wt:
            self.assertTrue(pathlib.Path(wt).is_dir())
            (pathlib.Path(wt) / "scratch.txt").write_text("x")
            self.assertFalse((pathlib.Path(repo) / "scratch.txt").exists(),
                             "worktree writes must not appear in the main tree")
        self.assertFalse(pathlib.Path(wt).exists(), "worktree removed on exit")

    def test_non_repo_raises_flow_error(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(flow.FlowError):
                with flow.worktree(d):
                    pass


if __name__ == "__main__":
    unittest.main()
