"""Verification command discovery from repository entry points."""

import json
import re
import shutil
import subprocess
from pathlib import Path


def _gitleaks_command():
    """Return a gitleaks command line that this install actually understands,
    or None. Newer builds use `detect`; older ones only expose `git`."""
    if not shutil.which("gitleaks"):
        return None
    for candidate in (
        ["gitleaks", "detect", "--no-banner"],
        ["gitleaks", "detect"],
        ["gitleaks", "git", "--no-banner", "."],
        ["gitleaks", "git", "."],
    ):
        try:
            probe = subprocess.run(
                candidate + ["--help"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.returncode == 0:
            return " ".join(candidate)
    return None


def detect_smoke_commands(repo):
    """Suggest a cheap between-verification check per detected technology.

    Suggestion only: the loop must not improvise its smoke check every round
    (the choice then differs run to run and is invisible in the config), so
    detect-verify reports these and the operator records one with
    `config-set --smoke-commands`. Only syntax/type-level checks belong here —
    a smoke command that runs the test suite is just the full verification
    wearing a cheaper name."""
    root = Path(repo)
    signals = []
    if (root / "pyproject.toml").exists() or (root / "setup.py").exists() or (root / "pytest.ini").exists() or (root / "setup.cfg").exists():
        signals.append(("python", "python -m compileall -q ."))
    elif _has_pytest_test_files(root):
        signals.append(("python", "python -m compileall -q ."))
    package_json = root / "package.json"
    if package_json.exists():
        # 只荐真实存在的脚本（wave14 candidate-429）：无 lint 键的包被荐
        # npm run lint 后，采纳方每轮冒烟都红。
        try:
            scripts = (json.loads(package_json.read_text(encoding="utf-8"))
                       .get("scripts") or {})
        except (ValueError, OSError):
            scripts = {}
        if "lint" in scripts:
            signals.append(("node", "npm run lint"))
    if (root / "Cargo.toml").exists():
        signals.append(("rust", "cargo check"))
    if (root / "go.mod").exists():
        signals.append(("go", "go vet ./..."))
    return signals


def _has_pytest_test_files(root):
    """True when pytest-style test modules exist without any packaging config.
    Walks the whole tree (any depth, any directory name): the root+tests/-only
    probe missed the common "package dir with test file beside it" and
    "tests/unit/" layouts — detect-verify reported nothing on a repo whose
    5000-line suite lives in scripts/ (wave14 candidate-428). detect-verify is
    a manual command, so the full walk costs nothing in the round loop."""
    for path in root.rglob("*.py"):
        name = path.name
        if name.startswith("test_") or name.endswith("_test"):
            return True
    return False


def detect_verify_commands(repo):
    """Return a list of (technology, command) pairs discovered from repo entry points."""
    root = Path(repo)
    signals = []
    if (root / "pyproject.toml").exists() or (root / "setup.py").exists() or (root / "pytest.ini").exists() or (root / "setup.cfg").exists():
        signals.append(("python", "pytest"))
    elif _has_pytest_test_files(root):
        # Fresh projects grow tests/ before any packaging config lands; the
        # test modules alone are already a pytest signal (1.11 实测：全新仓库
        # 首轮 detect-verify 落空，被迫手动 config-set).
        signals.append(("python", "pytest"))
    if (root / "package.json").exists():
        signals.append(("node", "npm test"))
    if (root / "Cargo.toml").exists():
        signals.append(("rust", "cargo test"))
    if (root / "go.mod").exists():
        signals.append(("go", "go test ./..."))
    if (root / "CMakeLists.txt").exists():
        signals.append(("cmake", "ctest"))
    makefile = root / "Makefile"
    if makefile.exists():
        # Probe only: an unreadable Makefile means "no make signal", not a
        # broken repo — detection must not turn into a failure here.
        try:
            content = makefile.read_text(encoding="utf-8", errors="replace")
            if re.search(r"(?m)^\s*test\s*:", content):
                signals.append(("make", "make test"))
        except OSError:
            pass
    gitleaks = _gitleaks_command()
    if gitleaks:
        signals.append(("secrets", gitleaks))
    if shutil.which("detect-secrets"):
        signals.append(("secrets", "detect-secrets scan"))
    return signals
