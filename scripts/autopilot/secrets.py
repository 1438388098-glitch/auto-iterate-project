"""Secret scanning for the staged diff: pattern table, compiled matcher, and masking.

Findings are masked at creation time so secret material never lands in
log.jsonl or stdout (the scan exists to prevent leaks, not to become one)."""

import re
import sys

from . import io

SECRET_PATTERNS = [
    r"AKIA[0-9A-Z]{16}",
    # PKCS#8 writes "ENCRYPTED PRIVATE KEY" — the explicit-alternatives form
    # missed it; accept any words before PRIVATE KEY.
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    # gho_/ghs_/ghu_/ghr_ are live GitHub App/OAuth/Actions/user-to-server/
    # refresh tokens, same 36-char shape as ghp_.
    r"gh[pousr]_[A-Za-z0-9]{36}",
    r"github_pat_[A-Za-z0-9_]{36,}",
    r"xox[baprs]-[A-Za-z0-9-]{10,}",
    r"AIza[0-9A-Za-z_-]{35}",
    # \b: without it every long hyphenated word ending in "sk-" (task-, risk-,
    # disk-) blocked commits, training users to reach for --allow-secrets.
    r"\bsk-[A-Za-z0-9_-]{20,}",
    # wave12 candidate-413 四种常见 CI/配置密钥形态：
    r"https://hooks\.slack\.com/services/[A-Za-z0-9/_-]{20,}",  # Slack 入站 webhook
    r"\bglpat-[A-Za-z0-9_-]{20,}",                              # GitLab PAT
    r"\bGOCSPX-[A-Za-z0-9_-]{20,}",                             # Google OAuth client secret
    r"\bpypi-AgEIcHlwaS5vcmc[A-Za-z0-9_-]{20,}",                # PyPI 发布 token
    r"\bnpm_[A-Za-z0-9]{36,}",                                  # npm 发布/CI token
    # Scoped inline flag keeps this valid on Python 3.11+ where a bare (?i)
    # mid-pattern is rejected; the rest of the table stays case-sensitive.
    r"(?i:api[_-]?key\s*[:=]\s*[\"']?[A-Za-z0-9+/]{20,}[\"']?)",
    r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}",
]

_COMPILED_DEFAULTS = [re.compile(pattern) for pattern in SECRET_PATTERNS]

# 编译期对抗样本（轮 16 candidate-308）：用户自供模式在进入 commit 热路径前
# 先对固定样本限时试跑——stdlib re 无超时且灾难性回溯的匹配持 GIL 不放
# （进程内线程探测会饿死整个解释器），所以探测走子进程：超时即杀、判危险拒绝。
# 实测 `(a+)+b` 对 26 字符行 1.15s 指数增长，64 字符样本对危险模式必然超时。
_PROBE_SNIPPET = (
    "import re,sys\n"
    "p = re.compile(sys.argv[1])\n"
    "p.search('a' * 64)\n"
    "print('ok')\n"
)
_PATTERN_PROBE_TIMEOUT = 2.0


_probe_verdicts = {}


def _pattern_compiles_in_time(pattern, timeout=_PATTERN_PROBE_TIMEOUT):
    """True unless a probe subprocess matching a fixed 64-char adversarial
    sample against the pattern finishes within `timeout` seconds. Results are
    memoized per pattern (one subprocess per distinct pattern per process).
    Environment failures fail OPEN (a missing python must not break secret
    scanning — the probe is a guard, not the scan itself)."""
    cached = _probe_verdicts.get(pattern)
    if cached is not None:
        return cached
    import subprocess
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _PROBE_SNIPPET, pattern],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False
    except OSError:
        return True  # fail-open 是文档化选择（探测是守卫不是扫描本体）
    verdict = proc.returncode == 0 and b"ok" in (proc.stdout or b"")
    _probe_verdicts[pattern] = verdict
    return verdict


def compile_patterns(extra_patterns=None):
    """Compile user-supplied patterns with a clean error instead of a traceback."""
    compiled = []
    for pattern in extra_patterns or []:
        if not pattern:
            continue
        try:
            candidate = re.compile(pattern)
        except re.error as exc:
            print(
                "[ERROR] Invalid regex in secret_patterns ({}): {}. Fix the pattern in "
                ".autopilot/config.json or on the init command line.".format(pattern, exc),
                file=sys.stderr,
            )
            raise SystemExit(2)
        if not _pattern_compiles_in_time(pattern):
            print(
                "[ERROR] secret_patterns entry ({}) is too dangerous to run: it did not finish matching a "
                "64-char adversarial probe within {:.1f}s (possible catastrophic backtracking). Remove or fix the "
                "pattern in .autopilot/config.json or on the init command line.".format(
                    pattern, _PATTERN_PROBE_TIMEOUT),
                file=sys.stderr,
            )
            raise SystemExit(2)
        compiled.append(candidate)
    return compiled


def mask_secret_text(text):
    """Keep enough context to locate the line without exposing the secret."""
    text = (text or "").strip()
    if len(text) <= 12:
        return text[:2] + "..."
    return text[:6] + "..." + text[-4:]


def scan_staged_diff(repo, extra_patterns=None, staged_numstat=None):
    """Scan the added lines of the staged diff for secret-like content. Returns a
    list of findings (pattern, file, masked text), deduplicated per pattern+line.
    Uses -U0 so context lines are not read, and core.quotepath=false so non-ASCII
    file names are reported literally. Fail-closed: if git diff fails, the scan
    reports a synthetic finding instead of pretending the tree is clean. Staged
    BINARY files also produce a finding: their content is unscannable, so a
    binary can only be committed via --allow-secrets (same fail-closed promise).
    `staged_numstat` accepts the caller's shared `git diff --cached --numstat`
    pass (wave10 candidate-389) — it must carry core.quotepath=false for
    literal file names; when given, the binary-detection spawn below is skipped."""
    diff = io.run_git(repo, "-c", "core.quotepath=false", "diff", "--cached", "-U0")
    if diff.returncode != 0:
        detail = (diff.stderr or diff.stdout or "git diff failed").strip()
        print(
            "[ERROR] Secret scan could not read the staged diff ({}). "
            "Refusing to treat the tree as clean.".format(detail),
            file=sys.stderr,
        )
        return [
            {
                "pattern": "git-diff-failure",
                "file": "(staged diff)",
                "text": mask_secret_text(detail[:200]),
            }
        ]
    patterns = _COMPILED_DEFAULTS + compile_patterns(extra_patterns)
    findings = []
    seen = set()
    current_file = None
    prev_line = ""
    for line in diff.stdout.splitlines():
        # File-header state machine: a real "+++ " header always directly
        # follows a "--- " (or "diff --git ") line. An ADDED content line whose
        # text itself starts with "++" shows up as "+++..." in the diff, so
        # matching any "+++"-prefixed line as a header used to let those lines
        # bypass the scan entirely.
        if line.startswith("+++ ") and (
            prev_line.startswith("--- ") or prev_line.startswith("diff --git ")
        ):
            target = line[4:].strip()
            if target == "/dev/null":
                current_file = None
            else:
                if target.startswith("b/"):
                    target = target[2:]
                current_file = io._unquote_git_path(target) if target.startswith('"') else target
        elif line.startswith("+"):
            for compiled in patterns:
                if compiled.search(line):
                    key = (compiled.pattern, line[:80])
                    if key in seen:
                        continue
                    seen.add(key)
                    findings.append(
                        {
                            "pattern": compiled.pattern,
                            "file": current_file,
                            "text": mask_secret_text(line[1:]),
                        }
                    )
        prev_line = line
    if staged_numstat is not None:
        numstat = staged_numstat
    else:
        numstat = io.run_git(repo, "-c", "core.quotepath=false", "diff", "--cached", "--numstat")
    if numstat.returncode == 0:
        for line in numstat.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) >= 3 and parts[0] == "-" and parts[1] == "-":
                binary_file = parts[2]
                key = ("binary-staged", binary_file)
                if key in seen:
                    continue
                seen.add(key)
                findings.append(
                    {
                        "pattern": "binary-staged",
                        "file": binary_file,
                        "text": "binary file staged; content not scannable",
                    }
                )
    return findings
