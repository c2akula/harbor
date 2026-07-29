#!/usr/bin/env python3
"""PreToolUse plan gate for Crush — no file edits until a plan artifact exists.

The local model chronically over-scopes turns (measured: paired-cell vs solo);
a gate that forces "what problem, what does done look like" BEFORE edits is
structural, not advisory. Port of the problem-space-gate pattern.

Gate condition: <project>/.crush/plan.md exists and is fresh (< 6h). The deny
reason teaches the protocol, and writing the plan file itself is always
allowed, so the loop is self-correcting. Escape hatch: a plan whose first line
is 'QUICK-FIX: <reason>' satisfies the gate like any other plan (it IS the
plan). Everything but the gate condition fails open — this is a workflow
control, not a safety control.
"""
import json
import os
import sqlite3
import sys
import time

FRESH_SECONDS = 6 * 3600
GATED_TOOLS = {"edit", "multiedit", "write", "download"}
PLAN_RELPATH = os.path.join(".crush", "plan.md")
EXPLORE_PROVIDER = "harbor-explore"

REASON = (
    "plan gate: no fresh plan artifact. Before editing files, write "
    "{plan} — a few lines: the problem (symptom vs prescription), acceptance "
    "criteria, chosen approach. For a genuinely trivial change a single line "
    "'QUICK-FIX: <reason>' suffices. Then retry the edit."
)

EXPLORE_REASON = (
    "explore mode is read-only: file edits are blocked by policy. Your "
    "deliverable is {plan} — write your findings and plan there. Switch to "
    "the execute model to implement."
)


def active_provider(root: str, session_id: str):
    """Provider of the session's newest assistant message — the in-flight turn.
    None when unknowable (fail toward execute, the permissive mode)."""
    try:
        db = os.path.join(root, ".crush", "crush.db")
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=0.2)
        try:
            row = con.execute(
                "SELECT provider FROM messages WHERE session_id = ? AND "
                "role = 'assistant' ORDER BY created_at DESC LIMIT 1",
                (session_id,)).fetchone()
        finally:
            con.close()
        return row[0] if row else None
    except Exception:
        return None


def find_project_root(cwd: str):
    """Nearest ancestor holding .crush/ — same discovery as the session db."""
    d = os.path.realpath(cwd or ".")
    for _ in range(30):
        if os.path.isdir(os.path.join(d, ".crush")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent
    return None


def gate(tool_name: str, file_path: str, cwd: str, session_id: str = ""):
    """'deny' reason string, or None. Never raises."""
    try:
        if os.environ.get("HARBOR_PLAN_GATE", "").lower() == "off":
            return None
        if tool_name not in GATED_TOOLS:
            return None
        root = find_project_root(cwd)
        if root is None:
            return None                 # not a Crush project: no opinion
        plan = os.path.join(root, PLAN_RELPATH)
        # The model must always be able to satisfy the gate itself.
        if file_path and os.path.realpath(file_path) == os.path.realpath(plan):
            return None
        # Explore mode: read-only regardless of plan freshness — the prefix
        # promises "attempting an edit will fail"; this is what keeps it true.
        if session_id and active_provider(root, session_id) == EXPLORE_PROVIDER:
            return EXPLORE_REASON.format(plan=plan)
        if os.path.exists(plan) and time.time() - os.path.getmtime(plan) < FRESH_SECONDS:
            return None
        return REASON.format(plan=plan)
    except Exception:
        return None


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        print("{}")
        return 0
    reason = gate(payload.get("tool_name", ""),
                  (payload.get("tool_input") or {}).get("file_path", ""),
                  payload.get("cwd", ""),
                  payload.get("session_id", ""))
    if reason:
        print(json.dumps({"decision": "deny", "reason": reason}))
    else:
        print("{}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
