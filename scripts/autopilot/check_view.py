"""check/loop 决策视图（candidate-316）：从 commands.py 抽出的纯读构造层。

build_check_payload 是循环最热的路径（check 与 round-prep 共用），这里集中
它的全部构件与措辞契约：backlog 健康看板、ready 价值计数、双语提示、逐候选
brief 化，以及 mining_exhausted 的供给枯竭判据。commands.py 保留命令编排与
审计写入（_audit_loop_decision 的 append_log 属命令侧），经由
`from .check_view import ...` 消费本模块。
"""

from datetime import datetime, timezone

from . import config, io, state

def _mining_run_new(run):
    """New-finding count of a mining run, with the pre-1.5.1 field fallback."""
    value = run.get("new")
    if value is None:
        value = run.get("findings")
    return int(value or 0)


def mining_exhausted(st):
    """True only after two consecutive mine runs added nothing new AND
    (when expansion waves exist) the last two waves added nothing either.
    Never-mined is NOT exhausted — the supply side has not been tried.
    Read-only probes (no --apply) don't mutate the backlog, so when findings
    exist their new-count degenerates to the raw count and misreads as churn;
    such runs are excluded. A zero-finding probe IS real zero-new evidence and
    still counts."""
    runs = [
        r for r in (st.get("mining_runs") or [])
        if isinstance(r, dict) and (r.get("apply", True) or not int(r.get("findings") or 0))
    ]
    if len(runs) < 2:
        return False
    if any(_mining_run_new(r) > 0 for r in runs[-2:]):
        return False
    waves = [w for w in (st.get("expansion_waves") or []) if isinstance(w, dict)]
    if waves and any((w.get("added") or 0) > 0 for w in waves[-2:]):
        return False
    return True


def brief_rank_entry(entry):
    """Compact one ranked backlog entry for the loop's per-round read. Ranking
    keeps score_breakdown and provenance because they explain a score to a
    human debugging the ranking; the loop only needs what it acts on, and the
    full form costs ~1.1KB per candidate per round (measured: 18KB for a
    16-candidate backlog)."""
    keep = (
        "id", "title", "type", "value", "effort", "status", "score",
        "ready", "blocked_by", "unlocks", "selected", "below_floor",
        "cut_reason", "origin", "confidence",
    )
    return {key: entry[key] for key in keep if key in entry}


def _backlog_watch(backlog, cfg):
    """Summarize pending backlog health for check. Empty/thin/unready backlog is an
    expansion trigger, never an automatic stop."""
    candidates = backlog.get("candidates") or []
    pending = [c for c in candidates if c.get("status") == "pending"]
    ready = []
    for candidate in pending:
        missing, is_ready = state.candidate_deps_status(backlog, candidate)
        if is_ready:
            ready.append(candidate)
    min_pending = cfg.get("min_pending_candidates")
    if min_pending is None:
        min_pending = 3
    # Expand when there is nothing actionable (ready==0), even if pending is
    # "full" of dependency-blocked items — otherwise check says work while
    # begin-round rejects every candidate.
    needs_expansion = len(ready) == 0 or len(pending) < min_pending
    return {
        "pending": len(pending),
        "ready": len(ready),
        "min_pending_candidates": min_pending,
        "needs_expansion": needs_expansion,
    }


def _ready_valuable_count(backlog, floor):
    """Pending candidates that are dependency-ready and at or above the value
    floor (``floor: null`` disables the filter). Shared by the finish gate,
    check's unverified-goal warning, and begin-round's goals-met refusal —
    "work remains" must mean the same thing in all three."""
    count = 0
    for candidate in backlog.get("candidates") or []:
        if candidate.get("status") != "pending":
            continue
        _, is_ready = state.candidate_deps_status(backlog, candidate)
        if is_ready and (floor is None or state._resolved_value(candidate) >= floor):
            count += 1
    return count


def _check_text(cfg, zh_text, en_text):
    """check guidance follows the report language (report_lang) so zh runs read
    zh advice; anything non-zh falls back to en."""
    return zh_text if (cfg.get("report_lang") or "zh") == "zh" else en_text


def rank_for_selection(backlog, st, cfg):
    """rank_candidates + 选中批次提取的单一出处（轮 24 candidate-317）：
    check 的契约「check 与 rank_candidates 不许给出矛盾答案」由同一次计算
    保证，不再靠四处手写维持（classic 判断曾出现两种等价拼写）。
    classic 模式仍返回完整 ranked（classic 评分在 rank_candidates 内部），
    但 selected 概念不存在 → 计数 0。返回 (ranked, selected_count)。"""
    ranked = state.rank_candidates(
        backlog, cfg, progress=state.progress_from_state(st, cfg),
        completed_goals=list(st.get("completed_goals") or []),
    )
    if (cfg.get("ranking_mode", "expected") or "expected") == "classic":
        return ranked, 0
    return ranked, len([e for e in ranked if e.get("selected")])


def build_check_payload(repo, full=False):
    """Build the check payload without printing it. `check` and `round-prep`
    share this: round-prep is check's loop-driving fields plus the ranked
    candidates, directives and schedule, so the loop needs one call per round
    instead of four. `full` adds the state and config dump (the debugging
    view; 5x the payload — the loop's contract is the brief form)."""
    # Validate git first: a non-git path must fail as "Not a git repository",
    # not as the misleading "state.json not found" that load_state would print.
    io.git_dir_for(repo)
    st = state.load_state(repo)
    cfg = config.load_config(repo)
    stop_reason = state.compute_stop_reason(st, cfg)
    warnings = []

    if st.get("current_round") is not None:
        warnings.append("current_round is open; complete, block, or cancel it before starting a new round.")

    repo_has_commits = io.has_commits(repo)
    # Probed once and shared by the detached-HEAD check and the feature-mode
    # branch-drift warning below: check is the loop's hottest command.
    head_branch = io.current_branch(repo) if repo_has_commits else None
    if not repo_has_commits:
        warnings.append("Repository has no commits yet; git log is unavailable and the first round creates the initial commit.")
    elif head_branch == "HEAD":
        warnings.append("Detached HEAD; consider checking out a branch before starting.")

    # Branch drift in feature mode: the round commands will refuse (commit/
    # begin-round/complete-round), so say so here — check is the pre-flight.
    # A FINISHED run no longer issues round commands; the recovery advice is
    # noise there and can point at a branch finish already reclaimed
    # (1.12 field test: finished runs warned about their deleted branch).
    if cfg.get("branch_mode") == "feature" and st.get("branch") and not st.get("finished_at"):
        expected = st["branch"]
        if head_branch == "HEAD":
            warnings.append(_check_text(
                cfg,
                "HEAD 处于 detached 状态，而本轮运行分支是 `{b}`；commit/begin-round/complete-round "
                "将被拒绝（提交会悬空）。运行 ensure-branch 或 `git checkout {b}` 恢复。".format(b=expected),
                "Detached HEAD while the run's branch is `{b}`; commit/begin-round/complete-round "
                "will be refused (the commit would be orphaned). Run ensure-branch or "
                "`git checkout {b}` to recover.".format(b=expected),
            ))
        elif head_branch != expected:
            warnings.append(_check_text(
                cfg,
                "分支漂移：当前在 `{c}`，而本轮运行分支是 `{b}`；commit/begin-round/complete-round 将被拒绝，"
                "防止把运行提交落到（并随 push 发布）错误分支。运行 ensure-branch 或 `git checkout {b}` 恢复。".format(
                    c=head_branch, b=expected
                ),
                "Branch drift: HEAD is on `{c}` but the run's branch is `{b}`; "
                "commit/begin-round/complete-round will be refused so the run's commits do not "
                "land on (and get pushed from) the wrong branch. Run ensure-branch or "
                "`git checkout {b}` to recover.".format(c=head_branch, b=expected),
            ))

    if st.get("config_fingerprint") and io.file_sha256(config.config_path_for(repo)) != st["config_fingerprint"]:
        io.append_log(repo, "config-drift", "warn")
        warnings.append(
            "autopilot config.json changed since init; verify the change was intentional "
            "(check_commands are executed and budgets are trusted by the loop)."
        )

    # Only the FIRST begin-round refuses a dirty tree; once a round is open
    # this warning contradicts reality (later rounds only WARN), so it is
    # emitted for the no-open-round case only.
    if st.get("current_round") is None and io.working_tree_dirty(repo):
        if not cfg.get("allow_uncommitted_changes"):
            if (cfg.get("commit_every_rounds") or 1) > 1:
                # Batch mode defers commits on purpose — same wording
                # contract as begin-round's warning (fixed 1.12.3 there,
                # synced here in 1.12.4): do NOT advise "commit or stash".
                # "dirty" stays a stable token for consumers grepping it.
                warnings.append(
                    "Working tree is dirty with deferred work from earlier rounds — expected in batch "
                    "mode (commit_every_rounds={}); the next flush round commits the batch.".format(
                        cfg.get("commit_every_rounds")
                    )
                )
            else:
                warnings.append(
                    "Working tree is dirty and allow_uncommitted_changes is false; only the first "
                    "begin-round refuses a dirty tree, later rounds only warn. Commit or stash before "
                    "starting the run, or set allow_uncommitted_changes: true."
                )
        else:
            warnings.append(
                "Working tree is dirty and allow_uncommitted_changes is true; pre-existing user "
                "changes may be committed as part of a round."
            )

    goals = cfg.get("goals") or []
    if not goals:
        warnings.append("goals is empty; the run is open-ended and will stop only on the configured budgets.")
    if (
        cfg.get("max_rounds") is None
        and cfg.get("max_minutes") is None
        and cfg.get("deadline") is None
        and cfg.get("max_tokens") is None
        and cfg.get("max_blocked_in_a_row") is None
        and not goals
    ):
        warnings.append("No stop condition is configured (goals and all max_* and deadline are unset); the loop has no automatic stopping point.")
    if goals and cfg.get("expand_after_goals") and (
        cfg.get("max_rounds") is None
        and cfg.get("max_minutes") is None
        and cfg.get("deadline") is None
        and cfg.get("max_tokens") is None
        and cfg.get("max_blocked_in_a_row") is None
    ):
        warnings.append("expand_after_goals is true but no other stop condition is configured; the expansion phase has no automatic stopping point.")
    if cfg.get("review_threshold") is not None:
        warnings.append("review_threshold is set; complete-round will require --review-score >= {}.".format(cfg["review_threshold"]))

    if not cfg.get("scan_secrets", True):
        warnings.append(_check_text(
            cfg,
            "secret 扫描已关闭（scan_secrets: false）：提交时不会拦截疑似密钥，请确认这是有意配置。",
            "Secret scanning is disabled (scan_secrets: false); secret-like content will not be "
            "blocked on commit. Confirm this is intentional.",
        ))

    if cfg.get("push"):
        remotes = io.run_git(repo, "remote")
        if remotes.returncode == 0 and not remotes.stdout.strip():
            warnings.append("push is true but no git remote is configured; complete-round will warn on every push attempt.")

    blocked_streak = state.count_consecutive_blocked(st)
    max_blocked = cfg.get("max_blocked_in_a_row")
    if max_blocked is not None and 1 <= blocked_streak < max_blocked:
        warnings.append(
            "blocked streak {}/{}: one more blocked round stops the run; a quality-failed "
            "round can complete instead via complete-round --below-threshold".format(
                blocked_streak, max_blocked
            )
        )

    if (
        cfg.get("review_threshold") is None
        and st.get("completed_rounds", 0) >= 3
        and all((entry.get("review_n") or 0) == 0 for entry in (st.get("type_stats") or {}).values())
    ):
        warnings.append(_check_text(
            cfg,
            "价值校准未激活，候选 value 自评未经验证；每轮 complete-round 传 --review-score",
            "Value calibration is inactive: candidate value self-scores are unverified; pass "
            "--review-score on every complete-round",
        ))

    backlog = state.load_backlog(repo)
    backlog_watch = _backlog_watch(backlog, cfg)
    candidates_per_round = cfg.get("candidates_per_round") or 1
    if backlog_watch["pending"] >= 2 * candidates_per_round:
        warnings.append(
            "Backlog pending ({}) is at least twice the batch width ({}); consider raising "
            "candidates_per_round to amortize the per-round fixed cost (verify, commit) over "
            "more work.".format(backlog_watch["pending"], candidates_per_round)
        )
    goals_unverified = state.unverified_goals(st, cfg)
    ready_floor = _ready_valuable_count(backlog, cfg.get("min_candidate_value"))
    if goals_unverified and ready_floor > 0:
        warnings.append(_check_text(
            cfg,
            "goal {} 未关联到已完成轮次（--round）；尚有 {} 条 value>=floor 的 ready 候选，不得 finish".format(
                ", ".join(goals_unverified), ready_floor),
            "goal {} has no completed-round anchor (--round); {} ready candidates at value>=floor "
            "remain, so finishing is withheld".format(", ".join(goals_unverified), ready_floor),
        ))
    # Lens-rotation observability: Deep Expansion runs in BOTH phases (thin
    # backlog and post-goal expansion), so the rotation keys live at the top
    # level rather than inside the expand-only payload. lenses_used is the
    # order-preserving union across recorded waves; lenses_unused is what the
    # next wave should draw from.
    waves = [w for w in (st.get("expansion_waves") or []) if isinstance(w, dict)]
    lenses_used = []
    for wave in waves:
        for lens in wave.get("lenses") or []:
            if lens not in lenses_used:
                lenses_used.append(lens)
    analysis_status, analysis_reason = state.analysis_validity(repo)
    commits_behind = state.analysis_commits_behind(repo)
    if backlog_watch["needs_expansion"] and analysis_status == "stale" and (commits_behind or 0) >= 3:
        warnings.append(
            "analysis cache is {} commits stale; run analysis-load + analysis-save with a "
            "fresh scan before the next expansion wave".format(commits_behind)
        )
    ranked, selected_count = rank_for_selection(backlog, st, cfg)
    selected_entries = [e for e in ranked if e.get("selected")]
    selected_empty_reason = None
    if (cfg.get("ranking_mode", "expected") or "expected") != "classic":
        if not selected_entries:
            ready_pool = [e for e in ranked
                          if e.get("status") == "pending" and e.get("ready") and not e.get("below_floor")]
            ready_below = [e for e in ranked
                           if e.get("status") == "pending" and e.get("ready") and e.get("below_floor")]
            if ready_pool:
                selected_empty_reason = ready_pool[0].get("cut_reason") or "quota"
            elif ready_below:
                selected_empty_reason = "floor"
    open_seed_list = state.open_seeds(st)
    seed_hint = ""
    if open_seed_list:
        seed_hint = (
            " Wave 0 first: verify and value-gate the open direction seeds ({}) via "
            "'backlog-add --from-seed <id>'; lens-scan Deep Expansion is the second wave.".format(
                ", ".join(seed.get("id") or "" for seed in open_seed_list[:5])
            )
        )
    action_hint = "expand" if (stop_reason is None and backlog_watch["needs_expansion"]) else (
        "stop" if stop_reason is not None else "work"
    )
    if (
        action_hint == "work"
        and (cfg.get("ranking_mode", "expected") or "expected") != "classic"
        and not selected_entries
        and backlog_watch["ready"] > 0
    ):
        # Contract: action_hint=work must imply a non-empty recommended batch.
        # Quota/type/cutoff cuts emptied the batch while ready candidates sit
        # in the pool — expanding (new candidates or quota relief) is the
        # honest move, not grinding a pool rank refused to select.
        action_hint = "expand"

    # Deterministic mining comes BEFORE judgment expansion: when the supply
    # side is thin/empty and `mine` is stale, the next step is `mine --apply`.
    # A non-empty ready pool that was quota/type-cut is an expansion problem
    # (diversity / origin limits), not a mining problem — leave those as expand.
    mining_runs = [r for r in (st.get("mining_runs") or []) if isinstance(r, dict)]
    last_mine = mining_runs[-1] if mining_runs else None
    mine_stale = (
        last_mine is None
        or (last_mine.get("findings") or 0) > (last_mine.get("applied") or 0)
    )
    if stop_reason is None and mine_stale and action_hint in ("expand", "work"):
        min_pending = backlog_watch.get("min_pending_candidates")
        if min_pending is None:
            min_pending = 3
        supply_thin = (
            backlog_watch["pending"] == 0
            or backlog_watch["ready"] == 0
            or backlog_watch["pending"] < min_pending
        )
        quota_cut = bool(selected_empty_reason) and backlog_watch["ready"] > 0
        if supply_thin and not quota_cut:
            action_hint = "mine"

    if stop_reason is None and action_hint == "mine":
        # ONE actionable path replaces the formerly contradictory pair ("run
        # Deep Expansion now" vs "run deterministic mining first"): mine is the
        # first supply side, Deep Expansion only tops up what mining cannot.
        warnings.append(_check_text(
            cfg,
            "Backlog 供给不足（pending={p}，ready={r}，min_pending_candidates={m}）："
            "先 `mine --apply`（再 backlog-rank）；mine 之后 backlog 仍薄才做 Deep Expansion 探索。"
            "Do not idle and do not finish — mining is the supply side.".format(
                p=backlog_watch["pending"], r=backlog_watch["ready"],
                m=backlog_watch["min_pending_candidates"],
            ),
            "Backlog supply is thin (pending={p}, ready={r}, min_pending_candidates={m}): "
            "run `mine --apply` first (then backlog-rank); run Deep Expansion only if the "
            "backlog is still thin after mining. "
            "Do not idle and do not finish — mining is the supply side.".format(
                p=backlog_watch["pending"], r=backlog_watch["ready"],
                m=backlog_watch["min_pending_candidates"],
            ),
        ) + seed_hint)
    elif stop_reason is None and backlog_watch["needs_expansion"]:
        if backlog_watch["pending"] == 0:
            warnings.append(
                "Backlog has no pending candidates and deterministic mining is already fresh. "
                "Do NOT idle or wait for the deadline: run Deep Expansion now (spawn explore "
                "subagents, add 3-5 candidates with backlog-add). Empty backlog is an expansion "
                "trigger, not a reason to stop."
                + seed_hint
            )
        elif backlog_watch["ready"] == 0:
            warnings.append(
                "Backlog has {} pending candidates but 0 are dependency-ready. "
                "Unblock depends-on chains or Deep Expansion for independent work; "
                "do not idle.".format(backlog_watch["pending"])
            )
        else:
            warnings.append(
                "Backlog pending candidates ({}) is below min_pending_candidates ({}). "
                "Top up via Deep Expansion (subagent scout) before the next begin-round; "
                "do not grind the last thin candidates or idle. You may still begin-round "
                "with existing ready candidates while expanding in parallel.".format(
                    backlog_watch["pending"], backlog_watch["min_pending_candidates"]
                )
            )

    payload = {
        "continue": stop_reason is None,
        "stop_reason": stop_reason,
        "warnings": warnings,
        "backlog": backlog_watch,
        "action_hint": action_hint,
        # 轮次进度（预算可见性）：max_rounds 为 null 时 remain 亦为 null
        "round_progress": {
            "round_seq": st.get("round_seq", 0),
            "max_rounds": cfg.get("max_rounds"),
            "remaining": (
                max(0, cfg["max_rounds"] - st.get("round_seq", 0))
                if isinstance(cfg.get("max_rounds"), int) else None
            ),
        },
        "selected_count": len(selected_entries),
        "selected_empty_reason": selected_empty_reason,
        "wave_no": len(waves),
        "lenses_used": lenses_used,
        "lenses_unused": [lens for lens in state.EXPANSION_LENSES if lens not in lenses_used],
        "mining": {
            "runs": len(mining_runs),
            "last_findings": (last_mine or {}).get("findings"),
            "last_applied": (last_mine or {}).get("applied"),
            "exhausted": mining_exhausted(st),
        },
        "analysis": {
            "status": analysis_status,
            "reason": analysis_reason,
            "commits_behind": commits_behind,
            # Same key set as round-prep's analysis object (which overwrites
            # this when the cache is fresh) — same-named fields must not
            # exist in one payload and vanish from the other.
            "cached": None,
        },
    }
    goals_met = state.all_goals_met(cfg, st)
    payload["goals_met"] = goals_met
    payload["goals_unverified"] = goals_unverified
    payload["phase"] = "expand" if (goals_met and cfg.get("expand_after_goals")) else "iterate"
    if payload["phase"] == "expand":
        # Expansion context for Wave 0 (seed wave). Key set is stable even when
        # every list is empty — `seeds` is [] rather than a missing key so the
        # consumer's key-existence check never flips between runs.
        type_stats = st.get("type_stats") or {}
        recent_types = {}
        for candidate_type in sorted(type_stats):
            entry = type_stats.get(candidate_type)
            if not isinstance(entry, dict):
                continue
            try:
                recent_types[candidate_type] = int(entry.get("completed") or 0)
            except (TypeError, ValueError, OverflowError):
                recent_types[candidate_type] = 0
        saturated = state.saturated_types(st, cfg.get("type_saturation_threshold", 2))
        underused = [t for t in state.VALID_CANDIDATE_TYPES if t not in saturated]
        suggested_themes = []
        for event in reversed(st.get("goal_events") or []):
            if not isinstance(event, dict):
                continue
            if event.get("goal"):
                suggested_themes.append("follow up on goal: {}".format(event["goal"]))
            suggested_themes.extend((event.get("recent_commit_topics") or [])[:2])
            break
        payload["expansion"] = {
            "seeds": [
                {
                    "id": seed.get("id"),
                    "title": seed.get("title"),
                    "type": seed.get("type") or "feature",
                    "from_capability": seed.get("from_capability") or "",
                    "source_goal": seed.get("source_goal") or "",
                    "status": seed.get("status"),
                }
                for seed in state.open_seeds(st)
            ],
            "completed_goals": list(st.get("completed_goals") or []),
            "recent_types": recent_types,
            "saturated_types": saturated,
            "underused_types": underused,
            "suggested_themes": suggested_themes,
            "min_pending_candidates": cfg.get("min_pending_candidates") if cfg.get("min_pending_candidates") is not None else 3,
        }
    completed_total = (
        st.get("completed_rounds", 0)
        + st.get("blocked_rounds", 0)
        + st.get("cancelled_rounds", 0)
        + st.get("reverted_rounds", 0)
    )
    # Boundary math: the anchor is the next round that will CLOSE (completed
    # rounds plus the open one, if any). Round k is itself a boundary when
    # k % every == 0 — with commit_every_rounds 5 the next boundary after 4
    # closed rounds is 5, and once round 5 has closed the same formula points
    # at 10 instead of re-reporting the finished round.
    anchor = completed_total + 1
    verify_every = cfg.get("verify_every_rounds") or 1
    commit_every = cfg.get("commit_every_rounds") or 1
    payload["next_verify_round"] = ((anchor - 1) // verify_every + 1) * verify_every
    payload["next_commit_round"] = ((anchor - 1) // commit_every + 1) * commit_every
    checkpoint_every = cfg.get("checkpoint_every")
    payload["next_checkpoint_round"] = (
        ((anchor - 1) // checkpoint_every + 1) * checkpoint_every if checkpoint_every else None
    )
    payload["blocked_streak"] = blocked_streak

    # Budget observability: max_minutes measures wall-clock since the last
    # round activity (pausing burns it), deadline is an absolute moment.
    max_minutes = cfg.get("max_minutes")
    remaining_minutes = None
    if max_minutes is not None:
        last_activity = io.parse_time(st.get("last_activity_at") or st.get("started_at"))
        if last_activity is not None:
            elapsed = (datetime.now(timezone.utc) - last_activity).total_seconds() / 60
            remaining_minutes = round(max(0.0, max_minutes - elapsed), 1)
    deadline_remaining = None
    deadline_at = io.parse_time(cfg.get("deadline"))
    if deadline_at is not None:
        deadline_remaining = round((deadline_at - datetime.now(timezone.utc)).total_seconds() / 60, 1)
    payload["budget"] = {
        "max_minutes": max_minutes,
        "last_activity_at": st.get("last_activity_at"),
        "remaining_minutes": remaining_minutes,
        "deadline_remaining_minutes": deadline_remaining,
    }
    # Subagent spend from an expansion wave is invisible to max_tokens (that
    # estimate counts diffs), so the wave cap is the bound that actually holds.
    max_waves = cfg.get("max_expansion_waves")
    waves_used = state.expansion_wave_count(st)
    payload["expansion_budget"] = {
        "waves_used": waves_used,
        "max_waves": max_waves,
        "waves_left": None if max_waves is None else max(0, max_waves - waves_used),
    }
    if max_waves is not None and waves_used >= max_waves and stop_reason is None:
        # `warnings` is the same list object already bound into payload.
        warnings.append(
            "Expansion wave cap reached ({}/{}): stop expanding and work the remaining backlog to a "
            "stop condition, or raise the cap with `config-set --max-expansion-waves N`. Note that "
            "max_tokens does not see subagent spend (it estimates from diffs), so this cap is the "
            "only hard bound on expansion cost.".format(waves_used, max_waves)
        )
    if full:
        payload["state"] = st
        payload["config"] = cfg
    # Round-prep reuse channel (round 12 perf candidate): ranked/cfg/state/
    # backlog are already computed here — round-prep used to re-load all
    # three, re-rank, and re-run analysis validity (~4 extra git spawns and
    # a full re-rank per round). cmd_check strips this private key before
    # printing; the JSON contract does not change.
    payload["_ctx"] = {
        "st": st, "cfg": cfg, "backlog": backlog,
        "ranked": ranked, "selected_entries": selected_entries,
    }
    return payload


