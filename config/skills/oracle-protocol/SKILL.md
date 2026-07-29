---
name: oracle-protocol
description: Use when consulting the oracle (frontier model escalation) or when the user asks for a second opinion, to compose a confidentiality-safe abstracted question and relay the answer correctly.
---

# Oracle consultation protocol

A frontier model is reachable via `harbor consult` for hard problems. It is
deliberately blind: no file access, neutral working directory, and two guards
— the question is checked against an index of the repository itself and
against the operator's marker list. The question text must stay clean — no
file paths, no real identifiers, no project or package names, no code except
as sanctioned by L1 below.

## Abstraction ladder — compose at L2 by default

- **L1 — pseudonymized reproduction**: minimal code, every identifier renamed
  generic, comments/strings stripped. ONLY when the user explicitly asks for
  L1 (structure-is-the-question cases: lock ordering, lifetime bugs).
- **L2 — structural paraphrase (DEFAULT)**: no code; mechanics restated
  generically ("a tick-driven fiber runtime where a barrier gates teardown").
- **L3 — canonical problem**: mapped to a public problem class
  ("readers-writers under cooperative scheduling with cancellation").

## Protocol

1. Consult only when the user asks, or accepts your offer to consult.
2. Compose the abstracted question; show it before running — that confirmation
   is the user's leak review. `harbor consult -l <1|2|3> "<question>"`
   (default 2); follow-ups with `harbor consult -c "<follow-up>"`.
3. If the user says "dial it down" / "L3", recompose one level more abstract
   and present again. If the command refuses (repository index or marker
   match), re-abstract — never override the guard.
4. Translate the oracle's general answer back to the codebase locally
   yourself, and attribute clearly: which analysis is the oracle's, which
   mapping is yours.
