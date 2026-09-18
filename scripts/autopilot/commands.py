"""Command handlers: init, rounds, backlog, commit, check, report, push, undo.

Orchestration only: secret scanning lives in ``secrets``, path guarding in
``guard``, verification discovery in ``verify``, and token estimation in
``io.estimate_tokens_for_round``."""

import json
import sys
import uuid
from pathlib import Path

from . import config, io, state
from .guard import path_allowed
from .secrets import scan_staged_diff
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


def cmd_init(args):
    repo = Path(args.repo).resolve()
    with io.run_lock(repo):
        git_dir = io.git_dir_for(repo)
        state_path = config.state_path_for(repo)

        if state_path.exists() and not args.force:
            print(
                "[WARN] state.json already exists. Resume with read/check instead of reinitializing.",
                file=sys.stderr,
            )
            return 0

        cfg = config.default_config(repo)
        existing_config = io.load_json(config.config_path_for(repo), None)
        if existing_config is not None:
            cfg.update(existing_config)
        cfg["repo"] = str(repo)

        if args.goal:
            cfg["goals"] = args.goal
        if args.goals_from_prompt:
            cfg["goals"] = state.split_goals(args.goals_from_prompt)
        for knob, minimum in (("--max-rounds", 1), ("--max-minutes", 0), ("--max-tokens", 0),
                              ("--max-round-scope", 1), ("--retries-per-round", 1),
                              ("--max-blocked-in-a-row", 1)):
            value = getattr(args, knob.lstrip("-").replace("-", "_"), None)
            if value is not None and value < minimum:
                io.append_log(repo, "init", "error", reason="{} out of range".format(knob))
                return emit_result(
                    args, False,
                    "[ERROR] {} must be a non-negative integer >= {} (negative values would "
                    "silently stop the loop or fail on first load).".format(knob, minimum),
                )
        if args.max_rounds is not None:
            cfg["max_rounds"] = args.max_rounds
        if args.max_minutes is not None:
            cfg["max_minutes"] = args.max_minutes
        if args.deadline is not None:
            resolved = io.parse_deadline(args.deadline)
            if resolved is None:
                io.append_log(repo, "init", "error", reason="unparseable deadline")
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
                io.append_log(repo, "init", "error", reason="candidates_per_round out of range")
                return emit_result(args, False, "[ERROR] --candidates-per-round must be a positive integer.")
            cfg["candidates_per_round"] = args.candidates_per_round
        if args.max_blocked_in_a_row is not None:
            cfg["max_blocked_in_a_row"] = args.max_blocked_in_a_row
        if args.commit_every_rounds is not None:
            if args.commit_every_rounds < 1:
                io.append_log(repo, "init", "error", reason="commit_every_rounds out of range")
                return emit_result(args, False, "[ERROR] --commit-every-rounds must be a positive integer.")
            cfg["commit_every_rounds"] = args.commit_every_rounds
        if args.verify_every_rounds is not None:
            if args.verify_every_rounds < 1:
                io.append_log(repo, "init", "error", reason="verify_every_rounds out of range")
                return emit_result(args, False, "[ERROR] --verify-every-rounds must be a positive integer.")
            cfg["verify_every_rounds"] = args.verify_every_rounds
        if args.checkpoint_every is not None:
            if args.checkpoint_every < 1:
                io.append_log(repo, "init", "error", reason="checkpoint_every out of range")
                return emit_result(args, False, "[ERROR] --checkpoint-every must be a positive integer.")
            cfg["checkpoint_every"] = args.checkpoint_every
        if args.expand_after_goals:
            cfg["expand_after_goals"] = True
        if args.review_threshold is not None:
            if args.review_threshold < 1 or args.review_threshold > 5:
                io.append_log(repo, "init", "error", reason="review_threshold out of range")
                return emit_result(args, False, "[ERROR] --review-threshold must be an integer 1-5.")
            cfg["review_threshold"] = args.review_threshold
        if args.scan_secrets is not None:
            cfg["scan_secrets"] = args.scan_secrets
        if args.secret_pattern:
            cfg["secret_patterns"] = list(args.secret_pattern)
        if args.type_saturation_threshold is not None:
            if args.type_saturation_threshold < 0:
                io.append_log(repo, "init", "error", reason="type_saturation_threshold out of range")
                return emit_result(args, False, "[ERROR] --type-saturation-threshold must be a non-negative integer.")
            cfg["type_saturation_threshold"] = args.type_saturation_threshold
        if args.ranking_mode is not None:
            cfg["ranking_mode"] = args.ranking_mode
        if args.min_candidate_value is not None:
            if args.min_candidate_value < 1 or args.min_candidate_value > 5:
                io.append_log(repo, "init", "error", reason="min_candidate_value out of range")
                return emit_result(args, False, "[ERROR] --min-candidate-value must be an integer 1-5.")
            cfg["min_candidate_value"] = args.min_candidate_value
        if args.max_same_type_per_round is not None:
            if args.max_same_type_per_round < 1:
                io.append_log(repo, "init", "error", reason="max_same_type_per_round out of range")
                return emit_result(args, False, "[ERROR] --max-same-type-per-round must be a positive integer.")
            cfg["max_same_type_per_round"] = args.max_same_type_per_round
        if getattr(args, "min_pending_candidates", None) is not None:
            if args.min_pending_candidates < 0:
                io.append_log(repo, "init", "error", reason="min_pending_candidates out of range")
                return emit_result(args, False, "[ERROR] --min-pending-candidates must be a non-negative integer.")
            cfg["min_pending_candidates"] = args.min_pending_candidates
        if getattr(args, "max_predicted_per_round", None) is not None:
            if args.max_predicted_per_round < 0:
                io.append_log(repo, "init", "error", reason="max_predicted_per_round out of range")
                return emit_result(args, False, "[ERROR] --max-predicted-per-round must be a non-negative integer.")
            cfg["max_predicted_per_round"] = args.max_predicted_per_round
        if args.allow_path:
            cfg["allow_paths"] = list(args.allow_path)
        if args.deny_path:
            cfg["deny_paths"] = list(args.deny_path)
        if args.report_lang is not None:
            cfg["report_lang"] = args.report_lang

        if not cfg.get("allow_uncommitted_changes") and not args.force and io.working_tree_dirty(repo):
            io.append_log(repo, "init", "error", reason="dirty working tree")
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

        config.save_config(repo, cfg)
        started_at = io.now_iso()
        run_id = uuid.uuid4().hex[:io.RUN_ID_LENGTH]
        st = state.default_state(repo, cfg["goals"], config_fingerprint=io.file_sha256(config.config_path_for(repo)))
        st["run_id"] = run_id
        st["created_at"] = started_at
        st["started_at"] = started_at
        st["last_activity_at"] = started_at
        state.save_state(repo, st)
        io.ensure_git_exclude(git_dir, cfg.get("track_state", False), to_stderr=getattr(args, "json", False))
        state.ensure_branch(repo, st, cfg, to_stderr=getattr(args, "json", False))
        io.append_log(repo, "init", "success", run_id=run_id, branch_mode=cfg.get("branch_mode"))
        return emit_result(args, True, "[OK] Initialized autopilot state.", data={"run_id": run_id})


def cmd_read(args):
    repo = Path(args.repo).resolve()
    print(json.dumps(state.load_state(repo), indent=2, ensure_ascii=False))
    return 0


def cmd_begin_round(args):
    repo = Path(args.repo).resolve()
    with io.run_lock(repo):
        st = state.load_state(repo)
        if st["current_round"] is not None:
            io.append_log(repo, "begin-round", "error", reason="round already open")
            return emit_result(args, False, "[ERROR] A round is already open. Complete, block, or cancel it first.")

        cfg = config.load_config(repo)
        stop_reason = state.compute_stop_reason(st, cfg)
        if stop_reason is not None:
            io.append_log(repo, "begin-round", "error", reason=stop_reason)
            return emit_result(args, False, "[ERROR] Autopilot is stopped: {}. Finish or adjust the config before opening a new round.".format(stop_reason))

        round_number = (
            st.get("completed_rounds", 0)
            + st.get("blocked_rounds", 0)
            + st.get("cancelled_rounds", 0)
            + st.get("reverted_rounds", 0)
            + 1
        )
        if getattr(args, "dry_run", False):
            print(
                "[DRY-RUN] Would open round {}: {}.".format(round_number, args.title),
                file=sys.stderr,
            )
            return 0
        candidate_ids = list(args.candidate_id) if args.candidate_id else []
        backlog = state.load_backlog(repo)
        ready_pending = []
        for candidate in backlog.get("candidates") or []:
            if candidate.get("status") != "pending":
                continue
            missing, is_ready = state.candidate_deps_status(backlog, candidate)
            if is_ready:
                ready_pending.append(candidate)
        if not candidate_ids:
            if ready_pending:
                io.append_log(repo, "begin-round", "error", reason="no candidate ids while ready backlog exists")
                return emit_result(
                    args, False,
                    "[ERROR] begin-round requires at least one --candidate-id when the backlog "
                    "has ready pending candidates (found {}). Pick with backlog-rank, or clear/"
                    "complete those candidates first. Empty rounds on a stocked backlog are refused "
                    "to prevent churn.".format(len(ready_pending)),
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
            state.update_candidates_status(repo, candidate_ids, "picked", round_number, backlog=backlog)

        floor = cfg.get("min_candidate_value")
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
            "started_at": io.now_iso(),
        }
        st["round"] = round_number
        st["current_round"] = current
        st["last_activity_at"] = current["started_at"]
        state.save_state(repo, st)
        io.append_log(repo, "begin-round", "success", round=round_number, candidate_id=candidate_ids)
        if getattr(args, "json", False):
            return emit_result(args, True, "round opened", data={"round": current})
        print(json.dumps(current, indent=2, ensure_ascii=False))
        return 0


def _resolve_tokens(args, repo, start_sha, worktree_baseline=None):
    """Thin wrapper over the single token-estimation authority
    (io.estimate_tokens_for_round); honors an explicit --tokens override."""
    if args.tokens is not None:
        return args.tokens
    return io.estimate_tokens_for_round(repo, start_sha, worktree_baseline)


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
                 seed_outcome=None, seed_notes=None):
    """Shared round-closing bookkeeping: bump the round counter, append tokens,
    record bounded history, release the round, update candidates in one backlog
    pass, resolve this round's seeds into the SAME state save, refresh type
    stats, and persist state."""
    if counter_key:
        st[counter_key] = st.get(counter_key, 0) + 1
    if tokens:
        st["estimated_tokens_used"] += tokens
    st["last_activity_at"] = io.now_iso()
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
    with io.run_lock(repo):
        st = state.load_state(repo)
        current = st["current_round"]
        if current is None:
            io.append_log(repo, "complete-round", "error", reason="no open round")
            return emit_result(args, False, "[ERROR] No open round to complete.")

        if args.commit_sha:
            verify = io.run_git(repo, "rev-parse", "--verify", "--quiet", args.commit_sha + "^{commit}")
            if verify.returncode != 0:
                io.append_log(repo, "complete-round", "error", reason="invalid commit sha")
                return emit_result(
                    args, False,
                    "[ERROR] --commit-sha does not resolve to a commit: {}".format(args.commit_sha),
                )

        tokens = _resolve_tokens(args, repo, current.get("start_sha"), current.get("worktree_baseline"))
        if getattr(args, "dry_run", False):
            print(
                "[DRY-RUN] Would record round {} as completed with {} estimated tokens.".format(
                    current["round"], tokens
                ),
                file=sys.stderr,
            )
            return 0

        cfg = config.load_config(repo)
        # Range-check the score even without a configured threshold: an
        # out-of-band 99 would poison the learned value calibration (clamped,
        # but still pinned to the ceiling) for the whole run.
        if getattr(args, "review_score", None) is not None and (args.review_score < 1 or args.review_score > 5):
            io.append_log(repo, "complete-round", "error", reason="review score out of range")
            return emit_result(args, False, "[ERROR] --review-score must be between 1 and 5.")
        review_threshold = cfg.get("review_threshold")
        if review_threshold is not None:
            if args.review_score is None:
                io.append_log(repo, "complete-round", "error", reason="review score required")
                return emit_result(
                    args, False,
                    "[ERROR] review_threshold is {} but no --review-score was provided. "
                    "Self-review the round on a 1-5 scale and pass --review-score.".format(review_threshold),
                )
            if args.review_score < review_threshold:
                io.append_log(repo, "complete-round", "error", reason="review score below threshold")
                return emit_result(
                    args, False,
                    "[ERROR] Self-review score {} is below review_threshold {}. "
                    "Rework the round and re-verify, or run block-round.".format(
                        args.review_score, review_threshold
                    ),
                )

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
                "review_score": getattr(args, "review_score", None),
                "review_notes": getattr(args, "review_notes", None) or "",
            },
            candidate_status="completed",
            candidate_round=current["round"],
            candidate_extra=(
                {"review_score": args.review_score} if getattr(args, "review_score", None) is not None else None
            ),
            seed_outcome="completed",
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
    with io.run_lock(repo):
        st = state.load_state(repo)
        current = st["current_round"]
        if current is None:
            io.append_log(repo, "block-round", "error", reason="no open round")
            return emit_result(args, False, "[ERROR] No open round to block.")

        tokens = _resolve_tokens(args, repo, current.get("start_sha"), current.get("worktree_baseline"))
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
        )
        io.append_log(repo, "block-round", "success", round=current["round"], reason=args.reason)
        return emit_result(args, True, "[OK] Round blocked.")


def cmd_cancel_round(args):
    repo = Path(args.repo).resolve()
    with io.run_lock(repo):
        st = state.load_state(repo)
        current = st["current_round"]
        if current is None:
            io.append_log(repo, "cancel-round", "error", reason="no open round")
            return emit_result(args, False, "[ERROR] No open round to cancel.")

        tokens = _resolve_tokens(args, repo, current.get("start_sha"), current.get("worktree_baseline"))
        if getattr(args, "dry_run", False):
            print(
                "[DRY-RUN] Would cancel round {}: {}.".format(current["round"], args.reason or ""),
                file=sys.stderr,
            )
            return 0
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
    onto a saturated type); falls back to feature."""
    for candidate_type in state.VALID_CANDIDATE_TYPES:
        if candidate_type not in saturated:
            return candidate_type
    return "feature"


def cmd_goal_met(args):
    repo = Path(args.repo).resolve()
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
                    "risk": 1,
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
                "round": st.get("round") or 0,
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
        data = None
        if getattr(args, "json", False):
            data = {"goal_event": goal_event, "seeds": seeds}
        return emit_result(args, True, message, data=data)


def cmd_seed_reject(args):
    repo = Path(args.repo).resolve()
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
    with io.run_lock(repo):
        if not config.state_path_for(repo).exists():
            io.append_log(repo, "backlog-add", "error", reason="not initialized")
            return emit_result(args, False, "[ERROR] Autopilot not initialized. Run init first.")
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
    with io.run_lock(repo):
        if not config.state_path_for(repo).exists():
            return emit_result(args, False, "[ERROR] Autopilot not initialized. Run init first.")
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
        io.append_log(repo, "backlog-update", "success", candidate_id=args.id, fields=changed)
        return emit_result(args, True, "[OK] Updated candidate {}: {}".format(args.id, ", ".join(changed)))


def cmd_backlog_remove(args):
    repo = Path(args.repo).resolve()
    with io.run_lock(repo):
        if not config.state_path_for(repo).exists():
            return emit_result(args, False, "[ERROR] Autopilot not initialized. Run init first.")
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
    backlog = state.load_backlog(repo)
    cfg = config.load_config(repo)
    st = state.load_state(repo)
    ranked = state.rank_candidates(
        backlog, cfg, progress=state.progress_from_state(st, cfg),
        completed_goals=list(st.get("completed_goals") or []),
    )
    print(json.dumps(ranked, indent=2, ensure_ascii=False))
    return 0


def cmd_backlog_pick(args):
    repo = Path(args.repo).resolve()
    with io.run_lock(repo):
        if not config.state_path_for(repo).exists():
            return emit_result(args, False, "[ERROR] Autopilot not initialized. Run init first.")
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
    with io.run_lock(repo):
        st = state.load_state(repo)
        cfg = config.load_config(repo)

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

        if cfg.get("allow_paths") or cfg.get("deny_paths"):
            # -z + core.quotepath=false: literal NUL-separated paths, immune to
            # quotePath escaping — deny_paths cannot be bypassed by odd file names.
            names = io.run_git(repo, "-c", "core.quotepath=false", "diff", "--cached", "--name-only", "-z")
            staged_paths = [p for p in names.stdout.split("\0") if p.strip()]
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
                    "{}: {}".format(f.get("file") or "?", f.get("text")) for f in findings[:5]
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
        if args.round:
            round_no = args.round
        elif open_round is not None:
            round_no = open_round.get("round")
            batch_mode = (cfg.get("commit_every_rounds") or 1) > 1
            if not cfg.get("allow_uncommitted_changes") and not batch_mode and not open_round.get("start_clean"):
                io.append_log(repo, "commit", "error", reason="round started with dirty tree")
                return emit_result(
                    args, False,
                    "[ERROR] This round began with a dirty working tree and allow_uncommitted_changes "
                    "is false. Commit only the round's own changes, or set allow_uncommitted_changes: true. "
                    "When commit_every_rounds > 1, uncommitted changes from earlier batched rounds are expected.",
                )
        else:
            io.append_log(repo, "commit", "error", reason="no open round")
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
    with io.run_lock(repo):
        if not config.state_path_for(repo).exists():
            return emit_result(args, False, "[ERROR] Autopilot not initialized. Run init first.")
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


def cmd_directive_add(args):
    repo = Path(args.repo).resolve()
    with io.run_lock(repo):
        if not config.state_path_for(repo).exists():
            return emit_result(args, False, "[ERROR] Autopilot not initialized. Run init first.")
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
    with io.run_lock(repo):
        if not config.state_path_for(repo).exists():
            return emit_result(args, False, "[ERROR] Autopilot not initialized. Run init first.")
        directives = state.load_directives(repo)
        entries = directives.get("directives") or []
        index = args.index
        if index < 1 or index > len(entries):
            return emit_result(
                args, False,
                "[ERROR] --index must be between 1 and {} (as shown by directive-list).".format(len(entries)),
            )
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
        round_number = (
            st.get("completed_rounds", 0)
            + st.get("blocked_rounds", 0)
            + st.get("cancelled_rounds", 0)
            + st.get("reverted_rounds", 0)
            + 1
        )
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
    if args.apply:
        if not commands:
            io.append_log(repo, "detect-verify", "error", reason="nothing detected")
            return emit_result(args, False, "[ERROR] No verification commands detected; nothing to apply.")
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


def cmd_check(args):
    repo = Path(args.repo).resolve()
    st = state.load_state(repo)
    cfg = config.load_config(repo)
    stop_reason = state.compute_stop_reason(st, cfg)
    warnings = []

    if st.get("current_round") is not None:
        warnings.append("current_round is open; complete, block, or cancel it before starting a new round.")

    repo_has_commits = io.has_commits(repo)
    if not repo_has_commits:
        warnings.append("Repository has no commits yet; git log is unavailable and the first round creates the initial commit.")
    elif io.current_branch(repo) == "HEAD":
        warnings.append("Detached HEAD; consider checking out a branch before starting.")

    if st.get("config_fingerprint") and io.file_sha256(config.config_path_for(repo)) != st["config_fingerprint"]:
        io.append_log(repo, "config-drift", "warn")
        warnings.append(
            "autopilot config.json changed since init; verify the change was intentional "
            "(check_commands are executed and budgets are trusted by the loop)."
        )

    if io.working_tree_dirty(repo):
        if not cfg.get("allow_uncommitted_changes"):
            warnings.append(
                "Working tree is dirty and allow_uncommitted_changes is false; begin-round/init "
                "will refuse to start until the tree is clean or the config allows uncommitted changes."
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

    if cfg.get("push"):
        remotes = io.run_git(repo, "remote")
        if remotes.returncode == 0 and not remotes.stdout.strip():
            warnings.append("push is true but no git remote is configured; complete-round will warn on every push attempt.")

    backlog = state.load_backlog(repo)
    backlog_watch = _backlog_watch(backlog, cfg)
    open_seed_list = state.open_seeds(st)
    seed_hint = ""
    if open_seed_list:
        seed_hint = (
            " Wave 0 first: verify and value-gate the open direction seeds ({}) via "
            "'backlog-add --from-seed <id>'; lens-scan Deep Expansion is the second wave.".format(
                ", ".join(seed.get("id") or "" for seed in open_seed_list[:5])
            )
        )
    if stop_reason is None and backlog_watch["needs_expansion"]:
        if backlog_watch["pending"] == 0:
            warnings.append(
                "Backlog has no pending candidates. Do NOT idle or wait for the deadline: "
                "run Deep Expansion now (spawn explore subagents, add 3-5 candidates with "
                "backlog-add). Empty backlog is an expansion trigger, not a reason to stop."
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

    action_hint = "expand" if (stop_reason is None and backlog_watch["needs_expansion"]) else (
        "stop" if stop_reason is not None else "work"
    )

    payload = {
        "continue": stop_reason is None,
        "stop_reason": stop_reason,
        "warnings": warnings,
        "backlog": backlog_watch,
        "action_hint": action_hint,
    }
    goals_met = state.all_goals_met(cfg, st)
    payload["goals_met"] = goals_met
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
    verify_every = cfg.get("verify_every_rounds") or 1
    commit_every = cfg.get("commit_every_rounds") or 1
    payload["next_verify_round"] = ((current_number // verify_every) + 1) * verify_every
    payload["next_commit_round"] = ((current_number // commit_every) + 1) * commit_every
    checkpoint_every = cfg.get("checkpoint_every")
    payload["next_checkpoint_round"] = (
        ((current_number // checkpoint_every) + 1) * checkpoint_every if checkpoint_every else None
    )
    if not getattr(args, "brief", False):
        payload["state"] = st
        payload["config"] = cfg

    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def cmd_diagnose(args):
    repo = Path(args.repo).resolve()
    try:
        git_dir = io.git_dir_for(repo)
    except SystemExit:
        print(json.dumps({"is_git_repo": False}, indent=2, ensure_ascii=False))
        return 0

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
