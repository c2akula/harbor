# Workflows: Crush agents vs `harbor flow`

Two ways to run multi-agent work. They answer different needs — use both.

## Which one, when

| You want | Use |
|---|---|
| exploratory work; the model decides how to split it | **Crush** — ask for subagents in your prompt |
| the same shape every run (pipelines, benchmarks, CI-like checks) | **`harbor flow`** |
| resume after a crash without re-paying model time | **`harbor flow`** (journaled) |
| structured JSON results feeding code | **`harbor flow`** (schema-validated) |
| code-enforced diversity (one prompt per lens) | **`harbor flow`** |

Rule of thumb: Crush-native is the daily driver; flows are the instrument.

## Crush-native fan-out

Ask for it in the prompt — the agent tool runs subagents in parallel:

```
Review ./pkg. Launch 5 subagents in parallel, one per area,
then synthesize one ranked list.
```

Notes:
- Subagents are read-only and cannot spawn their own (depth is capped at 2).
- Width is whatever you ask for; the server queues past its concurrency.

## `harbor flow`

A flow is a plain Python script run with primitives pre-bound:

```python
# flows/example.py
drafts = parallel([lambda: generate(PROMPT) for _ in range(4)],
                  tolerate_failures=True)
best = generate("Pick the best:\n" + "\n---\n".join(d for d in drafts if d),
                schema={"type": "object", "required": ["winner"],
                        "properties": {"winner": {"type": "string"}}})
print(best["winner"])
```

Run it:

```
harbor flow flows/example.py
harbor flow flows/example.py --resume <run-id>   # replay finished calls
```

### Primitives

| Primitive | Contract |
|---|---|
| `generate(prompt, *, system, schema, max_tokens)` | stateless completion; with `schema`, returns a validated object (one corrective retry) |
| `agent(prompt, cwd=".")` | a full Crush agent run; returns its output |
| `parallel(thunks, max_workers, tolerate_failures)` | run thunks concurrently, results in order |

### Failure contract

- `parallel` runs **every** branch, then raises one error naming the dead
  ones. Partial results are opt-in (`tolerate_failures=True`) — right for
  best-of-N and critic panels, wrong for pipelines.
- A reply that spent its whole token budget thinking raises a `FlowError`
  naming the budget (see the effort dial in [operating.md](operating.md)).

### Journaling

Every `generate`/`agent` call is recorded per run. `--resume <run-id>`
replays completed calls instantly and re-runs only what changed. Avoid
wall-clock or randomness in prompts if you want clean replays.

Concurrency is capped by `[flow] concurrency` in config — set it to the
server's `--max-num-seqs`.

## Shipped flows

- `flows/critic-panel.py` — lens-diverse review of one file
  (`CRITIC_TARGET=path harbor flow flows/critic-panel.py`)
- `flows/brainstorm.py` — best-of-N divergence + synthesis
  (`BRAINSTORM_Q="..." harbor flow flows/brainstorm.py`)
