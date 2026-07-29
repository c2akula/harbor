# Security model

harbor exists for one promise: **proprietary code never leaves machines you
administer.** Everything below serves that promise.

## The boundaries

| Layer | Boundary | Enforced by |
|---|---|---|
| Your code | your machines | the local model runs on YOUR box; no third-party inference API ever sees a file |
| Network | the tailnet | the server binds its tailnet address only — zero public ingress, no tunnels to manage |
| Model access | per-user keys | `harbor keys` issues and revokes them; the server rejects everything else |
| Frontier escalation | the oracle firewall | see below |
| This repo | a path allowlist in CI | anything not explicitly permitted fails the build |

## The oracle firewall

`harbor consult` can escalate a hard question to a frontier model. That call
is the only place anything leaves — so it is guarded four times:

1. **Codebase guard.** The outgoing question is checked against an index of
   *your actual repository* — every tracked file, docs and config included.
   Verbatim passages and distinctive identifiers block the call. The private
   set cannot be enumerated by hand; the repo itself is the closed set, so
   the guard derives from it.
2. **Marker guard.** A regex of identifiers you declare — the things that
   live in your head rather than in the repo. Refuses to run when empty:
   a guard that silently degrades to "allow" is worse than none.
3. **Blind execution.** The oracle process gets no file tools and a neutral
   working directory. It cannot read what it was not sent.
4. **Human review.** You see every consult before it leaves. The protocol
   (`config/ORACLE.md`) requires structural paraphrase, not pasted code.

Escape hatch: `ORACLE_UNSAFE=1` bypasses the guards deliberately — visible
in your shell history, never a default.

## What harbor does NOT protect against

Be honest about the edges:

- **A malicious operator.** Anyone with the box's SSH key owns the box.
- **Prompt-level leakage you approve.** The consult review gate is a human
  decision; harbor blocks accidents, not intent.
- **Tailnet compromise.** If your tailnet is breached, the model endpoint is
  reachable. Keys limit blast radius; they do not restore the boundary.
- **The model provider's checkpoint.** Weights are downloaded from a public
  registry; verify provenance to your own standard.

## Repo hygiene (for maintainers)

- CI's `manifest` job allowlists what may exist here. Unknown paths fail.
- A pre-commit scan (operator-side, marker-driven) catches declared
  identifiers before they commit.
- Secrets are never stored in this repo — every config value that smells
  like a credential is a *path* to a key file, never the key.
