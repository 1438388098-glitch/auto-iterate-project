"""Verification command discovery from repository entry points."""

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


def detect_verify_commands(repo):
    """Return a list of (technology, command) pairs discovered from repo entry points."""
    root = Path(repo)
    signals = []
    if (root / "pyproject.toml").exists() or (root / "setup.py").exists() or (root / "pytest.ini").exists() or (root / "setup.cfg").exists():
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
