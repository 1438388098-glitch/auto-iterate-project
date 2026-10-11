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
    "user-gap",
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
    self_name = Path(__file__).name
    for path in _iter_source_files(repo):
        if path.suffix.lower() == ".md":
            continue
        # Test-fixture markers are sample data, not tech debt: on this repo
        # every single marker finding lived inside a fixture string.
        if _is_test_path(_rel(repo, path)):
            continue
        # The scanner's own syntax-definition file matches MARKER_RE on its
        # regex pattern, comments and docstring — self-referential by
        # construction, never real debt. Same rationale as the fixture skip.
        if path.name == self_name:
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
    """except Exception: pass / bare except: pass swallow failures silently.

    The scanner's own docstring contains both example forms — self-referential
    like scan_markers' regex definitions, so the scanner skips its own file
    (wave14 candidate-427): without the skip every `mine --apply` mints two
    value-4 garbage candidates from this docstring."""
    self_name = Path(__file__).name
    findings = []
    for path in _iter_source_files(repo, suffixes={".py"}):
        if path.name == self_name:
            continue
        # except-pass in test files is usually a mocked-shape fixture; audit
        # product code only (test paths are already excluded from test-gap's
        # corpus and dead-export's defs for the same reason).
        if _is_test_path(_rel(repo, path)):
            continue
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


def _iter_parsable_source_trees(repo, include_tests=False):
    for path in _iter_source_files(repo, suffixes={".py"}):
        if not include_tests and _is_test_path(_rel(repo, path)):
            continue
        try:
            yield ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError, ValueError):
            continue


def _cli_handler_names(repo):
    """Symbols registered as CLI dispatch handlers via ``set_defaults(func=...)``.
    Their coverage lives in CLI-level (subprocess) tests, which the
    direct-symbol-reference checks in scan_test_gap cannot see — reporting them
    as test gaps is systematic false positive noise."""
    names = set()
    for tree in _iter_parsable_source_trees(repo):
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg != "func":
                    continue
                # Both `func=cmd_init` and `func=commands.cmd_init` styles
                # register a dispatch handler.
                if isinstance(keyword.value, ast.Name):
                    names.add(keyword.value.id)
                elif isinstance(keyword.value, ast.Attribute):
                    names.add(keyword.value.attr)
    return names


def _unittest_subclass_names(repo):
    """Class names inheriting from (unittest.)TestCase: instantiated by the
    test loader, so "defined but never referenced" does not apply. Subclasses
    live in test modules and usually reach TestCase through local intermediate
    base classes (RepoTest -> AutopilotTestBase -> TestCase), so the relation
    is closed transitively over the repo's own class graph."""
    bases_of = {}  # class name -> simple names of its bases
    for tree in _iter_parsable_source_trees(repo, include_tests=True):
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for base in node.bases:
                base_name = base.attr if isinstance(base, ast.Attribute) else (
                    base.id if isinstance(base, ast.Name) else None
                )
                if base_name:
                    bases_of.setdefault(node.name, []).append(base_name)
    names = {
        name for name, bases in bases_of.items() if any(b.endswith("TestCase") for b in bases)
    }
    changed = True
    while changed:
        changed = False
        for name, bases in bases_of.items():
            if name not in names and any(b in names for b in bases):
                names.add(name)
                changed = True
    return names


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
    cli_handlers = _cli_handler_names(repo)
    findings = []
    for path in _iter_source_files(repo, suffixes={".py"}):
        rel = _rel(repo, path)
        if _is_test_path(rel):
            continue
        for _kind, name, lineno in _python_top_level_defs(path):
            if name in cli_handlers:
                continue
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


def scan_hotspot(repo, limit=10, before_sha=None):
    """Files with the most recent commit churn — where bugs hide.

    before_sha (a run's run_start_sha) bounds the scan to pre-existing
    history: the run's own fresh commits are the work in progress, not
    pre-existing hotspots (1.12 field test: the scanner flagged files the
    run had just written itself)."""
    # --name-status so deletions (D) can be skipped: suggesting a review of a
    # file that no longer exists is noise.
    log_args = ["log", "--name-status", "--pretty=format:", "-50"]
    if before_sha:
        log_args.append(before_sha)
    result = io.run_git(repo, "-c", "core.quotepath=false", *log_args)
    if result.returncode != 0:
        return []
    counts = Counter()
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith(" "):
            continue
        parts = line.split("\t")
        if not parts or parts[0].startswith("D"):
            continue
        rel = parts[-1].strip()
        if not rel:
            continue
        if any(part.casefold() in SKIP_DIRS_FOLD for part in Path(rel).parts):
            continue
        counts[rel] += 1
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
        if len(findings) >= limit:
            # config.md 承诺 --limit N caps per-kind results——六个兄弟扫描器
            # 都落实了截断，唯独这里静默忽略（轮 21 candidate-320）。
            break
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
    test_cases = _unittest_subclass_names(repo)
    findings = []
    for name, rel, lineno in defs:
        if name in test_cases:
            continue
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


# ---- user-gap（1.16 机制一）：文档宣称 vs 实际入口的确定性差集 ----
# 前瞻供给的第一条确定性来源：产出 suggested_type=feature 的候选，补足
# 「7 个回望式扫描器无一产 feature」的供给空洞。扫描器只供事实，分级从紧
# （effort=3）——文档差集天然显得价值大，价值判断留给消费侧。

USER_GAP_KIND = "user-gap"

# 标题模板是身份的一半（镜像分支从标题反解宣称），改模板必须同步
# _user_gap_identity 的解析。
_CMD_CLAIM_RE = re.compile(r"^docs promise missing command `([^`]+)`$")
_FLAG_CLAIM_RE = re.compile(r"^docs promise missing flag `([^`]+)`$")

# 路线图类小节标题（收得很紧防误报）。
_ROADMAP_HEADING_RE = re.compile(
    r"^#{1,6}\s*(roadmap|planned(?:\s+(?:features?|work|improvements?))?|"
    r"future\s+(?:work|plans?|directions?|ideas?)|coming\s+soon|todo|"
    r"未来计划|后续计划|路线图|规划)\s*[:：]?\s*$",
    re.I,
)
_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.+)$")


def _gap_identity(raw):
    """user-gap 去重身份（1.16 设计必修 1-1）：归一化宣称文本——大小写、
    空白、列表标记、 emphasis 标记、首尾标点全不参与；**行号永不入身份**
    （README 一编辑行号全漂）。_dedup_key 与 _candidate_dedup_keys 镜像
    分支必须共用本函数，两边 key 规则分叉 = 每轮重复入账。"""
    text = re.sub(r"^[\s>*+\-]+", "", raw or "")
    text = re.sub(r"^\d+[.)]\s*", "", text)
    text = re.sub(r"[`*_]", "", text)
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text.strip(" .;:,，。；：")


def _user_gap_identity_from_title(title):
    """从标题反解身份（_dedup_key 与镜像分支同源）。探针 A 的标题带
    模板前缀 + backtick 宣称；探针 B 的标题即宣称原文。"""
    match = _CMD_CLAIM_RE.match(title or "")
    if match:
        return "cmd:{}".format(_gap_identity(match.group(1)))
    match = _FLAG_CLAIM_RE.match(title or "")
    if match:
        return "flag:{}".format(_gap_identity(match.group(1)))
    return "road:{}".format(_gap_identity(title))


def _doc_files_for_gap(root):
    """候选文档，按解析后的真实路径去重——Windows 的 exists() 大小写
    不敏感，README.md 与 readme.md 会命中同一文件，不去重就双份发现。"""
    seen = set()
    for name in ("README.md", "readme.md", "SKILL.md"):
        path = root / name
        if not path.exists():
            continue
        try:
            key = str(path.resolve()).casefold()
        except OSError:
            key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        yield path


def _claimed_cli_usage(text):
    """探针 A：backtick 里对 helper 子命令/旗标的宣称。返回
    [(span_match, command, [flags])]。文件路径宣称归 docs-drift 管（设计
    1-2 职责让位），这里只认命令与旗标。"""
    claims = []
    for match in re.finditer(r"`([^`\n]+)`", text):
        tokens = match.group(1).split()
        helper_idx = None
        for idx, token in enumerate(tokens):
            if "autopilot_state" in token:
                helper_idx = idx
                break
        if helper_idx is None or helper_idx + 1 >= len(tokens):
            continue
        command = tokens[helper_idx + 1]
        if not re.match(r"^[a-z][a-z0-9-]*$", command, re.I):
            continue
        flags = [t for t in tokens[helper_idx + 2:] if t.startswith("-")]
        claims.append((match, command.lower(), flags))
    return claims


def _real_cli_surface():
    """真实命令面。懒加载：cli 在模块层 import miner，反向 import 必须
    延迟到扫描时。返回 (commands, {command: set(option strings)})；
    argparse 不可用（理论路径）时返回空面，扫描器静默降级。"""
    try:
        import argparse
        from . import cli as cli_module
        parser = cli_module.build_parser()
        commands = set()
        flags = {}
        for action in parser._actions:
            if not isinstance(action, argparse._SubParsersAction):
                continue
            for name, sub in (getattr(action, "choices", None) or {}).items():
                commands.add(str(name))
                opts = set()
                for sub_action in getattr(sub, "_actions", []):
                    opts.update(sub_action.option_strings or [])
                flags[str(name)] = opts
        return commands, flags
    except Exception:
        return set(), {}


def _roadmap_findings(text, rel, limit):
    """探针 B：路线图小节下的列表项 = 有文档背书的前瞻意图。标题=宣称
    原文（不含行号，身份才稳定）；effort=3 从紧（设计 1-3）。"""
    findings = []
    in_section = False
    heading = ""
    for idx, line in enumerate(text.splitlines()):
        stripped = line.strip()
        heading_match = _ROADMAP_HEADING_RE.match(stripped)
        if heading_match:
            in_section = True
            heading = re.sub(r"^#+\s*", "", stripped)
            continue
        if in_section and re.match(r"^#{1,6}\s", stripped):
            in_section = False
            continue
        if not in_section:
            continue
        bullet = _BULLET_RE.match(line)
        if not bullet:
            continue
        claim = bullet.group(1).strip()
        if len(_gap_identity(claim)) < 6:
            continue
        findings.append(_finding(
            USER_GAP_KIND,
            claim,
            "Roadmap item under \"{}\" in {}: documented intent with no "
            "implementation behind it — a feature candidate or an explicit "
            "drop from the docs.".format(heading, rel),
            "{}:{}: [{}] {}".format(rel, idx + 1, heading, claim),
            file=rel,
            line=idx + 1,
            suggested_type="feature",
            value=3,
            effort=3,
        ))
        if len(findings) >= limit:
            break
    return findings


def scan_user_gap(repo, limit=15):
    """Docs promise vs shipped surface: claimed helper commands/flags that
    do not exist, and roadmap/planned items with no implementation. The
    forward-looking scanner (1.16 机制一) — the only kind that yields
    feature-type supply from facts instead of judgment. Identity is the
    normalized claim text, never the line number (see _gap_identity)."""
    root = Path(repo)
    findings = []
    commands, flag_surface = _real_cli_surface()
    for doc in _doc_files_for_gap(root):
        try:
            text = doc.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = _rel(repo, doc)
        for span, command, flags in _claimed_cli_usage(text):
            line = text[:span.start()].count("\n") + 1
            evidence = "{}:{}: `{}`".format(rel, line, span.group(1))
            if commands and command not in commands:
                findings.append(_finding(
                    USER_GAP_KIND,
                    "docs promise missing command `{}`".format(command),
                    "{} documents autopilot_state.py {} but the helper has no "
                    "such command — either ship it or fix the doc.".format(rel, command),
                    evidence, file=rel, line=line,
                    suggested_type="feature", value=3, effort=3,
                ))
            else:
                known = flag_surface.get(command) or set()
                for flag in flags:
                    if known and flag not in known:
                        findings.append(_finding(
                            USER_GAP_KIND,
                            "docs promise missing flag `{} {}`".format(command, flag),
                            "{} documents {} {} but the command does not accept "
                            "it.".format(rel, command, flag),
                            evidence, file=rel, line=line,
                            suggested_type="feature", value=3, effort=3,
                        ))
            if len(findings) >= limit:
                return findings
        findings.extend(_roadmap_findings(text, rel, limit - len(findings)))
        if len(findings) >= limit:
            return findings[:limit]
    return findings


SCANNERS = {
    "markers": scan_markers,
    "swallowed": scan_swallowed,
    "syntax": scan_syntax,
    "test-gap": scan_test_gap,
    "hotspot": scan_hotspot,
    "dead-export": scan_dead_export,
    "docs-drift": scan_docs_drift,
    "user-gap": scan_user_gap,
}


def mine_repo(repo, kinds=None, per_kind_limit=20, before_sha=None):
    """Run selected scanners; returns {findings, by_kind, kinds}.

    before_sha bounds the hotspot scanner to pre-existing history (a run's
    run_start_sha): the run's own commits are not pre-existing hotspots."""
    selected = list(kinds) if kinds else list(MINE_KINDS)
    unknown = [k for k in selected if k not in SCANNERS]
    if unknown:
        raise ValueError("unknown mine kinds: {}".format(", ".join(unknown)))
    findings = []
    by_kind = {}
    for kind in selected:
        try:
            # An explicit 0 (or negative) limit yields 0 findings — honored as given.
            if per_kind_limit and per_kind_limit > 0:
                if kind == "hotspot" and before_sha:
                    got = SCANNERS[kind](repo, limit=per_kind_limit, before_sha=before_sha)
                else:
                    got = SCANNERS[kind](repo, limit=per_kind_limit)
            else:
                got = []
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
    if kind == "docs-drift":
        # 每条断链独立（wave14 candidate-430）：evidence 含缺失路径，同一
        # README 引用 N 个死路径是 N 条独立供给，不该塌缩成第一条。
        return ("mine", kind, file, finding.get("evidence") or "")
    if kind in ("hotspot", "syntax"):
        return ("mine", kind, file)
    if kind == "user-gap":
        # 1.16 机制一：身份=归一化宣称文本（_user_gap_identity_from_title），
        # 不含 file/line——同一缺口在 README 与 SKILL 重复宣称是同一条供给，
        # README 行号漂移不改身份。与 _candidate_dedup_keys 镜像分支共用
        # 同一解析，分叉即每轮重复入账。
        return ("mine", kind, _user_gap_identity_from_title(finding.get("title")))
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
    elif kind == "user-gap":
        # 1.16 机制一镜像分支：从标题反解宣称身份（模板见
        # _user_gap_identity_from_title）。此分支若缺失会静默落进下面的
        # docs-drift else——产出不匹配 key、每轮重复入账，反射守卫测试
        # （每个 MINE_KINDS 成员）防的就是这个陷阱。
        keys.add(("mine", kind, _user_gap_identity_from_title(candidate.get("title"))))
    else:  # docs-drift: evidence reads "{readme}: references {missing}"
        if file is None:
            match = re.match(r"(.+): references ", body)
            if match:
                file = match.group(1)
        # wave14 在 finding 侧把 evidence 纳入 key（每条断链独立），镜像当时
        # 没跟上——两侧 arity 不等，round-trip 永远 MISS，去重只剩标题兜底。
        # 1.16 反射守卫顺手对齐：镜像用 body（= finding 侧的 evidence 本体）。
        keys.add(("mine", kind, file, body))
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
