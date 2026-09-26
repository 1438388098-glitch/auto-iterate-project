"""Command handlers: init, rounds, backlog, commit, check, report, push, undo.

Orchestration only: secret scanning lives in ``secrets``, path guarding in
``guard``, verification discovery in ``verify``, and token estimation in
``io.estimate_tokens_for_round``."""

import json
import math
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import config, io, state
from .guard import path_allowed
from .secrets import scan_staged_diff
from . import miner
from .verify import detect_verify_commands


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


def _require_initialized(args, repo, message="[ERROR] state.json not found. Run init first."):
    """Refuse an uninitialized repo BEFORE acquiring the run lock: the lock
    mkdirs .autopilot/, so a refused command must not leave the state directory
    behind in a repo that never opted in. The explicit guards pass their own
    "not initialized" text; the default mirrors state.load_state's wording so
    the two message families stay stable."""
    if not config.state_path_for(repo).exists():
        return emit_result(args, False, message)
    return None


def _feature_branch_guard(repo, st, cfg, action):
    """Refusal message when HEAD has drifted from the run's autopilot branch,
    or None when it is safe to proceed. feature branch_mode only: a manual
    checkout to another branch must not let round commands commit on it (a
    complete-round push would then publish the wrong branch), and a detached
    HEAD would orphan the commit. current mode records no branch and gets no
    guard; ensure-branch stays available as the repair path."""
    if cfg.get("branch_mode") != "feature" or not st.get("branch"):
        return None
    expected = st["branch"]
    current = io.current_branch(repo)
    if current == "HEAD":
        return (
            "[ERROR] Detached HEAD; refusing to {} because the commit would be orphaned. "
            "This run's branch is '{}': run 'ensure-branch' or 'git checkout {}' first.".format(
                action, expected, expected
            )
        )
    if current != expected:
        return (
            "[ERROR] Branch drift: HEAD is on '{}' but this run's branch is '{}'. Refusing to {} "
            "so the run's commits do not land on (and get pushed from) the wrong branch. "
            "Run 'ensure-branch' or 'git checkout {}' first.".format(
                current, expected, action, expected
            )
        )
    return None


def cmd_init(args):
    repo = Path(args.repo).resolve()
    # Validate the git repo BEFORE any filesystem side effect: acquiring the
    # lock mkdirs .autopilot/, so a mistyped --repo must be rejected first —
    # and --dry-run promises a run that changes no state (cli help), lock file
    # included. Everything up to the lock is read-only.
    git_dir = io.git_dir_for(repo)

    state_path = config.state_path_for(repo)
    if state_path.exists() and not args.force:
        # Answered before the dirty-tree gate: an existing run must get the
        # "resume" hint, not a dirty-tree complaint about an init that will
        # never happen. Non-zero exit: silently dropping the new flags behind
        # an exit 0 made re-inits look successful while doing nothing.
        return emit_result(
            args, False,
            "[WARN] state.json already exists. Resume with read/check instead of reinitializing.",
        )

    cfg = config.default_config(repo)
    # Inherit the existing config only WITHOUT --force: with --force the
    # defaults plus this command line are the single source of truth, so
    # `init --force` is a real recovery exit for a corrupt config (it used
    # to carry the broken values over and re-create the same dead state).
    if not args.force:
        existing_config = io.load_json(config.config_path_for(repo), None)
        if existing_config is not None:
            cfg.update(existing_config)
    cfg["repo"] = str(repo)

    if args.goal:
        cfg["goals"] = args.goal
    if args.goals_from_prompt:
        cfg["goals"] = state.split_goals(args.goals_from_prompt)
    # Only negatives are illegal here (zero is meaningful: max_rounds 0
    # stops immediately, retries 0 disables retrying). This mirrors the
    # non-negative policy in load_config — before this gate, a negative
    # knob faked a successful init and then failed on every load, and
    # max_blocked_in_a_row < 0 silently stopped the loop with zero rounds.
    for knob in ("--max-rounds", "--max-minutes", "--max-tokens",
                 "--max-round-scope", "--retries-per-round",
                 "--max-blocked-in-a-row", "--max-expansion-per-round"):
        value = getattr(args, knob.lstrip("-").replace("-", "_"), None)
        if value is None:
            continue
        # `--max-minutes nan` passes every comparison, fakes a successful
        # init, and writes a literal NaN into config.json.
        if isinstance(value, float) and not math.isfinite(value):
            return emit_result(
                args, False,
                "[ERROR] {} must be a finite number (NaN/inf budgets never trigger).".format(knob),
            )
        if value < 0:
            return emit_result(
                args, False,
                "[ERROR] {} must be a non-negative integer (negative values "
                "silently stop the loop or fail on first load).".format(knob),
            )
    if args.max_rounds is not None:
        cfg["max_rounds"] = args.max_rounds
    if args.max_minutes is not None:
        cfg["max_minutes"] = args.max_minutes
    if args.deadline is not None:
        resolved = io.parse_deadline(args.deadline)
        if resolved is None:
            return emit_result(
                args, False,
                "[ERROR] Could not parse --deadline '{}'. Use an ISO timestamp "
                "(2026-08-10T08:00:00), a relative duration (+8h / +30min / +1d), "
                "or a local HH:MM wall-clock time.".format(args.deadline),
            )
        cfg["deadline"] = resolved
    if args.max_tokens is not None:
        cfg["max_tokens"] = args.max_tokens
    if args.max_round_scope is not None:
        cfg["max_round_scope"] = args.max_round_scope
    if args.branch_mode is not None:
        cfg["branch_mode"] = args.branch_mode
    if args.allow_uncommitted_changes:
        cfg["allow_uncommitted_changes"] = True
    if args.track_state:
        cfg["track_state"] = True
    if args.check_commands:
        cfg["check_commands"] = args.check_commands
    if args.push:
        cfg["push"] = True
    if args.commit_message_prefix is not None:
        cfg["commit_message_prefix"] = args.commit_message_prefix
    if args.retries_per_round is not None:
        cfg["retries_per_round"] = args.retries_per_round
    if args.candidates_per_round is not None:
        if args.candidates_per_round < 1:
            return emit_result(args, False, "[ERROR] --candidates-per-round must be a positive integer.")
        cfg["candidates_per_round"] = args.candidates_per_round
    if args.max_blocked_in_a_row is not None:
        cfg["max_blocked_in_a_row"] = args.max_blocked_in_a_row
    if args.commit_every_rounds is not None:
        if args.commit_every_rounds < 1:
            return emit_result(args, False, "[ERROR] --commit-every-rounds must be a positive integer.")
        cfg["commit_every_rounds"] = args.commit_every_rounds
    if args.verify_every_rounds is not None:
        if args.verify_every_rounds < 1:
            return emit_result(args, False, "[ERROR] --verify-every-rounds must be a positive integer.")
        cfg["verify_every_rounds"] = args.verify_every_rounds
    if args.checkpoint_every is not None:
        if args.checkpoint_every < 1:
            return emit_result(args, False, "[ERROR] --checkpoint-every must be a positive integer.")
        cfg["checkpoint_every"] = args.checkpoint_every
    if args.expand_after_goals is not None:
        cfg["expand_after_goals"] = bool(args.expand_after_goals)
    if args.review_threshold is not None:
        if args.review_threshold < 1 or args.review_threshold > 5:
            return emit_result(args, False, "[ERROR] --review-threshold must be an integer 1-5.")
        cfg["review_threshold"] = args.review_threshold
    if args.scan_secrets is not None:
        cfg["scan_secrets"] = args.scan_secrets
    if args.secret_pattern:
        cfg["secret_patterns"] = list(args.secret_pattern)
    if args.type_saturation_threshold is not None:
        if args.type_saturation_threshold < 0:
            return emit_result(args, False, "[ERROR] --type-saturation-threshold must be a non-negative integer.")
        cfg["type_saturation_threshold"] = args.type_saturation_threshold
    if args.ranking_mode is not None:
        cfg["ranking_mode"] = args.ranking_mode
    if args.min_candidate_value is not None:
        if args.min_candidate_value < 1 or args.min_candidate_value > 5:
            return emit_result(args, False, "[ERROR] --min-candidate-value must be an integer 1-5.")
        cfg["min_candidate_value"] = args.min_candidate_value
    if args.max_same_type_per_round is not None:
        if args.max_same_type_per_round < 1:
            return emit_result(args, False, "[ERROR] --max-same-type-per-round must be a positive integer.")
        cfg["max_same_type_per_round"] = args.max_same_type_per_round
    if getattr(args, "min_pending_candidates", None) is not None:
        if args.min_pending_candidates < 0:
            return emit_result(args, False, "[ERROR] --min-pending-candidates must be a non-negative integer.")
        cfg["min_pending_candidates"] = args.min_pending_candidates
    if getattr(args, "max_predicted_per_round", None) is not None:
        if args.max_predicted_per_round < 0:
            return emit_result(args, False, "[ERROR] --max-predicted-per-round must be a non-negative integer.")
        cfg["max_predicted_per_round"] = args.max_predicted_per_round
    # Negative values are already rejected by the knob gate above.
    if getattr(args, "max_expansion_per_round", None) is not None:
        cfg["max_expansion_per_round"] = args.max_expansion_per_round
    if args.allow_path:
        cfg["allow_paths"] = list(args.allow_path)
    if args.deny_path:
        cfg["deny_paths"] = list(args.deny_path)
    if args.report_lang is not None:
        cfg["report_lang"] = args.report_lang

    if not cfg.get("allow_uncommitted_changes") and not args.force and io.working_tree_dirty(repo):
        return emit_result(
            args, False,
            "[ERROR] Working tree is dirty and allow_uncommitted_changes is false. "
            "Commit or stash user changes, or run init with --allow-uncommitted-changes "
            "(or --force to override).",
        )

    if getattr(args, "dry_run", False):
        print(
            "[DRY-RUN] Would initialize autopilot state in {} with run_id {}, {} rounds, goals={}.".format(
                repo, uuid.uuid4().hex[:io.RUN_ID_LENGTH], cfg.get("max_rounds"), cfg.get("goals")
            ),
            file=sys.stderr,
        )
        return 0

    # Rejections above all happen before this lock: the lock is the first
    # filesystem side effect and is only taken once init is committed to
    # writing, so a refused init never leaves a .autopilot/ behind.
    with io.run_lock(repo):
        # Re-check under the lock: another init may have won the race between
        # the pre-lock check and here.
        if state_path.exists() and not args.force:
            return emit_result(
                args, False,
                "[WARN] state.json already exists. Resume with read/check instead of reinitializing.",
            )
        # Same validation the next load_config would apply — an init that
        # writes a config it could never re-load is a bricked run. The source
        # label keeps the error pointing at the init flag, not at a config
        # file that does not exist yet.
        config.validate_config(cfg, source="values from init flags")
        config.save_config(repo, cfg)
        started_at = io.now_iso()
        run_id = uuid.uuid4().hex[:io.RUN_ID_LENGTH]
        st = state.default_state(repo, cfg["goals"], config_fingerprint=io.file_sha256(config.config_path_for(repo)))
        st["run_id"] = run_id
        st["created_at"] = started_at
        st["started_at"] = started_at
        st["last_activity_at"] = started_at
        # Anchor for run-level monotonic token accounting: everything this run
        # commits is measured from here (EMPTY_TREE on an unborn repo).
        init_head = io.run_git(repo, "rev-parse", "--verify", "-q", "HEAD")
        st["run_start_sha"] = init_head.stdout.strip() if init_head.returncode == 0 else io.EMPTY_TREE
        state.save_state(repo, st)
        io.ensure_git_exclude(git_dir, cfg.get("track_state", False), to_stderr=getattr(args, "json", False))
        state.ensure_branch(repo, st, cfg, to_stderr=getattr(args, "json", False))
        io.append_log(repo, "init", "success", run_id=run_id, branch_mode=cfg.get("branch_mode"))
        return emit_result(args, True, "[OK] Initialized autopilot state.", data={"run_id": run_id})
def cmd_read(args):
    repo = Path(args.repo).resolve()
    # Validate git first: a non-git path must fail as "Not a git repository",
    # not as the misleading "state.json not found" that load_state would print.
    io.git_dir_for(repo)
    print(json.dumps(state.load_state(repo), indent=2, ensure_ascii=False))
    return 0


def cmd_begin_round(args):
    repo = Path(args.repo).resolve()
    refused = _require_initialized(args, repo)
    if refused is not None:
        return refused
    with io.run_lock(repo):
        st = state.load_state(repo)
        if st["current_round"] is not None:
            io.append_log(repo, "begin-round", "error", reason="round already open")
            return emit_result(args, False, "[ERROR] A round is already open. Complete, block, or cancel it first.")

        cfg = config.load_config(repo)
        drift = _feature_branch_guard(repo, st, cfg, "begin a round")
        if drift is not None:
            io.append_log(repo, "begin-round", "error", reason="branch drift")
            return emit_result(args, False, drift)
        stop_reason = state.compute_stop_reason(st, cfg)
        if stop_reason is not None:
            io.append_log(repo, "begin-round", "error", reason=stop_reason)
            if stop_reason == "all goals met":
                # The stop-reason string itself stays untouched (priority tests
                # pin it); only the refusal message explains the way back in.
                n = _ready_valuable_count(state.load_backlog(repo), cfg.get("min_candidate_value"))
                return emit_result(
                    args, False,
                    "[ERROR] Autopilot is stopped: all goals met (expand_after_goals=false)。"
                    "backlog 尚有 {} 条 value>=min_candidate_value 的 ready 候选；"
                    "要继续探索请运行 config-set --expand-after-goals 或修改 .autopilot/config.json。".format(n),
                )
            return emit_result(args, False, "[ERROR] Autopilot is stopped: {}. Finish or adjust the config before opening a new round.".format(stop_reason))

        # Round numbers come from a monotonic sequence, not from the round
        # counters: zero-work aborted rounds do not advance max_rounds's
        # denominator, yet their numbers are never reused.
        round_number = st.get("round_seq", 0) + 1
        if getattr(args, "dry_run", False):
            print(
                "[DRY-RUN] Would open round {}: {}.".format(round_number, args.title),
                file=sys.stderr,
            )
            return 0
        candidate_ids = list(args.candidate_id) if args.candidate_id else []
        backlog = state.load_backlog(repo)
        floor = cfg.get("min_candidate_value")
        ready_above = []
        ready_below = []
        for candidate in backlog.get("candidates") or []:
            if candidate.get("status") != "pending":
                continue
            missing, is_ready = state.candidate_deps_status(backlog, candidate)
            if not is_ready:
                continue
            below = floor is not None and state._resolved_value(candidate) < floor
            (ready_below if below else ready_above).append(candidate)
        if not candidate_ids:
            if ready_above:
                io.append_log(repo, "begin-round", "error", reason="no candidate ids while ready backlog exists")
                return emit_result(
                    args, False,
                    "[ERROR] begin-round requires at least one --candidate-id when the backlog "
                    "has ready pending candidates (found {}). Pick with backlog-rank, or clear/"
                    "complete those candidates first. Empty rounds on a stocked backlog are refused "
                    "to prevent churn.".format(len(ready_above)),
                )
            if ready_below:
                io.append_log(
                    repo, "begin-round", "error",
                    reason="only below-floor candidates ready",
                    ready=len(ready_below),
                )
                return emit_result(
                    args, False,
                    "[ERROR] Only below-floor candidates are ready ({}). Run Deep Expansion first, "
                    "or pass --candidate-id explicitly to accept a quick win.".format(len(ready_below)),
                )
            print(
                "[WARN] begin-round without --candidate-id and no ready pending backlog items; "
                "this is an exploratory round. Prefer picking candidates via backlog-rank.",
                file=sys.stderr,
            )
        if candidate_ids:
            for cid in candidate_ids:
                candidate = state.find_candidate(backlog, cid)
                if candidate is None:
                    io.append_log(repo, "begin-round", "error", reason="candidate not found")
                    pending_ids = [c.get("id") for c in backlog.get("candidates") or []
                                   if c.get("status") == "pending"]
                    hint = ", ".join(str(p) for p in pending_ids[:5]) if pending_ids else "none (run backlog-rank)"
                    return emit_result(
                        args, False,
                        "[ERROR] Candidate not found in backlog: {}. Pending: {}".format(cid, hint),
                    )
                missing, ready = state.candidate_deps_status(backlog, candidate)
                if not ready:
                    io.append_log(repo, "begin-round", "error", reason="candidate deps unresolved", deps=missing)
                    return emit_result(
                        args, False,
                        "[ERROR] Candidate {} depends on unfinished work and cannot be picked: {}".format(
                            cid, "; ".join(missing)
                        ),
                    )
        if floor is not None:
            low_value_ids = [
                cid for cid in candidate_ids
                if state._resolved_value(state.find_candidate(backlog, cid) or {}) < floor
            ]
            if len(low_value_ids) > 1:
                print(
                    "[WARN] {} picked candidates are below min_candidate_value ({}); "
                    "at most one quick-win per round is recommended — the rest will compete "
                    "with higher-value work for this round's budget.".format(len(low_value_ids), floor),
                    file=sys.stderr,
                )

        dirty = io.working_tree_dirty(repo)
        # Snapshot of what was already uncommitted when the round started (same
        # .autopilot/ filter as working_tree_dirty), EXCLUDING the leftovers the
        # run's own prior rounds deliberately left uncommitted (batch mode
        # defers commits; blocked/cancelled rounds leave work behind) — commit
        # uses what remains to refuse absorbing pre-existing user changes.
        prior_deferred = set()
        for entry in st.get("history") or []:
            if isinstance(entry, dict):
                prior_deferred.update(entry.get("deferred_dirty_files") or [])
        start_dirty_files = (
            [p for p in io.uncommitted_paths(repo) if p not in prior_deferred]
            if dirty else []
        )
        allow_dirty = cfg.get("allow_uncommitted_changes", False)
        first_round = (
            st.get("completed_rounds", 0)
            + st.get("blocked_rounds", 0)
            + st.get("cancelled_rounds", 0)
            + st.get("reverted_rounds", 0)
        ) == 0
        if dirty and not allow_dirty:
            if first_round:
                io.append_log(repo, "begin-round", "error", reason="dirty working tree")
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

        # Every validation passed — only now mutate. Marking candidates picked
        # before the dirty-tree refusal used to leak them out of the pending
        # backlog, and a later begin-round without --candidate-id no longer
        # hit the ready-backlog guard because nothing looked pending.
        if candidate_ids:
            state.update_candidates_status(repo, candidate_ids, "picked", round_number, backlog=backlog)

        # One rev-parse answers "has commits?" and yields the SHA together
        # (has_commits + rev-parse would be two identical-cost calls).
        head = io.run_git(repo, "rev-parse", "--verify", "-q", "HEAD")
        start_sha = head.stdout.strip() if head.returncode == 0 else None

        current = {
            "round": round_number,
            "title": args.title,
            "reason": args.reason,
            "candidate_id": candidate_ids[0] if candidate_ids else None,
            "candidate_ids": candidate_ids,
            "start_sha": start_sha,
            "worktree_baseline": io.worktree_change_lines(repo),
            "start_clean": not dirty,
            "start_dirty_files": start_dirty_files,
            "started_at": io.now_iso(),
        }
        st["round"] = round_number
        st["round_seq"] = round_number
        st["current_round"] = current
        st["last_activity_at"] = current["started_at"]
        state.save_state(repo, st)
        io.append_log(repo, "begin-round", "success", round=round_number, candidate_id=candidate_ids)
        if getattr(args, "json", False):
            return emit_result(args, True, "round opened", data={"round": current})
        print(json.dumps(current, indent=2, ensure_ascii=False))
        return 0


def _resolve_tokens(args, repo, st):
    """Thin wrapper over the single token-estimation authority
    (io.estimate_tokens_for_round). Run-level monotonic accounting: the round
    is charged the run-wide delta above the already-billed water marks read
    from state; the returned (billed_text, billed_binary) tuple must be
    persisted by _close_round. An explicit --tokens override skips estimation
    but still advances the water marks to the current totals, so the
    overridden round's real lines are never billed again by a later round.
    Negative overrides would silently refund the budget — refuse them.
    Returns (tokens, (billed_text, billed_binary))."""
    run_start_sha = st.get("run_start_sha")
    billed_text = st.get("billed_text") or 0
    billed_binary = st.get("billed_binary") or 0
    if getattr(args, "tokens", None) is not None:
        if args.tokens < 0:
            print("[ERROR] --tokens must be a non-negative integer.", file=sys.stderr)
            raise SystemExit(2)
        _, total_text, total_binary = io.estimate_tokens_for_round(
            repo, run_start_sha, billed_text, billed_binary
        )
        return args.tokens, (max(billed_text, total_text), max(billed_binary, total_binary))
    tokens, total_text, total_binary = io.estimate_tokens_for_round(
        repo, run_start_sha, billed_text, billed_binary
    )
    return tokens, (total_text, total_binary)


def _refresh_type_stats(repo, st):
    st["type_stats"] = state.compute_type_stats(state.load_backlog(repo))


def _resolve_round_seeds_in_state(repo, st, current, outcome, notes=None):
    """Seed write-back for one closing round, mutated into the caller's state so
    the round close persists backlog + seed ledger without a crash window.
    Only seeds PROMOTED BY THIS ROUND's candidates are touched: a stale
    candidate from an earlier round completing must never verify/refute a seed
    that was since re-promoted elsewhere. completed -> verified, blocked ->
    refuted (+notes: failed hypotheses keep their evidence), cancelled -> open."""
    backlog = state.load_backlog(repo)
    round_ids = set(state.round_candidate_ids(current))
    seed_ids = []
    for candidate_id in round_ids:
        candidate = state.find_candidate(backlog, candidate_id)
        from_seed = (candidate or {}).get("from_seed")
        if from_seed and from_seed not in seed_ids:
            seed_ids.append(from_seed)
    for seed_id in seed_ids:
        seed = state.find_seed(st, seed_id)
        if seed is None:
            print(
                "[WARN] seed-writeback: seed {} no longer exists (truncated?); skipped.".format(seed_id),
                file=sys.stderr,
            )
            io.append_log(repo, "seed-writeback", "warn", seed=seed_id, reason="missing")
            continue
        owner = seed.get("promoted_candidate_id")
        if owner is not None and owner not in round_ids:
            print(
                "[WARN] seed-writeback: seed {} is owned by candidate {}, not this round; skipped.".format(
                    seed_id, owner
                ),
                file=sys.stderr,
            )
            io.append_log(repo, "seed-writeback", "warn", seed=seed_id, reason="not owned by this round", owner=owner)
            continue
        if outcome == "completed":
            state.resolve_seed(st, seed_id, "verified")
        elif outcome == "blocked":
            state.resolve_seed(st, seed_id, "refuted", notes=notes)
        else:
            state.resolve_seed(st, seed_id, "open")
        io.append_log(repo, "seed-writeback", "success", seed=seed_id, outcome=outcome)


def _close_round(repo, st, current, status, counter_key, tokens, history_entry,
                 candidate_status, candidate_round=None, candidate_extra=None,
                 seed_outcome=None, seed_notes=None, billed=None):
    """Shared round-closing bookkeeping: bump the round counter, append tokens,
    record bounded history, release the round, update candidates in one backlog
    pass, resolve this round's seeds into the SAME state save, refresh type
    stats, and persist state. `billed` is the run's advanced (text, binary)
    water-mark tuple from _resolve_tokens; run_start_sha is already on state
    and is persisted along with it."""
    if counter_key:
        st[counter_key] = st.get(counter_key, 0) + 1
    if tokens:
        st["estimated_tokens_used"] += tokens
    if billed is not None:
        st["billed_text"], st["billed_binary"] = billed
    st["last_activity_at"] = io.now_iso()
    # Attribute the round's leftovers (uncommitted at close, minus what the
    # round inherited dirty) so the NEXT begin-round does not treat the run's
    # own deferred work as pre-existing user changes: batch mode defers commits
    # across rounds, and blocked/cancelled rounds leave their work behind.
    # Zero-work aborts leave exactly the inherited set, so they carve nothing.
    try:
        inherited = set(current.get("start_dirty_files") or [])
        history_entry["deferred_dirty_files"] = [
            p for p in io.uncommitted_paths(repo) if p not in inherited
        ]
    except SystemExit:
        history_entry["deferred_dirty_files"] = []
    history_entry.setdefault("status", status)
    state.append_history(st, history_entry)
    st["current_round"] = None
    state.update_candidates_status(
        repo, state.round_candidate_ids(current), candidate_status, candidate_round,
        extra_fields=candidate_extra,
    )
    if seed_outcome is not None:
        _resolve_round_seeds_in_state(repo, st, current, seed_outcome, seed_notes)
    _refresh_type_stats(repo, st)
    state.save_state(repo, st)


def cmd_complete_round(args):
    repo = Path(args.repo).resolve()
    refused = _require_initialized(args, repo)
    if refused is not None:
        return refused
    with io.run_lock(repo):
        st = state.load_state(repo)
        current = st["current_round"]
        if current is None:
            io.append_log(repo, "complete-round", "error", reason="no open round")
            return emit_result(args, False, "[ERROR] No open round to complete.")

        cfg = config.load_config(repo)
        drift = _feature_branch_guard(repo, st, cfg, "complete the round")
        if drift is not None:
            io.append_log(repo, "complete-round", "error", reason="branch drift")
            return emit_result(args, False, drift)

        if args.commit_sha:
            verify = io.run_git(repo, "rev-parse", "--verify", "--quiet", args.commit_sha + "^{commit}")
            if verify.returncode != 0:
                io.append_log(repo, "complete-round", "error", reason="invalid commit sha")
                return emit_result(
                    args, False,
                    "[ERROR] --commit-sha does not resolve to a commit: {}".format(args.commit_sha),
                )

        tokens, billed = _resolve_tokens(args, repo, st)
        if getattr(args, "dry_run", False):
            print(
                "[DRY-RUN] Would record round {} as completed with {} estimated tokens.".format(
                    current["round"], tokens
                ),
                file=sys.stderr,
            )
            return 0

        # Range-check the score even without a configured threshold: an
        # out-of-band 99 would poison the learned value calibration (clamped,
        # but still pinned to the ceiling) for the whole run.
        if getattr(args, "review_score", None) is not None and (args.review_score < 1 or args.review_score > 5):
            io.append_log(repo, "complete-round", "error", reason="review score out of range")
            return emit_result(args, False, "[ERROR] --review-score must be between 1 and 5.")
        review_threshold = cfg.get("review_threshold")
        below_threshold = False
        if review_threshold is not None:
            if args.review_score is None:
                io.append_log(repo, "complete-round", "error", reason="review score required")
                return emit_result(
                    args, False,
                    "[ERROR] review_threshold is {} but no --review-score was provided. "
                    "Self-review the round on a 1-5 scale and pass --review-score.".format(review_threshold),
                )
            if args.review_score < review_threshold:
                if not getattr(args, "below_threshold", False):
                    io.append_log(repo, "complete-round", "error", reason="review score below threshold")
                    return emit_result(
                        args, False,
                        "[ERROR] Self-review score {} is below review_threshold {}. "
                        "Rework the round and re-verify, run block-round, or pass "
                        "--below-threshold to record the low score without blocking the run.".format(
                            args.review_score, review_threshold
                        ),
                    )
                # Explicit quality admit-down: the round stays completed (it
                # does NOT count blocked, so the blocked streak resets), and
                # the low score still feeds the calibration ledger.
                below_threshold = True

        _close_round(
            repo, st, current,
            status="completed",
            counter_key="completed_rounds",
            tokens=tokens,
            history_entry={
                "round": current["round"],
                "status": "completed",
                "title": args.title or current.get("title"),
                "summary": args.summary or "",
                "commit_sha": args.commit_sha,
                "estimated_tokens": tokens,
                "candidate_id": current.get("candidate_id"),
                # commit --round reads this to refuse absorbing the round's
                # pre-existing user changes into a late batch flush.
                "start_dirty_files": current.get("start_dirty_files"),
                "review_score": getattr(args, "review_score", None),
                "review_notes": getattr(args, "review_notes", None) or "",
                "below_threshold": below_threshold,
            },
            candidate_status="completed",
            candidate_round=current["round"],
            candidate_extra=(
                {"review_score": args.review_score} if getattr(args, "review_score", None) is not None else None
            ),
            seed_outcome="completed",
            billed=billed,
        )
        if below_threshold:
            io.append_log(
                repo, "complete-round", "warn",
                round=current["round"], reason="below threshold accepted",
                review_score=args.review_score, threshold=review_threshold,
            )
        io.append_log(
            repo, "complete-round", "success",
            round=current["round"], commit_sha=args.commit_sha, estimated_tokens=tokens,
        )

        if st.get("completed_rounds", 0) % io.PHASE_REPORT_INTERVAL == 0:
            state.write_phase_report(repo, st, cfg)

        push_warning = None
        if cfg.get("push") and args.commit_sha:
            output, err = io.git_push(repo)
            if err:
                io.append_log(repo, "push", "error", error=err)
                push_warning = err
            else:
                io.append_log(repo, "push", "success")

        if push_warning:
            print("[WARN] Round completed but push failed: {}".format(push_warning), file=sys.stderr)
            return emit_result(args, True, "[OK] Round completed (push failed, see warning).")
        return emit_result(args, True, "[OK] Round completed.")


def cmd_block_round(args):
    repo = Path(args.repo).resolve()
    refused = _require_initialized(args, repo)
    if refused is not None:
        return refused
    with io.run_lock(repo):
        st = state.load_state(repo)
        current = st["current_round"]
        if current is None:
            io.append_log(repo, "block-round", "error", reason="no open round")
            return emit_result(args, False, "[ERROR] No open round to block.")

        tokens, billed = _resolve_tokens(args, repo, st)
        if getattr(args, "dry_run", False):
            print(
                "[DRY-RUN] Would mark round {} as blocked: {}.".format(current["round"], args.reason),
                file=sys.stderr,
            )
            return 0
        _close_round(
            repo, st, current,
            status="blocked",
            counter_key="blocked_rounds",
            tokens=tokens,
            history_entry={
                "round": current["round"],
                "status": "blocked",
                "title": args.title or current.get("title"),
                "reason": args.reason or "",
                "candidate_id": current.get("candidate_id"),
            },
            candidate_status="blocked",
            candidate_round=current["round"],
            seed_outcome="blocked",
            seed_notes=args.reason,
            billed=billed,
        )
        io.append_log(repo, "block-round", "success", round=current["round"], reason=args.reason)
        return emit_result(args, True, "[OK] Round blocked.")


def cmd_cancel_round(args):
    repo = Path(args.repo).resolve()
    refused = _require_initialized(args, repo)
    if refused is not None:
        return refused
    with io.run_lock(repo):
        st = state.load_state(repo)
        current = st["current_round"]
        if current is None:
            io.append_log(repo, "cancel-round", "error", reason="no open round")
            return emit_result(args, False, "[ERROR] No open round to cancel.")

        # Zero-work probe retries (wrong flags, malformed refs, forgotten
        # --reason...) used to be billed as full cancelled rounds: a cancelled
        # round advanced max_rounds AND took the 500-token base each time.
        # A round with no working-tree delta against its begin-round snapshot,
        # no commit, and a short life is an aborted probe, not work.
        started = io.parse_time(current.get("started_at"))
        head = io.run_git(repo, "rev-parse", "--verify", "-q", "HEAD")
        head_sha = head.stdout.strip() if head.returncode == 0 else None
        elapsed = None
        if started is not None:
            elapsed = (io.parse_time(io.now_iso()) - started).total_seconds()
        zero_work = (
            io.worktree_change_lines(repo) == current.get("worktree_baseline")
            and head_sha == current.get("start_sha")
            and elapsed is not None and elapsed < 600
        )

        if zero_work:
            tokens, billed = 0, None
        else:
            tokens, billed = _resolve_tokens(args, repo, st)
        if getattr(args, "dry_run", False):
            print(
                "[DRY-RUN] Would cancel round {}{}: {}.".format(
                    current["round"],
                    " as aborted (zero work)" if zero_work else "",
                    args.reason or "",
                ),
                file=sys.stderr,
            )
            return 0
        if zero_work:
            _close_round(
                repo, st, current,
                status="aborted",
                counter_key=None,
                tokens=0,
                history_entry={
                    "round": current["round"],
                    "status": "aborted",
                    "title": args.title or current.get("title"),
                    "reason": args.reason or "",
                    "candidate_id": current.get("candidate_id"),
                },
                candidate_status="pending",
                seed_outcome="cancelled",
            )
            io.append_log(
                repo, "cancel-round", "success", round=current["round"],
                reason=args.reason, zero_work=True,
            )
            return emit_result(
                args, True,
                "[OK] Round aborted (zero work): no changes, no commit, under 10 minutes "
                "— no round budget or token base consumed.",
            )
        _close_round(
            repo, st, current,
            status="cancelled",
            counter_key="cancelled_rounds",
            tokens=tokens,
            history_entry={
                "round": current["round"],
                "status": "cancelled",
                "title": args.title or current.get("title"),
                "reason": args.reason or "",
                "candidate_id": current.get("candidate_id"),
            },
            candidate_status="pending",
            seed_outcome="cancelled",
            billed=billed,
        )
        io.append_log(repo, "cancel-round", "success", round=current["round"], reason=args.reason)
        return emit_result(args, True, "[OK] Round cancelled.")


def _recent_commit_topics(repo, limit=5):
    """Last N commit subjects (sha stripped), oldest last. Empty on unborn repos."""
    if not io.has_commits(repo):
        return []
    result = io.run_git(repo, "log", "--oneline", "-{}".format(limit))
    if result.returncode != 0:
        return []
    topics = []
    for line in result.stdout.splitlines():
        subject = line.strip()
        if " " in subject:
            subject = subject.split(" ", 1)[1]
        else:
            # No separator: a bare sha (empty commit subject) — not a topic.
            continue
        if subject:
            topics.append(subject)
    topics.reverse()
    return topics


def _last_completed_round_shas(st):
    """Commit SHA recorded by the most recent completed round that has one
    (deferred-commit rounds carry None and are skipped)."""
    for entry in reversed(st.get("history") or []):
        if entry.get("status") == "completed" and entry.get("commit_sha"):
            return [entry["commit_sha"]]
    return []


def _last_completed_candidate_ids(st):
    """Candidate ids recorded by the most recent completed round (bounded to the
    first recorded id for legacy single-candidate history entries)."""
    for entry in reversed(st.get("history") or []):
        if entry.get("status") == "completed":
            candidate_id = entry.get("candidate_id")
            return [candidate_id] if candidate_id else []
    return []


def _default_seed_type(saturated):
    """First candidate type that is not yet saturated (seed defaults avoid piling
    onto a saturated type), skipping bugfix: a passive repair is not a
    "because we shipped A, B is next" prediction shape. Falls back to feature."""
    for candidate_type in state.VALID_CANDIDATE_TYPES:
        if candidate_type != "bugfix" and candidate_type not in saturated:
            return candidate_type
    return "feature"


def cmd_goal_met(args):
    repo = Path(args.repo).resolve()
    refused = _require_initialized(args, repo)
    if refused is not None:
        return refused
    with io.run_lock(repo):
        st = state.load_state(repo)
        cfg = config.load_config(repo)
        goal = args.goal
        if not goal:
            io.append_log(repo, "goal-met", "error", reason="goal required")
            return emit_result(args, False, "[ERROR] --goal is required.")

        next_steps = list(args.next_step or [])
        unlocked = list(args.unlocked_capability or [])
        # Canonicalize the goal text: a zero-width character or trailing space
        # used to record a "met" goal that all_goals_met could never match,
        # so a goals-only run could never stop.
        normalized = state.normalize_goal_text(goal)
        cfg_goal_list = cfg.get("goals") or st.get("goals") or []
        for configured in cfg_goal_list:
            if state.normalize_goal_text(configured) == normalized:
                goal = configured  # record the configured spelling
                break
        else:
            if cfg_goal_list:
                print(
                    "[WARN] --goal {!r} does not match any configured goal "
                    "(compared normalized); recording as-is. Configured: {}".format(
                        goal, "; ".join(cfg_goal_list)
                    ),
                    file=sys.stderr,
                )
                io.append_log(repo, "goal-met", "warn", reason="goal not in configured goals", goal=goal)
        seed_type = getattr(args, "seed_type", None)
        if seed_type is not None and seed_type not in state.VALID_CANDIDATE_TYPES:
            io.append_log(repo, "goal-met", "error", reason="unknown seed type")
            return emit_result(
                args, False,
                "[ERROR] --seed-type must be one of {}.".format("|".join(state.VALID_CANDIDATE_TYPES)),
            )
        for name, value in (("--seed-value", args.seed_value), ("--seed-effort", args.seed_effort)):
            if value is not None and (value < 1 or value > 5):
                io.append_log(repo, "goal-met", "error", reason="{} out of range".format(name))
                return emit_result(args, False, "[ERROR] {} must be between 1 and 5.".format(name))

        goal_round = getattr(args, "round", None)
        if goal_round is not None:
            # Verification anchor: --round must point at a round that actually
            # completed. Without it the claim stays unverified and cannot stop
            # the run (state.unverified_goals).
            completed_rounds = {
                entry.get("round")
                for entry in st.get("history") or []
                if isinstance(entry, dict) and entry.get("status") == "completed"
            }
            if goal_round not in completed_rounds:
                io.append_log(repo, "goal-met", "error", reason="round not completed")
                return emit_result(
                    args, False,
                    "[ERROR] --round {} does not match a completed round in history; a goal can only "
                    "be verified against a round that actually completed.".format(goal_round),
                )

        if getattr(args, "dry_run", False):
            print("[DRY-RUN] Would mark goal as met: {} ({} direction seeds).".format(goal, len(next_steps)), file=sys.stderr)
            return 0

        saturated = []
        recent_topics = []
        round_candidate_ids = []
        if not args.no_auto_context:
            saturated = state.saturated_types(st, cfg.get("type_saturation_threshold", 2))
            recent_topics = _recent_commit_topics(repo)
            round_candidate_ids = _last_completed_candidate_ids(st)

        seeds = []
        if next_steps:
            default_type = seed_type or _default_seed_type(saturated)
            # Replaying the same goal-met must not duplicate the same hypothesis:
            # every open/promoted seed with an identical (goal, title) is kept.
            existing_hypotheses = {
                (s.get("source_goal"), s.get("title"))
                for s in st.get("goal_seeds") or []
                if isinstance(s, dict) and s.get("status") in ("open", "promoted")
            }
            for title in next_steps:
                if (goal, title) in existing_hypotheses:
                    continue
                seed = state.append_seed(st, {
                    "source_goal": goal,
                    "title": title,
                    "hypothesis": "if completed, then: {}".format(title),
                    "from_capability": unlocked[0] if unlocked else "",
                    "type": default_type,
                    "value": args.seed_value if args.seed_value is not None else 4,
                    "effort": args.seed_effort if args.seed_effort is not None else 2,
                    # 2 not 1: a prediction is inherently less certain than
                    # observed work — an unearned low risk inflates its rank.
                    "risk": 2,
                    "status": "open",
                })
                seeds.append(seed)

        if not any(state.normalize_goal_text(existing) == state.normalize_goal_text(goal)
                   for existing in st["completed_goals"]):
            st["completed_goals"].append(goal)
        st["last_activity_at"] = io.now_iso()
        goal_event = None
        if not args.no_auto_context or seeds or unlocked:
            goal_event = state.append_goal_event(st, {
                "goal": goal,
                "met_at": io.now_iso(),
                "round": goal_round if goal_round is not None else (st.get("round") or 0),
                "evidence": getattr(args, "evidence", None) or "",
                # No completed-round anchor -> the claim stays unverified and
                # compute_stop_reason withholds "all goals met" until a
                # goal-met --round <completed round> lands.
                "unverified": goal_round is None,
                "commit_shas": _last_completed_round_shas(st) if not args.no_auto_context else [],
                "candidate_ids": round_candidate_ids,
                "unlocked_capabilities": unlocked,
                "recent_commit_topics": recent_topics,
                "saturated_types": saturated,
                "seed_ids": [seed["id"] for seed in seeds],
            })
            for seed in seeds:
                seed["source_event_id"] = goal_event["id"]
        state.save_state(repo, st)
        io.append_log(repo, "goal-met", "success", goal=goal, seeds=[seed["id"] for seed in seeds])
        if state.all_goals_met(cfg, st) and cfg.get("expand_after_goals"):
            message = "[OK] Goal marked met. All goals are met; entering the expansion phase (expand_after_goals). Wave 0: verify and value-gate the direction seeds first."
        else:
            message = "[OK] Goal marked met."
        if seeds:
            # Text-mode consumers need the ids: SKILL.md's Wave 0 next step is
            # `backlog-add --from-seed <id>` / `seed-reject --id <id>`.
            message += " Created direction seeds: {}.".format(", ".join(seed["id"] for seed in seeds))
        if goal_round is None:
            # An exit-0 with no anchor word used to read as verified evidence:
            # say plainly that the claim will withhold the all-goals-met stop.
            message += " unverified（未关联已完成轮次，将阻止 all-goals-met 停止）"
        data = None
        if getattr(args, "json", False):
            data = {"goal_event": goal_event, "seeds": seeds}
        return emit_result(args, True, message, data=data)


def cmd_seed_reject(args):
    repo = Path(args.repo).resolve()
    refused = _require_initialized(args, repo)
    if refused is not None:
        return refused
    with io.run_lock(repo):
        st = state.load_state(repo)
        seed = state.find_seed(st, args.id)
        if seed is None:
            io.append_log(repo, "seed-reject", "error", reason="seed not found")
            open_ids = [s.get("id") for s in state.open_seeds(st) if s.get("id")]
            hint = ", ".join(open_ids[:5]) if open_ids else "none (see read / check --brief expansion)"
            return emit_result(
                args, False,
                "[ERROR] Direction seed not found in state: {}. Open seeds: {}".format(args.id, hint),
            )
        if seed.get("status") != "open":
            io.append_log(repo, "seed-reject", "error", reason="seed not open")
            return emit_result(
                args, False,
                "[ERROR] Seed {} is '{}' (only 'open' seeds can be rejected). "
                "Promoted seeds resolve through complete-round/block-round.".format(
                    args.id, seed.get("status")
                ),
            )
        if getattr(args, "dry_run", False):
            print("[DRY-RUN] Would reject seed {}: {}.".format(args.id, args.reason), file=sys.stderr)
            return 0
        state.resolve_seed(st, args.id, "rejected", notes=args.reason)
        st["last_activity_at"] = io.now_iso()
        state.save_state(repo, st)
        io.append_log(repo, "seed-reject", "success", seed=args.id, reason=args.reason)
        return emit_result(args, True, "[OK] Seed rejected: {}".format(args.id), data={"seed": args.id})


def cmd_finish(args):
    repo = Path(args.repo).resolve()
    refused = _require_initialized(args, repo)
    if refused is not None:
        return refused
    with io.run_lock(repo):
        st = state.load_state(repo)
        cfg = config.load_config(repo)

        if getattr(args, "dry_run", False):
            print(
                "[DRY-RUN] Would finish the run (reason={}, stay={}, open_round={}).".format(
                    args.reason, bool(getattr(args, "stay", False)), st.get("current_round") is not None
                ),
                file=sys.stderr,
            )
            return 0

        # P0 anti-early-stop gate: an honest finish needs a reached stop
        # condition or an explicit --force. Budgets remaining plus actionable
        # (or expansion-needing) backlog is exactly the premature stop this
        # skill exists to prevent — refuse before any state mutation, so a
        # refused finish leaves the run (and any open round) untouched.
        backlog = state.load_backlog(repo)
        watch = _backlog_watch(backlog, cfg)
        ready_floor = _ready_valuable_count(backlog, cfg.get("min_candidate_value"))
        ready_any = any_ready_candidates(backlog)
        selected_count = 0
        selected_empty_reason = None
        if (cfg.get("ranking_mode", "expected") or "expected") != "classic":
            ranked = state.rank_candidates(
                backlog, cfg, progress=state.progress_from_state(st, cfg),
                completed_goals=list(st.get("completed_goals") or []),
            )
            selected_count = len([e for e in ranked if e.get("selected")])
        # P0 anti-early-stop gate: an honest finish needs a reached stop
        # condition or an explicit --force. Refuse while ANY of the following
        # remains true — including below-floor ready work (the old gate only
        # counted value>=floor and let a run finish with a full selected batch
        # of quick-wins still on the table):
        #   - thin/empty backlog that still needs expansion or mining
        #   - any ready candidate (above or below floor)
        #   - a non-empty recommended batch
        #   - mining has not been exhausted (never-mined is NOT exhausted)
        mining_ok = mining_exhausted(st)
        gate_blocks = (
            state.compute_stop_reason(st, cfg) is None
            and (
                watch["needs_expansion"]
                or ready_floor > 0
                or ready_any > 0
                or selected_count > 0
                or not mining_ok
            )
        )
        if gate_blocks:
            if not getattr(args, "force", False):
                io.append_log(
                    repo, "finish", "error",
                    reason="gate: work remains",
                    ready_any=ready_any,
                    ready_floor=ready_floor,
                    selected=selected_count,
                    mining_exhausted=mining_ok,
                )
                return emit_result(
                    args, False,
                    "[ERROR] Refusing to finish: no stop condition reached; work remains "
                    "(ready_any={}, ready_floor={}, selected={}, mining_exhausted={}). "
                    "Run `mine --apply` / Deep Expansion, or pass --force to override.".format(
                        ready_any, ready_floor, selected_count, mining_ok
                    ),
                )
            io.append_log(repo, "finish-forced", "success", reason=args.reason, ready=ready_floor, ready_any=ready_any)

        # Uncommitted work must be surfaced, never silently folded into "the
        # run is finished": warn with the file list (same .autopilot/ ignore as
        # the rest of the dirty-tree logic) but do not refuse — --force and an
        # honest stop both stay valid.
        if io.working_tree_dirty(repo):
            dirty_paths = io.uncommitted_paths(repo)
            shown = dirty_paths[:10]
            more = "" if len(dirty_paths) <= 10 else " (+{} more)".format(len(dirty_paths) - 10)
            print(
                "[WARN] Finishing with uncommitted changes (not refused): {}{}".format(
                    ", ".join(shown), more
                ),
                file=sys.stderr,
            )

        open_round = st.get("current_round")
        if open_round is not None:
            print(
                "[WARN] finish called with round {} still open; auto-cancelling it.".format(open_round.get("round")),
                file=sys.stderr,
            )
            _close_round(
                repo, st, open_round,
                status="cancelled",
                counter_key="cancelled_rounds",
                tokens=0,
                history_entry={
                    "round": open_round["round"],
                    "status": "cancelled",
                    "title": open_round.get("title"),
                    "reason": "auto-cancelled at finish",
                    "candidate_id": open_round.get("candidate_id"),
                },
                candidate_status="pending",
                seed_outcome="cancelled",
            )
            io.append_log(repo, "cancel-round", "success", round=open_round.get("round"), reason="auto-cancelled at finish")

        st["finished_at"] = io.now_iso()
        st["stop_reason"] = args.reason or st.get("stop_reason") or "finished"
        state.save_state(repo, st)

        returned_to = None
        if not args.stay and cfg.get("branch_mode") == "feature":
            origin = st.get("origin_branch")
            if origin and origin != "HEAD" and io.branch_exists(repo, origin):
                try:
                    state._assert_safe_ref_name(origin, what="origin branch")
                except SystemExit:
                    origin = None
                if origin:
                    current = io.current_branch(repo)
                    if current != origin:
                        result = io.run_git(repo, "checkout", origin)
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
        io.append_log(repo, "finish", "success", reason=args.reason, returned_to=returned_to)
        retrospective_path = None
        try:
            markdown = state.build_retrospective(repo, st, cfg, cfg.get("report_lang", "zh"))
            path = io.autopilot_file_for(repo, io.RETROSPECTIVE_FILENAME)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(markdown, encoding="utf-8")
            retrospective_path = str(path)
        except OSError as exc:
            print(
                "[WARN] Could not write retrospective to {}: {}".format(
                    io.RETROSPECTIVE_FILENAME, exc
                ),
                file=sys.stderr,
            )
        message = "[OK] Autopilot run finished."
        if cfg.get("branch_mode") == "feature" and st.get("branch"):
            # The run's commits live on the autopilot branch and finish does
            # not merge (by design): say where the work landed, in the report
            # language, or the user discovers it only at merge time.
            zh_msg = (cfg.get("report_lang") or "zh") == "zh"
            origin = st.get("origin_branch")
            if origin and origin != "HEAD":
                origin_label = "`{}`".format(origin)
            else:
                origin_label = "原" if zh_msg else "the original"
            if zh_msg:
                message += " 提交保留在分支 `{}`，未合并到 {} 分支。".format(st["branch"], origin_label)
            else:
                message += " Commits remain on branch `{}`; not merged into {}.".format(
                    st["branch"], origin_label
                )
        data = {"returned_to": returned_to, "retrospective": retrospective_path}
        return emit_result(args, True, message, data=data)


def _seed_num(seed, name, default=None):
    """Coerce a seed's numeric field (value/effort/risk) to int; hand-edited
    state files may hold strings or junk — fall back instead of raising."""
    raw = (seed or {}).get(name)
    try:
        return int(raw)
    except (TypeError, ValueError, OverflowError):
        return default


def cmd_backlog_add(args):
    repo = Path(args.repo).resolve()
    refused = _require_initialized(args, repo, message="[ERROR] Autopilot not initialized. Run init first.")
    if refused is not None:
        return refused
    with io.run_lock(repo):
        backlog = state.load_backlog(repo)
        seed = None
        if getattr(args, "from_seed", None):
            st = state.load_state(repo)
            seed = state.find_seed(st, args.from_seed)
            if seed is None:
                io.append_log(repo, "backlog-add", "error", reason="seed not found")
                open_ids = [s.get("id") for s in state.open_seeds(st) if s.get("id")]
                hint = ", ".join(open_ids[:5]) if open_ids else "none (see read / check --brief expansion)"
                return emit_result(
                    args, False,
                    "[ERROR] Direction seed not found in state: {}. Open seeds: {}".format(
                        args.from_seed, hint
                    ),
                )
            if seed.get("status") != "open":
                io.append_log(
                    repo, "backlog-add", "error", reason="seed not open",
                )
                return emit_result(
                    args, False,
                    "[ERROR] Seed {} is '{}' (only 'open' seeds can be promoted). "
                    "Check state.json goal_seeds for candidates that are still open.".format(
                        args.from_seed, seed.get("status")
                    ),
                )
        title = args.title or (seed or {}).get("title")
        if not title:
            return emit_result(args, False, "[ERROR] --title is required (unless --from-seed supplies it).")
        origin = getattr(args, "origin", None)
        if origin is not None and origin not in ("observed", "predicted", "expansion"):
            return emit_result(args, False, "[ERROR] --origin must be one of observed|predicted|expansion.")
        if seed is not None and origin is None:
            origin = "predicted"
        if origin is None:
            origin = "observed"
        confidence = getattr(args, "confidence", None)
        if origin == "observed":
            # Observed work is trusted by definition; an explicit confidence is ignored.
            confidence = 1.0
        else:
            if confidence is None:
                if origin == "expansion" and getattr(args, "evidence", None):
                    # Expansion work already survived the main agent's value gate,
                    # and a stated evidence trail justifies a milder discount than
                    # unproven predictions get.
                    confidence = 0.9
                else:
                    confidence = 0.75
            # Chained comparison: NaN fails it too (NaN < 0.5 is False, so a
            # naive `confidence < 0.5 or confidence > 1.0` would let NaN through).
            if not (0.5 <= confidence <= 1.0):
                io.append_log(repo, "backlog-add", "error", reason="confidence out of range")
                return emit_result(
                    args, False,
                    "[ERROR] --confidence for predicted/expansion work must be between 0.5 and 1.0 "
                    "(it discounts the score; observed work is always 1.0).",
                )
        candidate_id = "candidate-{:03d}".format(backlog["next_id"])
        value = args.value
        if value is None and seed is not None:
            value = _seed_num(seed, "value")
        if value is None:
            value = config.LEGACY_IMPACT_SCORE.get(args.impact, 3)
        effort = args.effort
        if effort is None and seed is not None:
            effort = _seed_num(seed, "effort")
        if effort is None:
            effort = config.LEGACY_EFFORT_SCORE.get(args.effort_level, 3)
        if value < 1 or value > 5:
            io.append_log(repo, "backlog-add", "error", reason="value out of range")
            return emit_result(args, False, "[ERROR] --value must be between 1 and 5.")
        if effort < 1 or effort > 5:
            io.append_log(repo, "backlog-add", "error", reason="effort out of range")
            return emit_result(args, False, "[ERROR] --effort must be between 1 and 5.")
        if args.risk is not None and (args.risk < 1 or args.risk > 5):
            io.append_log(repo, "backlog-add", "error", reason="risk out of range")
            return emit_result(args, False, "[ERROR] --risk must be between 1 and 5.")
        if args.type and args.type not in state.VALID_CANDIDATE_TYPES:
            io.append_log(repo, "backlog-add", "error", reason="unknown type")
            return emit_result(
                args, False,
                "[ERROR] --type must be one of {}.".format("|".join(state.VALID_CANDIDATE_TYPES)),
            )
        if args.depends_on:
            for dep_id in args.depends_on:
                # Forward references are legal (add the dependency target in a
                # later backlog-add), so an unknown id only warns — loudly.
                if state.find_candidate(backlog, dep_id) is None:
                    print(
                        "[WARN] --depends-on references unknown candidate: {} (forward "
                        "reference?). It will block this candidate until the id exists "
                        "and completes — see backlog-list.".format(dep_id),
                        file=sys.stderr,
                    )
                    io.append_log(repo, "backlog-add", "warn", reason="unknown depends_on", dep=dep_id)
        candidate = {
            "id": candidate_id,
            "title": title,
            "reason": args.reason or "",
            "type": args.type or "feature",
            "risk": args.risk if args.risk is not None else 1,
            "depends_on": list(args.depends_on) if args.depends_on else [],
            "impact": args.impact or ("high" if value >= 4 else "medium" if value == 3 else "low"),
            "value": value,
            "effort": effort,
            "status": "pending",
            "round": None,
            "created_at": io.now_iso(),
            "updated_at": io.now_iso(),
        }
        if seed is not None:
            candidate["type"] = args.type or seed.get("type") or "feature"
            seed_risk = _seed_num(seed, "risk", default=1)
            candidate["risk"] = args.risk if args.risk is not None else (seed_risk or 1)
            candidate["from_seed"] = seed["id"]
            candidate["hypothesis"] = seed.get("hypothesis") or ""
        if origin != "observed" or seed is not None:
            candidate["origin"] = origin
            candidate["confidence"] = confidence
            based_on = getattr(args, "based_on", None)
            if based_on is None and seed is not None:
                based_on = seed.get("source_goal") or ""
            candidate["based_on"] = based_on or ""
            candidate["evidence"] = getattr(args, "evidence", None) or ""
        if getattr(args, "dry_run", False):
            print(
                "[DRY-RUN] Would add candidate {} ({}): {}.".format(
                    candidate_id, candidate["type"], title
                ),
                file=sys.stderr,
            )
            return 0
        backlog["candidates"].append(candidate)
        backlog["next_id"] += 1
        state.save_backlog(repo, backlog)
        if seed is not None:
            st = state.load_state(repo)
            state.resolve_seed(st, seed["id"], "promoted", candidate_id=candidate_id)
            state.save_state(repo, st)
        # Expansion/backlog work counts as activity so max_minutes does not burn
        # out while the agent is scouting instead of sitting in begin-round.
        # state.json is verified to exist above; a corrupt state must fail
        # cleanly here (fail-closed), never be swallowed into a fake success.
        st = state.load_state(repo)
        st["last_activity_at"] = io.now_iso()
        state.save_state(repo, st)
        io.append_log(repo, "backlog-add", "success", candidate_id=candidate_id, title=title)
        if getattr(args, "json", False):
            return emit_result(args, True, "backlog candidate added", data={"id": candidate_id})
        print(candidate_id)
        return 0


def cmd_backlog_update(args):
    repo = Path(args.repo).resolve()
    refused = _require_initialized(args, repo, message="[ERROR] Autopilot not initialized. Run init first.")
    if refused is not None:
        return refused
    with io.run_lock(repo):
        backlog = state.load_backlog(repo)
        candidate = state.find_candidate(backlog, args.id)
        if candidate is None:
            io.append_log(repo, "backlog-update", "error", reason="candidate not found")
            return emit_result(args, False, "[ERROR] Candidate not found in backlog: {}".format(args.id))
        if getattr(args, "dry_run", False):
            print("[DRY-RUN] Would update candidate {}.".format(args.id), file=sys.stderr)
            return 0
        changed = []
        if args.title is not None:
            candidate["title"] = args.title
            changed.append("title")
        if args.reason is not None:
            candidate["reason"] = args.reason
            changed.append("reason")
        if args.value is not None:
            if args.value < 1 or args.value > 5:
                io.append_log(repo, "backlog-update", "error", reason="value out of range")
                return emit_result(args, False, "[ERROR] --value must be between 1 and 5.")
            candidate["value"] = args.value
            candidate["impact"] = "high" if args.value >= 4 else "medium" if args.value == 3 else "low"
            changed.append("value")
        if args.effort is not None:
            if args.effort < 1 or args.effort > 5:
                io.append_log(repo, "backlog-update", "error", reason="effort out of range")
                return emit_result(args, False, "[ERROR] --effort must be between 1 and 5.")
            candidate["effort"] = args.effort
            changed.append("effort")
        if args.type is not None:
            if args.type not in state.VALID_CANDIDATE_TYPES:
                io.append_log(repo, "backlog-update", "error", reason="unknown type")
                return emit_result(
                    args, False,
                    "[ERROR] --type must be one of {}.".format("|".join(state.VALID_CANDIDATE_TYPES)),
                )
            candidate["type"] = args.type
            changed.append("type")
        if args.risk is not None:
            if args.risk < 1 or args.risk > 5:
                io.append_log(repo, "backlog-update", "error", reason="risk out of range")
                return emit_result(args, False, "[ERROR] --risk must be between 1 and 5.")
            candidate["risk"] = args.risk
            changed.append("risk")
        if args.depends_on is not None:
            for dep_id in args.depends_on:
                # Self-reference is always a bug (permanent ready:false lock).
                if dep_id == args.id:
                    return emit_result(args, False, "[ERROR] A candidate cannot depend on itself ({}).".format(dep_id))
                if state.find_candidate(backlog, dep_id) is None:
                    print(
                        "[WARN] --depends-on references unknown candidate: {} (forward "
                        "reference?). See backlog-list.".format(dep_id),
                        file=sys.stderr,
                    )
            candidate["depends_on"] = list(args.depends_on)
            changed.append("depends_on")
        if args.status is not None:
            candidate["status"] = args.status
            changed.append("status")
        if not changed:
            io.append_log(repo, "backlog-update", "error", reason="nothing to update")
            return emit_result(
                args, False,
                "[ERROR] Nothing to update. Pass --title, --reason, --value, --effort, "
                "--type, --risk, --depends-on, or --status.",
            )
        candidate["updated_at"] = io.now_iso()
        state.save_backlog(repo, backlog)
        # Keep state.type_stats in sync with the backlog: check's expansion
        # payload reads the state snapshot, and a stale snapshot (e.g. after
        # hand-flipping a status to completed) would contradict what
        # rank_candidates computes fresh from the backlog on the same state.
        st = state.load_state(repo)
        _refresh_type_stats(repo, st)
        state.save_state(repo, st)
        io.append_log(repo, "backlog-update", "success", candidate_id=args.id, fields=changed)
        return emit_result(args, True, "[OK] Updated candidate {}: {}".format(args.id, ", ".join(changed)))


def cmd_backlog_remove(args):
    repo = Path(args.repo).resolve()
    refused = _require_initialized(args, repo, message="[ERROR] Autopilot not initialized. Run init first.")
    if refused is not None:
        return refused
    with io.run_lock(repo):
        backlog = state.load_backlog(repo)
        candidates = backlog.get("candidates", [])
        updated = [c for c in candidates if c.get("id") != args.id]
        if len(updated) == len(candidates):
            io.append_log(repo, "backlog-remove", "error", reason="candidate not found")
            return emit_result(args, False, "[ERROR] Candidate not found in backlog: {}".format(args.id))
        if getattr(args, "dry_run", False):
            print("[DRY-RUN] Would remove candidate {}.".format(args.id), file=sys.stderr)
            return 0
        backlog["candidates"] = updated
        state.save_backlog(repo, backlog)
        # Same sync as backlog-update: state.type_stats must not go stale.
        st = state.load_state(repo)
        _refresh_type_stats(repo, st)
        state.save_state(repo, st)
        io.append_log(repo, "backlog-remove", "success", candidate_id=args.id)
        return emit_result(args, True, "[OK] Removed candidate {}.".format(args.id))


def cmd_backlog_list(args):
    repo = Path(args.repo).resolve()
    if not config.state_path_for(repo).exists():
        return emit_result(args, False, "[ERROR] Autopilot not initialized. Run init first.")
    print(json.dumps(state.load_backlog(repo), indent=2, ensure_ascii=False))
    return 0


def cmd_backlog_rank(args):
    repo = Path(args.repo).resolve()
    if not config.state_path_for(repo).exists():
        return emit_result(args, False, "[ERROR] Autopilot not initialized. Run init first.")
    top = getattr(args, "top", None)
    if top is not None and top < 1:
        return emit_result(args, False, "[ERROR] --top must be a positive integer.")
    backlog = state.load_backlog(repo)
    cfg = config.load_config(repo)
    st = state.load_state(repo)
    ranked = state.rank_candidates(
        backlog, cfg, progress=state.progress_from_state(st, cfg),
        completed_goals=list(st.get("completed_goals") or []),
    )
    total = len(ranked)
    if getattr(args, "pending_only", False):
        # The loop only ever picks pending work; the completed/blocked tail is
        # history that ranking keeps for its statistics, not for the reader.
        ranked = [entry for entry in ranked if entry.get("status") in ("pending", "picked")]
    if top is not None:
        ranked = ranked[:top]
    if getattr(args, "brief", False):
        ranked = [brief_rank_entry(entry) for entry in ranked]
    print(json.dumps(ranked, indent=2, ensure_ascii=False))
    if len(ranked) < total:
        # Say what was withheld: a silently truncated ranking reads as "this is
        # all there is" and hides the below-floor quick wins at the tail.
        print(
            "[INFO] backlog-rank: showing {} of {} candidates (--top/--pending-only). "
            "Drop the flags for the full ranking.".format(len(ranked), total),
            file=sys.stderr,
        )
    return 0


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


def cmd_backlog_pick(args):
    repo = Path(args.repo).resolve()
    refused = _require_initialized(args, repo, message="[ERROR] Autopilot not initialized. Run init first.")
    if refused is not None:
        return refused
    with io.run_lock(repo):
        backlog = state.load_backlog(repo)
        candidate = state.find_candidate(backlog, args.id)
        if candidate is None:
            io.append_log(repo, "backlog-pick", "error", reason="candidate not found")
            return emit_result(args, False, "[ERROR] Candidate not found in backlog: {}".format(args.id))
        if getattr(args, "dry_run", False):
            print("[DRY-RUN] Would mark candidate {} as picked.".format(args.id), file=sys.stderr)
            return 0
        candidate["status"] = "picked"
        candidate["updated_at"] = io.now_iso()
        state.save_backlog(repo, backlog)
        io.append_log(repo, "backlog-pick", "success", candidate_id=args.id)
        return emit_result(args, True, "[OK] Picked candidate: {}".format(args.id))


def cmd_ensure_branch(args):
    repo = Path(args.repo).resolve()
    refused = _require_initialized(args, repo)
    if refused is not None:
        return refused
    with io.run_lock(repo):
        st = state.load_state(repo)
        cfg = config.load_config(repo)
        if getattr(args, "dry_run", False):
            print(
                "[DRY-RUN] Would ensure the autopilot branch (branch_mode={}, current state branch={}).".format(
                    cfg.get("branch_mode"), st.get("branch")
                ),
                file=sys.stderr,
            )
            return 0
        branch = state.ensure_branch(repo, st, cfg, to_stderr=getattr(args, "json", False))
        io.append_log(repo, "ensure-branch", "success", branch=branch)
        return emit_result(args, True, "[OK] Autopilot branch ready: {}".format(branch or "current"), data={"branch": branch})


def cmd_push(args):
    repo = Path(args.repo).resolve()
    refused = _require_initialized(args, repo)
    if refused is not None:
        return refused
    with io.run_lock(repo):
        state.load_state(repo)
        cfg = config.load_config(repo)
        if not cfg.get("push"):
            io.append_log(repo, "push", "error", reason="push disabled in config")
            return emit_result(
                args, False,
                "[ERROR] push is disabled in .autopilot/config.json (push: false). "
                "Set 'push': true to allow pushing.",
            )
        if getattr(args, "dry_run", False):
            print("[DRY-RUN] Would push the current branch to its remote.", file=sys.stderr)
            return 0
        output, err = io.git_push(repo)
        if err:
            io.append_log(repo, "push", "error", error=err)
            return emit_result(args, False, "[ERROR] git push failed: {}".format(err))
        io.append_log(repo, "push", "success")
        return emit_result(args, True, "[OK] Pushed to remote.")


def cmd_commit(args):
    repo = Path(args.repo).resolve()
    refused = _require_initialized(args, repo)
    if refused is not None:
        return refused
    with io.run_lock(repo):
        st = state.load_state(repo)
        cfg = config.load_config(repo)

        drift = _feature_branch_guard(repo, st, cfg, "commit")
        if drift is not None:
            io.append_log(repo, "commit", "error", reason="branch drift")
            return emit_result(args, False, drift)

        identity_ok, _, _ = io.git_identity_ok(repo)
        if not identity_ok:
            io.append_log(repo, "commit", "error", reason="identity not configured")
            return emit_result(
                args, False,
                "[ERROR] Git identity is not configured. Run 'git config user.name ...' "
                "and 'git config user.email ...' first.",
            )

        staged = io.run_git(repo, "diff", "--cached", "--quiet")
        if staged.returncode == 0:
            io.append_log(repo, "commit", "error", reason="nothing staged")
            return emit_result(args, False, "[ERROR] Nothing is staged. Run 'git add <files>' for the current round first.")

        bypassed_secrets = bool(cfg.get("scan_secrets", True) and getattr(args, "allow_secrets", False))

        # Staged paths feed three independent guards (the .autopilot/ exclusion,
        # allow/deny_paths, the pre-existing-changes interception) — fetch once.
        # -z + core.quotepath=false: literal NUL-separated paths, immune to
        # quotePath escaping — deny_paths cannot be bypassed by odd file names.
        names = io.run_git(repo, "-c", "core.quotepath=false", "diff", "--cached", "--name-only", "-z")
        staged_paths = [p for p in names.stdout.split("\0") if p.strip()]

        # .autopilot/ is run state, not product code: with track_state: false it
        # is hidden via .git/info/exclude, so only `git add -f` can stage it.
        # This guard runs unconditionally — the allow/deny checks below are
        # config-gated and would miss it.
        if not cfg.get("track_state"):
            forced_state = [p for p in staged_paths if io._is_autopilot_path(p)]
            if forced_state:
                io.append_log(repo, "commit", "error", reason="autopilot state staged", paths=forced_state)
                return emit_result(
                    args, False,
                    "[ERROR] Staged files under .autopilot/ are run state, not product code "
                    "(track_state is false): {}. Unstage them ('git reset -q HEAD -- .autopilot/') "
                    "or set track_state: true if the state should be versioned.".format(
                        ", ".join(forced_state)
                    ),
                )

        if cfg.get("allow_paths") or cfg.get("deny_paths"):
            blocked = [
                path for path in staged_paths
                if not path_allowed(path, cfg.get("allow_paths"), cfg.get("deny_paths"))
            ]
            if blocked:
                io.append_log(repo, "commit", "error", reason="path whitelist violated", paths=blocked)
                return emit_result(
                    args, False,
                    "[ERROR] Staged files violate allow_paths/deny_paths: {}".format(", ".join(blocked)),
                )

        if cfg.get("scan_secrets", True) and not getattr(args, "allow_secrets", False):
            findings = scan_staged_diff(repo, cfg.get("secret_patterns"))
            if findings:
                # Findings are masked (secrets.mask_secret_text) so the audit log
                # never stores secret material.
                io.append_log(repo, "commit", "error", reason="secrets detected", findings=findings)
                detail = "; ".join(
                    "{}: {} (matched {})".format(f.get("file") or "?", f.get("text"), f.get("pattern"))
                    for f in findings[:5]
                )
                return emit_result(
                    args, False,
                    "[ERROR] Secret-like content detected in the staged diff: {}. "
                    "Remove it, or commit with --allow-secrets / set scan_secrets: false.".format(detail),
                )

        stat = io.run_git(repo, "diff", "--cached", "--stat")
        if stat.returncode == 0 and not getattr(args, "json", False):
            print(stat.stdout.rstrip())

        max_scope = cfg.get("max_round_scope")
        if max_scope is not None:
            changed_lines = _staged_numstat_lines(repo)
            if changed_lines > max_scope:
                io.append_log(repo, "commit", "error", reason="max_round_scope exceeded")
                return emit_result(
                    args, False,
                    "[ERROR] Staged diff exceeds max_round_scope ({} lines > {}). "
                    "Split the change into smaller rounds.".format(changed_lines, max_scope),
                )

        prefix = cfg.get("commit_message_prefix", "autopilot")
        open_round = st.get("current_round")
        warn_unverifiable = False
        start_dirty = None
        if args.round is not None:
            if args.round < 1:
                io.append_log(repo, "commit", "error", reason="round out of range")
                return emit_result(args, False, "[ERROR] --round must be a positive integer (got {}).".format(args.round))
            # --round is a batch flush for changes belonging to an
            # ALREADY-completed round, not an arbitrary-commit escape hatch:
            # the number must exist in the run's completed history.
            completed_history = [
                h for h in (st.get("history") or [])
                if isinstance(h, dict) and h.get("status") == "completed"
            ]
            round_entry = next(
                (h for h in completed_history if h.get("round") == args.round), None
            )
            if round_entry is None:
                io.append_log(repo, "commit", "error", reason="round not in completed history", round=args.round)
                known = ", ".join(
                    str(h.get("round")) for h in completed_history if isinstance(h.get("round"), int)
                )
                return emit_result(
                    args, False,
                    "[ERROR] --round {}: no completed round with that number (completed rounds: {}). "
                    "--round is only for flushing staged changes that belong to already-completed "
                    "rounds; open a round with begin-round instead.".format(
                        args.round, known or "none"
                    ),
                )
            round_no = args.round
            start_dirty = round_entry.get("start_dirty_files")
        elif open_round is not None:
            round_no = open_round.get("round")
            start_dirty = open_round.get("start_dirty_files")
        else:
            io.append_log(repo, "commit", "error", reason="no open round")
            return emit_result(
                args, False,
                "[ERROR] No round is open. Use begin-round first, or pass --round <N> to flush "
                "staged changes for an already-completed round (batch flush only; <N> must exist "
                "in the run's completed history).",
            )

        # Batch-mode cadence helper: with commit_every_rounds > 1 the loop is
        # expected to commit on flush rounds only; committing earlier is legal
        # but the agent must know the cadence slipped.
        if open_round is not None:
            commit_every = cfg.get("commit_every_rounds") or 1
            if commit_every > 1 and round_no % commit_every != 0:
                next_flush = ((round_no - 1) // commit_every + 1) * commit_every
                print(
                    "[WARN] Batch mode (commit_every_rounds={}): round {} is not a flush round; "
                    "the next flush round is {}. Committing now is fine — later rounds must not "
                    "assume this batch was already flushed.".format(commit_every, round_no, next_flush),
                    file=sys.stderr,
                )

        # Never absorb pre-existing user changes into an autopilot commit unless
        # allow_uncommitted_changes said so. This holds in batch mode too, where
        # the old round-start dirty check never ran (commit_every_rounds > 1
        # made batch_mode always true). A record without start_dirty_files
        # (state written by an older version) fails open with a warning.
        preexisting = []
        if not cfg.get("allow_uncommitted_changes") and isinstance(start_dirty, list):
            start_dirty_set = set(start_dirty)
            preexisting = sorted(p for p in staged_paths if p in start_dirty_set)
        if preexisting:
            io.append_log(repo, "commit", "error", reason="pre-existing user changes staged", paths=preexisting)
            return emit_result(
                args, False,
                "[ERROR] Staged files were already uncommitted when round {} started (pre-existing "
                "user changes, allow_uncommitted_changes is false): {}. Unstage them ('git reset "
                "HEAD -- <file>'), or set allow_uncommitted_changes: true.".format(
                    round_no, ", ".join(preexisting)
                ),
            )
        if not isinstance(start_dirty, list):
            warn_unverifiable = True

        message = "{}(round-{}): {}".format(prefix, round_no, args.summary)

        if warn_unverifiable:
            print(
                "[WARN] Cannot verify pre-existing changes for round {}: its record predates "
                "start_dirty_files tracking, so the staged set was not checked against the "
                "round's starting tree.".format(round_no),
                file=sys.stderr,
            )

        if getattr(args, "dry_run", False):
            print(
                "[DRY-RUN] Would commit staged changes with message: {}".format(message),
                file=sys.stderr,
            )
            return 0

        result = io.run_git(repo, "commit", "-m", message)
        if result.returncode != 0:
            print(result.stderr.strip(), file=sys.stderr)
            io.append_log(repo, "commit", "error", reason="git commit failed")
            return emit_result(args, False, "[ERROR] git commit failed. Check pre-commit hooks or staged files.")
        sha = io.run_git(repo, "rev-parse", "HEAD").stdout.strip()
        if bypassed_secrets:
            # Audit trail for the --allow-secrets bypass (U: SEC-03).
            io.append_log(repo, "commit", "success", commit_sha=sha, message=message, round=round_no, secrets_bypassed=True)
        else:
            io.append_log(repo, "commit", "success", commit_sha=sha, message=message, round=round_no)
        message = "[OK] Committed {}: {}".format(sha, message)
        if getattr(args, "json", False):
            return emit_result(args, True, message, data={"commit_sha": sha, "commit_message": message})
        print(message)
        return 0


def cmd_secret_scan(args):
    repo = Path(args.repo).resolve()
    if not config.state_path_for(repo).exists():
        return emit_result(args, False, "[ERROR] Autopilot not initialized. Run init first.")
    cfg = config.load_config(repo)
    findings = scan_staged_diff(repo, cfg.get("secret_patterns"))
    payload = {"findings": findings, "clean": not findings}
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0 if not findings else 2
    if findings:
        # Machine-relevant findings go to stderr so stdout stays clean for
        # text consumers; text is masked by the scanner.
        for f in findings:
            print("{}: {} (matched {})".format(f.get("file"), f.get("text"), f.get("pattern")), file=sys.stderr)
        return 2
    print("[OK] No secret-like content found in the staged diff.")
    return 0


def _staged_numstat_lines(repo):
    result = io.run_git(repo, "diff", "--cached", "--numstat")
    if result.returncode != 0:
        # Fail-closed: a failed numstat must not look like a zero-line diff,
        # or max_round_scope would silently stop applying.
        print(
            "[ERROR] Could not read staged numstat ({}); refusing to treat the diff as empty.".format(
                (result.stderr or result.stdout or "git diff --numstat failed").strip()
            ),
            file=sys.stderr,
        )
        raise SystemExit(2)
    text, binary = io._parse_numstat(result.stdout)
    return text + binary


def _write_report_output(args, repo, markdown, kind):
    """Shared --output handling: refuse to write outside the target repository
    unless --force is passed (guards against agent-directed arbitrary writes).
    Relative paths resolve against --repo, not the process CWD — agents invoke
    the helper from arbitrary working directories."""
    if not args.output:
        return None
    candidate = Path(args.output)
    if not candidate.is_absolute():
        candidate = repo / candidate
    output = candidate.resolve()
    try:
        output.relative_to(repo)
    except ValueError:
        if not getattr(args, "force", False):
            return emit_result(
                args, False,
                "[ERROR] --output {} is outside the target repository. Pass --force "
                "to write there anyway.".format(output),
            )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(markdown, encoding="utf-8")
    return emit_result(args, True, "[OK] {} written to {}".format(kind, output), data={"path": str(output)})


def cmd_report(args):
    repo = Path(args.repo).resolve()
    st = state.load_state(repo)
    cfg = config.load_config(repo)
    lang = args.lang or cfg.get("report_lang", "zh")
    markdown = state.build_report(repo, st, cfg, lang)
    result = _write_report_output(args, repo, markdown, "Report")
    if result is not None:
        return result
    if getattr(args, "json", False):
        print(json.dumps({"ok": True, "message": "report", "markdown": markdown}, indent=2, ensure_ascii=False))
        return 0
    print(markdown)
    return 0


def cmd_retrospective(args):
    repo = Path(args.repo).resolve()
    if not config.state_path_for(repo).exists():
        return emit_result(args, False, "[ERROR] Autopilot not initialized. Run init first.")
    st = state.load_state(repo)
    cfg = config.load_config(repo)
    lang = args.lang or cfg.get("report_lang", "zh")
    markdown = state.build_retrospective(repo, st, cfg, lang)
    result = _write_report_output(args, repo, markdown, "Retrospective")
    if result is not None:
        return result
    if getattr(args, "json", False):
        print(json.dumps({"ok": True, "message": "retrospective", "markdown": markdown}, indent=2, ensure_ascii=False))
        return 0
    print(markdown)
    return 0


def cmd_analysis_save(args):
    repo = Path(args.repo).resolve()
    refused = _require_initialized(args, repo, message="[ERROR] Autopilot not initialized. Run init first.")
    if refused is not None:
        return refused
    with io.run_lock(repo):
        if not args.content:
            return emit_result(args, False, "[ERROR] --content is required.")
        try:
            content = json.loads(args.content)
        except ValueError:
            return emit_result(args, False, "[ERROR] --content must be a JSON value.")
        if getattr(args, "dry_run", False):
            print(
                "[DRY-RUN] Would cache the analysis snapshot (name={}).".format(args.name or "unnamed"),
                file=sys.stderr,
            )
            return 0
        git_head = None
        if io.has_commits(repo):
            git_head = io.run_git(repo, "rev-parse", "HEAD").stdout.strip()
        data = {
            "saved_at": io.now_iso(),
            "git_head": git_head,
            "config_mtime": state.config_mtime(repo),
            "analysis": content,
        }
        state.save_analysis(repo, data)
        io.append_log(repo, "analysis-save", "success", name=args.name)
        return emit_result(
            args, True, "[OK] Analysis cached.",
            data={"git_head": git_head, "valid_until": "next HEAD or config change"},
        )


def cmd_analysis_load(args):
    repo = Path(args.repo).resolve()
    if not config.state_path_for(repo).exists():
        return emit_result(args, False, "[ERROR] Autopilot not initialized. Run init first.")
    validity, reason = state.analysis_validity(repo)
    data = state.load_analysis(repo)
    io.append_log(repo, "analysis-load", "success" if validity == "fresh" else "warn", validity=validity)
    payload = {
        "valid": validity == "fresh",
        "status": validity,
        "reason": reason,
        "analysis": (data or {}).get("analysis") if isinstance(data, dict) else None,
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def cmd_config_set(args):
    """Runtime config adjustment. Rewrites .autopilot/config.json and refreshes
    state's config_fingerprint, so the deliberate change is not flagged as
    config-drift. Numeric budgets accept a --clear-* twin that stores null;
    --deadline resolves relative expressions (same parser as init) into an
    absolute timestamp. Guard-rail fields (branch_mode, allow_uncommitted_changes,
    path whitelists) are deliberately not settable at runtime."""
    repo = Path(args.repo).resolve()
    refused = _require_initialized(args, repo, message="[ERROR] Autopilot not initialized. Run init first.")
    if refused is not None:
        return refused

    positive_int_fields = (
        ("candidates_per_round", args.candidates_per_round),
        ("commit_every_rounds", args.commit_every_rounds),
        ("verify_every_rounds", args.verify_every_rounds),
        ("checkpoint_every", args.checkpoint_every),
        ("max_rounds", args.max_rounds),
        ("max_minutes", args.max_minutes),
        ("max_tokens", args.max_tokens),
    )
    requested = {name: value for name, value in positive_int_fields if value is not None}
    for name, value in requested.items():
        if value < 1:
            io.append_log(repo, "config-set", "error", reason="non-positive value", field=name, value=value)
            return emit_result(
                args, False,
                "[ERROR] --{} must be a positive integer (got {}).".format(name.replace("_", "-"), value),
            )
    for value_name, clear_name in (
        ("max_rounds", "clear_max_rounds"),
        ("max_minutes", "clear_max_minutes"),
        ("max_tokens", "clear_max_tokens"),
        ("deadline", "clear_deadline"),
    ):
        if getattr(args, clear_name):
            if value_name in requested:
                return emit_result(
                    args, False,
                    "[ERROR] --{} and --{} are mutually exclusive.".format(
                        value_name.replace("_", "-"), clear_name.replace("_", "-")
                    ),
                )
            requested[value_name] = None
    if args.deadline is not None:
        resolved = io.parse_deadline(args.deadline)
        if resolved is None:
            io.append_log(repo, "config-set", "error", reason="unparsable deadline", value=args.deadline)
            return emit_result(
                args, False,
                "[ERROR] Could not parse --deadline '{}'. Use an ISO timestamp "
                "(2026-08-10T08:00:00), a relative duration (+8h / +30min / +1d), "
                "or a local HH:MM wall-clock time.".format(args.deadline),
            )
        requested["deadline"] = resolved
    for name in ("push", "scan_secrets", "report_lang"):
        value = getattr(args, name)
        if value is not None:
            requested[name] = value
    if args.expand_after_goals is not None:
        requested["expand_after_goals"] = bool(args.expand_after_goals)
    if getattr(args, "clear_check_commands", False):
        if args.check_commands:
            return emit_result(
                args, False,
                "[ERROR] --check-commands and --clear-check-commands are mutually exclusive.",
            )
        requested["check_commands"] = []
    elif args.check_commands:
        requested["check_commands"] = list(args.check_commands)

    if not requested:
        io.append_log(repo, "config-set", "error", reason="nothing to set")
        return emit_result(
            args, False,
            "[ERROR] config-set requires at least one field to set "
            "(--expand-after-goals/--no-expand-after-goals, --candidates-per-round, "
            "--commit-every-rounds, --verify-every-rounds, --checkpoint-every, "
            "--max-rounds/--clear-max-rounds, --max-minutes/--clear-max-minutes, "
            "--max-tokens/--clear-max-tokens, --deadline/--clear-deadline, "
            "--push/--no-push, --scan-secrets/--no-scan-secrets, --report-lang).",
        )

    with io.run_lock(repo):
        # load_config validates the on-disk file; values above were parsed and
        # range-checked already, and the reload below re-runs the full
        # validator over what we actually wrote.
        cfg = config.load_config(repo)
        if getattr(args, "dry_run", False):
            preview = ", ".join(
                "{}={}".format(name, str(value).lower() if isinstance(value, bool) else value)
                for name, value in sorted(requested.items())
            )
            print(
                "[DRY-RUN] Would set {} in .autopilot/config.json and refresh the "
                "state config fingerprint.".format(preview),
                file=sys.stderr,
            )
            return 0
        cfg.update(requested)
        config.save_config(repo, cfg)
        reloaded = config.load_config(repo)
        mismatched = [
            name for name, value in requested.items()
            if reloaded.get(name) != value
        ]
        if mismatched:
            io.append_log(repo, "config-set", "error", reason="reload mismatch", fields=mismatched)
            return emit_result(
                args, False,
                "[ERROR] config-set could not persist: {}.".format(", ".join(mismatched)),
            )
        st = state.load_state(repo)
        st["config_fingerprint"] = io.file_sha256(config.config_path_for(repo))
        state.save_state(repo, st)
        summary = ", ".join(
            "{}={}".format(name, str(value).lower() if isinstance(value, bool) else value)
            for name, value in sorted(requested.items())
        )
        io.append_log(repo, "config-set", "success", fields={k: v for k, v in requested.items()})
        return emit_result(
            args, True,
            "[OK] Config updated: {}; config fingerprint refreshed (no config-drift warning).".format(summary),
        )


def cmd_expansion_record(args):
    """Record one Deep Expansion wave's lens set into state.expansion_waves.
    Makes the SKILL.md lens-rotation rule ("never the same set twice") auditable:
    check reports wave_no / lenses_used / lenses_unused from these records, and
    repeating the previous wave's exact set warns instead of silently passing."""
    repo = Path(args.repo).resolve()
    refused = _require_initialized(args, repo, message="[ERROR] Autopilot not initialized. Run init first.")
    if refused is not None:
        return refused
    with io.run_lock(repo):
        lenses = list(args.lens or [])
        unknown = [lens for lens in lenses if lens not in state.EXPANSION_LENSES]
        if unknown:
            io.append_log(repo, "expansion-wave", "error", reason="unknown lens", lenses=unknown)
            return emit_result(
                args, False,
                "[ERROR] Unknown expansion lens: {}. Must be one of: {}.".format(
                    ", ".join(unknown), ", ".join(state.EXPANSION_LENSES)
                ),
            )
        if getattr(args, "dry_run", False):
            print(
                "[DRY-RUN] Would record expansion wave with lenses: {}.".format(
                    ", ".join(sorted(set(lenses)))
                ),
                file=sys.stderr,
            )
            return 0
        st = state.load_state(repo)
        previous = st.get("expansion_waves") or []
        previous_set = set(previous[-1].get("lenses") or []) if previous else None
        wave = state.append_expansion_wave(st, lenses)
        st["last_activity_at"] = io.now_iso()
        state.save_state(repo, st)
        repeated = previous_set is not None and set(wave["lenses"]) == previous_set
        if repeated:
            print(
                "[WARN] This wave used the same lens set as the previous one; rotate lenses "
                "(see check expansion.lenses_unused).",
                file=sys.stderr,
            )
            io.append_log(
                repo, "expansion-wave", "warn",
                reason="same lens set as previous wave", lenses=wave["lenses"],
            )
        io.append_log(repo, "expansion-wave", "success", lenses=wave["lenses"], wave_no=len(st.get("expansion_waves") or []))
        message = "[OK] Expansion wave {} recorded (lenses: {}).".format(
            len(st.get("expansion_waves") or []), ", ".join(wave["lenses"])
        )
        if repeated:
            message += " Warning: same lens set as the previous wave."
        return emit_result(args, True, message, data={"wave": wave})


def cmd_directive_add(args):
    repo = Path(args.repo).resolve()
    refused = _require_initialized(args, repo, message="[ERROR] Autopilot not initialized. Run init first.")
    if refused is not None:
        return refused
    with io.run_lock(repo):
        if not args.text:
            return emit_result(args, False, "[ERROR] --text is required.")
        if getattr(args, "dry_run", False):
            print("[DRY-RUN] Would add directive: {}.".format(args.text), file=sys.stderr)
            return 0
        count = state.add_directive(repo, args.text)
        io.append_log(repo, "directive-add", "success", text=args.text)
        return emit_result(args, True, "[OK] Directive added ({} active).".format(count), data={"count": count})


def cmd_directive_list(args):
    repo = Path(args.repo).resolve()
    if not config.state_path_for(repo).exists():
        return emit_result(args, False, "[ERROR] Autopilot not initialized. Run init first.")
    directives = state.load_directives(repo)
    # Each entry carries its 1-based index so directive-remove --index has an
    # unambiguous source (the error message points here).
    payload = {
        "directives": [
            dict({"index": position + 1}, **entry)
            for position, entry in enumerate(directives.get("directives") or [])
        ]
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0
    return 0


def cmd_directive_remove(args):
    repo = Path(args.repo).resolve()
    refused = _require_initialized(args, repo, message="[ERROR] Autopilot not initialized. Run init first.")
    if refused is not None:
        return refused
    with io.run_lock(repo):
        directives = state.load_directives(repo)
        entries = directives.get("directives") or []
        index = args.index
        if index < 1 or index > len(entries):
            return emit_result(
                args, False,
                "[ERROR] --index must be between 1 and {} (as shown by directive-list).".format(len(entries)),
            )
        if getattr(args, "dry_run", False):
            print(
                "[DRY-RUN] Would remove directive: {}.".format((entries[index - 1].get("text") or "")[:80]),
                file=sys.stderr,
            )
            return 0
        removed = entries.pop(index - 1)
        state.save_directives(repo, directives)
        io.append_log(repo, "directive-remove", "success", text=removed.get("text"), index=index)
        return emit_result(
            args, True,
            "[OK] Directive removed: {}".format((removed.get("text") or "")[:80]),
            data={"count": len(entries)},
        )


def cmd_undo_round(args):
    repo = Path(args.repo).resolve()
    refused = _require_initialized(args, repo)
    if refused is not None:
        return refused
    with io.run_lock(repo):
        st = state.load_state(repo)
        config.load_config(repo)
        if st.get("current_round") is not None:
            io.append_log(repo, "undo-round", "error", reason="round already open")
            return emit_result(
                args, False,
                "[ERROR] A round is open. Complete, block, or cancel it before undoing a commit.",
            )
        verify = io.run_git(repo, "rev-parse", "--verify", "--quiet", args.sha + "^{commit}")
        if verify.returncode != 0:
            io.append_log(repo, "undo-round", "error", reason="invalid sha")
            return emit_result(args, False, "[ERROR] --sha does not resolve to a commit: {}".format(args.sha))
        full_sha = verify.stdout.strip()
        parents = io.run_git(repo, "rev-list", "--parents", "-n", "1", full_sha)
        is_merge = len(parents.stdout.split()) > 2
        if getattr(args, "dry_run", False):
            print(
                "[DRY-RUN] Would git revert commit {} into a new commit.".format(full_sha),
                file=sys.stderr,
            )
            return 0
        result = io.run_git(repo, "revert", "--no-edit", full_sha)
        if result.returncode != 0:
            print(result.stderr.strip(), file=sys.stderr)
            io.append_log(repo, "undo-round", "error", reason="revert failed", sha=full_sha)
            if is_merge:
                return emit_result(
                    args, False,
                    "[ERROR] git revert failed: {} is a merge commit (revert needs a "
                    "mainline decision). Resolve manually — e.g. `git revert -m 1 {}` — "
                    "commit, then record the round with commit/complete-round.".format(
                        full_sha[:12], full_sha[:12]
                    ),
                )
            return emit_result(
                args, False,
                "[ERROR] git revert failed (likely a conflict). Resolve the conflict and commit "
                "manually, then record the round with commit/complete-round.",
            )
        revert_sha = io.run_git(repo, "rev-parse", "HEAD").stdout.strip()
        # Round numbers come from the same monotonic sequence as begin-round.
        round_number = st.get("round_seq", 0) + 1
        st["reverted_rounds"] = st.get("reverted_rounds", 0) + 1
        st["last_activity_at"] = io.now_iso()
        state.append_history(
            st,
            {
                "round": round_number,
                "status": "revert",
                "title": args.title or "Undo commit {}".format(full_sha[:7]),
                "summary": args.summary or "Reverted {}".format(full_sha[:7]),
                "commit_sha": revert_sha,
                "reverted_sha": full_sha,
                "candidate_id": None,
            },
        )
        st["round"] = round_number
        st["round_seq"] = round_number
        state.save_state(repo, st)
        io.append_log(repo, "undo-round", "success", round=round_number, commit_sha=revert_sha, reverted_sha=full_sha)
        return emit_result(
            args, True,
            "[OK] Reverted {} -> {}".format(full_sha[:12], revert_sha[:12]),
            data={"round": round_number, "revert_sha": revert_sha, "reverted_sha": full_sha},
        )


def cmd_detect_verify(args):
    repo = Path(args.repo).resolve()
    signals = detect_verify_commands(repo)
    commands = [cmd for _, cmd in signals]
    payload = {"detected": [{"tech": tech, "command": cmd} for tech, cmd in signals], "commands": commands}
    payload["ok"] = bool(commands)
    payload["message"] = (
        "[OK] Detected {} verification command(s).".format(len(commands))
        if commands else
        "[ERROR] No verification commands detected; nothing to apply."
    )
    if commands and args.apply:
        if getattr(args, "dry_run", False):
            print(
                "[DRY-RUN] Would set check_commands to {}.".format(commands),
                file=sys.stderr,
            )
            return 0
        cfg = config.load_config(repo)
        cfg["check_commands"] = commands
        config.save_config(repo, cfg)
        io.append_log(repo, "detect-verify", "success", commands=commands)
        return emit_result(args, True, "[OK] check_commands set to {}".format(commands), data=payload)
    if not commands and args.apply:
        io.append_log(repo, "detect-verify", "error", reason="nothing detected")
        if getattr(args, "json", False):
            print(json.dumps(payload, indent=2, ensure_ascii=False))
            return 2
        return emit_result(args, False, payload["message"])
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


def _append_mining_run(st, findings_count, new_count, applied_count, kinds, apply_mode=False):
    """Record one mine attempt (bounded). Used by the finish gate to tell
    'mining exhausted' from 'mining never tried'. Exhaustion counts NEW
    findings: the raw count never reaches zero on repos with resident ones.
    Read-only probes (no --apply) never touch the backlog, so their new-count
    degenerates to the raw count and they are excluded from the exhaustion
    sequence via the `apply` flag."""
    runs = st.setdefault("mining_runs", [])
    runs.append(
        {
            "at": io.now_iso(),
            "findings": int(findings_count or 0),
            "new": int(new_count or 0),
            "applied": int(applied_count or 0),
            "apply": bool(apply_mode),
            "kinds": list(kinds or []),
        }
    )
    if len(runs) > 20:
        del runs[: len(runs) - 20]


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


def any_ready_candidates(backlog):
    """Dependency-ready pending candidates regardless of value floor."""
    count = 0
    for candidate in backlog.get("candidates") or []:
        if candidate.get("status") != "pending":
            continue
        _, is_ready = state.candidate_deps_status(backlog, candidate)
        if is_ready:
            count += 1
    return count


def cmd_mine(args):
    """Run deterministic scanners and optionally promote findings to backlog."""
    repo = Path(args.repo).resolve()
    refused = _require_initialized(args, repo, message="[ERROR] Autopilot not initialized. Run init first.")
    if refused is not None:
        return refused
    with io.run_lock(repo):
        kinds = list(args.kind) if getattr(args, "kind", None) else None
        limit = getattr(args, "limit", None)
        try:
            # None takes the default; an explicit 0 is honored as 0, not as the default.
            result = miner.mine_repo(repo, kinds=kinds, per_kind_limit=20 if limit is None else limit)
        except ValueError as exc:
            return emit_result(args, False, "[ERROR] {}".format(exc))
        backlog = state.load_backlog(repo)
        new_findings = miner.filter_new_findings(result["findings"], backlog.get("candidates"))
        applied = []
        if getattr(args, "dry_run", False):
            payload = {
                "ok": True,
                "kinds": result["kinds"],
                "by_kind": result["by_kind"],
                "found": result["count"],
                "new": len(new_findings),
                "applied": 0,
                "candidate_ids": [],
                "findings": new_findings[:50],
            }
            print(
                "[DRY-RUN] Would record mining run: {} findings, {} new, apply={}".format(
                    result["count"], len(new_findings), bool(getattr(args, "apply", False))
                ),
                file=sys.stderr,
            )
            if getattr(args, "json", False):
                print(json.dumps(payload, indent=2, ensure_ascii=False))
            else:
                for finding in new_findings[:10]:
                    print("  [{}] {}".format(finding.get("kind"), finding.get("title")))
            return 0
        if getattr(args, "apply", False):
            st = state.load_state(repo)
            now = io.now_iso()
            for finding in new_findings:
                fields = miner.finding_to_candidate_fields(finding)
                candidate_id = "candidate-{:03d}".format(backlog["next_id"])
                candidate = {
                    "id": candidate_id,
                    "title": fields["title"],
                    "reason": fields["reason"],
                    "type": fields["type"] if fields["type"] in state.VALID_CANDIDATE_TYPES else "bugfix",
                    "risk": fields.get("risk", 1),
                    "depends_on": [],
                    "impact": "high" if fields["value"] >= 4 else "medium" if fields["value"] == 3 else "low",
                    "value": fields["value"],
                    "effort": fields["effort"],
                    "status": "pending",
                    "round": None,
                    "created_at": now,
                    "updated_at": now,
                    "origin": "observed",
                    "confidence": 1.0,
                    "based_on": "",
                    "evidence": fields["evidence"],
                    "from_mine": finding.get("kind"),
                    "file": finding.get("file"),
                    "line": finding.get("line"),
                }
                backlog["candidates"].append(candidate)
                backlog["next_id"] += 1
                applied.append(candidate_id)
                io.append_log(
                    repo, "backlog-add", "success",
                    candidate_id=candidate_id, title=candidate["title"], via="mine",
                )
            state.save_backlog(repo, backlog)
            st["last_activity_at"] = io.now_iso()
            _append_mining_run(st, result["count"], len(new_findings), len(applied), result["kinds"], apply_mode=True)
            state.save_state(repo, st)
        else:
            st = state.load_state(repo)
            _append_mining_run(st, result["count"], len(new_findings), 0, result["kinds"], apply_mode=False)
            state.save_state(repo, st)
            state.save_state(repo, st)

        payload = {
            "ok": True,
            "kinds": result["kinds"],
            "by_kind": result["by_kind"],
            "found": result["count"],
            "new": len(new_findings),
            "applied": len(applied),
            "candidate_ids": applied,
            "findings": new_findings[:50],
            "exhausted": mining_exhausted(state.load_state(repo)),
        }
        if getattr(args, "json", False):
            print(json.dumps(payload, indent=2, ensure_ascii=False))
            return 0
        message = "[OK] Mine: {} findings ({} new, {} applied).".format(
            result["count"], len(new_findings), len(applied)
        )
        return emit_result(args, True, message, data=payload)


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


def cmd_check(args):
    repo = Path(args.repo).resolve()
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
    if cfg.get("branch_mode") == "feature" and st.get("branch"):
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
        warnings.append(
            "价值校准未激活，候选 value 自评未经验证；每轮 complete-round 传 --review-score"
        )

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
        warnings.append(
            "goal {} 未关联到已完成轮次（--round）；尚有 {} 条 value>=floor 的 ready 候选，不得 finish".format(
                ", ".join(goals_unverified), ready_floor
            )
        )
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
    ranking_expected = cfg.get("ranking_mode", "expected") == "expected"
    ranked = None
    selected_entries = []
    selected_empty_reason = None
    if ranking_expected:
        # One rank pass shared with the batch contract: check must never say
        # "work" while rank_candidates would mark an empty selection — the two
        # answers come from the same computation, not two drifted ones.
        ranked = state.rank_candidates(
            backlog, cfg, progress=state.progress_from_state(st, cfg),
            completed_goals=list(st.get("completed_goals") or []),
        )
        selected_entries = [e for e in ranked if e.get("selected")]
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
        and ranking_expected
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
        "selected_count": len(selected_entries) if ranking_expected else None,
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
    current_number = completed_total + (1 if st.get("current_round") else 0)
    # Boundary math: round k itself IS a boundary when k % every == 0, so the
    # next boundary after round k is computed from k-1 (round 5 with
    # commit_every_rounds 5 reports next_commit_round 5, not 10).
    anchor = max(1, current_number)
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
    if not getattr(args, "brief", False):
        payload["state"] = st
        payload["config"] = cfg

    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def cmd_diagnose(args):
    repo = Path(args.repo).resolve()
    # Quiet probe: git_dir_for would print "[ERROR] Not a git repository" and
    # exit 2, but diagnose is a health report — non-git is a *finding* here,
    # reported as data with exit 0, not an error.
    if io.run_git(repo, "rev-parse", "--is-inside-work-tree").returncode != 0:
        print(json.dumps({"is_git_repo": False, "repo": str(repo)}, indent=2, ensure_ascii=False))
        return 0
    git_dir = io.git_dir_for(repo)

    identity_ok, name, email = io.git_identity_ok(repo)
    has_commits = io.has_commits(repo)
    branch = io.current_branch(repo)
    info = {
        "is_git_repo": True,
        "repo": str(repo),
        "has_commits": has_commits,
        "current_branch": branch,
        "detached_head": bool(has_commits and branch == "HEAD"),
        "dirty": io.working_tree_dirty(repo),
        "user_name": name,
        "user_email": email,
        "identity_ok": identity_ok,
        "has_pre_commit_hook": (git_dir / "hooks" / "pre-commit").exists(),
    }
    print(json.dumps(info, indent=2, ensure_ascii=False))
    return 0
