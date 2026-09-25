"""Deterministic repository mining: turn repo facts into backlog-ready candidates.

The agent-side Deep Expansion lenses are judgment calls; this module is the
supply side — file:line evidence the helper can always produce, so the loop
never depends on the agent inventing work from a blank page. Scanners are
stdlib-only and side-effect free (read-only).
"""

import ast
import os
import re
import subprocess
from collections import Counter
from pathlib import Path

from . import io

# Directories never walked while mining a target repo.
SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".autopilot",
    ".pytest_cache",
    ".mypy_cache",
    ".tox",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".zcode",
    "dist",
    "build",
    "target",
    ".idea",
    ".vscode",
}

# Casefolded once: pruning must also catch Node_Modules / NodeModules spellings.
SKIP_DIRS_FOLD = {d.casefold() for d in SKIP_DIRS}

SOURCE_SUFFIXES = {
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".go",
    ".rs",
    ".java",
    ".rb",
    ".php",
    ".c",
    ".h",
    ".cpp",
    ".hpp",
    ".cs",
    ".swift",
    ".kt",
    ".md",
    ".sh",
    ".ps1",
}

MARKER_RE = re.compile(r"\b(TODO|FIXME|HACK|XXX)\b[:\s-]*(.*)$", re.I)

# Doc extensions whose TODO markers are docs work, not code refactoring.
DOC_SUFFIXES = {".md", ".rst", ".txt"}

# Compile failures with no meaningful line: null bytes, undecodable source.
ENCODING_ERROR_RE = re.compile(r"null bytes|codec|decod", re.I)

# Swallowed exceptions: the pass may share the except line or a later one,
# and the except header may carry a trailing comment (blank/comment-only
# lines in between are allowed, as before).
SWALLOW_PASS_RE = re.compile(
    r"\bexcept\b[^:\n]*:(?:[ \t]*(?:#[^\n]*)?\n)+[ \t]+pass\b"
    r"|\bexcept\b[^:\n]*:[ \t]*pass\b",
    re.I,
)

# Test modules by basename; names merely containing "test" (contest.py) do not count.
TEST_MODULE_RE = re.compile(r"(?:^|/)(?:test_[^/]*|[^/]*_test|test)\.py$", re.I)
TEST_DIR_NAMES = ("test", "tests")

MINE_KINDS = (
    "markers",
    "swallowed",
    "syntax",
    "test-gap",
    "hotspot",
    "dead-export",
    "docs-drift",
)


def _iter_source_files(repo, suffixes=None):
    root = Path(repo)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if d.casefold() not in SKIP_DIRS_FOLD and not d.startswith(".egg")
        ]
        for name in filenames:
            path = Path(dirpath) / name
            if suffixes is not None and path.suffix.lower() not in suffixes:
                continue
            if suffixes is None and path.suffix.lower() not in SOURCE_SUFFIXES:
                continue
            if any(part.casefold() in SKIP_DIRS_FOLD for part in path.parts):
                continue
            yield path


def _is_test_path(rel):
    """True for test modules (test_*.py / *_test.py / test.py) and anything
    under a tests/ or test/ directory component."""
    norm = rel.replace("\\", "/").lower()
    if TEST_MODULE_RE.search(norm):
        return True
    return any(part in TEST_DIR_NAMES for part in norm.split("/")[:-1])


def _rel(repo, path):
    try:
        return str(Path(path).resolve().relative_to(Path(repo).resolve())).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def _finding(kind, title, reason, evidence, file=None, line=None,
             suggested_type="bugfix", value=3, effort=2, risk=1):
    return {
        "kind": kind,
        "title": title[:200],
        "reason": reason[:500],
        "evidence": evidence[:500],
        "file": file,
        "line": line,
        "suggested_type": suggested_type,
        "value": value,
        "effort": effort,
        "risk": risk,
    }


def scan_markers(repo, limit=30):
    """TODO/FIXME/HACK/XXX markers with file:line."""
    findings = []
    for path in _iter_source_files(repo):
        if path.suffix.lower() == ".md":
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            match = MARKER_RE.search(line)
            if not match:
                continue
            note = match.group(2).strip() or line.strip()
            rel = _rel(repo, path)
            marker = match.group(1).upper()
            if marker in ("FIXME", "HACK"):
                suggested = "bugfix"
            elif path.suffix.lower() in DOC_SUFFIXES:
                suggested = "docs"
            else:
                suggested = "refactor"
            findings.append(_finding(
                "markers",
                "Resolve {} at {}:{}".format(marker, rel, lineno),
                "Unfinished marker in source; resolve, document, or delete it.",
                "{}:{}: {}".format(rel, lineno, note[:120]),
                file=rel,
                line=lineno,
                suggested_type=suggested,
                value=3,
                effort=2,
            ))
            if len(findings) >= limit:
                return findings
    return findings


def scan_swallowed(repo, limit=20):
    """except Exception: pass / bare except: pass swallow failures silently."""
    findings = []
    for path in _iter_source_files(repo, suffixes={".py"}):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in SWALLOW_PASS_RE.finditer(text):
            lineno = text[: match.start()].count("\n") + 1
            rel = _rel(repo, path)
            findings.append(_finding(
                "swallowed",
                "Stop swallowing exceptions at {}:{}".format(rel, lineno),
                "Silent except-pass hides failures from tests and operators.",
                "{}:{}: {}".format(rel, lineno, match.group(0).splitlines()[0].strip()[:120]),
                file=rel,
                line=lineno,
                suggested_type="bugfix",
                value=4,
                effort=2,
            ))
            if len(findings) >= limit:
                return findings
    return findings


def scan_syntax(repo, limit=20):
    """Python files that fail py_compile (and a cheap JS parse via node --check)."""
    findings = []
    for path in _iter_source_files(repo, suffixes={".py"}):
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        try:
            compile(source, str(path), "exec")
        except (SyntaxError, ValueError) as exc:
            rel = _rel(repo, path)
            if isinstance(exc, SyntaxError):
                msg = exc.msg or "syntax error"
                lineno = exc.lineno
            else:  # undecodable source: null bytes, bad codec
                msg = str(exc) or "source cannot be decoded"
                lineno = None
            if ENCODING_ERROR_RE.search(msg):
                # Encoding failures have no real line — none is fabricated.
                findings.append(_finding(
                    "syntax",
                    "Fix encoding problem in {}".format(rel),
                    "File is not decodable as Python source (null bytes or wrong codec); it cannot compile.",
                    "{}: {}".format(rel, msg[:120]),
                    file=rel,
                    line=None,
                    suggested_type="bugfix",
                    value=5,
                    effort=2,
                ))
            else:
                findings.append(_finding(
                    "syntax",
                    "Fix syntax error in {}".format(rel),
                    "File does not compile; the package cannot be imported safely.",
                    "{}:{}: {}".format(rel, lineno or 0, msg[:120]),
                    file=rel,
                    line=lineno or 0,
                    suggested_type="bugfix",
                    value=5,
                    effort=2,
                ))
            if len(findings) >= limit:
                return findings
    return findings


def _python_top_level_defs(path):
    """(kind, name, lineno) for module-level def/class, or [] when unparsable."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(text, filename=str(path))
    except (OSError, SyntaxError, ValueError):
        return []
    out = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name.startswith("_"):
                continue
            out.append((type(node).__name__, node.name, node.lineno))
    return out


def scan_test_gap(repo, limit=25):
    """Public Python defs/classes with no obvious test reference."""
    root = Path(repo)
    test_blob = []
    test_files = []
    for path in _iter_source_files(repo, suffixes={".py"}):
        rel = _rel(repo, path)
        if _is_test_path(rel):
            try:
                test_blob.append(path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                pass
            test_files.append(rel)
    if not test_files:
        # No tests at all: report that as one high-value finding rather than N.
        sources = [p for p in _iter_source_files(repo, suffixes={".py"})]
        if sources:
            rel = _rel(repo, sources[0])
            return [_finding(
                "test-gap",
                "Add a test suite for this Python package",
                "No test files discovered; regressions are invisible to verify rounds.",
                "no test*.py / tests/ package found (sample source: {})".format(rel),
                file=rel,
                suggested_type="test",
                value=5,
                effort=4,
            )]
        return []

    corpus = "\n".join(test_blob)
    findings = []
    for path in _iter_source_files(repo, suffixes={".py"}):
        rel = _rel(repo, path)
        if _is_test_path(rel):
            continue
        for _kind, name, lineno in _python_top_level_defs(path):
            if name not in corpus:
                findings.append(_finding(
                    "test-gap",
                    "Cover public {} `{}` with tests".format(_kind.lower(), name),
                    "No test file references this public symbol.",
                    "{}:{}: {} {}".format(rel, lineno, _kind.lower(), name),
                    file=rel,
                    line=lineno,
                    suggested_type="test",
                    value=4,
                    effort=3,
                ))
                if len(findings) >= limit:
                    return findings
    return findings


def scan_hotspot(repo, limit=10):
    """Files with the most recent commit churn — where bugs hide."""
    result = io.run_git(repo, "-c", "core.quotepath=false", "log", "--name-only", "--pretty=format:", "-50")
    if result.returncode != 0:
        return []
    counts = Counter()
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith(" "):
            continue
        if any(part.casefold() in SKIP_DIRS_FOLD for part in Path(line).parts):
            continue
        counts[line] += 1
    findings = []
    for rel, n in counts.most_common():
        if n < 2:
            break  # descending order: a single commit is churn noise, not a hotspot
        findings.append(_finding(
            "hotspot",
            "Review hotspot {} ({} recent commits)".format(rel, n),
            "High-churn file: prioritize tests, docs, and cleanup here first.",
            "git log --name-only: {} touched {} of last 50 commits".format(rel, n),
            file=rel,
            suggested_type="test" if n >= 5 else "refactor",
            value=3 if n < 5 else 4,
            effort=3,
            risk=2,
        ))
    return findings


def scan_dead_export(repo, limit=15):
    """Python public names defined in packages but never referenced elsewhere."""
    defs = []  # (name, rel, lineno)
    chunks = []
    for path in _iter_source_files(repo, suffixes={".py"}):
        rel = _rel(repo, path)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        chunks.append(text)
        for _kind, name, lineno in _python_top_level_defs(path):
            if name.startswith("test_"):
                continue
            defs.append((name, rel, lineno))
    corpus = "\n".join(chunks)
    findings = []
    for name, rel, lineno in defs:
        # Count occurrences outside the defining line.
        occurrences = len(re.findall(r"\b" + re.escape(name) + r"\b", corpus))
        if occurrences <= 1:
            findings.append(_finding(
                "dead-export",
                "Remove or wire up unused public symbol `{}`".format(name),
                "Defined once and never referenced — dead code or unfinished feature.",
                "{}:{}: {}".format(rel, lineno, name),
                file=rel,
                line=lineno,
                suggested_type="refactor",
                value=3,
                effort=2,
            ))
            if len(findings) >= limit:
                break
    return findings


def scan_docs_drift(repo, limit=10):
    """README documents commands/modules that are missing (and vice versa light check)."""
    root = Path(repo)
    findings = []
    readme = None
    for candidate in ("README.md", "readme.md", "references/overview.md"):
        path = root / candidate
        if path.exists():
            readme = path
            break
    if readme is None:
        return findings
    try:
        text = readme.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return findings
    # Scripts referenced in the README should exist.
    for match in re.finditer(r"scripts/([A-Za-z0-9_./-]+\.py)", text):
        rel_path = "scripts/" + match.group(1)
        if not (root / rel_path).exists():
            findings.append(_finding(
                "docs-drift",
                "Fix README reference to missing {}".format(rel_path),
                "Documentation points at a file that is not in the repo.",
                "{}: references {}".format(_rel(repo, readme), rel_path),
                file=_rel(repo, readme),
                suggested_type="docs",
                value=3,
                effort=1,
            ))
            if len(findings) >= limit:
                return findings
    return findings


SCANNERS = {
    "markers": scan_markers,
    "swallowed": scan_swallowed,
    "syntax": scan_syntax,
    "test-gap": scan_test_gap,
    "hotspot": scan_hotspot,
    "dead-export": scan_dead_export,
    "docs-drift": scan_docs_drift,
}


def mine_repo(repo, kinds=None, per_kind_limit=20):
    """Run selected scanners; returns {findings, by_kind, kinds}."""
    selected = list(kinds) if kinds else list(MINE_KINDS)
    unknown = [k for k in selected if k not in SCANNERS]
    if unknown:
        raise ValueError("unknown mine kinds: {}".format(", ".join(unknown)))
    findings = []
    by_kind = {}
    for kind in selected:
        try:
            # An explicit 0 (or negative) limit yields 0 findings — honored as given.
            got = SCANNERS[kind](repo, limit=per_kind_limit) if per_kind_limit and per_kind_limit > 0 else []
        except OSError:
            got = []
        by_kind[kind] = len(got)
        findings.extend(got)
    return {
        "kinds": selected,
        "findings": findings,
        "by_kind": by_kind,
        "count": len(findings),
    }


def _evidence_body(finding):
    """Scanner evidence minus its leading 'file:line: ' prefix."""
    prefix = "{}:{}: ".format(finding.get("file"), finding.get("line"))
    body = finding.get("evidence") or ""
    if body.startswith(prefix):
        return body[len(prefix):].strip()
    return body.strip()


def _dedup_key(finding):
    """Line-stable identity for a finding, prefixed ('mine', ...) to keep the
    key space disjoint from title/file:line matches. Line numbers drift as
    files are edited, so markers/swallowed key on the marker text,
    hotspot/syntax/docs-drift on the file, and test-gap/dead-export on the
    symbol. Unknown kinds keep the old line-anchored identity."""
    kind = finding.get("kind")
    file = finding.get("file")
    if kind in ("markers", "swallowed"):
        return ("mine", kind, file, _evidence_body(finding)[:120])
    if kind in ("hotspot", "syntax", "docs-drift"):
        return ("mine", kind, file)
    if kind in ("test-gap", "dead-export"):
        match = re.search(r"`([^`]+)`", finding.get("title") or "")
        symbol = match.group(1) if match else (finding.get("title") or "")[:80]
        return ("mine", kind, file, symbol)
    return ("mine", kind, file, finding.get("line"), (finding.get("title") or "")[:80])


def _candidate_dedup_keys(candidate):
    """Mirror of _dedup_key rebuilt from a stored backlog candidate. Mine
    candidates carry file/line; older ones are recovered from the evidence
    text, whose leading formats are scanner-owned constants."""
    keys = set()
    kind = candidate.get("from_mine")
    evidence = candidate.get("evidence") or ""
    if not kind and " | " in evidence:
        head = evidence.split(" | ", 1)[0]
        if head in MINE_KINDS:
            kind = head
    if kind not in MINE_KINDS:
        return keys
    body = evidence.split(" | ", 1)[1] if " | " in evidence else ""
    file = candidate.get("file")
    if kind in ("markers", "swallowed", "syntax", "test-gap", "dead-export"):
        lead = re.match(r"([\w./-]+):(\d+): ", body)
        if lead:
            file = file or lead.group(1)
            if kind in ("markers", "swallowed"):
                body = body[lead.end():]
        if kind in ("markers", "swallowed"):
            keys.add(("mine", kind, file, body.strip()[:120]))
        elif kind == "syntax":
            keys.add(("mine", kind, file))
        else:  # test-gap / dead-export: symbol from the backticked title
            match = re.search(r"`([^`]+)`", candidate.get("title") or "")
            symbol = match.group(1) if match else (candidate.get("title") or "")[:80]
            keys.add(("mine", kind, file, symbol))
    elif kind == "hotspot":
        if file is None:
            match = re.search(r"git log --name-only: (.+) touched \d+ of last 50 commits", body)
            if match:
                file = match.group(1)
        keys.add(("mine", kind, file))
    else:  # docs-drift: evidence reads "{readme}: references {missing}"
        if file is None:
            match = re.match(r"(.+): references ", body)
            if match:
                file = match.group(1)
        keys.add(("mine", kind, file))
    return keys


def filter_new_findings(findings, existing_candidates):
    """Drop findings that already exist in the backlog. Identity is
    line-stable (see _dedup_key): a marker that drifted a few lines or a
    hotspot whose churn count changed is the same problem, not a new one.
    markers/swallowed dedup on the marker text alone — their titles embed a
    line number and must not shadow a changed note on the same line. Other
    kinds also honor exact title matches (covers legacy candidates such as the
    suite-level test gap); unknown kinds keep the old file:line identity."""
    seen = set()
    for candidate in existing_candidates or []:
        seen.update(_candidate_dedup_keys(candidate))
        if candidate.get("title"):
            seen.add(("title", candidate["title"][:80]))
        file = candidate.get("file")
        line = candidate.get("line")
        if file and line:
            seen.add(("pair", file, int(line)))
        for token in re.findall(r"([\w./-]+):(\d+)", candidate.get("evidence") or ""):
            seen.add(("pair", token[0], int(token[1])))
    out = []
    local = set()
    for finding in findings:
        key = _dedup_key(finding)
        if key in local:
            continue
        local.add(key)
        if key in seen:
            continue
        kind = finding.get("kind")
        if kind not in ("markers", "swallowed"):
            title = (finding.get("title") or "")[:80]
            if title and ("title", title) in seen:
                continue
            if kind not in MINE_KINDS and finding.get("file") and finding.get("line"):
                if ("pair", finding["file"], int(finding["line"])) in seen:
                    continue
        out.append(finding)
    return out


def finding_to_candidate_fields(finding):
    """Map a finding onto backlog-add fields."""
    return {
        "title": finding["title"],
        "reason": finding["reason"],
        "value": finding.get("value", 3),
        "effort": finding.get("effort", 2),
        "type": finding.get("suggested_type", "bugfix"),
        "risk": finding.get("risk", 1),
        "origin": "observed",
        "confidence": 1.0,
        "evidence": "{} | {}".format(finding.get("kind"), finding.get("evidence")),
    }
