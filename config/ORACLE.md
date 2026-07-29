# Oracle

A frontier Claude model is available via `harbor consult` (subscription-billed) for hard problems.

CONFIDENTIALITY: the oracle must never see this codebase. It has no file access, and the
question text itself must stay clean: no file paths, no real identifiers, no project or
package names, and no code except as sanctioned below. Two guards enforce this before
anything leaves: the question is checked against an index of the repository itself
(every tracked file — verbatim passages and distinctive identifiers block the call),
and against the operator's declared marker regex.

Abstraction ladder (compose at L2 by default):

- L1 — pseudonymized reproduction: minimal code with every identifier renamed to generic
  ones, comments/strings stripped. ONLY when the user explicitly asks for L1 (questions
  where the structure itself is the question, e.g. lock ordering, lifetime bugs).
- L2 — structural paraphrase (DEFAULT): no code; mechanics restated generically
  (e.g. "a tick-driven fiber runtime where a barrier gates teardown").
- L3 — canonical problem: mapped to a public problem class
  (e.g. "readers-writers under cooperative scheduling with cancellation").

Protocol:

- Invoke ONLY when the user explicitly asks (e.g. "ask the oracle", "second opinion").
- Usage via bash: `harbor consult -l <1|2|3> "<abstracted question>"` (level defaults
  to 2). Follow-up: `harbor consult -c "<follow-up>"`.
- The command is shown to the user before running — that confirmation is the leak review.
  If the user denies it saying it is too specific ("dial it down", "L3"), recompose one
  level more abstract and present it again.
- The command refuses questions matching the repository index or the marker list;
  if it does, re-abstract rather than reword around the guard.
- Translate the oracle's general answer back to the codebase locally yourself, and
  attribute it as the oracle's.
