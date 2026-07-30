"""Crush config ownership — merge asserts owned keys, never clobbers user keys."""
import json
import pathlib
import tempfile
import unittest
import unittest.mock

from harbor import crush
from harbor.config import Config


def fake_cfg(key_file: pathlib.Path) -> Config:
    return Config(
        vm_name="", provider="hyperstack", ssh_user="u", ssh_key=pathlib.Path("/x"),
        api="http://x", key_file=pathlib.Path("/x"), rate_per_hr=1.0,
        model_key_file=key_file, slot_context=131072,
        effort="max", flow_concurrency=0, endpoint_url="http://127.0.0.1:8081", oracle_markers="x", oracle_model="",
    )


class ClientAndServerAgreeOnModelNames(unittest.TestCase):
    """harbor generates both sides of this contract — the crush config that
    requests a model by id, and the unit that tells vLLM which ids to answer
    to. vLLM 404s on unknown ids, so drift breaks every session."""

    def setUp(self):
        self.keyfile = pathlib.Path(tempfile.mkdtemp()) / "key"
        self.keyfile.write_text("sk-test\n")
        self.cfg = fake_cfg(self.keyfile)

    def test_every_requested_model_id_is_actually_served(self):
        import re

        from harbor import model

        requested = {m["id"]
                     for p in crush.owned_fragment(self.cfg)["providers"].values()
                     for m in p["models"]}
        for spec in model.KNOWN.values():
            unit = model.render_unit(self.cfg, spec)
            # Names run until the next flag, so stop at a leading "--".
            served = set(re.search(r"--served-model-name ((?:(?!--)\S+\s*)+)",
                                   unit).group(1).split())
            self.assertLessEqual(
                requested, served,
                f"crush asks for {sorted(requested - served)} but the unit "
                f"serves {sorted(served)} — every session 404s")

    def test_the_bootstrap_seed_unit_serves_them_too(self):
        """A fresh box is seeded by bootstrap.sh, not `harbor model`; both
        must carry the full name set."""
        import re

        boot = (pathlib.Path(__file__).resolve().parents[2]
                / "cloud" / "bootstrap.sh").read_text()
        requested = {m["id"]
                     for p in crush.owned_fragment(self.cfg)["providers"].values()
                     for m in p["models"]}
        served = set(re.search(r"--served-model-name ((?:(?!--)\S+\s*)+)",
                               boot).group(1).split())
        self.assertLessEqual(requested, served)


class ProviderNamesSayWhatTheyAre(unittest.TestCase):
    """Providers are named for their MODE, not the serving engine — engine
    names rot on every migration while the modes are the stable contract."""

    def setUp(self):
        self.keyfile = pathlib.Path(tempfile.mkdtemp()) / "key"
        self.keyfile.write_text("sk-test\n")
        self.cfg = fake_cfg(self.keyfile)

    def test_provider_keys_are_mode_named(self):
        self.assertEqual(set(crush.owned_fragment(self.cfg)["providers"]),
                         {"harbor-execute", "harbor-explore"})


class MergeContract(unittest.TestCase):
    def setUp(self):
        self.keyfile = pathlib.Path(tempfile.mkdtemp()) / "key"
        self.keyfile.write_text("sk-test\n")
        self.cfg = fake_cfg(self.keyfile)

    def test_user_keys_pass_through_untouched(self):
        """The merge-not-clobber requirement: mcp/lsp/model selection are the
        user's and must survive a sync byte-identical."""
        live = {
            "mcp": {"claude-mem": {"type": "stdio", "command": "/x/node"}},
            "lsp": {"clangd": {"command": "/x/clangd"}},
            "models": {"large": {"provider": "harbor-execute", "model": "qwen-coding"}},
            "options": {"context_paths": ["/a", "/b"]},
        }
        merged = crush.merge(live, self.cfg)
        self.assertEqual(merged["mcp"], live["mcp"])
        self.assertEqual(merged["lsp"], live["lsp"])
        self.assertEqual(merged["models"], live["models"])
        self.assertEqual(merged["options"]["context_paths"], ["/a", "/b"])

    def test_owned_providers_are_asserted(self):
        merged = crush.merge({}, self.cfg)
        execute = merged["providers"]["harbor-execute"]
        explore = merged["providers"]["harbor-explore"]
        self.assertEqual(execute["type"], "llamacpp")
        self.assertEqual(execute["api_key"], "sk-test")
        self.assertTrue(merged["options"]["disable_default_providers"])
        self.assertEqual([m["id"] for m in execute["models"]], ["qwen-coding"])
        self.assertEqual([m["id"] for m in explore["models"]], ["qwen-explore"])

    def test_mode_prefixes_are_wired_and_distinct(self):
        """The prefixes ride as an extra system message, which the serving
        template accepts only because the unit ships the leading-system-merge
        shim — see model.TEMPLATE_SHIM. Server and client change together."""
        merged = crush.merge({}, self.cfg)
        execute = merged["providers"]["harbor-execute"]["system_prompt_prefix"]
        explore = merged["providers"]["harbor-explore"]["system_prompt_prefix"]
        self.assertIn("smallest change", execute)
        self.assertIn("READ-ONLY", explore)
        self.assertIn(".crush/plan.md", explore)
        self.assertIn("harbor consult", explore, "consult carve-out must survive")
        self.assertNotEqual(execute, explore)

    def test_one_sampling_profile_everywhere(self):
        """D1 decision: temp 0.6/presence 0 is the only profile."""
        merged = crush.merge({}, self.cfg)
        for prov in merged["providers"].values():
            for m in prov["models"]:
                self.assertEqual(m["extra_body"]["temperature"], 0.6)
                self.assertEqual(m["extra_body"]["presence_penalty"], 0.0)

    def test_output_cap_follows_the_model_card(self):
        """The vendor-recommended output cap, from the one module that owns
        token numbers."""
        from harbor import effort
        merged = crush.merge({}, self.cfg)
        for prov in merged["providers"].values():
            for m in prov["models"]:
                self.assertEqual(m["default_max_tokens"], effort.MAX_TOKENS)

    def test_effort_label_resolves_to_the_budget(self):
        import dataclasses

        from harbor import effort
        cfg = dataclasses.replace(self.cfg, effort="high")
        merged = crush.merge({}, cfg)
        for prov in merged["providers"].values():
            for m in prov["models"]:
                self.assertEqual(m["extra_body"]["thinking_token_budget"],
                                 effort.budget("high"))

    def test_max_effort_sends_no_budget_field(self):
        merged = crush.merge({}, self.cfg)   # fixture: effort="max"
        for prov in merged["providers"].values():
            for m in prov["models"]:
                self.assertNotIn("thinking_token_budget", m["extra_body"])

    def test_context_window_follows_slot_context(self):
        """The stale-context_window bug class: client window must track config."""
        merged = crush.merge({}, self.cfg)
        for m in merged["providers"]["harbor-execute"]["models"]:
            self.assertEqual(m["context_window"], 131072)

    def test_foreign_hooks_survive_ours_asserted_once(self):
        live = {"hooks": {"PreToolUse": [
            {"name": "user hook", "matcher": "*", "command": "/theirs"},
            {"name": "bash policy", "matcher": "bash", "command": "/stale/path"},
        ]}}
        merged = crush.merge(live, self.cfg)
        names = [h["name"] for h in merged["hooks"]["PreToolUse"]]
        self.assertEqual(names, ["user hook", "bash policy", "plan gate"])
        by_name = {h["name"]: h for h in merged["hooks"]["PreToolUse"]}
        self.assertIn("bash-policy.py", by_name["bash policy"]["command"],
                      "stale command path must be re-asserted")
        self.assertIn("plan-gate.py", by_name["plan gate"]["command"])

    def test_merge_is_idempotent(self):
        once = crush.merge({}, self.cfg)
        twice = crush.merge(once, self.cfg)
        self.assertEqual(once, twice)
        self.assertEqual(crush.diff_lines(once, twice), [])

    def test_diff_redacts_key_values(self):
        drift = crush.diff_lines({}, crush.merge({}, self.cfg))
        joined = "\n".join(drift)
        self.assertNotIn("sk-test", joined, "diff output must never print a key")

    def test_check_reports_drift_and_sync_clears_it(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"mcp": {"keepme": {"a": 1}}}, f)
        p = pathlib.Path(f.name)
        try:
            self.assertTrue(crush.check(self.cfg, p), "fresh file must show drift")
            crush.sync(self.cfg, p, apply=True)
            self.assertEqual(crush.check(self.cfg, p), [], "post-sync must be clean")
            after = json.loads(p.read_text())
            self.assertEqual(after["mcp"], {"keepme": {"a": 1}}, "user key survived")
        finally:
            p.unlink()


class LiveDrift(unittest.TestCase):
    def test_live_crush_json_matches_harbor_owned_keys(self):
        """The drift alarm: fails when the live config and the package disagree,
        whichever side was edited. Skips where there is no live install."""
        from harbor import config as config_mod
        if not crush.LIVE_PATH.exists() or not config_mod.DEFAULT_PATH.exists():
            self.skipTest("no live crush/harbor config on this machine")
        cfg = config_mod.load()
        if not cfg.model_key_file.exists():
            self.skipTest("no model key on this machine")
        drift = crush.check(cfg)
        self.assertEqual(drift, [], "run 'harbor crush sync' (or update the "
                         "fragment in src/harbor/crush.py if the live edit was deliberate)")


if __name__ == "__main__":
    unittest.main()
