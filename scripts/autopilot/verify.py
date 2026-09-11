"""Verification command discovery from repository entry points."""

import re
import shutil
from pathlib import Path


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
    if shutil.which("gitleaks"):
        signals.append(("secrets", "gitleaks git --no-banner ."))
    if shutil.which("detect-secrets"):
        signals.append(("secrets", "detect-secrets scan"))
    return signals
