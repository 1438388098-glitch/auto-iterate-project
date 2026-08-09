#!/usr/bin/env python3
"""Deterministic state, backlog, branch, and budget helper for auto-iterate-project."""

import argparse
import csv
import fnmatch
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_MAX_ROUNDS = 10
SCHEMA_VERSION = 4
AUTOPILOT_DIR = ".autopilot"
CONFIG_FILENAME = "config.json"
STATE_FILENAME = "state.json"
BACKLOG_FILENAME = "backlog.json"
LOCK_FILENAME = "lock"
LOG_FILENAME = "log.jsonl"
PHASE_REPORT_PREFIX = "phase-report-round-"

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

LEGACY_IMPACT_SCORE = {"high": 5, "medium": 3, "low": 1}
LEGACY_EFFORT_SCORE = {"small": 1, "medium": 3, "large": 5}


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


def compute_stop_reason(state, config):
    """Return the stop reason (or None) based on state and config. Shared by check and begin-round."""
    stop_reason = None

    if state.get("finished_at"):
        stop_reason = "already finished"
    elif state.get("stop_reason"):
        stop_reason = state["stop_reason"]

    if stop_reason is None:
        goals = config.get("goals") or state.get("goals") or []
        completed_goals = set(state.get("completed_goals") or [])
        if goals and all(goal in completed_goals for goal in goals):
            stop_reason = "all goals met"

    if stop_reason is None:
        total_rounds = state.get("completed_rounds", 0) + state.get("blocked_rounds", 0)
        max_rounds = config.get("max_rounds")
        if max_rounds is not None and total_rounds >= max_rounds:
            stop_reason = "max_rounds reached"

    if stop_reason is None:
        consecutive_blocked = count_consecutive_blocked(state)
        max_blocked = config.get("max_blocked_in_a_row")
        if max_blocked is not None and consecutive_blocked >= max_blocked:
            stop_reason = "max_blocked_in_a_row reached ({}/{})".format(consecutive_blocked, max_blocked)

    if stop_reason is None:
        max_minutes = config.get("max_minutes")
        reference = state.get("last_activity_at") or state.get("started_at")
        started_at = parse_time(reference)
        if max_minutes is not None and started_at is not None:
            elapsed_minutes = (datetime.now(timezone.utc) - started_at).total_seconds() / 60
            if elapsed_minutes >= max_minutes:
                stop_reason = "max_minutes reached ({:.1f}/{})".format(elapsed_minutes, max_minutes)

    if stop_reason is None:
        max_tokens = config.get("max_tokens")
        used_tokens = state.get("estimated_tokens_used", 0)
        if max_tokens is not None and used_tokens >= max_tokens:
            stop_reason = "max_tokens soft budget reached ({}/{})".format(used_tokens, max_tokens)

    return stop_reason


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


def emit_result(args, ok, message, data=None):
    """Print a result and return the exit code. JSON mode emits a single machine-readable object."""
    if getattr(args, "json", False):
        payload = {"ok": ok, "message": message}
        if data:
            payload.update(data)
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0 if ok else 2
    if message:
        if ok:
            print(message)
        else:
            print(message, file=sys.stderr)
    return 0 if ok else 2


# --- Runtime agent detection and adaptation ---

AGENT_OVERRIDE_ENV = "AUTOPILOT_AGENT"

# Env vars that uniquely identify a runtime. Values may be "" or "1"; presence wins.
AGENT_ENV_SIGNALS = [
    ("opencode", ("OPENCODE",)),
    ("claude-code", ("CLAUDE_CODE",)),
    ("codex", ("CODEX",)),
]

# Global config markers that identify a runtime when env vars are absent.
AGENT_HOME_MARKERS = [
    ("opencode", ("opencode.json",)),
    ("claude-code", ("CLAUDE.md",)),
    ("codex", ("AGENTS.md",)),
]

AGENT_PROFILES = {
    "opencode": {
        "label": "OpenCode",
        "default_shell": "powershell" if os.name == "nt" else "bash",
        "skill_dir": ".config/opencode/skills",
        "project_marker": "opencode.json",
        "agent_config": "opencode.json",
    },
    "claude-code": {
        "label": "Claude Code",
        "default_shell": "bash",
        "skill_dir": ".claude/skills",
        "project_marker": "CLAUDE.md",
        "agent_config": ".claude/settings.json",
    },
    "codex": {
        "label": "Codex",
        "default_shell": "bash",
        "skill_dir": ".codex/skills",
        "project_marker": "AGENTS.md",
        "agent_config": "AGENTS.md",
    },
    "generic": {
        "label": "Generic agent",
        "default_shell": "powershell" if os.name == "nt" else "bash",
        "skill_dir": ".agents/skills",
        "project_marker": "AGENTS.md",
        "agent_config": "AGENTS.md",
    },
}

KNOWN_AGENTS = ("opencode", "claude-code", "codex", "generic")


def _home_dir():
    return Path(os.environ.get("USERPROFILE") or os.environ.get("HOME") or str(Path.home()))


def _env_var_present(name):
    return name in os.environ and os.environ.get(name) != ""


def detect_agent(cwd=None, home=None):
    """Detect the runtime agent from env vars, then cwd markers, then home markers.
    Returns (agent, detected_by) where agent is one of KNOWN_AGENTS."""
    cwd = Path(cwd) if cwd else Path.cwd()
    home = Path(home) if home else _home_dir()

    override = os.environ.get(AGENT_OVERRIDE_ENV, "")
    if override:
        if override not in KNOWN_AGENTS:
            override = "generic"
        return override, "override:{}".format(AGENT_OVERRIDE_ENV)

    for agent, vars_ in AGENT_ENV_SIGNALS:
        for name in vars_:
            if _env_var_present(name):
                return agent, "env:{}".format(name)

    # cwd project markers beat home markers (a project is usually configured for one agent)
    for agent, names in AGENT_HOME_MARKERS:
        for name in names:
            if (cwd / name).exists():
                return agent, "cwd:{}".format(name)
            if (cwd / ".opencode" / name).exists() and agent == "opencode":
                return agent, "cwd:.opencode/{}".format(name)

    for agent, names in AGENT_HOME_MARKERS:
        for name in names:
            marker = home / name
            if marker.exists() or (home / ("." + name)).exists():
                return agent, "home:{}".format(name)

    return "generic", "fallback"


def detect_python():
    """Return the first available python launcher: python, python3, then py."""
    for candidate in ("python", "python3", "py"):
        if shutil.which(candidate):
            return candidate
    return "python"


def agent_profile(agent):
    return AGENT_PROFILES.get(agent, AGENT_PROFILES["generic"])


def cmd_detect_agent(args):
    agent, detected_by = detect_agent(cwd=Path(args.repo).resolve(), home=args.home)
    profile = agent_profile(agent)
    python_cmd = detect_python()
    payload = {
        "agent": agent,
        "label": profile["label"],
        "detected_by": detected_by,
        "shell": profile["default_shell"],
        "python_cmd": python_cmd,
        "skill_dir": os.environ.get("SKILL_DIR") or (str(_home_dir() / profile["skill_dir"]) if not args.home else str(Path(args.home) / profile["skill_dir"])),
        "project_marker": profile["project_marker"],
        "agent_config": profile["agent_config"],
        "adaptation": {
            "use_python": python_cmd,
            "shell_syntax": profile["default_shell"],
            "command_style": "powershell" if profile["default_shell"] == "powershell" else "bash",
        },
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


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


def default_config(repo):
    return {
        "repo": str(repo),
        "goals": [],
        "max_rounds": DEFAULT_MAX_ROUNDS,
        "max_minutes": None,
        "max_tokens": None,
        "max_round_scope": None,
        "allow_uncommitted_changes": False,
        "push": False,
        "branch_mode": "current",
        "commit_message_prefix": "autopilot",
        "retries_per_round": 3,
        "candidates_per_round": 1,
        "max_blocked_in_a_row": 2,
        "check_commands": [],
        "track_state": False,
        "allow_paths": [],
        "deny_paths": [],
        "report_lang": "zh",
    }


def default_backlog():
    return {
        "next_id": 1,
        "candidates": [],
    }


def config_path_for(repo):
    return repo / AUTOPILOT_DIR / CONFIG_FILENAME


def state_path_for(repo):
    return repo / AUTOPILOT_DIR / STATE_FILENAME


def backlog_path_for(repo):
    return repo / AUTOPILOT_DIR / BACKLOG_FILENAME


def _config_error(path, message):
    print(
        "[ERROR] Invalid .autopilot/config.json: {}. Fix the value or delete the file and run init again.".format(message),
        file=sys.stderr,
    )
    raise SystemExit(2)


def load_config(repo):
    defaults = default_config(repo)
    config = load_json(config_path_for(repo), None)
    if config is None:
        return defaults
    if not isinstance(config, dict):
        _config_error(config_path_for(repo), "must be a JSON object, got {}".format(type(config).__name__))
    merged = dict(defaults)
    merged.update(config)
    merged["repo"] = str(repo)

    if not isinstance(merged["goals"], list):
        _config_error(config_path_for(repo), "'goals' must be an array of strings")
    for key in ("max_rounds", "max_minutes", "max_tokens", "max_round_scope"):
        value = merged.get(key)
        if value is not None and not isinstance(value, (int, float)):
            _config_error(config_path_for(repo), "'{}' must be a number or null".format(key))
        if isinstance(value, bool) or (isinstance(value, (int, float)) and value < 0):
            _config_error(config_path_for(repo), "'{}' must be a non-negative number or null".format(key))
    if merged.get("max_blocked_in_a_row") is not None and not isinstance(merged["max_blocked_in_a_row"], int):
        _config_error(config_path_for(repo), "'max_blocked_in_a_row' must be an integer or null")
    if merged.get("retries_per_round") is not None and not isinstance(merged["retries_per_round"], int):
        _config_error(config_path_for(repo), "'retries_per_round' must be an integer or null")
    cpr = merged.get("candidates_per_round")
    if cpr is not None and (not isinstance(cpr, int) or isinstance(cpr, bool) or cpr < 1):
        _config_error(config_path_for(repo), "'candidates_per_round' must be a positive integer")
    if not isinstance(merged["check_commands"], list) or not all(isinstance(c, str) for c in merged["check_commands"]):
        _config_error(config_path_for(repo), "'check_commands' must be an array of strings")
    for key in ("allow_paths", "deny_paths"):
        if not isinstance(merged[key], list) or not all(isinstance(p, str) for p in merged[key]):
            _config_error(config_path_for(repo), "'{}' must be an array of strings".format(key))
    if merged.get("report_lang") not in ("zh", "en"):
        _config_error(config_path_for(repo), "'report_lang' must be 'zh' or 'en'")
    if merged.get("branch_mode") not in ("current", "feature"):
        _config_error(config_path_for(repo), "'branch_mode' must be 'current' or 'feature'")
    for key in ("push", "allow_uncommitted_changes", "track_state"):
        if not isinstance(merged[key], bool):
            _config_error(config_path_for(repo), "'{}' must be true or false".format(key))
    if not isinstance(merged["commit_message_prefix"], str):
        _config_error(config_path_for(repo), "'commit_message_prefix' must be a string")
    return merged


def save_config(repo, config):
    save_json(config_path_for(repo), config)


def migrate_state(state):
    """Backfill missing state fields. Returns True if anything changed."""
    defaults = {
        "run_id": uuid.uuid4().hex[:12],
        "branch": None,
        "origin_branch": None,
        "last_activity_at": None,
        "goals": [],
        "completed_goals": [],
        "history": [],
        "current_round": None,
        "completed_rounds": 0,
        "blocked_rounds": 0,
        "cancelled_rounds": 0,
        "reverted_rounds": 0,
        "estimated_tokens_used": 0,
        "repo": None,
        "created_at": now_iso(),
        "started_at": None,
        "round": 0,
        "stop_reason": None,
        "finished_at": None,
    }
    changed = False
    for key, value in defaults.items():
        if key not in state:
            state[key] = value
            changed = True
    if state.get("started_at") is None:
        state["started_at"] = state.get("created_at")
        changed = True
    if state.get("last_activity_at") is None:
        state["last_activity_at"] = state.get("started_at")
        changed = True
    return changed


def load_state(repo):
    state = load_json(state_path_for(repo))
    if state is None:
        print("[ERROR] state.json not found. Run init first.", file=sys.stderr)
        raise SystemExit(2)
    if not isinstance(state, dict):
        print(
            "[ERROR] .autopilot/state.json must be a JSON object, got {}. "
            "Fix or delete it and run init again.".format(type(state).__name__),
            file=sys.stderr,
        )
        raise SystemExit(2)
    changed = migrate_state(state)
    if state.get("schema", 1) < SCHEMA_VERSION:
        state["schema"] = SCHEMA_VERSION
        changed = True
    state["repo"] = str(repo)
    if changed:
        save_state(repo, state)
    return state


def save_state(repo, state):
    save_json(state_path_for(repo), state)


def load_backlog(repo):
    return load_json(backlog_path_for(repo), default_backlog())


def save_backlog(repo, backlog):
    save_json(backlog_path_for(repo), backlog)


def find_candidate(backlog, candidate_id):
    for candidate in backlog.get("candidates", []):
        if candidate.get("id") == candidate_id:
            return candidate
    return None


def update_candidate_status(repo, candidate_id, status, round_number=None):
    if not candidate_id:
        return
    backlog = load_backlog(repo)
    candidate = find_candidate(backlog, candidate_id)
    if candidate is None:
        print("[WARN] Candidate not found in backlog: {}".format(candidate_id), file=sys.stderr)
        return
    candidate["status"] = status
    candidate["updated_at"] = now_iso()
    if round_number is not None:
        candidate["round"] = round_number
    save_backlog(repo, backlog)


def round_candidate_ids(current):
    """All candidate ids attached to a round. Supports multi-candidate rounds
    (candidates_per_round > 1) while staying backward compatible with state
    files that only recorded a single candidate_id."""
    if not isinstance(current, dict):
        return []
    ids = current.get("candidate_ids") or []
    if not ids and current.get("candidate_id"):
        ids = [current.get("candidate_id")]
    return ids


def ensure_branch_impl(repo, state, config, to_stderr=False):
    def say(msg):
        if to_stderr:
            print(msg, file=sys.stderr)
        else:
            print(msg)

    if config.get("branch_mode") != "feature":
        say("[SKIP] branch_mode is current; no autopilot branch created.")
        return None

    if state.get("origin_branch") is None:
        try:
            state["origin_branch"] = current_branch(repo)
        except SystemExit:
            state["origin_branch"] = None
        save_state(repo, state)

    branch = state.get("branch")
    if branch and not branch.startswith("autopilot/"):
        print(
            "[ERROR] state.json branch {!r} is not an autopilot branch; refusing to check it out. "
            "Reset .autopilot/state.json or run init --force to recover.".format(branch),
            file=sys.stderr,
        )
        raise SystemExit(2)

    if not config.get("allow_uncommitted_changes") and tracked_changes(repo):
        print(
            "[ERROR] Working tree has tracked changes and allow_uncommitted_changes is false; "
            "refusing to switch branches so user changes are not carried over.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    if not branch:
        branch = "autopilot/" + state.get("run_id", uuid.uuid4().hex[:12])
        if branch_exists(repo, branch):
            result = run_git(repo, "checkout", branch)
        else:
            result = run_git(repo, "checkout", "-b", branch)
        if result.returncode != 0:
            print(result.stderr.strip(), file=sys.stderr)
            raise SystemExit(2)
        state["branch"] = branch
        save_state(repo, state)
    else:
        current = current_branch(repo)
        if current != branch:
            result = run_git(repo, "checkout", branch)
            if result.returncode != 0:
                print(result.stderr.strip(), file=sys.stderr)
                raise SystemExit(2)

    say("[OK] Active branch: {}".format(branch))
    return branch


_GOAL_CONNECTORS = ("并且", "以及", "同时", "另外", "还有")


def split_goals(text):
    """Split a natural-language request into concrete goals by sentence and comma
    delimiters, stripping leading connectors like 以及/并且."""
    if not text:
        return []
    goals = []
    for part in re.split(r"[。．；;\n\r，,、.]+", text):
        goal = part.strip(" \t\u3000")
        for conn in _GOAL_CONNECTORS:
            if goal.startswith(conn) and len(goal) > len(conn):
                goal = goal[len(conn):].strip(" \t\u3000")
                break
        goal = goal.strip("：:()（）\"' ")
        if len(goal) >= 2:
            goals.append(goal)
    if not goals:
        goals = [text.strip()]
    return goals


def cmd_init(args):
    repo = Path(args.repo).resolve()
    with run_lock(repo):
        git_dir = git_dir_for(repo)
        state_path = state_path_for(repo)

        if state_path.exists() and not args.force:
            print(
                "[WARN] state.json already exists. Resume with read/check instead of reinitializing.",
                file=sys.stderr,
            )
            return 0

        config = default_config(repo)
        existing_config = load_json(config_path_for(repo), None)
        if existing_config is not None:
            config.update(existing_config)
        config["repo"] = str(repo)

        if args.goal:
            config["goals"] = args.goal
        if args.goals_from_prompt:
            config["goals"] = split_goals(args.goals_from_prompt)
        if args.max_rounds is not None:
            config["max_rounds"] = args.max_rounds
        if args.max_minutes is not None:
            config["max_minutes"] = args.max_minutes
        if args.max_tokens is not None:
            config["max_tokens"] = args.max_tokens
        if args.max_round_scope is not None:
            config["max_round_scope"] = args.max_round_scope
        if args.branch_mode is not None:
            config["branch_mode"] = args.branch_mode
        if args.allow_uncommitted_changes:
            config["allow_uncommitted_changes"] = True
        if args.track_state:
            config["track_state"] = True
        if args.check_commands:
            config["check_commands"] = args.check_commands
        if args.push:
            config["push"] = True
        if args.commit_message_prefix is not None:
            config["commit_message_prefix"] = args.commit_message_prefix
        if args.retries_per_round is not None:
            config["retries_per_round"] = args.retries_per_round
        if args.candidates_per_round is not None:
            if args.candidates_per_round < 1:
                append_log(repo, "init", "error", reason="candidates_per_round out of range")
                return emit_result(args, False, "[ERROR] --candidates-per-round must be a positive integer.")
            config["candidates_per_round"] = args.candidates_per_round
        if args.max_blocked_in_a_row is not None:
            config["max_blocked_in_a_row"] = args.max_blocked_in_a_row
        if args.allow_path:
            config["allow_paths"] = list(args.allow_path)
        if args.deny_path:
            config["deny_paths"] = list(args.deny_path)
        if args.report_lang is not None:
            config["report_lang"] = args.report_lang

        if not config.get("allow_uncommitted_changes") and not args.force and working_tree_dirty(repo):
            append_log(repo, "init", "error", reason="dirty working tree")
            return emit_result(
                args, False,
                "[ERROR] Working tree is dirty and allow_uncommitted_changes is false. "
                "Commit or stash user changes, or run init with --allow-uncommitted-changes "
                "(or --force to override).",
            )

        if getattr(args, "dry_run", False):
            print(
                "[DRY-RUN] Would initialize autopilot state in {} with run_id {}, {} rounds, goals={}.".format(
                    repo, uuid.uuid4().hex[:12], config.get("max_rounds"), config.get("goals")
                ),
                file=sys.stderr,
            )
            return 0

        save_config(repo, config)
        started_at = now_iso()
        run_id = uuid.uuid4().hex[:12]
        state = {
            "schema": SCHEMA_VERSION,
            "run_id": run_id,
            "repo": str(repo),
            "branch": None,
            "origin_branch": None,
            "created_at": started_at,
            "started_at": started_at,
            "last_activity_at": started_at,
            "round": 0,
            "completed_rounds": 0,
            "blocked_rounds": 0,
            "cancelled_rounds": 0,
            "reverted_rounds": 0,
            "estimated_tokens_used": 0,
            "goals": config["goals"],
            "completed_goals": [],
            "current_round": None,
            "history": [],
            "stop_reason": None,
            "finished_at": None,
        }
        save_state(repo, state)
        ensure_git_exclude(git_dir, config.get("track_state", False), to_stderr=getattr(args, "json", False))
        ensure_branch_impl(repo, state, config, to_stderr=getattr(args, "json", False))
        append_log(repo, "init", "success", run_id=run_id, branch_mode=config.get("branch_mode"))
        return emit_result(args, True, "[OK] Initialized autopilot state.", data={"run_id": run_id})


def cmd_read(args):
    repo = Path(args.repo).resolve()
    print(json.dumps(load_state(repo), indent=2, ensure_ascii=False))
    return 0


def cmd_begin_round(args):
    repo = Path(args.repo).resolve()
    with run_lock(repo):
        state = load_state(repo)
        if state["current_round"] is not None:
            append_log(repo, "begin-round", "error", reason="round already open")
            return emit_result(args, False, "[ERROR] A round is already open. Complete, block, or cancel it first.")

        config = load_config(repo)
        stop_reason = compute_stop_reason(state, config)
        if stop_reason is not None:
            append_log(repo, "begin-round", "error", reason=stop_reason)
            return emit_result(args, False, "[ERROR] Autopilot is stopped: {}. Finish or adjust the config before opening a new round.".format(stop_reason))

        round_number = (
            state.get("completed_rounds", 0)
            + state.get("blocked_rounds", 0)
            + state.get("cancelled_rounds", 0)
            + state.get("reverted_rounds", 0)
            + 1
        )
        if getattr(args, "dry_run", False):
            print(
                "[DRY-RUN] Would open round {}: {}.".format(round_number, args.title),
                file=sys.stderr,
            )
            return 0
        candidate_ids = list(args.candidate_id) if args.candidate_id else []
        if candidate_ids:
            backlog = load_backlog(repo)
            for cid in candidate_ids:
                candidate = find_candidate(backlog, cid)
                if candidate is None:
                    append_log(repo, "begin-round", "error", reason="candidate not found")
                    return emit_result(args, False, "[ERROR] Candidate not found in backlog: {}".format(cid))
                update_candidate_status(repo, cid, "picked", round_number)

        dirty = working_tree_dirty(repo)
        allow_dirty = config.get("allow_uncommitted_changes", False)
        first_round = (
            state.get("completed_rounds", 0)
            + state.get("blocked_rounds", 0)
            + state.get("cancelled_rounds", 0)
            + state.get("reverted_rounds", 0)
        ) == 0
        if dirty and not allow_dirty:
            if first_round:
                append_log(repo, "begin-round", "error", reason="dirty working tree")
                return emit_result(
                    args, False,
                    "[ERROR] Working tree is dirty and allow_uncommitted_changes is false. "
                    "Commit or stash user changes, or set allow_uncommitted_changes: true.",
                )
            print(
                "[WARN] Working tree is dirty; uncommitted changes from earlier rounds will be "
                "included if staged. Consider committing or reverting them first.",
                file=sys.stderr,
            )
        elif dirty and allow_dirty:
            print(
                "[WARN] Working tree is dirty and allow_uncommitted_changes is true; pre-existing "
                "user changes may be committed as part of this round.",
                file=sys.stderr,
            )

        start_sha = None
        if has_commits(repo):
            start_sha = run_git(repo, "rev-parse", "HEAD").stdout.strip()

        current = {
            "round": round_number,
            "title": args.title,
            "reason": args.reason,
            "candidate_id": candidate_ids[0] if candidate_ids else None,
            "candidate_ids": candidate_ids,
            "start_sha": start_sha,
            "start_clean": not dirty,
            "started_at": now_iso(),
        }
        state["round"] = round_number
        state["current_round"] = current
        state["last_activity_at"] = current["started_at"]
        save_state(repo, state)
        append_log(repo, "begin-round", "success", round=round_number, candidate_id=candidate_ids)
        if getattr(args, "json", False):
            return emit_result(args, True, "round opened", data={"round": current})
        print(json.dumps(current, indent=2, ensure_ascii=False))
        return 0


def _resolve_tokens(args, repo, start_sha):
    if args.tokens is None:
        return estimate_tokens_for_round(repo, start_sha)
    return args.tokens


def cmd_complete_round(args):
    repo = Path(args.repo).resolve()
    with run_lock(repo):
        state = load_state(repo)
        current = state["current_round"]
        if current is None:
            append_log(repo, "complete-round", "error", reason="no open round")
            return emit_result(args, False, "[ERROR] No open round to complete.")

        if args.commit_sha:
            verify = run_git(repo, "rev-parse", "--verify", "--quiet", args.commit_sha + "^{commit}")
            if verify.returncode != 0:
                append_log(repo, "complete-round", "error", reason="invalid commit sha")
                return emit_result(
                    args, False,
                    "[ERROR] --commit-sha does not resolve to a commit: {}".format(args.commit_sha),
                )

        tokens = _resolve_tokens(args, repo, current.get("start_sha"))
        if getattr(args, "dry_run", False):
            print(
                "[DRY-RUN] Would record round {} as completed with {} estimated tokens.".format(
                    current["round"], tokens
                ),
                file=sys.stderr,
            )
            return 0

        state["completed_rounds"] += 1
        state["estimated_tokens_used"] += tokens
        state["last_activity_at"] = now_iso()
        state["history"].append(
            {
                "round": current["round"],
                "status": "completed",
                "title": args.title or current.get("title"),
                "summary": args.summary or "",
                "commit_sha": args.commit_sha,
                "estimated_tokens": tokens,
                "candidate_id": current.get("candidate_id"),
                "finished_at": now_iso(),
            }
        )
        state["current_round"] = None
        save_state(repo, state)
        for cid in round_candidate_ids(current):
            update_candidate_status(repo, cid, "completed", current["round"])
        append_log(
            repo, "complete-round", "success",
            round=current["round"], commit_sha=args.commit_sha, estimated_tokens=tokens,
        )

        config = load_config(repo)
        if state.get("completed_rounds", 0) % 10 == 0:
            _write_phase_report(repo, state, config)

        push_warning = None
        if config.get("push"):
            output, err = git_push(repo)
            if err:
                append_log(repo, "push", "error", error=err)
                push_warning = err
            else:
                append_log(repo, "push", "success")

        if push_warning:
            print("[WARN] Round completed but push failed: {}".format(push_warning), file=sys.stderr)
            return emit_result(args, True, "[OK] Round completed (push failed, see warning).")
        return emit_result(args, True, "[OK] Round completed.")


def cmd_block_round(args):
    repo = Path(args.repo).resolve()
    with run_lock(repo):
        state = load_state(repo)
        current = state["current_round"]
        if current is None:
            append_log(repo, "block-round", "error", reason="no open round")
            return emit_result(args, False, "[ERROR] No open round to block.")

        tokens = _resolve_tokens(args, repo, current.get("start_sha"))
        if getattr(args, "dry_run", False):
            print(
                "[DRY-RUN] Would mark round {} as blocked: {}.".format(current["round"], args.reason),
                file=sys.stderr,
            )
            return 0
        state["blocked_rounds"] += 1
        state["estimated_tokens_used"] += tokens
        state["last_activity_at"] = now_iso()
        state["history"].append(
            {
                "round": current["round"],
                "status": "blocked",
                "title": args.title or current.get("title"),
                "reason": args.reason or "",
                "candidate_id": current.get("candidate_id"),
                "finished_at": now_iso(),
            }
        )
        state["current_round"] = None
        save_state(repo, state)
        for cid in round_candidate_ids(current):
            update_candidate_status(repo, cid, "blocked", current["round"])
        append_log(repo, "block-round", "success", round=current["round"], reason=args.reason)
        return emit_result(args, True, "[OK] Round blocked.")


def cmd_cancel_round(args):
    repo = Path(args.repo).resolve()
    with run_lock(repo):
        state = load_state(repo)
        current = state["current_round"]
        if current is None:
            append_log(repo, "cancel-round", "error", reason="no open round")
            return emit_result(args, False, "[ERROR] No open round to cancel.")

        tokens = _resolve_tokens(args, repo, current.get("start_sha"))
        if getattr(args, "dry_run", False):
            print(
                "[DRY-RUN] Would cancel round {}: {}.".format(current["round"], args.reason or ""),
                file=sys.stderr,
            )
            return 0
        state["estimated_tokens_used"] += tokens
        state["cancelled_rounds"] = state.get("cancelled_rounds", 0) + 1
        state["last_activity_at"] = now_iso()
        state["history"].append(
            {
                "round": current["round"],
                "status": "cancelled",
                "title": args.title or current.get("title"),
                "reason": args.reason or "",
                "candidate_id": current.get("candidate_id"),
                "finished_at": now_iso(),
            }
        )
        state["current_round"] = None
        save_state(repo, state)
        for cid in round_candidate_ids(current):
            update_candidate_status(repo, cid, "pending")
        append_log(repo, "cancel-round", "success", round=current["round"], reason=args.reason)
        return emit_result(args, True, "[OK] Round cancelled.")


def cmd_goal_met(args):
    repo = Path(args.repo).resolve()
    with run_lock(repo):
        state = load_state(repo)
        goal = args.goal
        if not goal:
            append_log(repo, "goal-met", "error", reason="goal required")
            return emit_result(args, False, "[ERROR] --goal is required.")
        if getattr(args, "dry_run", False):
            print("[DRY-RUN] Would mark goal as met: {}.".format(goal), file=sys.stderr)
            return 0
        if goal not in state["completed_goals"]:
            state["completed_goals"].append(goal)
            state["last_activity_at"] = now_iso()
        save_state(repo, state)
        append_log(repo, "goal-met", "success", goal=goal)
        return emit_result(args, True, "[OK] Goal marked met.")


def cmd_finish(args):
    repo = Path(args.repo).resolve()
    with run_lock(repo):
        state = load_state(repo)
        config = load_config(repo)

        if getattr(args, "dry_run", False):
            print(
                "[DRY-RUN] Would finish the run (reason={}, stay={}, open_round={}).".format(
                    args.reason, bool(getattr(args, "stay", False)), state.get("current_round") is not None
                ),
                file=sys.stderr,
            )
            return 0

        open_round = state.get("current_round")
        if open_round is not None:
            print(
                "[WARN] finish called with round {} still open; auto-cancelling it.".format(open_round.get("round")),
                file=sys.stderr,
            )
            state["cancelled_rounds"] = state.get("cancelled_rounds", 0) + 1
            state["current_round"] = None
            state["history"].append(
                {
                    "round": open_round["round"],
                    "status": "cancelled",
                    "title": open_round.get("title"),
                    "reason": "auto-cancelled at finish",
                    "candidate_id": open_round.get("candidate_id"),
                    "finished_at": now_iso(),
                }
            )
            for cid in round_candidate_ids(open_round):
                update_candidate_status(repo, cid, "pending")
            append_log(repo, "cancel-round", "success", round=open_round.get("round"), reason="auto-cancelled at finish")

        state["finished_at"] = now_iso()
        state["stop_reason"] = args.reason or state.get("stop_reason") or "finished"
        save_state(repo, state)

        returned_to = None
        if not args.stay and config.get("branch_mode") == "feature":
            origin = state.get("origin_branch")
            if origin and origin != "HEAD" and branch_exists(repo, origin):
                current = current_branch(repo)
                if current != origin:
                    result = run_git(repo, "checkout", origin)
                    if result.returncode == 0:
                        returned_to = origin
                        if getattr(args, "json", False):
                            print("[OK] Returned to branch: {}".format(origin), file=sys.stderr)
                        else:
                            print("[OK] Returned to branch: {}".format(origin))
                    else:
                        print(
                            "[WARN] Could not return to branch {}: {}".format(
                                origin, result.stderr.strip()
                            ),
                            file=sys.stderr,
                        )
        append_log(repo, "finish", "success", reason=args.reason, returned_to=returned_to)
        message = "[OK] Autopilot run finished."
        data = {"returned_to": returned_to}
        return emit_result(args, True, message, data=data)


def _candidate_score(candidate):
    value = candidate.get("value")
    if value is None:
        value = LEGACY_IMPACT_SCORE.get(candidate.get("impact"), 3)
    effort = candidate.get("effort")
    if effort is None or isinstance(effort, str):
        effort = LEGACY_EFFORT_SCORE.get(effort, 3)
    try:
        value = int(value)
        effort = int(effort)
    except (TypeError, ValueError):
        value, effort = 3, 3
    if effort <= 0:
        effort = 1
    return float(value) / float(effort)


def cmd_backlog_add(args):
    repo = Path(args.repo).resolve()
    with run_lock(repo):
        backlog = load_backlog(repo)
        candidate_id = "candidate-{:03d}".format(backlog["next_id"])
        value = args.value if args.value is not None else LEGACY_IMPACT_SCORE.get(args.impact, 3)
        effort = args.effort if args.effort is not None else LEGACY_EFFORT_SCORE.get(args.effort_level, 3)
        if value < 1 or value > 5:
            append_log(repo, "backlog-add", "error", reason="value out of range")
            return emit_result(args, False, "[ERROR] --value must be between 1 and 5.")
        if effort < 1 or effort > 5:
            append_log(repo, "backlog-add", "error", reason="effort out of range")
            return emit_result(args, False, "[ERROR] --effort must be between 1 and 5.")
        candidate = {
            "id": candidate_id,
            "title": args.title,
            "reason": args.reason or "",
            "impact": args.impact or ("high" if value >= 4 else "medium" if value == 3 else "low"),
            "value": value,
            "effort": effort,
            "status": "pending",
            "round": None,
            "created_at": now_iso(),
            "updated_at": now_iso(),
        }
        backlog["candidates"].append(candidate)
        backlog["next_id"] += 1
        save_backlog(repo, backlog)
        append_log(repo, "backlog-add", "success", candidate_id=candidate_id, title=args.title)
        if getattr(args, "json", False):
            return emit_result(args, True, "backlog candidate added", data={"id": candidate_id})
        print(candidate_id)
        return 0


def cmd_backlog_update(args):
    repo = Path(args.repo).resolve()
    with run_lock(repo):
        backlog = load_backlog(repo)
        candidate = find_candidate(backlog, args.id)
        if candidate is None:
            append_log(repo, "backlog-update", "error", reason="candidate not found")
            return emit_result(args, False, "[ERROR] Candidate not found in backlog: {}".format(args.id))
        changed = []
        if args.title is not None:
            candidate["title"] = args.title
            changed.append("title")
        if args.reason is not None:
            candidate["reason"] = args.reason
            changed.append("reason")
        if args.value is not None:
            if args.value < 1 or args.value > 5:
                append_log(repo, "backlog-update", "error", reason="value out of range")
                return emit_result(args, False, "[ERROR] --value must be between 1 and 5.")
            candidate["value"] = args.value
            candidate["impact"] = "high" if args.value >= 4 else "medium" if args.value == 3 else "low"
            changed.append("value")
        if args.effort is not None:
            if args.effort < 1 or args.effort > 5:
                append_log(repo, "backlog-update", "error", reason="effort out of range")
                return emit_result(args, False, "[ERROR] --effort must be between 1 and 5.")
            candidate["effort"] = args.effort
            changed.append("effort")
        if args.status is not None:
            candidate["status"] = args.status
            changed.append("status")
        if not changed:
            append_log(repo, "backlog-update", "error", reason="nothing to update")
            return emit_result(args, False, "[ERROR] Nothing to update. Pass --title, --reason, --value, --effort, or --status.")
        candidate["updated_at"] = now_iso()
        save_backlog(repo, backlog)
        append_log(repo, "backlog-update", "success", candidate_id=args.id, fields=changed)
        return emit_result(args, True, "[OK] Updated candidate {}: {}".format(args.id, ", ".join(changed)))


def cmd_backlog_remove(args):
    repo = Path(args.repo).resolve()
    with run_lock(repo):
        backlog = load_backlog(repo)
        candidates = backlog.get("candidates", [])
        updated = [c for c in candidates if c.get("id") != args.id]
        if len(updated) == len(candidates):
            append_log(repo, "backlog-remove", "error", reason="candidate not found")
            return emit_result(args, False, "[ERROR] Candidate not found in backlog: {}".format(args.id))
        backlog["candidates"] = updated
        save_backlog(repo, backlog)
        append_log(repo, "backlog-remove", "success", candidate_id=args.id)
        return emit_result(args, True, "[OK] Removed candidate {}.".format(args.id))


def cmd_backlog_list(args):
    repo = Path(args.repo).resolve()
    if not state_path_for(repo).exists():
        return emit_result(args, False, "[ERROR] Autopilot not initialized. Run init first.")
    print(json.dumps(load_backlog(repo), indent=2, ensure_ascii=False))
    return 0


def cmd_backlog_rank(args):
    repo = Path(args.repo).resolve()
    if not state_path_for(repo).exists():
        return emit_result(args, False, "[ERROR] Autopilot not initialized. Run init first.")
    backlog = load_backlog(repo)
    ranked = []
    for candidate in backlog.get("candidates", []):
        entry = dict(candidate)
        entry["score"] = round(_candidate_score(candidate), 3)
        ranked.append(entry)
    ranked.sort(key=lambda item: (item.get("status") != "pending", -item.get("score", 0), item.get("id")))
    print(json.dumps(ranked, indent=2, ensure_ascii=False))
    return 0


def cmd_backlog_pick(args):
    repo = Path(args.repo).resolve()
    with run_lock(repo):
        backlog = load_backlog(repo)
        candidate = find_candidate(backlog, args.id)
        if candidate is None:
            append_log(repo, "backlog-pick", "error", reason="candidate not found")
            return emit_result(args, False, "[ERROR] Candidate not found in backlog: {}".format(args.id))
        candidate["status"] = "picked"
        candidate["updated_at"] = now_iso()
        save_backlog(repo, backlog)
        append_log(repo, "backlog-pick", "success", candidate_id=args.id)
        return emit_result(args, True, "[OK] Picked candidate: {}".format(args.id))


def cmd_ensure_branch(args):
    repo = Path(args.repo).resolve()
    with run_lock(repo):
        state = load_state(repo)
        config = load_config(repo)
        branch = ensure_branch_impl(repo, state, config, to_stderr=getattr(args, "json", False))
        append_log(repo, "ensure-branch", "success", branch=branch)
        return emit_result(args, True, "[OK] Autopilot branch ready: {}".format(branch or "current"), data={"branch": branch})


def cmd_push(args):
    repo = Path(args.repo).resolve()
    with run_lock(repo):
        state = load_state(repo)
        config = load_config(repo)
        if not config.get("push"):
            append_log(repo, "push", "error", reason="push disabled in config")
            return emit_result(
                args, False,
                "[ERROR] push is disabled in .autopilot/config.json (push: false). "
                "Set 'push': true to allow pushing.",
            )
        if getattr(args, "dry_run", False):
            print("[DRY-RUN] Would push the current branch to its remote.", file=sys.stderr)
            return 0
        output, err = git_push(repo)
        if err:
            append_log(repo, "push", "error", error=err)
            return emit_result(args, False, "[ERROR] git push failed: {}".format(err))
        append_log(repo, "push", "success")
        return emit_result(args, True, "[OK] Pushed to remote.")


def _path_allowed(path, allow_paths, deny_paths):
    """A staged path is allowed unless deny_paths matches it, and unless allow_paths
    is non-empty and does not match it. Patterns are fnmatch globs against the full
    path and the basename."""
    def matches(p, pattern):
        if not pattern:
            return False
        if fnmatch.fnmatch(p, pattern):
            return True
        base = os.path.basename(p)
        return bool(base and fnmatch.fnmatch(base, pattern))

    if any(matches(path, pat) for pat in (deny_paths or [])):
        return False
    if allow_paths:
        if not any(matches(path, pat) for pat in allow_paths):
            return False
    return True


def cmd_commit(args):
    repo = Path(args.repo).resolve()
    with run_lock(repo):
        state = load_state(repo)
        config = load_config(repo)

        identity_ok, _, _ = git_identity_ok(repo)
        if not identity_ok:
            append_log(repo, "commit", "error", reason="identity not configured")
            return emit_result(
                args, False,
                "[ERROR] Git identity is not configured. Run 'git config user.name ...' "
                "and 'git config user.email ...' first.",
            )

        staged = run_git(repo, "diff", "--cached", "--quiet")
        if staged.returncode == 0:
            append_log(repo, "commit", "error", reason="nothing staged")
            return emit_result(args, False, "[ERROR] Nothing is staged. Run 'git add <files>' for the current round first.")

        if config.get("allow_paths") or config.get("deny_paths"):
            names = run_git(repo, "diff", "--cached", "--name-only")
            blocked = [
                line for line in names.stdout.splitlines()
                if line.strip() and not _path_allowed(line, config.get("allow_paths"), config.get("deny_paths"))
            ]
            if blocked:
                append_log(repo, "commit", "error", reason="path whitelist violated", paths=blocked)
                return emit_result(
                    args, False,
                    "[ERROR] Staged files violate allow_paths/deny_paths: {}".format(", ".join(blocked)),
                )

        stat = run_git(repo, "diff", "--cached", "--stat")
        if stat.returncode == 0 and not getattr(args, "json", False):
            print(stat.stdout.rstrip())

        max_scope = config.get("max_round_scope")
        if max_scope is not None:
            changed_lines = _staged_numstat_lines(repo)
            if changed_lines > max_scope:
                append_log(repo, "commit", "error", reason="max_round_scope exceeded")
                return emit_result(
                    args, False,
                    "[ERROR] Staged diff exceeds max_round_scope ({} lines > {}). "
                    "Split the change into smaller rounds.".format(changed_lines, max_scope),
                )

        prefix = config.get("commit_message_prefix", "autopilot")
        open_round = state.get("current_round")
        if args.round:
            round_no = args.round
        elif open_round is not None:
            round_no = open_round.get("round")
            if not config.get("allow_uncommitted_changes") and not open_round.get("start_clean"):
                append_log(repo, "commit", "error", reason="round started with dirty tree")
                return emit_result(
                    args, False,
                    "[ERROR] This round began with a dirty working tree and allow_uncommitted_changes "
                    "is false. Commit only the round's own changes, or set allow_uncommitted_changes: true.",
                )
        else:
            append_log(repo, "commit", "error", reason="no open round")
            return emit_result(
                args, False,
                "[ERROR] No round is open. Use begin-round first, or pass --round explicitly "
                "to allow an orphan commit (e.g. after cancel/block).",
            )
        message = "{}(round-{}): {}".format(prefix, round_no, args.summary)

        if getattr(args, "dry_run", False):
            print(
                "[DRY-RUN] Would commit staged changes with message: {}".format(message),
                file=sys.stderr,
            )
            return 0

        result = run_git(repo, "commit", "-m", message)
        if result.returncode != 0:
            print(result.stderr.strip(), file=sys.stderr)
            append_log(repo, "commit", "error", reason="git commit failed")
            return emit_result(args, False, "[ERROR] git commit failed. Check pre-commit hooks or staged files.")
        sha = run_git(repo, "rev-parse", "HEAD").stdout.strip()
        append_log(repo, "commit", "success", commit_sha=sha, message=message, round=round_no)
        message = "[OK] Committed {}: {}".format(sha, message)
        if getattr(args, "json", False):
            return emit_result(args, True, message, data={"commit_sha": sha, "commit_message": message})
        print(message)
        return 0


def _staged_numstat_lines(repo):
    result = run_git(repo, "diff", "--cached", "--numstat")
    if result.returncode != 0:
        return 0
    text, binary = _parse_numstat(result.stdout)
    return text + binary


def count_consecutive_blocked(state):
    count = 0
    for entry in reversed(state.get("history", [])):
        if entry.get("status") == "blocked":
            count += 1
        else:
            break
    return count


_STATUS_LABELS = {
    "zh": {
        "completed": "已完成", "blocked": "受阻", "cancelled": "已取消",
        "revert": "已回滚", "picked": "进行中", "pending": "待处理",
        "run_report": "Auto Iterate 运行报告", "meta": "运行信息", "goals": "目标",
        "counts": "轮次统计", "history": "轮次历史", "backlog": "改进清单",
        "gitlog": "最近提交", "next": "下一步建议", "active": "活动分支",
        "tokens": "估算 Token", "repo": "仓库", "none": "无",
    },
    "en": {
        "completed": "completed", "blocked": "blocked", "cancelled": "cancelled",
        "revert": "reverted", "picked": "in progress", "pending": "pending",
        "run_report": "Auto Iterate Run Report", "meta": "Run info", "goals": "Goals",
        "counts": "Round counts", "history": "Round history", "backlog": "Backlog",
        "gitlog": "Recent commits", "next": "Next likely improvement", "active": "Active branch",
        "tokens": "Estimated tokens", "repo": "Repo", "none": "none",
    },
}


def build_report(repo, state, config, lang="en"):
    """Build a deterministic markdown report from state, config, and backlog."""
    zh = lang == "zh"
    L = _STATUS_LABELS["zh" if zh else "en"]
    out = []
    out.append("# {}".format(L["run_report"]))
    out.append("")

    out.append("## {}".format(L["meta"]))
    out.append("")
    out.append("- {}: `{}`".format(L["repo"], state.get("repo")))
    out.append("- run_id: `{}`".format(state.get("run_id")))
    out.append("- {}: `{}`".format(L["active"], state.get("branch") or state.get("origin_branch") or L["none"]))
    out.append("- started_at: `{}`".format(state.get("started_at")))
    out.append("- last_activity_at: `{}`".format(state.get("last_activity_at")))
    out.append("- finished_at: `{}`".format(state.get("finished_at") or L["none"]))
    out.append("- stop_reason: `{}`".format(state.get("stop_reason") or L["none"]))
    out.append("")

    goals = config.get("goals") or state.get("goals") or []
    completed = set(state.get("completed_goals") or [])
    out.append("## {}".format(L["goals"]))
    out.append("")
    if not goals:
        out.append("- {}".format(L["none"]))
    else:
        for goal in goals:
            mark = "x" if goal in completed else " "
            out.append("- [{}] {}".format(mark, goal))
    out.append("")

    out.append("## {}".format(L["counts"]))
    out.append("")
    out.append("- {}: {}".format(L["completed"], state.get("completed_rounds", 0)))
    out.append("- {}: {}".format(L["blocked"], state.get("blocked_rounds", 0)))
    out.append("- {}: {}".format(L["cancelled"], state.get("cancelled_rounds", 0)))
    out.append("- {}: {}".format(L["revert"], state.get("reverted_rounds", 0)))
    out.append("- {}: {}".format(L["tokens"], state.get("estimated_tokens_used", 0)))
    out.append("")

    history = state.get("history") or []
    out.append("## {}".format(L["history"]))
    out.append("")
    if not history:
        out.append("- {}".format(L["none"]))
    else:
        out.append("| round | status | title | commit | tokens |")
        out.append("|---|---|---|---|---|")
        for entry in reversed(history):
            status = entry.get("status", "?")
            label = _STATUS_LABELS["zh" if zh else "en"].get(status, status)
            sha = entry.get("commit_sha") or ""
            if sha:
                sha = "`{}`".format(sha[:12])
            tokens = entry.get("estimated_tokens", "")
            out.append("| {} | {} | {} | {} | {} |".format(
                entry.get("round", ""), label, entry.get("title", ""), sha, tokens
            ))
    out.append("")

    backlog = load_backlog(repo).get("candidates") or []
    out.append("## {}".format(L["backlog"]))
    out.append("")
    if not backlog:
        out.append("- {}".format(L["none"]))
    else:
        out.append("| id | title | value/effort | status |")
        out.append("|---|---|---|---|")
        for c in backlog:
            label = _STATUS_LABELS["zh" if zh else "en"].get(c.get("status", "pending"), c.get("status", "pending"))
            out.append("| `{}` | {} | {}/{} | {} |".format(
                c.get("id", ""), c.get("title", ""), c.get("value", ""), c.get("effort", ""), label
            ))
    out.append("")

    if has_commits(repo):
        log = run_git(repo, "log", "--oneline", "-10")
        if log.returncode == 0 and log.stdout.strip():
            out.append("## {}".format(L["gitlog"]))
            out.append("")
            out.append("```")
            out.append(log.stdout.rstrip())
            out.append("```")
            out.append("")

    pending = [c for c in backlog if c.get("status") == "pending"]
    if pending:
        ranked = sorted(pending, key=_candidate_score, reverse=True)
        top = ranked[0]
        out.append("## {}".format(L["next"]))
        out.append("")
        out.append("- {} `{}` (value={}, effort={})".format(
            top.get("id"), top.get("title"), top.get("value"), top.get("effort")
        ))
        out.append("")

    return "\n".join(out)


def _write_phase_report(repo, state, config):
    """Write a 10-round phase report in the configured language."""
    lang = config.get("report_lang", "zh")
    completed = state.get("completed_rounds", 0)
    markdown = build_report(repo, state, config, lang)
    path = repo / AUTOPILOT_DIR / "{}{}.md".format(PHASE_REPORT_PREFIX, completed)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(markdown, encoding="utf-8")
        print(
            "[PHASE] Completed {} rounds; phase report written to {} (lang={}).".format(completed, path, lang),
            file=sys.stderr,
        )
    except OSError as exc:
        print("[WARN] Could not write phase report: {}".format(exc), file=sys.stderr)


def cmd_report(args):
    repo = Path(args.repo).resolve()
    state = load_state(repo)
    config = load_config(repo)
    lang = args.lang or config.get("report_lang", "zh")
    markdown = build_report(repo, state, config, lang)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(markdown, encoding="utf-8")
        return emit_result(args, True, "[OK] Report written to {}".format(output), data={"path": str(output)})
    print(markdown)
    return 0


def cmd_undo_round(args):
    repo = Path(args.repo).resolve()
    with run_lock(repo):
        state = load_state(repo)
        config = load_config(repo)
        if state.get("current_round") is not None:
            append_log(repo, "undo-round", "error", reason="round already open")
            return emit_result(
                args, False,
                "[ERROR] A round is open. Complete, block, or cancel it before undoing a commit.",
            )
        verify = run_git(repo, "rev-parse", "--verify", "--quiet", args.sha + "^{commit}")
        if verify.returncode != 0:
            append_log(repo, "undo-round", "error", reason="invalid sha")
            return emit_result(args, False, "[ERROR] --sha does not resolve to a commit: {}".format(args.sha))
        full_sha = verify.stdout.strip()
        if getattr(args, "dry_run", False):
            print(
                "[DRY-RUN] Would git revert commit {} into a new commit.".format(full_sha),
                file=sys.stderr,
            )
            return 0
        result = run_git(repo, "revert", "--no-edit", full_sha)
        if result.returncode != 0:
            print(result.stderr.strip(), file=sys.stderr)
            append_log(repo, "undo-round", "error", reason="revert failed", sha=full_sha)
            return emit_result(
                args, False,
                "[ERROR] git revert failed (likely a conflict). Resolve the conflict and commit "
                "manually, then record the round with commit/complete-round.",
            )
        revert_sha = run_git(repo, "rev-parse", "HEAD").stdout.strip()
        round_number = (
            state.get("completed_rounds", 0)
            + state.get("blocked_rounds", 0)
            + state.get("cancelled_rounds", 0)
            + state.get("reverted_rounds", 0)
            + 1
        )
        state["reverted_rounds"] = state.get("reverted_rounds", 0) + 1
        state["last_activity_at"] = now_iso()
        state["history"].append(
            {
                "round": round_number,
                "status": "revert",
                "title": args.title or "Undo commit {}".format(full_sha[:7]),
                "summary": args.summary or "Reverted {}".format(full_sha[:7]),
                "commit_sha": revert_sha,
                "reverted_sha": full_sha,
                "candidate_id": None,
                "finished_at": now_iso(),
            }
        )
        save_state(repo, state)
        append_log(repo, "undo-round", "success", round=round_number, commit_sha=revert_sha, reverted_sha=full_sha)
        return emit_result(
            args, True,
            "[OK] Reverted {} -> {}".format(full_sha[:12], revert_sha[:12]),
            data={"round": round_number, "revert_sha": revert_sha, "reverted_sha": full_sha},
        )


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
    return signals


def cmd_detect_verify(args):
    repo = Path(args.repo).resolve()
    signals = detect_verify_commands(repo)
    commands = [cmd for _, cmd in signals]
    payload = {"detected": [{"tech": tech, "command": cmd} for tech, cmd in signals], "commands": commands}
    if args.apply:
        if not commands:
            append_log(repo, "detect-verify", "error", reason="nothing detected")
            return emit_result(args, False, "[ERROR] No verification commands detected; nothing to apply.")
        config = load_config(repo)
        config["check_commands"] = commands
        save_config(repo, config)
        append_log(repo, "detect-verify", "success", commands=commands)
        return emit_result(args, True, "[OK] check_commands set to {}".format(commands), data=payload)
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    if not commands:
        print("[INFO] No test/build configuration detected. Set check_commands manually or use --check-commands on init.")
        return 0
    print("Detected verification commands:")
    for tech, cmd in signals:
        print("  [{}] {}".format(tech, cmd))
    return 0


def cmd_check(args):
    repo = Path(args.repo).resolve()
    state = load_state(repo)
    config = load_config(repo)
    stop_reason = compute_stop_reason(state, config)
    warnings = []

    if state.get("current_round") is not None:
        warnings.append("current_round is open; complete, block, or cancel it before starting a new round.")

    if not has_commits(repo):
        warnings.append("Repository has no commits yet; git log is unavailable and the first round creates the initial commit.")

    if is_detached_head(repo):
        warnings.append("Detached HEAD; consider checking out a branch before starting.")

    identity_ok, _, _ = git_identity_ok(repo)
    if not identity_ok:
        warnings.append("Git user.name/user.email not configured; commits will fail until set.")

    if working_tree_dirty(repo):
        if not config.get("allow_uncommitted_changes"):
            warnings.append(
                "Working tree is dirty and allow_uncommitted_changes is false; begin-round/init "
                "will refuse to start until the tree is clean or the config allows uncommitted changes."
            )
        else:
            warnings.append(
                "Working tree is dirty and allow_uncommitted_changes is true; pre-existing user "
                "changes may be committed as part of a round."
            )

    goals = config.get("goals") or []
    if not goals:
        warnings.append("goals is empty; the run is open-ended and will stop only on the configured budgets.")
    if (
        config.get("max_rounds") is None
        and config.get("max_minutes") is None
        and config.get("max_tokens") is None
        and config.get("max_blocked_in_a_row") is None
        and not goals
    ):
        warnings.append("No stop condition is configured (goals and all max_* are unset); the loop has no automatic stopping point.")

    if config.get("push"):
        remotes = run_git(repo, "remote")
        if remotes.returncode == 0 and not remotes.stdout.strip():
            warnings.append("push is true but no git remote is configured; complete-round will warn on every push attempt.")

    payload = {
        "continue": stop_reason is None,
        "stop_reason": stop_reason,
        "warnings": warnings,
    }
    if not getattr(args, "brief", False):
        payload["state"] = state
        payload["config"] = config

    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def cmd_diagnose(args):
    repo = Path(args.repo).resolve()
    try:
        git_dir = git_dir_for(repo)
    except SystemExit:
        print(json.dumps({"is_git_repo": False}, indent=2, ensure_ascii=False))
        return 0

    identity_ok, name, email = git_identity_ok(repo)
    info = {
        "is_git_repo": True,
        "repo": str(repo),
        "has_commits": has_commits(repo),
        "current_branch": current_branch(repo),
        "detached_head": is_detached_head(repo),
        "dirty": working_tree_dirty(repo),
        "user_name": name,
        "user_email": email,
        "identity_ok": identity_ok,
        "has_pre_commit_hook": (git_dir / "hooks" / "pre-commit").exists(),
    }
    print(json.dumps(info, indent=2, ensure_ascii=False))
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command")

    def add_json(sub):
        sub.add_argument("--json", action="store_true", help="Emit a single machine-readable JSON result object")

    def add_dry_run(sub):
        sub.add_argument("--dry-run", action="store_true", help="Print what would happen without changing state or git")

    init_parser = subparsers.add_parser("init", help="Initialize config and state")
    init_parser.add_argument("--repo", default=".")
    init_parser.add_argument("--goal", action="append", default=[])
    init_parser.add_argument("--goals-from-prompt", default=None, help="Split a natural-language request into goals")
    init_parser.add_argument("--max-rounds", type=int, default=None)
    init_parser.add_argument("--max-minutes", type=float, default=None)
    init_parser.add_argument("--max-tokens", type=int, default=None)
    init_parser.add_argument("--max-round-scope", type=int, default=None)
    init_parser.add_argument("--branch-mode", choices=["current", "feature"], default=None)
    init_parser.add_argument("--allow-uncommitted-changes", action="store_true")
    init_parser.add_argument("--track-state", action="store_true")
    init_parser.add_argument("--check-commands", action="append", default=None)
    init_parser.add_argument("--push", action="store_true")
    init_parser.add_argument("--commit-message-prefix", default=None)
    init_parser.add_argument("--retries-per-round", type=int, default=None)
    init_parser.add_argument("--candidates-per-round", type=int, default=None, help="Backlog candidates to work per round (default 1)")
    init_parser.add_argument("--max-blocked-in-a-row", type=int, default=None)
    init_parser.add_argument("--allow-path", action="append", default=None, help="Glob of paths allowed in commits (repeatable)")
    init_parser.add_argument("--deny-path", action="append", default=None, help="Glob of paths never allowed in commits (repeatable)")
    init_parser.add_argument("--report-lang", choices=["zh", "en"], default=None)
    init_parser.add_argument("--force", action="store_true")
    add_json(init_parser)
    add_dry_run(init_parser)

    read_parser = subparsers.add_parser("read", help="Print current state")
    read_parser.add_argument("--repo", default=".")

    diagnose_parser = subparsers.add_parser("diagnose", help="Inspect repository health and git environment")
    diagnose_parser.add_argument("--repo", default=".")

    begin_parser = subparsers.add_parser("begin-round", help="Open a round")
    begin_parser.add_argument("--repo", default=".")
    begin_parser.add_argument("--title", required=True)
    begin_parser.add_argument("--reason", required=True)
    begin_parser.add_argument("--candidate-id", action="append", default=None,
                             help="Backlog candidate id for this round (repeatable for multi-candidate rounds)")
    add_json(begin_parser)
    add_dry_run(begin_parser)

    complete_parser = subparsers.add_parser("complete-round", help="Finish a round successfully")
    complete_parser.add_argument("--repo", default=".")
    complete_parser.add_argument("--title")
    complete_parser.add_argument("--summary", required=True)
    complete_parser.add_argument("--commit-sha", required=True)
    complete_parser.add_argument("--tokens", type=int, default=None)
    add_json(complete_parser)
    add_dry_run(complete_parser)

    block_parser = subparsers.add_parser("block-round", help="Mark a round blocked")
    block_parser.add_argument("--repo", default=".")
    block_parser.add_argument("--title")
    block_parser.add_argument("--reason", required=True)
    block_parser.add_argument("--tokens", type=int, default=None)
    add_json(block_parser)
    add_dry_run(block_parser)

    cancel_parser = subparsers.add_parser("cancel-round", help="Abandon an open round without counting it blocked")
    cancel_parser.add_argument("--repo", default=".")
    cancel_parser.add_argument("--title")
    cancel_parser.add_argument("--reason")
    cancel_parser.add_argument("--tokens", type=int, default=None)
    add_json(cancel_parser)
    add_dry_run(cancel_parser)

    commit_parser = subparsers.add_parser("commit", help="Stage-verified commit with prefix message and scope guard")
    commit_parser.add_argument("--repo", default=".")
    commit_parser.add_argument("--round", type=int, default=None)
    commit_parser.add_argument("--summary", required=True)
    add_json(commit_parser)
    add_dry_run(commit_parser)

    undo_parser = subparsers.add_parser("undo-round", help="Revert a bad commit and record it as a revert round")
    undo_parser.add_argument("--repo", default=".")
    undo_parser.add_argument("--sha", required=True)
    undo_parser.add_argument("--title")
    undo_parser.add_argument("--summary")
    add_json(undo_parser)
    add_dry_run(undo_parser)

    goal_parser = subparsers.add_parser("goal-met", help="Mark a configured goal met")
    goal_parser.add_argument("--repo", default=".")
    goal_parser.add_argument("--goal", required=True)
    add_json(goal_parser)
    add_dry_run(goal_parser)

    finish_parser = subparsers.add_parser("finish", help="Finish the run")
    finish_parser.add_argument("--repo", default=".")
    finish_parser.add_argument("--reason")
    finish_parser.add_argument("--stay", action="store_true", help="Stay on the autopilot branch instead of returning to origin")
    add_json(finish_parser)
    add_dry_run(finish_parser)

    report_parser = subparsers.add_parser("report", help="Print or write a markdown run report")
    report_parser.add_argument("--repo", default=".")
    report_parser.add_argument("--output", default=None, help="Write the report to a file instead of stdout")
    report_parser.add_argument("--lang", choices=["zh", "en"], default=None, help="Report language (default: config report_lang)")
    add_json(report_parser)

    detect_verify_parser = subparsers.add_parser("detect-verify", help="Detect test/build commands from repo entry points")
    detect_verify_parser.add_argument("--repo", default=".")
    detect_verify_parser.add_argument("--apply", action="store_true", help="Write detected commands into config check_commands")
    add_json(detect_verify_parser)

    backlog_add_parser = subparsers.add_parser("backlog-add", help="Add a backlog candidate")
    backlog_add_parser.add_argument("--repo", default=".")
    backlog_add_parser.add_argument("--title", required=True)
    backlog_add_parser.add_argument("--reason")
    backlog_add_parser.add_argument("--value", type=int, default=None, help="Value 1-5 (preferred)")
    backlog_add_parser.add_argument("--effort", type=int, default=None, help="Effort 1-5 (preferred)")
    backlog_add_parser.add_argument("--impact", choices=["high", "medium", "low"], default=None, help="Legacy")
    backlog_add_parser.add_argument("--effort-level", choices=["small", "medium", "large"], default=None, help="Legacy")
    add_json(backlog_add_parser)

    backlog_update_parser = subparsers.add_parser("backlog-update", help="Update a backlog candidate's fields or status")
    backlog_update_parser.add_argument("--repo", default=".")
    backlog_update_parser.add_argument("--id", required=True)
    backlog_update_parser.add_argument("--title")
    backlog_update_parser.add_argument("--reason")
    backlog_update_parser.add_argument("--value", type=int, default=None, help="Value 1-5")
    backlog_update_parser.add_argument("--effort", type=int, default=None, help="Effort 1-5")
    backlog_update_parser.add_argument("--status", choices=["pending", "picked", "completed", "blocked"])
    add_json(backlog_update_parser)

    backlog_remove_parser = subparsers.add_parser("backlog-remove", help="Remove a backlog candidate")
    backlog_remove_parser.add_argument("--repo", default=".")
    backlog_remove_parser.add_argument("--id", required=True)
    add_json(backlog_remove_parser)

    backlog_list_parser = subparsers.add_parser("backlog-list", help="List backlog candidates")
    backlog_list_parser.add_argument("--repo", default=".")

    backlog_rank_parser = subparsers.add_parser("backlog-rank", help="List candidates sorted by value-to-effort ratio")
    backlog_rank_parser.add_argument("--repo", default=".")

    backlog_pick_parser = subparsers.add_parser("backlog-pick", help="Mark a candidate picked")
    backlog_pick_parser.add_argument("--repo", default=".")
    backlog_pick_parser.add_argument("--id", required=True)
    add_json(backlog_pick_parser)

    ensure_branch_parser = subparsers.add_parser("ensure-branch", help="Create or check out the autopilot feature branch")
    ensure_branch_parser.add_argument("--repo", default=".")
    add_json(ensure_branch_parser)

    push_parser = subparsers.add_parser("push", help="Push the current branch to its remote (never force)")
    push_parser.add_argument("--repo", default=".")
    add_json(push_parser)
    add_dry_run(push_parser)

    check_parser = subparsers.add_parser("check", help="Check stop conditions")
    check_parser.add_argument("--repo", default=".")
    check_parser.add_argument("--brief", action="store_true", help="Return only continue/stop_reason/warnings without state and config")

    detect_parser = subparsers.add_parser("detect-agent", help="Detect the runtime agent and print adaptation context")
    detect_parser.add_argument("--repo", default=".")
    detect_parser.add_argument("--home", default=None, help="Override the agent home directory for detection")

    return parser


def main():
    args = build_parser().parse_args()
    if args.command is None:
        build_parser().error("a command is required")
    handlers = {
        "init": cmd_init,
        "read": cmd_read,
        "diagnose": cmd_diagnose,
        "begin-round": cmd_begin_round,
        "complete-round": cmd_complete_round,
        "block-round": cmd_block_round,
        "cancel-round": cmd_cancel_round,
        "commit": cmd_commit,
        "undo-round": cmd_undo_round,
        "goal-met": cmd_goal_met,
        "finish": cmd_finish,
        "report": cmd_report,
        "detect-verify": cmd_detect_verify,
        "backlog-add": cmd_backlog_add,
        "backlog-update": cmd_backlog_update,
        "backlog-remove": cmd_backlog_remove,
        "backlog-list": cmd_backlog_list,
        "backlog-rank": cmd_backlog_rank,
        "backlog-pick": cmd_backlog_pick,
        "ensure-branch": cmd_ensure_branch,
        "push": cmd_push,
        "check": cmd_check,
        "detect-agent": cmd_detect_agent,
    }
    sys.exit(handlers[args.command](args))


if __name__ == "__main__":
    main()
