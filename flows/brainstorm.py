# Best-of-N divergent brainstorm + synthesis.
# The measured lever is run-to-run VARIANCE, not temperature: N independent
# stateless generations, then a synthesis pass over the union.
#
# Usage: harbor flow flows/brainstorm.py  (question via BRAINSTORM_Q env)
import os
import sys

N = int(os.environ.get("BRAINSTORM_N", "4"))
question = os.environ.get("BRAINSTORM_Q", "").strip()
if not question:
    sys.exit("set BRAINSTORM_Q to the question")

PROMPT = (
    "Enumerate distinct viable approaches. Prefer breadth over depth — "
    "genuinely different mechanisms, not variations. For each: how it works "
    "(2-3 sentences) and its key trade-off.\n\nQuestion: " + question
)

# Best-of-N is variance mining: losing one draft costs breadth, not truth.
drafts = parallel([lambda: generate(PROMPT) for _ in range(N)],
                  tolerate_failures=True)
drafts = [d for d in drafts if d]
print(f"[{len(drafts)}/{N} drafts]", file=sys.stderr)

# Synthesis over the union. Known failure mode from the first best-of-N
# experiment: the synthesizer can over-prune a correct option — so it is
# instructed to keep every distinct approach and rank rather than drop.
merged = "\n\n---\n\n".join(drafts)
print(generate(
    "Below are independent brainstorm drafts answering the same question. "
    "Merge them into ONE deduplicated list of distinct approaches. KEEP every "
    "genuinely distinct approach (do not drop any — rank instead: strongest "
    "first, with a one-line reason). Mark approaches that appeared in only "
    "one draft with [rare] — those are the diversity wins.\n\nQuestion: "
    + question + "\n\nDrafts:\n" + merged,
    max_tokens=8192,
))
