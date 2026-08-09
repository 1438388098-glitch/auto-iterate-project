"""State, backlog, branch, stop-condition, scoring, and report logic."""

import json
import re
import sys
import uuid
from datetime import datetime, timezone

from . import config, io


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
        "created_at": io.now_iso(),
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
    state = io.load_json(config.state_path_for(repo))
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
    if state.get("schema", 1) < io.SCHEMA_VERSION:
        state["schema"] = io.SCHEMA_VERSION
        changed = True
    state["repo"] = str(repo)
    if changed:
        save_state(repo, state)
    return state


def save_state(repo, state):
    io.save_json(config.state_path_for(repo), state)


def load_backlog(repo):
    return io.load_json(config.backlog_path_for(repo), config.default_backlog())


def save_backlog(repo, backlog):
    io.save_json(config.backlog_path_for(repo), backlog)


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
    candidate["updated_at"] = io.now_iso()
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
            state["origin_branch"] = io.current_branch(repo)
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

    if not config.get("allow_uncommitted_changes") and io.tracked_changes(repo):
        print(
            "[ERROR] Working tree has tracked changes and allow_uncommitted_changes is false; "
            "refusing to switch branches so user changes are not carried over.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    if not branch:
        branch = "autopilot/" + state.get("run_id", uuid.uuid4().hex[:12])
        if io.branch_exists(repo, branch):
            result = io.run_git(repo, "checkout", branch)
        else:
            result = io.run_git(repo, "checkout", "-b", branch)
        if result.returncode != 0:
            print(result.stderr.strip(), file=sys.stderr)
            raise SystemExit(2)
        state["branch"] = branch
        save_state(repo, state)
    else:
        current = io.current_branch(repo)
        if current != branch:
            result = io.run_git(repo, "checkout", branch)
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
        started_at = io.parse_time(reference)
        if max_minutes is not None and started_at is not None:
            elapsed_minutes = (datetime.now(timezone.utc) - started_at).total_seconds() / 60
            if elapsed_minutes >= max_minutes:
                stop_reason = "max_minutes reached ({:.1f}/{})".format(elapsed_minutes, max_minutes)

    if stop_reason is None:
        deadline = config.get("deadline")
        deadline_at = io.parse_time(deadline)
        if deadline_at is not None and datetime.now(timezone.utc) >= deadline_at:
            stop_reason = "deadline reached ({})".format(deadline)

    if stop_reason is None:
        max_tokens = config.get("max_tokens")
        used_tokens = state.get("estimated_tokens_used", 0)
        if max_tokens is not None and used_tokens >= max_tokens:
            stop_reason = "max_tokens soft budget reached ({}/{})".format(used_tokens, max_tokens)

    return stop_reason


def count_consecutive_blocked(state):
    count = 0
    for entry in reversed(state.get("history", [])):
        if entry.get("status") == "blocked":
            count += 1
        else:
            break
    return count


def _candidate_score(candidate):
    value = candidate.get("value")
    if value is None:
        value = config.LEGACY_IMPACT_SCORE.get(candidate.get("impact"), 3)
    effort = candidate.get("effort")
    if effort is None or isinstance(effort, str):
        effort = config.LEGACY_EFFORT_SCORE.get(effort, 3)
    try:
        value = int(value)
        effort = int(effort)
    except (TypeError, ValueError):
        value, effort = 3, 3
    if effort <= 0:
        effort = 1
    return float(value) / float(effort)


_STATUS_LABELS = {
    "zh": {
        "completed": "已完成", "blocked": "受阻", "cancelled": "已取消",
        "revert": "已回滚", "picked": "进行中", "pending": "待处理",
        "run_report": "Auto Iterate 运行报告", "meta": "运行信息", "goals": "目标",
        "counts": "轮次统计", "history": "轮次历史", "backlog": "改进清单",
        "gitlog": "最近提交", "next": "下一步建议", "active": "活动分支",
        "tokens": "估算 Token", "repo": "仓库", "none": "无",
        "deadline": "定时截止",
    },
    "en": {
        "completed": "completed", "blocked": "blocked", "cancelled": "cancelled",
        "revert": "reverted", "picked": "in progress", "pending": "pending",
        "run_report": "Auto Iterate Run Report", "meta": "Run info", "goals": "Goals",
        "counts": "Round counts", "history": "Round history", "backlog": "Backlog",
        "gitlog": "Recent commits", "next": "Next likely improvement", "active": "Active branch",
        "tokens": "Estimated tokens", "repo": "Repo", "none": "none",
        "deadline": "Deadline timer",
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
    out.append("- {}: `{}`".format(L["deadline"], config.get("deadline") or L["none"]))
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

    if io.has_commits(repo):
        log = io.run_git(repo, "log", "--oneline", "-10")
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


def write_phase_report(repo, state, config):
    """Write a 10-round phase report in the configured language."""
    lang = config.get("report_lang", "zh")
    completed = state.get("completed_rounds", 0)
    markdown = build_report(repo, state, config, lang)
    path = repo / io.AUTOPILOT_DIR / "{}{}.md".format(io.PHASE_REPORT_PREFIX, completed)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(markdown, encoding="utf-8")
        print(
            "[PHASE] Completed {} rounds; phase report written to {} (lang={}).".format(completed, path, lang),
            file=sys.stderr,
        )
    except OSError as exc:
        print("[WARN] Could not write phase report: {}".format(exc), file=sys.stderr)
