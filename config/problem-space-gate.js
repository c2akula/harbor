// Port of ~/.claude/hooks/problem-space-gate.sh (Claude Code PreToolUse hook).
// Source-file edits require a recent problem-space artifact
// (<project>/.claude/tmp/problem-space*.md, <8h old).
// Escape hatch: the artifact may be a single line "QUICK-FIX: <reason>".
import { readdirSync, statSync } from "node:fs"
import { join } from "node:path"

const SOURCE_RE = /\.(c|cc|cpp|cxx|h|hpp|hh|py|js|jsx|ts|tsx|go|rs|java|rb)$/
const MAX_AGE_MIN = 480
const GATED_TOOLS = new Set(["edit", "write", "patch"])

const DENY =
  "problem-space gate: no recent problem-space artifact. Before editing source, " +
  "follow the problem-space-first skill and write <project>/.claude/tmp/problem-space.md — " +
  "symptom vs prescription, actual failure location, acceptance criteria, approaches " +
  "considered, primary sources consulted. For a genuine quick fix it may be one line: " +
  "'QUICK-FIX: <reason>'. Then retry the edit."

export const ProblemSpaceGate = async ({ directory }) => ({
  "tool.execute.before": async (input, output) => {
    if (!GATED_TOOLS.has(input.tool)) return
    const args = output.args ?? {}
    const fp = args.filePath ?? args.file_path ?? args.path ?? ""
    if (!SOURCE_RE.test(fp)) return

    const tmp = join(directory, ".claude", "tmp")
    let entries = []
    try {
      entries = readdirSync(tmp)
    } catch {
      // no .claude/tmp directory -> gate is closed
    }
    const fresh = entries.some((f) => {
      if (!/^problem-space.*\.md$/.test(f)) return false
      try {
        return (Date.now() - statSync(join(tmp, f)).mtimeMs) / 60000 < MAX_AGE_MIN
      } catch {
        return false
      }
    })
    if (!fresh) throw new Error(DENY)
  },
})
