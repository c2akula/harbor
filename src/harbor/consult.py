"""Guarded escalation to a frontier Claude model (subscription-billed) WITHOUT
exposing the codebase: no file tools, neutral cwd so no project context loads,
and questions must already be abstracted away from proprietary specifics.
The permission prompt before each call is the human leak-review gate."""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from . import codebase
from .config import Config

LEVELS = {
    1: "pseudonymized minimal reproduction — identifiers renamed, structure real",
    2: "structural paraphrase — mechanics described generically, no code",
    3: "canonical problem — mapped to a public problem class",
}

NEUTRAL_CWD = Path.home() / ".local" / "state" / "oracle"

SYSTEM_PROMPT = (
    "You are an oracle consulted for hard engineering problems. You have no "
    "access to the code under discussion, and questions reaching you are "
    "deliberately abstracted away from a proprietary codebase. Answer "
    "decisively in general terms, state your reasoning and assumptions, and "
    "where specifics would change the answer, enumerate the cases. You may "
    "search the web for public primary sources."
)


def compose_question(question: str, level: int) -> str:
    return f"[abstraction level {level}: {LEVELS[level]}]\n\n{question}"


def guard_against_codebase(question: str, cwd: str) -> tuple[bool, str]:
    """Check the outgoing question against the repository you are working in.

    The primary guard: a marker list can only catch what someone thought to
    name, while the repo itself is the actual closed set and is on disk at the
    handover point. Covers every tracked file — docs and config leak as
    readily as source.

    Silent when there is no repo to compare against: the marker guard still
    applies, and refusing every consult outside a repo would make escalation
    unusable rather than safe.
    """
    if os.environ.get("ORACLE_UNSAFE"):
        return True, ""
    try:
        root = codebase.repo_root(cwd or os.getcwd())
        if root is None:
            return True, ""
        idx = codebase.load(root)
        hit = codebase.check(question, idx)
    except Exception:
        return True, ""          # never break escalation on an indexing fault
    if hit is None:
        return True, ""
    kind, count = hit
    if kind == "verbatim":
        return False, (
            f"question contains {count} passage(s) appearing verbatim in "
            f"{root.name} (indexed {idx.files} tracked files, including docs "
            "and config). Paraphrase it structurally, or set ORACLE_UNSAFE=1 "
            "if you are certain this is safe to send.")
    return False, (
        f"question contains {count} identifier(s) that appear in {root.name}. "
        "Rename or describe them generically, or set ORACLE_UNSAFE=1 if you "
        "are certain this is safe to send.")


def guard(question: str, markers: str) -> tuple[bool, str]:
    """Operator-declared markers — a cheap SECONDARY filter for identifiers
    that live in someone's head rather than in the repo. Fails CLOSED: with
    nothing configured it refuses, because a guard that degrades to 'allow' is
    worse than no guard."""
    if os.environ.get("ORACLE_UNSAFE"):
        return True, ""
    if not markers:
        return False, (
            "ORACLE_MARKERS is not set (config.toml [oracle] markers) — "
            "refusing to send anything rather than run with an empty guard."
        )
    found = sorted(set(m.group(0) for m in re.finditer(markers, question, re.I)))
    if found:
        return False, (
            f"question contains proprietary markers: {' '.join(found)}\n"
            "abstract the question, or set ORACLE_UNSAFE=1 to override."
        )
    return True, ""


def run(cfg: Config, question: str, level: int, continue_session: bool,
        model: str | None) -> int:
    if level not in LEVELS:
        print("harbor consult: level must be 1, 2, or 3", file=sys.stderr)
        return 2

    full = compose_question(question, level)

    # Primary: compare against the repository we were invoked from, before
    # changing directory away from it. Check the USER's text, not the composed
    # form — the level annotation is harbor's own boilerplate and lives in this
    # file, so composing first makes every consult match the repo itself.
    ok, reason = guard_against_codebase(question, os.getcwd())
    if not ok:
        print(f"harbor consult: {reason}", file=sys.stderr)
        return 3

    ok, reason = guard(full, cfg.oracle_markers)
    if not ok:
        print(f"harbor consult: {reason}", file=sys.stderr)
        return 3

    # The oracle must always reach the real frontier backend on subscription —
    # never a redirected local model.
    for var in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN",
                "ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL"):
        os.environ.pop(var, None)

    NEUTRAL_CWD.mkdir(parents=True, exist_ok=True)
    os.chdir(NEUTRAL_CWD)

    argv = ["claude", "-p"]
    if continue_session:
        argv.append("--continue")
    chosen = model or cfg.oracle_model
    if chosen:
        argv += ["--model", chosen]
    argv += ["--allowedTools", "WebSearch", "WebFetch",
             "--append-system-prompt", SYSTEM_PROMPT, full]
    os.execvp("claude", argv)
    raise AssertionError("unreachable")
