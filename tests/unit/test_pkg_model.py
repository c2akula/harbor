"""Model switching — the rendered vLLM unit and its non-negotiable flags."""
import pathlib
import tempfile
import unittest

from harbor import model
from harbor.config import Config


def cfg_with(slot_context: int = 131072) -> Config:
    keyfile = pathlib.Path(tempfile.mkdtemp()) / "key"
    keyfile.write_text("sk-model-key\n")
    return Config(
        vm_name="box", provider="hyperstack", ssh_user="ubuntu", ssh_key=pathlib.Path("/x"),
        api="http://x", key_file=pathlib.Path("/x"), rate_per_hr=1.0,
        model_key_file=keyfile, slot_context=slot_context,
        effort="max", flow_concurrency=0,
        endpoint_url="http://127.0.0.1:8081", oracle_markers="x", oracle_model="",
    )


class AgentFlagsAreMandatory(unittest.TestCase):
    """Omitting these does not degrade gracefully — it breaks agents at request
    time with an error clients report as a generic failure."""

    def test_tool_calling_is_enabled_with_the_qwen_parser(self):
        unit = model.render_unit(cfg_with(), model.KNOWN["qwen35-awq"])
        self.assertIn("--enable-auto-tool-choice", unit)
        self.assertIn("--tool-call-parser qwen3_xml", unit)

    def test_reasoning_parser_is_set_so_thinking_does_not_leak(self):
        unit = model.render_unit(cfg_with(), model.KNOWN["qwen35-awq"])
        self.assertIn("--reasoning-parser qwen3", unit)

    def test_every_known_model_carries_them(self):
        for name, spec in model.KNOWN.items():
            unit = model.render_unit(cfg_with(), spec)
            self.assertIn("--enable-auto-tool-choice", unit, name)
            self.assertIn("--reasoning-parser", unit, name)


class RenderedUnit(unittest.TestCase):
    def test_serves_from_the_persistent_volume(self):
        """Weights on the instance's ephemeral disk are lost on every park."""
        unit = model.render_unit(cfg_with(), model.KNOWN["qwen35-awq"])
        self.assertIn(f"{model.VOLUME}/qwen-awq", unit)
        self.assertIn(f"{model.VOLUME}/venv/bin/vllm", unit)

    def test_waits_for_the_volume_mount(self):
        """Without this, systemd starts the server before /weights exists."""
        unit = model.render_unit(cfg_with(), model.KNOWN["qwen35-awq"])
        self.assertIn(f"RequiresMountsFor={model.VOLUME}", unit)

    def test_carries_the_api_key_and_concurrency(self):
        unit = model.render_unit(cfg_with(), model.KNOWN["qwen35-awq"])
        self.assertIn("--api-key sk-model-key", unit)
        self.assertIn("--max-num-seqs 16", unit)

    def test_prefix_caching_uses_the_hybrid_aware_mode(self):
        """This is a hybrid model; prefix caching does not reach its
        linear-attention layers without align mode."""
        unit = model.render_unit(cfg_with(), model.KNOWN["qwen35-awq"])
        self.assertIn("--enable-prefix-caching", unit)
        self.assertIn("--mamba-cache-mode align", unit)

    def test_venv_bin_is_on_path_not_just_the_entry_point(self):
        """vLLM shells out to `ninja`, which lives only in the venv; without
        this the service loads weights then crash-loops."""
        unit = model.render_unit(cfg_with(), model.KNOWN["qwen35-awq"])
        self.assertIn(f"Environment=PATH={model.VOLUME}/venv/bin:", unit)

    def test_awq_is_the_documented_default(self):
        self.assertIn("default", model.KNOWN["qwen35-awq"].note)

    def test_the_full_native_window_is_served(self):
        """The checkpoint's native window is 262144; max-model-len is a
        per-request cap on a shared pool, so serving less buys nothing."""
        for spec in model.KNOWN.values():
            self.assertEqual(spec.max_model_len, 262144)


class ChatTemplateShim(unittest.TestCase):
    """Crush sends the mode doctrine as a second system message; the
    checkpoint's template rejects any system message that is not first. The
    served template is therefore the checkpoint's own with a merging shim
    prepended — consecutive leading system messages become one."""

    def test_unit_serves_the_merged_template(self):
        unit = model.render_unit(cfg_with(), model.KNOWN["qwen35-awq"])
        self.assertIn(f"--chat-template {model.VOLUME}/chat-template.jinja", unit)

    def test_remote_script_builds_it_from_the_checkpoint_template(self):
        script = model.remote_script(model.KNOWN["qwen35-awq"])
        self.assertIn(f"{model.VOLUME}/qwen-awq/chat_template.jinja", script)
        self.assertIn("chat-template.jinja", script)

    def test_shim_only_touches_leading_system_runs(self):
        self.assertIn("loop.index0 == _lead.n", model.TEMPLATE_SHIM)
        self.assertIn("join('\\n\\n')", model.TEMPLATE_SHIM)

    def test_shim_handles_both_content_shapes(self):
        """The server hands the template plain strings OR typed-parts lists
        depending on model capabilities; doctrine must merge under both."""
        self.assertIn("m.content is string", model.TEMPLATE_SHIM)
        self.assertIn("map(attribute='text')", model.TEMPLATE_SHIM)

    def test_shim_in_bootstrap_matches_the_package(self):
        """Two copies of the shim exist (seed and switch); they must not
        drift — a box bootstrapped and a box switched must serve alike."""
        import pathlib
        boot = (pathlib.Path(__file__).resolve().parents[2]
                / "cloud" / "bootstrap.sh").read_text()
        for line in model.TEMPLATE_SHIM.strip().splitlines():
            # The heredoc keeps jinja escapes literal; compare unescaped.
            self.assertIn(line.replace("\\n", "\n").strip(),
                          boot.replace("\\n", "\n"),
                          f"bootstrap shim drifted at: {line.strip()[:50]}")

    def test_missing_checkpoint_template_fails_loudly(self):
        script = model.remote_script(model.KNOWN["qwen35-awq"])
        self.assertIn("test -f", script)

    def test_bootstrap_seed_serves_it_too(self):
        import pathlib
        boot = (pathlib.Path(__file__).resolve().parents[2]
                / "cloud" / "bootstrap.sh").read_text()
        self.assertIn("--chat-template", boot)
        self.assertIn("_lead", boot, "the merging shim must be in the seed")


class RemoteScript(unittest.TestCase):
    """What `harbor model` runs on the box, as opposed to what it writes."""

    def test_enables_the_unit_not_just_restarts_it(self):
        """Restart alone leaves a disabled unit disabled: the box serves now
        and comes back dead after the next park."""
        script = model.remote_script(model.KNOWN["qwen35-awq"])
        self.assertIn("systemctl enable", script)

    def test_refuses_when_the_checkpoint_is_not_on_the_volume(self):
        script = model.remote_script(model.KNOWN["qwen35-awq"])
        self.assertIn(f"test -d '{model.VOLUME}/qwen-awq'", script)

    def test_waits_for_health_before_reporting_success(self):
        script = model.remote_script(model.KNOWN["qwen35-awq"])
        self.assertIn("/health", script)


class UnknownModel(unittest.TestCase):
    def test_rejected_and_lists_what_is_known(self):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc = model.switch(cfg_with(), "not-a-model")
        self.assertEqual(rc, 2)
        self.assertIn("qwen35-awq", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
