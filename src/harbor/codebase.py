"""What must not leave: an index built from the repository you are working in.

The private set is open — it cannot be enumerated, and any attempt produces a
list that is already out of date. The *codebase* is a closed set sitting on
disk, so the guard checks a query against the thing itself rather than against
a description of it.

Indexes EVERY tracked file, not just source: documentation describes
architecture and config carries internal hosts and paths, so both leak as
readily as code.

Two signals, both derived from the repo and therefore always current:
  literal   — hashed k-gram windows catch verbatim lifts (pasted code, quoted
              comments, copied config lines)
  symbols   — distinctive identifiers extracted from the corpus, so the marker
              list writes itself and tracks the code as it changes
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

# 5 words is the working compromise: code lines are short, so a wider window
# never matches a pasted signature, while a narrower one collides on common
# idioms like `for i in range len`.
WINDOW = 5
MIN_SYMBOL = 6      # shorter tokens are almost always generic
CACHE_DIR = Path.home() / ".local" / "state" / "harbor" / "codebase-index"

# Tokens that appear in every codebase and would fire on innocent questions.
# Deliberately short: the length filter does most of the work, and an
# over-eager stoplist would punch holes in the guard.
STOPWORDS = {
    "config", "configuration", "function", "return", "import", "export",
    "default", "public", "private", "static", "string", "number", "object",
    "boolean", "package", "module", "include", "require", "params",
    "parameter", "arguments", "options", "result", "results", "values",
    "should", "because", "although", "before", "between", "through",
    "another", "example", "without", "against", "already", "certain",
    "process", "request", "response", "server", "client", "system", "update",
    "create", "delete", "insert", "select", "handler", "manager", "service",
    "buffer", "message", "context", "session", "backend", "frontend",
    "database", "instance", "provider", "callback", "iterator", "wrapper",
}

_WORD = re.compile(r"[A-Za-z0-9_./-]+")
_SYMBOL = re.compile(r"[A-Za-z_][A-Za-z0-9_]{%d,}" % (MIN_SYMBOL - 1))
_INTERNAL_CAPS = re.compile(r"[a-z][A-Z]")


def _is_identifier_shaped(token: str) -> bool:
    """Distinguish an identifier from an English word.

    The index covers prose, so a bare length filter would turn every long word
    in the documentation into a marker (`wraparound`, `dataclass`). Real
    identifiers carry structure English does not: snake_case, camelCase,
    SCREAMING constants, embedded digits, dotted/hyphenated compounds.

    Short plain names (a bare project codename) fall through deliberately —
    the operator-declared marker list is the filter for those.
    """
    if "_" in token:
        return True
    if _INTERNAL_CAPS.search(token):
        return True
    if token.isupper() and len(token) >= 5:
        return True
    if any(c.isdigit() for c in token):
        return True
    # A compound only counts when it carries a non-English signal: a dot
    # (hostname/path), three-plus parts, or mixed case. Plain two-word
    # hyphenations (`lock-free`) are ordinary prose.
    if any(c in token for c in "-."):
        parts = [p for p in re.split(r"[-.]", token) if p]
        # Two real parts minimum: a trailing full stop ("monthly.") would
        # otherwise make any prose word look dotted.
        if len(parts) >= 2 and ("." in token or len(parts) >= 3
                                or not token.islower()):
            return len(token) >= 8
    return False


@dataclass(frozen=True)
class Index:
    windows: frozenset[str]     # hashed literal k-gram windows
    symbols: frozenset[str]     # distinctive identifiers, lowercased
    root: str
    files: int


def repo_root(start: str | Path) -> Path | None:
    """The git repo containing `start`, or None if it isn't in one."""
    try:
        out = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10)
        return Path(out.stdout.strip()) if out.returncode == 0 else None
    except Exception:
        return None


def _normalise(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def _hash(words: list[str]) -> str:
    return hashlib.sha256(" ".join(words).encode()).hexdigest()[:16]


def _fingerprint(root: Path) -> str:
    """Cheap staleness key: HEAD plus the newest tracked mtime. Catches both
    commits and uncommitted edits without hashing the whole tree."""
    head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    newest = 0.0
    for rel in _tracked(root):
        try:
            newest = max(newest, (root / rel).stat().st_mtime)
        except OSError:
            continue
    return f"{head}:{newest:.0f}"


def _tracked(root: Path) -> list[str]:
    out = subprocess.run(["git", "-C", str(root), "ls-files"],
                         capture_output=True, text=True)
    return out.stdout.split() if out.returncode == 0 else []


def build(root: Path) -> Index:
    windows: set[str] = set()
    symbols: set[str] = set()
    counted = 0
    for rel in _tracked(root):
        path = root / rel
        try:
            if not path.is_file() or path.stat().st_size > 4_000_000:
                continue
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        if "\0" in text[:1024]:          # binary
            continue
        counted += 1

        # Path components are distinctive even when plain (acme_telemetry/...).
        for part in re.split(r"[/.\-_]", rel):
            if len(part) >= MIN_SYMBOL and part.lower() not in STOPWORDS:
                symbols.add(part.lower())
        if _is_identifier_shaped(rel):
            symbols.add(rel.lower())

        words = _normalise(text)
        for i in range(len(words) - WINDOW + 1):
            windows.add(_hash(words[i:i + WINDOW]))
        for sym in _SYMBOL.findall(text):
            low = sym.lower()
            if (_is_identifier_shaped(sym) and low not in STOPWORDS
                    and not low.isdigit()):
                symbols.add(low)
        # Hyphenated/dotted compounds the identifier regex splits apart
        # (metrics-sink.internal, ROTATION-TAG-9).
        for tok in _WORD.findall(text):
            if (not tok[0].isdigit() and any(c in tok for c in "-.")
                    and _is_identifier_shaped(tok)):
                symbols.add(tok.lower())
    return Index(frozenset(windows), frozenset(symbols), str(root), counted)


def load(root: Path, *, max_age: float = 0) -> Index:
    """Cached build. The cache lives outside the repo so indexing never adds
    a file to the thing being protected."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(str(root).encode()).hexdigest()[:16]
    cache = CACHE_DIR / f"{key}.json"
    fingerprint = _fingerprint(root)
    if cache.exists():
        try:
            blob = json.loads(cache.read_text())
            fresh = blob.get("fingerprint") == fingerprint
            if not fresh and max_age:
                fresh = time.time() - blob.get("built", 0) < max_age
            if fresh:
                return Index(frozenset(blob["windows"]), frozenset(blob["symbols"]),
                             blob["root"], blob["files"])
        except Exception:
            pass
    idx = build(root)
    try:
        cache.write_text(json.dumps({
            "fingerprint": fingerprint, "built": time.time(),
            "windows": sorted(idx.windows), "symbols": sorted(idx.symbols),
            "root": idx.root, "files": idx.files,
        }))
    except OSError:
        pass
    return idx


def check(text: str, idx: Index) -> tuple[str, int] | None:
    """(kind, count) of what the text shares with the repo, or None if clean.

    Returns counts, never the matched content: this result is printed, and
    echoing the matched text would put proprietary material into a terminal
    log — the same mistake in a smaller place.
    """
    words = _normalise(text)
    verbatim = sum(
        1 for i in range(len(words) - WINDOW + 1)
        if _hash(words[i:i + WINDOW]) in idx.windows)
    if verbatim:
        return ("verbatim", verbatim)
    # Query tokens keep their separators (RELEASE-TAG-9, acme_telemetry/foo)
    # while symbols are extracted without them, so compare the parts too —
    # otherwise every hyphenated or path-shaped identifier slips through.
    candidates = set(words)
    for word in words:
        candidates.update(part for part in re.split(r"[./\-]", word) if part)
    hits = candidates & idx.symbols
    if hits:
        return ("identifiers", len(hits))
    return None
