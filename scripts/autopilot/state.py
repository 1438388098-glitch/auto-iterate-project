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
        "type_stats": {},
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


VALID_CANDIDATE_TYPES = ("bugfix", "feature", "refactor", "perf", "test", "docs")


def compute_type_stats(backlog):
    """Per-type completion/block statistics derived from the backlog. Feeds ranking
    (saturation + blocked penalties) and the retrospective report. Types outside the
    VALID_CANDIDATE_TYPES list are grouped under their own key."""
    stats = {}
    for candidate in backlog.get("candidates", []):
        candidate_type = candidate.get("type") or "feature"
        if candidate_type not in VALID_CANDIDATE_TYPES:
            candidate_type = "other"
        entry = stats.setdefault(
            candidate_type,
            {"completed": 0, "blocked": 0, "total": 0, "effort_sum": 0, "value_sum": 0},
        )
        status = candidate.get("status")
        if status in ("completed", "blocked"):
            entry["total"] += 1
            if status == "completed":
                entry["completed"] += 1
                try:
                    entry["effort_sum"] += int(candidate.get("effort") or 0)
                    entry["value_sum"] += int(candidate.get("value") or 0)
                except (TypeError, ValueError):
                    pass
            else:
                entry["blocked"] += 1
    for entry in stats.values():
        done = entry["completed"] + entry["blocked"]
        entry["blocked_rate"] = round(entry["blocked"] / done, 3) if done else 0.0
        entry["avg_effort"] = round(entry["effort_sum"] / entry["completed"], 2) if entry["completed"] else 0.0
        entry["avg_value"] = round(entry["value_sum"] / entry["completed"], 2) if entry["completed"] else 0.0
        entry.pop("effort_sum", None)
        entry.pop("value_sum", None)
    return stats


def candidate_deps_status(backlog, candidate):
    """Return (missing_deps, ready). A candidate is ready when every candidate in its
    depends_on list exists in the backlog and is completed."""
    deps = candidate.get("depends_on") or []
    if not deps:
        return [], True
    missing = []
    for dep_id in deps:
        dep = find_candidate(backlog, dep_id)
        if dep is None:
            missing.append("{} (missing)".format(dep_id))
        elif dep.get("status") != "completed":
            missing.append("{} ({})".format(dep_id, dep.get("status") or "pending"))
    return missing, len(missing) == 0


def candidate_adjusted_score(candidate, type_stats=None, saturation_threshold=2):
    """Adjusted value/effort score. Risk discounts high-risk work, type saturation
    downweights repeating an already-worked type, and a type's blocked history
    discounts candidates in consistently-blocking areas. Returns (score, breakdown)."""
    base = _candidate_score(candidate)
    try:
        risk = int(candidate.get("risk") or 1)
    except (TypeError, ValueError):
        risk = 1
    risk = max(1, min(5, risk))
    risk_factor = max(0.5, 1.0 - 0.08 * (risk - 1))

    candidate_type = candidate.get("type") or "feature"
    if candidate_type not in VALID_CANDIDATE_TYPES:
        candidate_type = "other"
    entry = (type_stats or {}).get(candidate_type, {})
    completed_n = entry.get("completed") or 0
    blocked_n = entry.get("blocked") or 0
    try:
        threshold = max(0, int(saturation_threshold or 0))
    except (TypeError, ValueError):
        threshold = 2
    saturation_factor = 0.85 ** max(0, completed_n - threshold)
    blocked_factor = 0.9 ** blocked_n

    score = base * risk_factor * saturation_factor * blocked_factor
    return score, {
        "base": round(base, 3),
        "risk": risk,
        "risk_factor": round(risk_factor, 3),
        "saturation_factor": round(saturation_factor, 3),
        "blocked_factor": round(blocked_factor, 3),
    }


def rank_candidates(backlog, config):
    """Rank backlog candidates by adjusted value/effort. Pending, dependency-ready
    candidates come first (by score desc), then pending-but-blocked candidates (with
    their blocked_by reasons), then picked/completed/blocked candidates."""
    type_stats = compute_type_stats(backlog)
    threshold = (config or {}).get("type_saturation_threshold", 2)
    entries = []
    for candidate in backlog.get("candidates", []):
        entry = dict(candidate)
        score, breakdown = candidate_adjusted_score(candidate, type_stats, threshold)
        missing, ready = candidate_deps_status(backlog, candidate)
        entry["score"] = round(score, 3)
        entry["score_breakdown"] = breakdown
        entry["ready"] = ready
        entry["blocked_by"] = missing
        entries.append(entry)
    entries.sort(
        key=lambda item: (
            item.get("status") != "pending",
            not item.get("ready"),
            -item.get("score", 0.0),
            item.get("id") or "",
        )
    )
    return entries


def analysis_path_for(repo):
    return repo / io.AUTOPILOT_DIR / io.ANALYSIS_FILENAME


def load_analysis(repo):
    return io.load_json(analysis_path_for(repo), None)


def save_analysis(repo, data):
    io.save_json(analysis_path_for(repo), data)


def config_mtime(repo):
    path = config.config_path_for(repo)
    try:
        return int(path.stat().st_mtime_ns)
    except OSError:
        return None


def analysis_validity(repo):
    """Check whether the cached .autopilot/analysis.json can be reused. Returns
    ('missing'|'fresh'|'stale', reason). A cache is stale when the repository HEAD
    moved since it was saved (the code the analysis described changed) or the
    autopilot config changed (commands/limits the analysis relied on changed)."""
    analysis = load_analysis(repo)
    if analysis is None:
        return "missing", "no cached analysis"
    if not isinstance(analysis, dict):
        return "stale", "cached analysis is corrupt"
    cached_head = analysis.get("git_head")
    current_head = None
    if io.has_commits(repo):
        current_head = io.run_git(repo, "rev-parse", "HEAD").stdout.strip()
    if cached_head != current_head:
        return "stale", "HEAD changed since the analysis was saved"
    cached_mtime = analysis.get("config_mtime")
    current_mtime = config_mtime(repo)
    if cached_mtime != current_mtime:
        return "stale", "autopilot config changed since the analysis was saved"
    return "fresh", "cached analysis is up to date"


def build_retrospective(repo, state, config, lang="zh"):
    """Run-level retrospective: per-type success stats, blocked rounds, and the
    verification setup, in the configured language."""
    zh = lang == "zh"
    out = []
    if zh:
        out.append("# 迭代复盘（Retrospective）")
        out.append("")
        out.append("- 仓库: `{}`".format(state.get("repo")))
        out.append("- run_id: `{}`".format(state.get("run_id")))
        out.append("- 完成轮次: {}".format(state.get("completed_rounds", 0)))
        out.append("- 受阻轮次: {}".format(state.get("blocked_rounds", 0)))
        out.append("- 回滚轮次: {}".format(state.get("reverted_rounds", 0)))
        out.append("- 估算 Token: {}".format(state.get("estimated_tokens_used", 0)))
        out.append("")
        out.append("## 按类型统计")
        out.append("")
    else:
        out.append("# Run Retrospective")
        out.append("")
        out.append("- repo: `{}`".format(state.get("repo")))
        out.append("- run_id: `{}`".format(state.get("run_id")))
        out.append("- completed rounds: {}".format(state.get("completed_rounds", 0)))
        out.append("- blocked rounds: {}".format(state.get("blocked_rounds", 0)))
        out.append("- reverted rounds: {}".format(state.get("reverted_rounds", 0)))
        out.append("- estimated tokens: {}".format(state.get("estimated_tokens_used", 0)))
        out.append("")
        out.append("## Stats by type")
        out.append("")
    stats = state.get("type_stats") or compute_type_stats(load_backlog(repo))
    if not stats:
        out.append("- {}: {}".format("无" if zh else "none", "—"))
    else:
        if zh:
            out.append("| 类型 | 完成 | 受阻 | 受阻率 | 平均工作量 | 平均价值 |")
            out.append("|---|---|---|---|---|---|")
        else:
            out.append("| type | completed | blocked | blocked rate | avg effort | avg value |")
            out.append("|---|---|---|---|---|---|")
        for candidate_type in sorted(stats):
            s = stats[candidate_type]
            out.append("| {} | {} | {} | {} | {} | {} |".format(
                candidate_type,
                s.get("completed", 0),
                s.get("blocked", 0),
                s.get("blocked_rate", 0.0),
                s.get("avg_effort", 0.0),
                s.get("avg_value", 0.0),
            ))
    out.append("")

    blocked = [h for h in state.get("history", []) if h.get("status") == "blocked"]
    out.append("## {}".format("受阻轮次" if zh else "Blocked rounds"))
    out.append("")
    if not blocked:
        out.append("- {}".format("无" if zh else "none"))
    else:
        for entry in blocked:
            out.append("- round {}: {} — {}".format(
                entry.get("round", "?"), entry.get("title", ""), entry.get("reason", "")
            ))
    out.append("")

    checks = config.get("check_commands") or []
    out.append("## {}".format("验证命令" if zh else "Verification commands"))
    out.append("")
    if not checks:
        out.append("- {}".format("未配置" if zh else "none configured"))
    else:
        for command in checks:
            out.append("- `{}`".format(command))
    out.append("")

    ranked = rank_candidates(load_backlog(repo), config)
    ready = [r for r in ranked if r.get("status") == "pending" and r.get("ready")]
    out.append("## {}".format("下一步建议" if zh else "Next likely improvement"))
    out.append("")
    if not ready:
        out.append("- {}".format("无" if zh else "none"))
    else:
        top = ready[0]
        out.append("- {} `{}` (value={}, effort={}, type={})".format(
            top.get("id"), top.get("title"), top.get("value"), top.get("effort"), top.get("type") or "feature"
        ))
    out.append("")
    return "\n".join(out)


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
        out.append("| id | title | type | value/effort | status |")
        out.append("|---|---|---|---|---|")
        for c in backlog:
            label = _STATUS_LABELS["zh" if zh else "en"].get(c.get("status", "pending"), c.get("status", "pending"))
            out.append("| `{}` | {} | {} | {}/{} | {} |".format(
                c.get("id", ""), c.get("title", ""), c.get("type") or "feature",
                c.get("value", ""), c.get("effort", ""), label,
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

    ranked = rank_candidates(load_backlog(repo), config)
    ready = [r for r in ranked if r.get("status") == "pending" and r.get("ready")]
    if ready:
        top = ready[0]
        out.append("## {}".format(L["next"]))
        out.append("")
        out.append("- {} `{}` (value={}, effort={}, type={}, score={})".format(
            top.get("id"), top.get("title"), top.get("value"), top.get("effort"),
            top.get("type") or "feature", top.get("score"),
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
