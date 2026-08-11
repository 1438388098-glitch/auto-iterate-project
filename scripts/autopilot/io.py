"""Low-level IO, git, time, and lock helpers. No imports from sibling modules."""

import csv
import io
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
PHASE_REPORT_PREFIX = "phase-report-round-"

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

DEFAULT_MAX_ROUNDS = 10
SCHEMA_VERSION = 5


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def parse_time(value):
    """Parse an ISO-8601 timestamp, tolerant of Python 3.6 (no colon in tzoffset)
    and of the colon form written by now_iso(). Returns None when unparseable."""
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
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
        if unit in ("min", "mins", "minute", "minutes", "m"):
            delta = timedelta(minutes=amount)
        elif unit in ("h", "hour", "hours", "hr"):
            delta = timedelta(hours=amount)
        elif unit in ("d", "day", "days"):
            delta = timedelta(days=amount)
        else:
            delta = timedelta(weeks=amount)
        return (datetime.now(timezone.utc) + delta).isoformat()
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
    """Atomically write JSON via a temp file in the same directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(tmp_name, str(path))
    except BaseException:
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


def is_detached_head(repo):
    return has_commits(repo) and current_branch(repo) == "HEAD"


def _is_autopilot_path(path):
    if not path:
        return False
    if path == ".autopilot":
        return True
    return path.startswith(".autopilot/") or path.startswith(".autopilot\\")


def _porcelain_entries(repo):
    result = run_git(repo, "status", "--porcelain")
    entries = []
    for line in result.stdout.splitlines():
        if len(line) < 3:
            continue
        code = line[:2]
        path = line[3:]
        if path.startswith('"'):
            try:
                import codecs
                path = codecs.decode(path, "unicode_escape").strip('"')
            except Exception:
                pass
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
    name = run_git(repo, "config", "user.name").stdout.strip()
    email = run_git(repo, "config", "user.email").stdout.strip()
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
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            return True
        try:
            rows = list(csv.reader(io.StringIO(result.stdout)))
        except Exception:
            rows = []
        for row in rows:
            if len(row) >= 2 and row[1].strip() == str(pid):
                return True
        return False
    try:
        os.kill(pid, 0)
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


def _acquire_lock(repo):
    path = lock_path_for(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    holder = None
    if path.exists():
        holder = _read_lock_file(path)
    if isinstance(holder, dict):
        same_host = not holder.get("hostname") or holder.get("hostname") == socket.gethostname()
        if _pid_alive(holder.get("pid")) and same_host:
            print(
                "[ERROR] Another autopilot run appears active (pid {}). "
                "Refusing to modify state. If that process is dead, delete {}.".format(
                    holder.get("pid"), path
                ),
                file=sys.stderr,
            )
            raise SystemExit(2)
        print(
            "[WARN] Removing stale autopilot lock left by pid {}.".format(holder.get("pid")),
            file=sys.stderr,
        )
        try:
            path.unlink()
        except OSError:
            pass
    elif path.exists():
        print("[WARN] Removing corrupt autopilot lock file at {}.".format(path), file=sys.stderr)
        try:
            path.unlink()
        except OSError:
            pass
    save_json(path, {"pid": os.getpid(), "started_at": now_iso(), "hostname": socket.gethostname()})
    return path


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
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


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


def _numstat_total(stdout):
    text, _ = _parse_numstat(stdout)
    return text


def estimate_tokens_for_round(repo, start_sha):
    """Estimate tokens spent on a round: committed diff since round start, plus any
    still-uncommitted working-tree/staged changes. Anchoring on the round start SHA
    avoids double counting previously committed rounds. Binary files are charged a
    flat cost because line counts are meaningless for them."""
    text = 0
    binary = 0
    base = start_sha or EMPTY_TREE
    if has_commits(repo):
        result = run_git(repo, "diff", "--numstat", base, "HEAD")
        if result.returncode == 0:
            t, b = _parse_numstat(result.stdout)
            text += t
            binary += b
    for diff_args in (("diff", "--numstat"), ("diff", "--cached", "--numstat")):
        result = run_git(repo, *diff_args)
        if result.returncode == 0:
            t, b = _parse_numstat(result.stdout)
            text += t
            binary += b
    return 500 + text * 12 + binary * 100


def worktree_change_lines(repo):
    """Total changed units (text lines + binary files) in the working tree and the
    index combined. Used to measure how much a batched round actually added on top
    of the snapshot taken at begin-round, so deferred commits do not double count
    earlier rounds' still-uncommitted lines in token accounting."""
    text = 0
    binary = 0
    for diff_args in (("diff", "--numstat"), ("diff", "--cached", "--numstat")):
        result = run_git(repo, *diff_args)
        if result.returncode == 0:
            t, b = _parse_numstat(result.stdout)
            text += t
            binary += b
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
            remote = remotes.stdout.strip().splitlines()[0]
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
        lines.append(".autopilot/")
        exclude_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        say("[OK] Added .autopilot/ to .git/info/exclude")


def _home_dir():
    return Path(os.environ.get("USERPROFILE") or os.environ.get("HOME") or str(Path.home()))
