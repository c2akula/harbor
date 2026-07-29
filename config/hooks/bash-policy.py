#!/usr/bin/env python3
"""PreToolUse policy hook for Crush — the auto-mode equivalent.

Three outcomes, and the third is the important one:

  ALLOW   affirmatively pre-approved; Crush skips the permission prompt.
          Only for commands that cannot modify anything.
  DENY    blocked with a reason the model sees and can act on.
          Only for commands that are destructive or exfiltrating.
  SILENT  no opinion -> the normal permission prompt. THE DEFAULT.

Design rule: silence is safe, allow is not. Anything not provably read-only
falls through to you. We would rather prompt too often than auto-approve once
wrongly — an over-eager allowlist is indistinguishable from --yolo.

Deliberately deterministic: bash calls are frequent, and a model call per
command would add seconds of latency and contend for a serving slot. If model
judgement is ever added for the ambiguous middle it must use the LOCAL model —
never the oracle, since commands carry paths and project identifiers.
"""
import hashlib, json, os, re, sqlite3, sys, time

# Destructive or exfiltrating. Checked FIRST; a match always denies.
DENY = [
    (r"\brm\s+(-[a-zA-Z]*\s+)*-[a-zA-Z]*[rf]", "recursive/forced delete"),
    (r"\bdd\s+.*\bof=/dev/", "raw write to a device"),
    (r"\bmkfs\b", "filesystem creation"),
    (r">\s*/dev/(sd|nvme|hd)", "raw write to a disk"),
    (r"\bgit\s+push\b.*(--force|-f)\b", "force push"),
    (r"\bgit\s+reset\s+--hard\b", "discards uncommitted work"),
    (r"\bgit\s+clean\s+-[a-zA-Z]*[fd]", "deletes untracked files"),
    (r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(ba)?sh", "pipes a download into a shell"),
    (r"\bchmod\s+(-R\s+)?777\b", "world-writable permissions"),
    (r"\bsudo\b", "privilege escalation"),
    (r":\(\)\s*\{.*\};\s*:", "fork bomb"),
]

# Provably read-only. The command must consist ONLY of these, optionally piped
# into a display filter. Anchored, and re-checked per pipeline segment.
SAFE_HEAD = re.compile(r"""^\s*(
      ls | pwd | whoami | date | uname | hostname | which | type
    | cat | head | tail | wc | file | stat | du | df
    | grep | rg | find | fd | tree | basename | dirname | realpath
    | echo | printf | env | uptime | id
    | git\s+(status|log|diff|show|branch|remote|blame|describe|rev-parse|ls-files)
    | systemctl\s+(--user\s+)?(status|is-active|list-units|list-timers|cat)
    | ps | free | nproc | jobs
)\b""", re.X)

# Filters that cannot themselves modify anything.
SAFE_FILTER = re.compile(r"""^\s*(
    head | tail | wc | sort | uniq | cut | tr | grep | rg | awk | sed | jq | column | less | cat
)\b""", re.X)

# Any of these mean we cannot reason about the command statically -> SILENT.
OPAQUE = re.compile(r"(\$\(|`|>>?|<|&&|\|\||;|\beval\b|\bexec\b|\bxargs\b)")


def classify(cmd: str):
    for pat, why in DENY:
        if re.search(pat, cmd, re.I):
            return "deny", why
    if OPAQUE.search(cmd):
        return "silent", None          # redirects/substitution/chaining: ask
    segments = [s for s in cmd.split("|")]
    if not SAFE_HEAD.match(segments[0]):
        return "silent", None
    for seg in segments[1:]:
        if not SAFE_FILTER.match(seg):
            return "silent", None
    return "allow", None


# --- thrash detection -------------------------------------------------------
# Outcome-aware: Crush persists the session to <project>/.crush/crush.db, so
# the previous calls' RESULTS are readable. Same command + same result twice
# already -> a third attempt is thrash (deny with a nudge). Same command with
# CHANGING results is a poll loop and is left alone. Every failure path here
# is fail-open (silent): a broken supervisor must never break the session.

THRASH_STREAK = 2       # prior identical (command, result) pairs before deny
SCAN_MESSAGES = 80      # recent rows to scan; bounds query cost
MARKER_TTL = 7 * 86400  # nudge markers older than this are pruned
EXPLORE_PROVIDER = "harbor-explore"


def _find_db(cwd: str):
    d = os.path.realpath(cwd or ".")
    for _ in range(30):
        p = os.path.join(d, ".crush", "crush.db")
        if os.path.exists(p):
            return p
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent
    return None


def _recent_bash_results(db_path: str, session_id: str):
    """Newest-first [(command, result_content)] for finished bash calls."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=0.2)
    try:
        rows = con.execute(
            "SELECT role, parts FROM messages WHERE session_id = ? "
            "ORDER BY created_at DESC LIMIT ?", (session_id, SCAN_MESSAGES),
        ).fetchall()
    finally:
        con.close()
    calls, results = {}, {}
    order = []                          # call ids, newest first
    for role, parts in rows:
        for part in json.loads(parts):
            data = part.get("data") or {}
            if part.get("type") == "tool_call" and data.get("name") == "bash":
                try:
                    command = json.loads(data.get("input") or "{}").get("command", "")
                except Exception:
                    continue
                calls[data.get("id")] = command
                order.append(data.get("id"))
            elif part.get("type") == "tool_result":
                results[data.get("tool_call_id")] = data.get("content", "")
    return [(calls[i], results[i]) for i in order if i in calls and i in results]


def _nudged_marker(session_id: str, streak_hash: str):
    root = os.environ.get("HARBOR_HOOK_STATE",
                          os.path.expanduser("~/.local/state/harbor/hook"))
    os.makedirs(root, exist_ok=True)
    _prune(root)
    return os.path.join(root, f"nudged-{session_id}-{streak_hash}")


def _prune(root: str):
    """Markers are per-(session, streak) and never revisited after a week."""
    try:
        cutoff = time.time() - MARKER_TTL
        for name in os.listdir(root):
            p = os.path.join(root, name)
            if name.startswith("nudged-") and os.path.getmtime(p) < cutoff:
                os.unlink(p)
    except Exception:
        pass


def _active_provider(db_path: str, session_id: str):
    """Provider of the session's newest assistant message; None if unknowable."""
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=0.2)
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


def explore_verdict(cmd: str, verdict: str, session_id: str, cwd: str):
    """In explore mode, unproven commands are DENIED, not prompted — the
    prefix promises state-changing commands will fail; this keeps it true.
    Carve-out: `harbor ...` commands (consult above all) keep the normal
    prompt — the permission prompt IS the operator's leak-review gate."""
    try:
        if verdict != "silent":
            return None                 # allow stays allow; deny already denied
        if re.match(r"^\s*harbor\b", cmd):
            return None
        db = _find_db(cwd)
        if not db or not session_id:
            return None
        if _active_provider(db, session_id) != EXPLORE_PROVIDER:
            return None
        return ("explore mode is read-only: only provably read-only commands "
                "run here, and this one cannot be verified as read-only. "
                "Decompose it into simple read-only steps, or switch to the "
                "execute model to make changes.")
    except Exception:
        return None


def thrash_verdict(cmd: str, session_id: str, cwd: str):
    """'deny' reason string, or None. Never raises."""
    try:
        db = _find_db(cwd)
        if not db or not session_id:
            return None
        pairs = _recent_bash_results(db, session_id)
        streak = [p for p in pairs[:THRASH_STREAK]]
        if len(streak) < THRASH_STREAK:
            return None
        if any(c != cmd for c, _ in streak):
            return None
        first_result = streak[0][1]
        if any(r != first_result for _, r in streak):
            return None                 # results changing: poll loop, not thrash
        streak_hash = hashlib.sha256(
            (cmd + "\x00" + first_result).encode()).hexdigest()[:16]
        marker = _nudged_marker(session_id, streak_hash)
        if os.path.exists(marker):
            return None                 # one nudge per streak; after that, prompt
        with open(marker, "w"):
            pass
        return (f"this exact command has already run {THRASH_STREAK} times "
                "with identical output — running it again will not change "
                "anything. Change the input, inspect state differently, or "
                "ask the user. If the repetition is deliberate, re-run with "
                "a trivial variation to bypass this check.")
    except Exception:
        return None


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        print("{}")                    # unparseable: no opinion
        return 0

    if payload.get("tool_name") != "bash":
        print("{}")
        return 0

    cmd = (payload.get("tool_input") or {}).get("command", "")
    verdict, why = classify(cmd)

    if verdict == "deny":
        print(json.dumps({"decision": "deny",
                          "reason": f"blocked by policy: {why}. If this is "
                                    f"genuinely needed, ask the user to run it."}))
        return 0

    session_id = payload.get("session_id", "")
    cwd = payload.get("cwd", "")

    nudge = thrash_verdict(cmd, session_id, cwd)
    if nudge:
        print(json.dumps({"decision": "deny", "reason": nudge}))
        return 0

    ro = explore_verdict(cmd, verdict, session_id, cwd)
    if ro:
        print(json.dumps({"decision": "deny", "reason": ro}))
        return 0

    if verdict == "allow":
        print(json.dumps({"decision": "allow"}))
    else:
        print("{}")                    # fall through to the normal prompt
    return 0


if __name__ == "__main__":
    sys.exit(main())
