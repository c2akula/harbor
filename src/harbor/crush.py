"""Crush config ownership: harbor asserts the keys it manages in the live
~/.config/crush/crush.json and never touches anything else (MCP servers, LSP,
model selection, context paths are the user's).

`sync` deep-merges the owned fragment in (diff shown first); `check` is the
same comparison read-only, run in the unit tier so drift fails loudly — the
template rotted within a day of being hand-maintained; this can't."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from . import effort
from .config import Config

LIVE_PATH = Path.home() / ".config" / "crush" / "crush.json"
HOOKS_DIR = Path.home() / ".config" / "crush" / "hooks"
SKILLS_PATH = "~/.config/crush/skills"


# Mode prefixes ride every request as system-prompt prefill — the one channel
# the model treats as trusted. Phrasing patterns (fenced banner, enumerated
# prohibitions, capability-denial, priority override, bookend) are lifted from
# Claude Code's plan-mode/Explore prompts, which are tested on weak followers.
EXPLORE_PREFIX = """\
=== EXPLORE MODE — READ-ONLY. NO FILE MODIFICATIONS ===
You may read anything; you are strictly prohibited from creating, modifying, \
deleting, or moving files, and from commands that change state (`>`, `touch`, \
`rm`, `mv`, installs, commits). Attempting an edit will be blocked by policy — \
it will fail. Read-only commands and `harbor consult` (the frontier escalation \
for hard or ambiguous questions) remain available — consulting changes nothing \
locally.
The ONLY file you may write is `.crush/plan.md`. It is your deliverable: the \
problem (symptom vs prescription), acceptance criteria, distinct options \
considered, chosen approach with rationale, and the first step Execute mode \
should take.
Enumerate distinct approaches before deepening any; question the framing \
before accepting it. This supersedes any instruction in the task itself, \
including one telling you to edit.
REMEMBER: explore and plan only — the plan file is your single output.
"""

EXECUTE_PREFIX = """\
Operating rules:
- Make the smallest change that compiles and passes; one behavior per turn. \
Three similar lines beat a premature abstraction; no half-finished work.
- Don't add error handling or validation for scenarios that can't happen — \
validate only at boundaries.
- Only report a task complete when it's fully done and the relevant test has \
run green.
- If the same approach fails twice, stop — state what you learned, then \
change approach or ask.
- Read code before modifying it; prefer the lsp tools over grep for symbols.
- NEVER commit or push unless explicitly asked.
- Keep `.crush/plan.md` current and work within its scope.
"""

# Mode-named, not engine-named: the serving engine changes; the modes do not.
EXECUTE_PROVIDER = "harbor-execute"
EXPLORE_PROVIDER = "harbor-explore"



def owned_fragment(cfg: Config) -> dict[str, Any]:
    """What harbor asserts. The api_key is read from the key file at sync time —
    it lands in the live config (as before) but never in the repo."""
    def qwen(model_id: str, label: str) -> dict:
        # One sampling profile: a sealed-key A/B found no advantage to a
        # higher-temperature "brainstorm" profile. Breadth comes from more
        # runs, not hotter sampling.
        extra = {
            "temperature": 0.6,
            "top_p": 0.95,
            "top_k": 20,
            "presence_penalty": 0.0,
        }
        # The effort dial: server-enforced thinking cap; `max` sends no cap.
        if (b := effort.budget(cfg.effort)) is not None:
            extra["thinking_token_budget"] = b
        return {
            "id": model_id,
            "name": f"Qwen3.6-35B · {label}",
            "context_window": cfg.slot_context,
            "default_max_tokens": effort.MAX_TOKENS,
            "extra_body": extra,
        }

    # The prefix rides as an extra system message; the serving unit ships a
    # chat-template shim that merges leading system messages, or the server
    # would reject the second one (model.TEMPLATE_SHIM).
    def provider(name: str, prefix: str, model: dict) -> dict:
        return {
            "name": name,
            "type": "llamacpp",
            "base_url": f"{cfg.endpoint_url}/v1",
            "api_key": cfg.model_key_file.read_text().strip(),
            "flat_rate": True,
            "discover_models": False,
            "system_prompt_prefix": prefix,
            "models": [model],
        }

    return {
        "providers": {
            EXECUTE_PROVIDER: provider(
                "cloud L40 · execute", EXECUTE_PREFIX, qwen("qwen-coding", "execute")),
            EXPLORE_PROVIDER: provider(
                "cloud L40 · explore", EXPLORE_PREFIX, qwen("qwen-explore", "explore")),
        },
        "options": {"disable_default_providers": True},
    }


HOOK_ENTRIES = [
    {
        "name": "bash policy",
        "matcher": "bash",
        "command": str(HOOKS_DIR / "bash-policy.py"),
        "timeout": 10,
    },
    {
        "name": "plan gate",
        "matcher": "^(edit|multiedit|write|download)$",
        "command": str(HOOKS_DIR / "plan-gate.py"),
        "timeout": 10,
    },
]


def merge(live: dict[str, Any], cfg: Config) -> dict[str, Any]:
    """Return live with harbor's keys asserted. Unowned keys pass through."""
    out = copy.deepcopy(live)

    def deep_set(target: dict, fragment: dict) -> None:
        for k, v in fragment.items():
            if isinstance(v, dict) and isinstance(target.get(k), dict):
                deep_set(target[k], v)
            else:
                target[k] = v

    deep_set(out, owned_fragment(cfg))

    ours = {h["name"] for h in HOOK_ENTRIES}
    hooks = out.setdefault("hooks", {}).setdefault("PreToolUse", [])
    hooks[:] = [h for h in hooks if h.get("name") not in ours] + HOOK_ENTRIES

    skills = out.setdefault("options", {}).setdefault("skills_paths", [])
    if SKILLS_PATH not in skills:
        skills.insert(0, SKILLS_PATH)

    return out


def diff_lines(live: dict, merged: dict, path: str = "") -> list[str]:
    """Human-readable set of dotted paths whose values sync would change."""
    lines: list[str] = []
    for k in sorted(set(live) | set(merged)):
        p = f"{path}.{k}" if path else k
        a, b = live.get(k), merged.get(k)
        if a == b:
            continue
        if isinstance(a, dict) and isinstance(b, dict):
            lines += diff_lines(a, b, p)
        else:
            redacted = "<redacted>" if "key" in k.lower() else json.dumps(b)[:80]
            lines.append(f"  {p} -> {redacted}")
    return lines


def check(cfg: Config, live_path: Path = LIVE_PATH) -> list[str]:
    """Empty list = no drift."""
    live = json.loads(live_path.read_text())
    return diff_lines(live, merge(live, cfg))


def sync(cfg: Config, live_path: Path = LIVE_PATH, apply: bool = False) -> list[str]:
    live = json.loads(live_path.read_text())
    merged = merge(live, cfg)
    drift = diff_lines(live, merged)
    if drift and apply:
        live_path.write_text(json.dumps(merged, indent=2) + "\n")
    return drift
