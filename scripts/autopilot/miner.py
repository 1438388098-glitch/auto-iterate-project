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
SWALLOW_RE = re.compile(r"except\s*(Exception\s*)?:\s*(?:#.*)?$", re.I)
SWALLOW_PASS_RE = re.compile(
    r"except\s*(Exception\s*)?:\s*\n\s+pass\b",
    re.I,
)

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
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".egg")]
        for name in filenames:
            path = Path(dirpath) / name
            if suffixes is not None and path.suffix.lower() not in suffixes:
                continue
            if suffixes is None and path.suffix.lower() not in SOURCE_SUFFIXES:
                continue
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            yield path


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
            findings.append(_finding(
                "markers",
                "Resolve {} at {}:{}".format(match.group(1).upper(), rel, lineno),
                "Unfinished marker in source; resolve, document, or delete it.",
                "{}:{}: {}".format(rel, lineno, note[:120]),
                file=rel,
                line=lineno,
                suggested_type="bugfix" if match.group(1).upper() in ("FIXME", "HACK") else "docs",
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
        except SyntaxError as exc:
            rel = _rel(repo, path)
            findings.append(_finding(
                "syntax",
                "Fix syntax error in {}".format(rel),
                "File does not compile; the package cannot be imported safely.",
                "{}:{}: {}".format(rel, exc.lineno or 0, (exc.msg or "syntax error")[:120]),
                file=rel,
                line=exc.lineno or 0,
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
        name_l = path.name.lower()
        if "test" in name_l or rel.startswith("tests/") or "/tests/" in rel:
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
        if "test" in path.name.lower() or rel.startswith("tests/") or "/tests/" in rel:
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
    result = io.run_git(repo, "log", "--name-only", "--pretty=format:", "-50")
    if result.returncode != 0:
        return []
    counts = Counter()
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith(" "):
            continue
        if any(part in SKIP_DIRS for part in Path(line).parts):
            continue
        counts[line] += 1
    findings = []
    for rel, n in counts.most_common(limit):
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
            got = SCANNERS[kind](repo, limit=per_kind_limit)
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


def _dedup_key(finding):
    return (
        finding.get("kind"),
        finding.get("file"),
        finding.get("line"),
        (finding.get("title") or "")[:80],
    )


def filter_new_findings(findings, existing_candidates):
    """Drop findings that already exist in the backlog (file:line evidence match)."""
    seen = set()
    for candidate in existing_candidates or []:
        evidence = (candidate.get("evidence") or "")
        title = (candidate.get("title") or "")
        file = candidate.get("file")
        line = candidate.get("line")
        if file and line:
            seen.add((file, int(line)))
        for token in re.findall(r"([\w./-]+):(\d+)", evidence):
            seen.add((token[0], int(token[1])))
        if title:
            seen.add(("title", title[:80]))
    out = []
    local = set()
    for finding in findings:
        key = _dedup_key(finding)
        if key in local:
            continue
        local.add(key)
        if finding.get("file") and finding.get("line"):
            if (finding["file"], int(finding["line"])) in seen:
                continue
        if ("title", (finding.get("title") or "")[:80]) in seen:
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
