"""Low-level IO, git, time, and lock helpers. No imports from sibling modules."""

import csv
import hashlib
import io as _stdlib_io
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

AUTOPILOT_DIR = ".autopilot"
CONFIG_FILENAME = "config.json"
STATE_FILENAME = "state.json"
BACKLOG_FILENAME = "backlog.json"
LOCK_FILENAME = "lock"
LOG_FILENAME = "log.jsonl"
ANALYSIS_FILENAME = "analysis.json"
DIRECTIVES_FILENAME = "directives.json"
RETROSPECTIVE_FILENAME = "retrospective.md"
PHASE_REPORT_PREFIX = "phase-report-round-"

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

DEFAULT_MAX_ROUNDS = 10
SCHEMA_VERSION = 6

# Goal-event / direction-seed sizing constants (single authority — do not
# hardcode these elsewhere). Seeds and events live in state.json with bounded
# growth so long-running expansion loops never bloat the state file.
GOAL_EVENTS_LIMIT = 20
SEEDS_LIMIT = 50
SEED_TEXT_LIMIT = 500
EXPANSION_WAVES_LIMIT = 20

# Run/state sizing constants (single authority — do not hardcode these elsewhere).
RUN_ID_LENGTH = 12
HISTORY_LIMIT = 100
HISTORY_TEXT_LIMIT = 2000
PHASE_REPORT_INTERVAL = 10
PHASE_REPORT_KEEP = 3
LOG_ROTATE_BYTES = 5 * 1024 * 1024
LOG_ROTATE_KEEP = 3
TASKLIST_TIMEOUT = 3

# Token estimation (single authority; consumed by io.estimate_tokens_for_round).
TOKEN_BASE = 500
TOKENS_PER_TEXT_LINE = 12
TOKENS_PER_BINARY_FILE = 100


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def parse_time(value):
    """Parse an ISO-8601 timestamp, tolerant of Python 3.6 (no colon in tzoffset)
    and of the colon form written by now_iso(). Returns None when unparseable."""
    if not value:
        return None
    normalized = value
    # Only a trailing Z is the UTC designator; a mid-string Z must not be rewritten.
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    if len(normalized) >= 6:
        tail = normalized[-6:]
        if (
            tail[0] in "+-"
            and tail[3] == ":"
            and tail[1:3].isdigit()
            and tail[4:6].isdigit()
        ):
            normalized = normalized[:-6] + tail[:3] + tail[4:]
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(normalized, fmt)
        except ValueError:
            continue
    return None


def parse_deadline(value):
    """Resolve a deadline/timer expression into an ISO-8601 timestamp, or None when
    unparseable. Accepts (whitespace-insensitive):
      - ISO-8601 timestamps (with or without a timezone offset; the colon form and
        the trailing-Z form written by now_iso() both work).
      - relative durations from now: ``+30m`` / ``+30min`` / ``+8h`` / ``+1d`` /
        ``+2w`` (also spelled-out minutes/hours/days/weeks).
      - a wall-clock time ``HH:MM`` in local time, meaning today at that time, or
        tomorrow at that time when it is already past (e.g. ``08:00`` for
        "tomorrow morning").
    The returned value is an absolute UTC ISO timestamp, so re-reading the config
    never shifts the timer."""
    if not value:
        return None
    expr = value.strip()
    if not expr:
        return None
    if expr.startswith("+"):
        match = re.match(
            r"^\+\s*([0-9]+(?:\.[0-9]+)?)\s*(min|mins|minute|minutes|m|h|hour|hours|hr|d|day|days|w|week|weeks)$",
            expr,
            re.IGNORECASE,
        )
        if not match:
            return None
        amount = float(match.group(1))
        unit = match.group(2).lower()
        try:
            if unit in ("min", "mins", "minute", "minutes", "m"):
                delta = timedelta(minutes=amount)
            elif unit in ("h", "hour", "hours", "hr"):
                delta = timedelta(hours=amount)
            elif unit in ("d", "day", "days"):
                delta = timedelta(days=amount)
            else:
                delta = timedelta(weeks=amount)
            return (datetime.now(timezone.utc) + delta).isoformat()
        except (OverflowError, OSError, ValueError):
            # An absurd duration (30-digit weeks) must reach the caller's
            # "unparseable deadline" path, not crash init with a traceback.
            return None
    parsed = parse_time(expr)
    if parsed is not None:
        return parsed.isoformat()
    # Accept naive ISO timestamps by treating them as UTC (parse_time requires an offset).
    parsed = parse_time(expr + "+00:00")
    if parsed is not None:
        return parsed.isoformat()
    match = re.match(r"^([01]?[0-9]|2[0-3]):([0-5][0-9])$", expr)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        now = datetime.now().astimezone()
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return target.astimezone(timezone.utc).isoformat()
    return None


def load_json(path, default=None):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError) as exc:
        print("[ERROR] Failed to read {}: {}".format(path, exc), file=sys.stderr)
        raise SystemExit(2)


def save_json(path, data):
    """Atomically write JSON via a temp file in the same directory, flushed and
    fsynced before the rename (allow_nan=False also fail-closes on NaN/inf
    payloads instead of writing JSON other parsers reject)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, str(path))
    except BaseException:
        # Best-effort temp cleanup: a failed unlink here must not mask the
        # original error that triggered it (re-raised below).
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def run_git(repo, *args, timeout=120):
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        result = subprocess.run(
            ["git", "-C", str(repo)] + list(args),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            env=env,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        print("[ERROR] git command timed out: git {}".format(" ".join(list(args))), file=sys.stderr)
        raise SystemExit(2)
    except OSError as exc:
        print("[ERROR] git is not available: {}".format(exc), file=sys.stderr)
        raise SystemExit(2)
    return result


def git_dir_for(repo):
    result = run_git(repo, "rev-parse", "--absolute-git-dir")
    if result.returncode != 0:
        print("[ERROR] Not a git repository: {}".format(repo), file=sys.stderr)
        raise SystemExit(2)
    return Path(result.stdout.strip())


def current_branch(repo):
    # symbolic-ref works for normal and unborn branches; detached HEAD returns "HEAD".
    result = run_git(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
    if result.returncode != 0:
        return "HEAD"
    return result.stdout.strip()


def branch_exists(repo, branch):
    result = run_git(repo, "rev-parse", "--verify", "--quiet", "refs/heads/" + branch)
    return result.returncode == 0


def has_commits(repo):
    return run_git(repo, "rev-parse", "--verify", "-q", "HEAD").returncode == 0


def _is_autopilot_path(path):
    if not path:
        return False
    if path == AUTOPILOT_DIR:
        return True
    return path.startswith(AUTOPILOT_DIR + "/") or path.startswith(AUTOPILOT_DIR + "\\")


_GIT_QUOTED_ESCAPE_RE = re.compile(r"\\([0-7]{3})|\\(.)")


def _unquote_git_path(path):
    """Undo git's C-style path quoting. Octal escapes encode raw UTF-8 bytes, so
    decode byte-wise (latin-1 round-trip) and re-decode as UTF-8 — the old
    ``unicode_escape`` approach produced mojibake for non-ASCII names."""
    if not path.startswith('"'):
        return path

    def repl(match):
        if match.group(1):
            return chr(int(match.group(1), 8))
        return {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}.get(
            match.group(2), match.group(2)
        )

    unescaped = _GIT_QUOTED_ESCAPE_RE.sub(repl, path.strip('"'))
    try:
        return unescaped.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return unescaped


def _porcelain_entries(repo):
    result = run_git(repo, "-c", "core.quotepath=false", "status", "--porcelain")
    if result.returncode != 0:
        print(
            "[ERROR] git status failed (exit {}): {}".format(
                result.returncode, result.stderr.strip() or "unknown error"
            ),
            file=sys.stderr,
        )
        raise SystemExit(2)
    entries = []
    for line in result.stdout.splitlines():
        if len(line) < 3:
            continue
        code = line[:2]
        path = _unquote_git_path(line[3:])
        entries.append((code, path))
    return entries


def working_tree_dirty(repo, ignore_autopilot=True):
    """True when the working tree has changes the skill cares about. Changes under
    .autopilot/ are ignored so track_state:true (which versions .autopilot) never
    makes the tree appear dirty."""
    for code, path in _porcelain_entries(repo):
        if ignore_autopilot and _is_autopilot_path(path):
            continue
        return True
    return False


def uncommitted_paths(repo):
    """Repo-relative paths with uncommitted changes, same filter as
    working_tree_dirty (.autopilot/ ignored). Callers that only need a boolean
    should use working_tree_dirty; this is for WARN output that names files."""
    return [path for code, path in _porcelain_entries(repo) if not _is_autopilot_path(path)]


def tracked_changes(repo):
    """True when tracked files are modified/staged/deleted (untracked files and
    .autopilot/ are ignored). Used to refuse branch switches that would carry
    user changes across branches."""
    for code, path in _porcelain_entries(repo):
        if code.startswith("??"):
            continue
        if _is_autopilot_path(path):
            continue
        return True
    return False


def git_identity_ok(repo):
    result = run_git(repo, "config", "--get-regexp", r"^user\.(name|email)$")
    name = email = ""
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            key, _, value = line.partition(" ")
            if key == "user.name":
                name = value.strip()
            elif key == "user.email":
                email = value.strip()
    return bool(name and email), name, email


def lock_path_for(repo):
    return repo / AUTOPILOT_DIR / LOCK_FILENAME


def _pid_alive(pid):
    if not pid:
        return False
    if os.name == "nt":
        # os.kill(pid, 0) terminates the process on some Windows Python builds,
        # so query tasklist instead and parse the CSV output exactly.
        try:
            result = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH", "/FI", "PID eq {}".format(pid)],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                universal_newlines=True,
                encoding="utf-8",
                errors="replace",
                timeout=TASKLIST_TIMEOUT,
            )
        except (OSError, subprocess.SubprocessError):
            return True
        try:
            rows = list(csv.reader(_stdlib_io.StringIO(result.stdout)))
        except Exception:
            # Unparseable output: treat the process as alive so a live lock is
            # never deleted based on missing information.
            return True
        for row in rows:
            if len(row) >= 2 and row[1].strip() == str(pid):
                return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        # EPERM means the process exists but belongs to another user: treating
        # it as dead would delete a live run's lock. Windows already fails
        # closed on tasklist trouble; align the POSIX branch.
        return True
    except OSError:
        return False


def _read_lock_file(path):
    try:
        raw = path.read_text(encoding="utf-8-sig")
        holder = json.loads(raw)
        if isinstance(holder, dict):
            return holder
        return None
    except (OSError, ValueError):
        return None


def _create_lock_exclusive(path):
    """Atomically create the lock file (O_CREAT|O_EXCL). Returns False when the
    file already exists — never overwrites another process's lock."""
    payload = json.dumps(
        {"pid": os.getpid(), "started_at": now_iso(), "hostname": socket.gethostname()}
    )
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
    except OSError:
        try:
            os.unlink(str(path))
        except OSError:
            pass
        raise
    return True


def _acquire_lock(repo):
    path = lock_path_for(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        if _create_lock_exclusive(path):
            return path
        holder = _read_lock_file(path)
        if isinstance(holder, dict):
            hostname = holder.get("hostname")
            same_host = not hostname or hostname == socket.gethostname()
            if same_host and _pid_alive(holder.get("pid")):
                print(
                    "[ERROR] Another autopilot run appears active (pid {}). "
                    "Refusing to modify state. If that process is dead, delete {}.".format(
                        holder.get("pid"), path
                    ),
                    file=sys.stderr,
                )
                raise SystemExit(2)
            if not same_host:
                # Shared/network disk: a live lock from another host must not be
                # deleted. Only a clearly dead local pid or corrupt lock is stale.
                print(
                    "[ERROR] Autopilot lock at {} is held by host {} (pid {}). "
                    "Refusing to delete a lock from another host. If that run is dead, "
                    "remove the lock file manually.".format(
                        path, hostname, holder.get("pid")
                    ),
                    file=sys.stderr,
                )
                raise SystemExit(2)
            print(
                "[WARN] Removing stale autopilot lock left by pid {}.".format(holder.get("pid")),
                file=sys.stderr,
            )
        else:
            print("[WARN] Removing corrupt autopilot lock file at {}.".format(path), file=sys.stderr)
        try:
            path.unlink()
        except OSError:
            pass
    print(
        "[ERROR] Could not acquire the autopilot lock at {} after retrying.".format(path),
        file=sys.stderr,
    )
    raise SystemExit(2)


def _release_lock(path):
    try:
        path.unlink()
    except OSError:
        pass


@contextmanager
def run_lock(repo):
    path = _acquire_lock(repo)
    try:
        yield
    finally:
        _release_lock(path)


def append_log(repo, event, status, **fields):
    entry = {"ts": now_iso(), "event": event, "status": status}
    entry.update(fields)
    path = repo / AUTOPILOT_DIR / LOG_FILENAME
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > LOG_ROTATE_BYTES:
            for index in range(LOG_ROTATE_KEEP - 1, 0, -1):
                older = Path(str(path) + ".{}".format(index))
                newer = Path(str(path) + ".{}".format(index + 1))
                if older.exists():
                    os.replace(str(older), str(newer))
            os.replace(str(path), str(path) + ".1")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:
        print("[WARN] Failed to append to {}: {}".format(path, exc), file=sys.stderr)


def _parse_numstat(stdout):
    """Split `git diff --numstat` output into (text_lines, binary_count).
    Binary files report `-` for both columns and previously slipped past
    max_round_scope and token accounting entirely."""
    text = 0
    binary = 0
    for line in stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0] == "-" and parts[1] == "-":
            binary += 1
        elif len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            text += int(parts[0]) + int(parts[1])
    return text, binary


def estimate_tokens_for_round(repo, run_start_sha, billed_text, billed_binary):
    """Run-level monotonic accounting: each line is billed exactly once.

    The run's total changed units are the committed diff from the run's start
    SHA to HEAD plus the current working-tree/index delta against HEAD. The
    two measurements are disjoint (committed lines left the worktree diff when
    they were committed), so their sum is the run's true total and
    re-measuring it after a commit can never double count lines an earlier
    round already billed while they were still uncommitted. The round is
    charged only the delta above the run's monotonic high-water marks
    (billed_text / billed_binary, persisted in state by _close_round); the
    TOKEN_BASE is charged every round that closes with real work. Binary files
    are charged a flat cost because line counts are meaningless for them.
    Returns (tokens, total_text, total_binary)."""
    committed_t = committed_b = 0
    base = run_start_sha or EMPTY_TREE
    if has_commits(repo):
        result = run_git(repo, "diff", "--numstat", base, "HEAD")
        if result.returncode == 0:
            committed_t, committed_b = _parse_numstat(result.stdout)
    worktree_t, worktree_b = worktree_units(repo)
    total_text = committed_t + worktree_t
    total_binary = committed_b + worktree_b
    delta_text = max(0, total_text - (billed_text or 0))
    delta_binary = max(0, total_binary - (billed_binary or 0))
    # TOKEN_BASE only applies to rounds that actually introduce new units.
    # A verify-only / commit-only close with delta 0 must not drain max_tokens.
    if delta_text or delta_binary:
        tokens = TOKEN_BASE + delta_text * TOKENS_PER_TEXT_LINE + delta_binary * TOKENS_PER_BINARY_FILE
    else:
        tokens = 0
    return tokens, total_text, total_binary


def worktree_units(repo):
    """(text_lines, binary_files) changed in the working tree and the index
    combined, measured against HEAD. Tuple form of worktree_change_lines."""
    text = 0
    binary = 0
    for diff_args in (("diff", "--numstat"), ("diff", "--cached", "--numstat")):
        result = run_git(repo, *diff_args)
        if result.returncode == 0:
            t, b = _parse_numstat(result.stdout)
            text += t
            binary += b
    return text, binary


def worktree_change_lines(repo):
    """Total changed units (text lines + binary files) in the working tree and the
    index combined, measured against HEAD. Used by the zero-work cancel
    threshold and the round's worktree snapshot; the tuple form
    (worktree_units) feeds run-level token accounting."""
    text, binary = worktree_units(repo)
    return text + binary


def git_push(repo):
    """Push the current branch to its upstream remote (or the first remote) using an
    explicit non-force refspec. Never pushes unrelated branches or hidden refs."""
    branch = current_branch(repo)
    if branch == "HEAD":
        return None, "Cannot push a detached HEAD."
    remote = None
    upstream = run_git(repo, "config", "--get", "branch.{}.remote".format(branch))
    if upstream.returncode == 0 and upstream.stdout.strip():
        remote = upstream.stdout.strip()
    else:
        remotes = run_git(repo, "remote")
        if remotes.returncode == 0 and remotes.stdout.strip():
            names = remotes.stdout.strip().splitlines()
            # Alphabetical order alone can select a fork over origin (the
            # standard contribution setup) and push somewhere the user never
            # intended. Prefer the conventional names first.
            remote = names[0]
            for preferred in ("origin", "upstream"):
                if preferred in names:
                    remote = preferred
                    break
    if not remote:
        return None, "No git remote is configured."
    result = run_git(repo, "push", remote, "{}:{}".format(branch, branch))
    if result.returncode != 0:
        return None, result.stderr.strip()
    return result.stdout.strip(), None


def ensure_git_exclude(git_dir, tracked, to_stderr=False):
    def say(msg):
        if to_stderr:
            print(msg, file=sys.stderr)
        else:
            print(msg)

    if tracked:
        say("[SKIP] track_state enabled; .autopilot/ is not excluded and may be committed.")
        return
    info_dir = git_dir / "info"
    info_dir.mkdir(parents=True, exist_ok=True)
    exclude_path = info_dir / "exclude"
    lines = []
    if exclude_path.exists():
        lines = exclude_path.read_text(encoding="utf-8").splitlines()
    if ".autopilot/" not in lines:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(AUTOPILOT_DIR + "/")
        exclude_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        say("[OK] Added .autopilot/ to .git/info/exclude")


def autopilot_file_for(repo, filename):
    return repo / AUTOPILOT_DIR / filename


def file_sha256(path):
    """SHA-256 hex digest of a file's bytes, or None when unreadable."""
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def home_dir():
    return Path(os.environ.get("USERPROFILE") or os.environ.get("HOME") or str(Path.home()))
