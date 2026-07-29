"""Workflow primitives: deterministic orchestration over the local model.

A flow is a plain Python script composing three primitives:

    generate(prompt, ...)   stateless chat completion — no tools, no history;
                            the strongest independence for critics/judges.
    agent(prompt, ...)      tooled `crush run` child in a working directory.
    parallel(thunks)        run thunks concurrently, capped at the server's
                            slot count — results in submission order.

Scripts run via `harbor flow <script.py>`. Structured output: pass `schema=`
(a JSON Schema dict); the reply must be a JSON object matching it, validated
with ONE corrective retry — enough to fix formatting slips, not enough to
mask a model that cannot do the task.
"""
from __future__ import annotations

import concurrent.futures
import contextlib
import hashlib
import json
import os
import re
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Any, Callable, Iterator

import requests

from . import config as config_mod
from . import effort as effort_mod

# Transport override for tests: a command that reads the request JSON on stdin
# and writes the completion text to stdout.
GENERATE_CMD_ENV = "HARBOR_FLOW_GENERATE_CMD"
AGENT_CMD_ENV = "HARBOR_FLOW_AGENT_CMD"
SLOTS_ENV = "HARBOR_FLOW_SLOTS"

DEFAULT_WORKERS = 2                     # fallback when the server can't be asked
RUNS_DIR = Path.home() / ".local" / "state" / "harbor" / "flow-runs"


class FlowError(RuntimeError):
    pass


# --- slot pool ---------------------------------------------------------------
# One shared gate for every model-consuming leaf (generate + agent), sized from
# the server's real slot count. parallel() only fans out threads; the LEAVES
# gate actual concurrency — so nested groups can't oversubscribe the server.

_pool: threading.BoundedSemaphore | None = None
_pool_lock = threading.Lock()


def _slot_count() -> int:
    """How many model calls may run at once.

    Engine-dependent and not reliably inferable: llama.cpp has fixed `-np`
    slots it reports via /props; vLLM batches continuously up to max_num_seqs
    and advertises no equivalent. So explicit config wins, llama.cpp
    auto-detect is a convenience, and the fallback is deliberately timid.
    """
    if env := os.environ.get(SLOTS_ENV):
        return max(1, int(env))
    try:
        cfg = config_mod.load()
    except Exception:
        return DEFAULT_WORKERS
    if cfg.flow_concurrency:
        return max(1, cfg.flow_concurrency)
    try:                                    # llama.cpp only; vLLM has no /props
        r = requests.get(f"{cfg.endpoint_url}/props", timeout=5,
                         headers={"Authorization":
                                  f"Bearer {cfg.model_key_file.read_text().strip()}"})
        r.raise_for_status()
        return max(1, int(r.json()["total_slots"]))
    except Exception:
        return DEFAULT_WORKERS


def _slots() -> threading.BoundedSemaphore:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = threading.BoundedSemaphore(_slot_count())
        return _pool


# --- journal-replay ----------------------------------------------------------
# Every model call is journaled (content key + occurrence -> result); resuming
# replays unchanged calls instead of re-paying model time. Flows must be
# deterministic: no wall-clock or randomness in prompts, or keys won't match.

_journal: dict[tuple[str, int], Any] = {}
_journal_seen: dict[str, int] = {}
_journal_path: Path | None = None
_journal_lock = threading.Lock()

NONDETERMINISM = re.compile(r"\b(time\.time|datetime\.now|random\.|uuid\.)")


def _call_key(kind: str, payload: str) -> str:
    return hashlib.sha256(f"{kind}\x00{payload}".encode()).hexdigest()


def _replay_or_run(kind: str, payload: str, run: Callable[[], Any]) -> Any:
    key = _call_key(kind, payload)
    with _journal_lock:
        n = _journal_seen.get(key, 0)
        _journal_seen[key] = n + 1
        if (key, n) in _journal:
            return _journal[(key, n)]
    result = run()
    with _journal_lock:
        _journal[(key, n)] = result
        if _journal_path:
            with open(_journal_path, "a") as f:
                f.write(json.dumps({"k": key, "n": n, "r": result}) + "\n")
    return result


def _load_journal(run_id: str) -> None:
    global _journal_path
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    _journal_path = RUNS_DIR / f"{run_id}.jsonl"
    if _journal_path.exists():
        for line in _journal_path.read_text().splitlines():
            try:
                e = json.loads(line)
                _journal[(e["k"], e["n"])] = e["r"]
            except (json.JSONDecodeError, KeyError):
                continue                # torn tail from an interrupted run


def _endpoint() -> tuple[str, str]:
    cfg = config_mod.load()
    return (f"{cfg.endpoint_url}/v1/chat/completions",
            cfg.model_key_file.read_text().strip())


def _reasoning_budget() -> int | None:
    """Thinking cap resolved from the configured effort; None = uncapped."""
    return effort_mod.budget(config_mod.load().effort)


def _complete(messages: list[dict], max_tokens: int) -> str:
    with _slots():
        if cmd := os.environ.get(GENERATE_CMD_ENV):
            r = subprocess.run(cmd, shell=True, input=json.dumps(messages),
                               capture_output=True, text=True, timeout=1800)
            if r.returncode != 0:
                raise FlowError(f"generate override failed: {r.stderr.strip()}")
            return r.stdout
        url, key = _endpoint()
        body = {"model": "qwen", "messages": messages, "max_tokens": max_tokens,
                "temperature": 0.6, "top_p": 0.95, "top_k": 20}
        # Cap thinking (server-enforced), reserving answer room within this
        # call's own max_tokens — uncapped thinking can consume all of it and
        # return a null-content reply.
        budget = _reasoning_budget()
        if budget is not None:
            body["thinking_token_budget"] = min(
                budget, max(max_tokens - effort_mod.ANSWER_RESERVE, 0))
        resp = requests.post(url, json=body, timeout=1800,
                             headers={"Authorization": f"Bearer {key}"})
        resp.raise_for_status()
        msg = resp.json()["choices"][0]["message"]
        content = msg.get("content")
        if content is None:
            # A reasoning model that exhausts max_tokens mid-thought returns
            # null content, with the text stranded in the reasoning field
            # (named `reasoning` or `reasoning_content` depending on server).
            thought = len(msg.get("reasoning")
                          or msg.get("reasoning_content") or "")
            raise FlowError(
                f"model returned no content — it spent the whole max_tokens "
                f"budget ({max_tokens}) reasoning ({thought} chars of "
                f"reasoning_content). Raise max_tokens or lower the thinking "
                f"budget.")
        return content


def _repair_json(text: str) -> str:
    """Close what the model left open: track bracket depth outside strings,
    auto-close an unterminated string, then append missing closers. Local
    models fragment JSON near token limits; a formatting slip should not
    burn the single corrective retry."""
    depth_stack: list[str] = []
    in_string = escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch in "{[":
            depth_stack.append("}" if ch == "{" else "]")
        elif ch in "}]" and depth_stack:
            depth_stack.pop()
    repaired = text
    if in_string:
        repaired += '"'
    return repaired + "".join(reversed(depth_stack))


def _extract_json(text: str) -> Any:
    """The reply may wrap JSON in a code fence or prose; find the object,
    repairing truncated tails if needed."""
    fenced = re.findall(r"```(?:json)?\s*\n(.*?)```", text, re.S)
    candidates = fenced + [text]
    for cand in candidates:
        cand = cand.strip()
        start = cand.find("{")
        if start < 0:
            continue
        for attempt in (cand[start:cand.rfind("}") + 1], _repair_json(cand[start:])):
            try:
                return json.loads(attempt)
            except json.JSONDecodeError:
                continue
    raise FlowError(f"no JSON object found in reply: {text[:200]!r}")


def _validate(obj: Any, schema: dict) -> str | None:
    """Minimal structural check: required keys + primitive types. Returns a
    complaint string or None. (Full JSON Schema is YAGNI here.)"""
    if not isinstance(obj, dict):
        return "reply is not a JSON object"
    types = {"string": str, "number": (int, float), "integer": int,
             "boolean": bool, "array": list, "object": dict}
    missing = [k for k in schema.get("required", []) if k not in obj]
    if missing:
        return f"missing required keys: {missing}"
    for key, spec in schema.get("properties", {}).items():
        if key in obj and spec.get("type") in types:
            val, want = obj[key], spec["type"]
            # bool subclasses int in Python; JSON Schema keeps them distinct.
            if isinstance(val, bool) and want != "boolean":
                return f"key '{key}' should be {want}"
            if not isinstance(val, types[want]):
                return f"key '{key}' should be {want}"
    return None


def generate(prompt: str, *, system: str = "", schema: dict | None = None,
             max_tokens: int = 32768) -> Any:
    """Stateless completion. With schema: returns the validated object.
    Journaled: an identical call in a resumed run replays without model time."""
    payload = json.dumps({"p": prompt, "s": system, "j": schema, "m": max_tokens})
    return _replay_or_run("generate", payload,
                          lambda: _generate_live(prompt, system, schema, max_tokens))


def _generate_live(prompt: str, system: str, schema: dict | None,
                   max_tokens: int) -> Any:
    messages = ([{"role": "system", "content": system}] if system else [])
    if schema:
        prompt += ("\n\nReturn ONLY a JSON object matching this schema, "
                   "no other text:\n" + json.dumps(schema))
    messages.append({"role": "user", "content": prompt})
    text = _complete(messages, max_tokens)
    if not schema:
        return text
    try:
        obj = _extract_json(text)
        complaint = _validate(obj, schema)
    except FlowError as e:
        obj, complaint = None, str(e)
    if complaint is None:
        return obj
    retry = messages + [
        {"role": "assistant", "content": text},
        {"role": "user", "content":
            f"Your reply was invalid: {complaint}. Return ONLY the corrected "
            "JSON object, nothing else."}]
    obj = _extract_json(_complete(retry, max_tokens))
    complaint = _validate(obj, schema)
    if complaint:
        raise FlowError(f"schema still violated after retry: {complaint}")
    return obj


def agent(prompt: str, *, cwd: str = ".") -> str:
    """Tooled `crush run` child. Returns its final text output. Journaled and
    slot-gated like generate. NOTE: cwd is part of the journal key — agents in
    throwaway worktrees re-run on resume (fresh worktree = fresh path)."""
    return _replay_or_run("agent", json.dumps({"p": prompt, "c": cwd}),
                          lambda: _agent_live(prompt, cwd))


def _agent_live(prompt: str, cwd: str) -> str:
    with _slots():
        if cmd := os.environ.get(AGENT_CMD_ENV):
            r = subprocess.run(cmd, shell=True, input=prompt, capture_output=True,
                               text=True, timeout=3600, cwd=cwd)
        else:
            r = subprocess.run(["crush", "run", prompt], capture_output=True,
                               text=True, timeout=3600, cwd=cwd)
    if r.returncode != 0:
        raise FlowError(f"agent failed ({r.returncode}): {r.stderr.strip()[:300]}")
    return r.stdout


@contextlib.contextmanager
def worktree(repo: str) -> Iterator[str]:
    """Disposable git worktree for a mutating agent — parallel implementers
    can't conflict when each works its own checkout. Removed on exit."""
    name = f"flow-{uuid.uuid4().hex[:8]}"
    path = str(Path(repo) / ".harbor-worktrees" / name)
    r = subprocess.run(["git", "-C", repo, "worktree", "add", "--detach", path],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise FlowError(f"worktree add failed: {r.stderr.strip()[:300]}")
    try:
        yield path
    finally:
        subprocess.run(["git", "-C", repo, "worktree", "remove", "--force", path],
                       capture_output=True)


def parallel(thunks: list[Callable[[], Any]],
             max_workers: int | None = None,
             tolerate_failures: bool = False) -> list[Any]:
    """Run thunks concurrently; results in submission order.

    Every branch runs to completion, then any failures are raised together
    naming their branches — a silent None slot lets a downstream stage run on
    missing inputs and still claim success. tolerate_failures=True opts into
    None slots for fan-outs where partial results are genuinely acceptable
    (a critic panel down one lens is still a panel).

    Threads only fan out here; actual model concurrency is gated at the
    leaves by the shared slot pool, so nesting cannot oversubscribe."""
    workers = max_workers or max(len(thunks), 1)
    results: list[Any] = [None] * len(thunks)
    errors: dict[int, Exception] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(t): i for i, t in enumerate(thunks)}
        for fut in concurrent.futures.as_completed(futures):
            try:
                results[futures[fut]] = fut.result()
            except Exception as e:
                errors[futures[fut]] = e
    if errors and not tolerate_failures:
        detail = "; ".join(
            f"branch {i + 1} of {len(thunks)}: {e}"
            for i, e in sorted(errors.items()))
        raise FlowError(f"parallel fan-out failed — {detail}")
    return results


def run_script(path: str, resume: str | None = None) -> int:
    """Execute a flow script with the primitives pre-bound in its globals.
    Journaled: pass resume=<run-id> to replay completed calls from a prior
    run instead of re-paying model time."""
    source = open(path).read()
    if NONDETERMINISM.search(source):
        print("flow warning: script uses wall-clock/randomness — resumed runs "
              "will not replay calls whose prompts change run-to-run.",
              file=__import__("sys").stderr)
    run_id = resume or uuid.uuid4().hex[:12]
    _load_journal(run_id)
    print(f"flow run {run_id} ({'resumed' if resume else 'new'}) — "
          f"resume later with: harbor flow {path} --resume {run_id}",
          file=__import__("sys").stderr)
    scope = {"generate": generate, "agent": agent, "parallel": parallel,
             "worktree": worktree, "__name__": "__main__", "__file__": path}
    exec(compile(source, path, "exec"), scope)
    return 0
