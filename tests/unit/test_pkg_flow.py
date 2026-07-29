"""Flow primitives — orchestration behavior with the transport stubbed."""
import json
import os
import pathlib
import tempfile
import time
import unittest

from harbor import flow

SCHEMA = {"type": "object", "required": ["verdict", "score"],
          "properties": {"verdict": {"type": "string"},
                         "score": {"type": "integer"}}}


def stub_generate(script: str):
    """Point generate() at a shell command; scripts see messages on stdin."""
    os.environ[flow.GENERATE_CMD_ENV] = script


class GenerateContract(unittest.TestCase):
    def tearDown(self):
        os.environ.pop(flow.GENERATE_CMD_ENV, None)

    def test_plain_generate_returns_text(self):
        stub_generate("echo hello world")
        self.assertEqual(flow.generate("hi").strip(), "hello world")

    def test_schema_valid_first_try(self):
        stub_generate("""echo '{"verdict": "ok", "score": 3}'""")
        obj = flow.generate("judge", schema=SCHEMA)
        self.assertEqual(obj, {"verdict": "ok", "score": 3})

    def test_fenced_json_is_extracted(self):
        stub_generate("printf 'text\\n```json\\n{\"verdict\": \"ok\", \"score\": 1}\\n```\\n'")
        obj = flow.generate("judge", schema=SCHEMA)
        self.assertEqual(obj["score"], 1)

    def test_one_corrective_retry_then_success(self):
        """First reply invalid (missing key) → retry prompt → valid reply."""
        with tempfile.TemporaryDirectory() as d:
            flag = pathlib.Path(d) / "second"
            script = (f"if [ -f {flag} ]; then "
                      f"""echo '{{"verdict": "ok", "score": 2}}'; """
                      f"else touch {flag}; echo '{{\"verdict\": \"ok\"}}'; fi")
            stub_generate(script)
            obj = flow.generate("judge", schema=SCHEMA)
            self.assertEqual(obj["score"], 2)
            self.assertTrue(flag.exists(), "retry path must have been taken")

    def test_persistent_schema_violation_raises(self):
        stub_generate("""echo '{"wrong": true}'""")
        with self.assertRaises(flow.FlowError):
            flow.generate("judge", schema=SCHEMA)

    def test_a_boolean_does_not_satisfy_integer(self):
        stub_generate("""echo '{"verdict": "ok", "score": true}'""")
        with self.assertRaises(flow.FlowError):
            flow.generate("judge", schema=SCHEMA)

    def test_type_mismatch_is_a_violation(self):
        stub_generate("""echo '{"verdict": "ok", "score": "three"}'""")
        with self.assertRaises(flow.FlowError):
            flow.generate("judge", schema=SCHEMA)


class ReasoningBudget(unittest.TestCase):
    """generate() caps thinking via the server's thinking_token_budget, leaving
    room for the answer inside max_tokens."""

    def _body_sent(self, budget, max_tokens):
        import unittest.mock
        captured = {}

        class Resp:
            status_code = 200

            def raise_for_status(self): pass

            def json(self):
                return {"choices": [{"message": {"content": "ok"}}]}

        def post(url, json=None, **kw):
            captured.update(json)
            return Resp()

        with unittest.mock.patch("harbor.flow.requests.post", side_effect=post), \
             unittest.mock.patch("harbor.flow._endpoint",
                                 return_value=("http://x/v1/chat/completions", "k")), \
             unittest.mock.patch("harbor.flow._reasoning_budget", return_value=budget):
            flow.generate("q", max_tokens=max_tokens)
        return captured

    def test_budget_shrinks_to_leave_answer_room(self):
        from harbor import effort
        body = self._body_sent(24576, 16384)
        self.assertEqual(body["thinking_token_budget"],
                         16384 - effort.ANSWER_RESERVE)

    def test_tiny_calls_disable_thinking_rather_than_starve_the_answer(self):
        from harbor import effort
        body = self._body_sent(24576, effort.ANSWER_RESERVE // 2)
        self.assertEqual(body["thinking_token_budget"], 0)

    def test_full_budget_when_max_tokens_allows(self):
        body = self._body_sent(24576, 32768)
        self.assertEqual(body["thinking_token_budget"], 24576)

    def test_uncapped_sends_no_field(self):
        body = self._body_sent(None, 4096)
        self.assertNotIn("thinking_token_budget", body)


class ReasoningOnlyReply(unittest.TestCase):
    """A thinking model that spends its whole budget reasoning returns null
    `content` with the text stranded in reasoning_content; the error must
    name the budget, not surface as a downstream type error."""

    def test_null_content_raises_a_flow_error_naming_the_cause(self):
        import unittest.mock

        class Resp:
            status_code = 200

            def raise_for_status(self): pass

            def json(self):
                return {"choices": [{"message": {
                    "content": None,
                    "reasoning_content": "thinking but never concluding"}}]}

        with unittest.mock.patch("harbor.flow.requests.post", return_value=Resp()), \
             unittest.mock.patch("harbor.flow._endpoint",
                                 return_value=("http://x/v1/chat/completions", "k")):
            with self.assertRaises(flow.FlowError) as ctx:
                flow.generate("think about it", max_tokens=8)
        self.assertIn("max_tokens", str(ctx.exception),
                      "the message must point at the budget, not at a regex")


class ParallelContract(unittest.TestCase):
    def test_results_in_submission_order(self):
        thunks = [lambda i=i: (time.sleep(0.05 * (3 - i)), i)[1] for i in range(3)]
        self.assertEqual(flow.parallel(thunks), [0, 1, 2])

    def test_a_failed_branch_fails_the_fan_out_naming_it(self):
        """Silent None slots let a downstream stage run on missing inputs and
        still claim success; the default is now loud."""
        def boom():
            raise RuntimeError("kaput")
        with self.assertRaises(flow.FlowError) as ctx:
            flow.parallel([lambda: "a", boom, lambda: "c"])
        self.assertIn("branch 2", str(ctx.exception))
        self.assertIn("kaput", str(ctx.exception))

    def test_all_branches_still_run_before_the_failure_is_raised(self):
        """Aggregate-then-raise: one early failure must not strand the other
        branches half-done."""
        ran = []

        def boom():
            raise RuntimeError("x")
        try:
            flow.parallel([lambda: ran.append(1), boom, lambda: ran.append(2)])
        except flow.FlowError:
            pass
        self.assertEqual(sorted(ran), [1, 2])

    def test_opt_in_tolerance_yields_none_slots(self):
        """Lens-diverse panels accept partial results; they say so."""
        def boom():
            raise RuntimeError("x")
        out = flow.parallel([lambda: "a", boom, lambda: "c"],
                            tolerate_failures=True)
        self.assertEqual(out, ["a", None, "c"])

    def test_worker_cap_is_respected(self):
        import threading
        live, peak, lock = 0, [0], threading.Lock()

        def probe():
            nonlocal live
            with lock:
                live += 1
                peak[0] = max(peak[0], live)
            time.sleep(0.05)
            with lock:
                live -= 1

        flow.parallel([probe] * 6, max_workers=2)
        self.assertLessEqual(peak[0], 2)


class AgentContract(unittest.TestCase):
    def tearDown(self):
        os.environ.pop(flow.AGENT_CMD_ENV, None)

    def test_agent_uses_injected_command_and_returns_stdout(self):
        os.environ[flow.AGENT_CMD_ENV] = "tr a-z A-Z"
        self.assertEqual(flow.agent("review this").strip(), "REVIEW THIS")

    def test_agent_failure_raises_with_stderr(self):
        os.environ[flow.AGENT_CMD_ENV] = "echo nope >&2; exit 3"
        with self.assertRaises(flow.FlowError) as ctx:
            flow.agent("x")
        self.assertIn("nope", str(ctx.exception))


class ScriptRunner(unittest.TestCase):
    def test_script_gets_primitives_prebound(self):
        os.environ[flow.GENERATE_CMD_ENV] = "echo out"
        try:
            with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
                f.write("import pathlib\n"
                        "r = parallel([lambda: generate('a'), lambda: generate('b')])\n"
                        f"pathlib.Path({str(f.name + '.out')!r}).write_text(str(len(r)))\n")
            flow.run_script(f.name)
            self.assertEqual(pathlib.Path(f.name + ".out").read_text(), "2")
        finally:
            os.environ.pop(flow.GENERATE_CMD_ENV, None)
            pathlib.Path(f.name).unlink(missing_ok=True)
            pathlib.Path(f.name + ".out").unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
