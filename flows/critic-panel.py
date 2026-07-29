# Lens-diverse critic panel over a diff or file.
# Identical critics measurably don't pay (+1-2 findings for 3x cost) and share
# blind spots — diverse lenses are what pays. One stateless critic per lens,
# then a merge that keeps provenance.
#
# Usage: harbor flow flows/critic-panel.py  (target via CRITIC_TARGET: a file path)
import os
import pathlib
import sys

target = os.environ.get("CRITIC_TARGET", "").strip()
if not target or not pathlib.Path(target).exists():
    sys.exit("set CRITIC_TARGET to a file to review")
content = pathlib.Path(target).read_text()

LENSES = {
    "correctness": "logic errors, off-by-ones, unhandled states, broken invariants",
    "security": "injection, path traversal, secrets exposure, missing authentication, trust boundaries",
    "concurrency": "races, deadlocks, unsynchronized shared state, signal/reentrancy hazards",
    "interface": "misleading names, surprising contracts, error paths callers will misuse",
}

SCHEMA = {"type": "object", "required": ["findings"],
          "properties": {"findings": {"type": "array"}}}


def critique(lens: str, focus: str):
    obj = generate(
        f"You are an adversarial code reviewer looking ONLY through the "
        f"{lens} lens: {focus}. Ignore everything outside that lens. "
        f"Report genuine defects, not style. If there are none through this "
        f"lens, return an empty list.\n\nFile under review:\n\n{content}",
        schema=SCHEMA,
    )
    return [(lens, f) for f in obj.get("findings", [])]


# A panel down one lens is still a panel: partial results accepted.
results = parallel([lambda l=l, f=f: critique(l, f) for l, f in LENSES.items()],
                   tolerate_failures=True)
findings = [item for sub in results if sub for item in sub]
print(f"[{len(findings)} findings from {len(LENSES)} lenses]", file=sys.stderr)
for lens, finding in findings:
    print(f"[{lens}] {finding}")
