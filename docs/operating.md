# Operating harbor day to day

## Commands

| Command | Does |
|---|---|
| `harbor up` | resume the box — or recreate it from the volume if it was released |
| `harbor down` | park (hibernate); GPU billing stops |
| `harbor down --release` | delete the VM, keep the volume |
| `harbor status` | box · model · load · watchdog · credit |
| `harbor hold 3` | block auto-park for 3h (`3h` works too; `off` releases) |
| `harbor model <name>` | switch served models (re-renders the unit on the box) |
| `harbor share` | print the team onboarding message — see [setup-team.md](setup-team.md) |
| `harbor join '<blob>'` | join someone else's box (teammates; installs the link) |
| `harbor peers list\|remove` | devices on the box's network |
| `harbor keys add\|revoke\|list\|rotate` | model API keys; `rotate` re-mints the team key |
| `harbor crush check\|sync` | assert harbor-owned keys in crush.json |
| `harbor consult` | guarded escalation to a frontier model |
| `harbor flow <script>` | deterministic workflows — see [workflows.md](workflows.md) |

## The watchdog (auto-park)

- Checks every ~10 minutes; three idle checks → warning; one more → hibernate.
- **Holds and strikes are shared on the box** — a hold placed on any laptop
  blocks every user's watchdog.
- Traffic, tmux sessions (local or on the box), and holds all count as
  activity.
- If the box or its state can't be reached, the watchdog does nothing:
  it never parks what it cannot see.
- A fresh boot starts with a clean slate — no inherited strikes.

## The effort dial

`[endpoint] effort` = `none | low | medium | high | max` — a server-enforced
cap on thinking tokens per request. `high` is the default; `max` removes the
cap (a request can then spend its whole budget thinking and return nothing).
Re-run `harbor crush sync` after changing it.

## Capacity expectations (one 48 GB card)

- Around five developers running subagent-heavy sessions at once will
  saturate it; sessions complete, latency roughly doubles at the ceiling.
- `harbor status`'s `load` line shows running/queued requests, and flags
  preemptions when the shared context pool is under real pressure.
- Every client is offered the full context window against one shared pool —
  deliberate over-commitment. The server degrades by queueing and
  preemption, never by erroring.

## When something's off

| Symptom | First move |
|---|---|
| `model not reachable` | `harbor up`; then `ssh <box> systemctl status vllm` |
| watchdog parked mid-work | `harbor hold` next time; holds are shared |
| a session 404s on the model | `harbor crush sync` (id drift is contract-tested, but stale configs happen) |
| resume fails with no stock | wait, or add a fallback flavor and `harbor down --release` + `up` |
| `up` reports a half-made VM | run `up` again — it continues against it; or `down --release` |
