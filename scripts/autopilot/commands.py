"""Command handlers: init, rounds, backlog, commit, check, report, push, undo."""

import fnmatch
import json
import os
import re
import sys
import uuid
from pathlib import Path

from . import config, io, state


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
                    repo, uuid.uuid4().hex[:12], cfg.get("max_rounds"), cfg.get("goals")
                ),
                file=sys.stderr,
            )
            return 0

        config.save_config(repo, cfg)
        started_at = io.now_iso()
        run_id = uuid.uuid4().hex[:12]
        st = {
            "schema": io.SCHEMA_VERSION,
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
            "goals": cfg["goals"],
            "completed_goals": [],
            "current_round": None,
            "history": [],
            "stop_reason": None,
            "finished_at": None,
        }
        state.save_state(repo, st)
        io.ensure_git_exclude(git_dir, cfg.get("track_state", False), to_stderr=getattr(args, "json", False))
        state.ensure_branch_impl(repo, st, cfg, to_stderr=getattr(args, "json", False))
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
        if candidate_ids:
            backlog = state.load_backlog(repo)
            for cid in candidate_ids:
                candidate = state.find_candidate(backlog, cid)
                if candidate is None:
                    io.append_log(repo, "begin-round", "error", reason="candidate not found")
                    return emit_result(args, False, "[ERROR] Candidate not found in backlog: {}".format(cid))
                state.update_candidate_status(repo, cid, "picked", round_number)

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

        start_sha = None
        if io.has_commits(repo):
            start_sha = io.run_git(repo, "rev-parse", "HEAD").stdout.strip()

        current = {
            "round": round_number,
            "title": args.title,
            "reason": args.reason,
            "candidate_id": candidate_ids[0] if candidate_ids else None,
            "candidate_ids": candidate_ids,
            "start_sha": start_sha,
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


def _resolve_tokens(args, repo, start_sha):
    if args.tokens is None:
        return io.estimate_tokens_for_round(repo, start_sha)
    return args.tokens


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

        tokens = _resolve_tokens(args, repo, current.get("start_sha"))
        if getattr(args, "dry_run", False):
            print(
                "[DRY-RUN] Would record round {} as completed with {} estimated tokens.".format(
                    current["round"], tokens
                ),
                file=sys.stderr,
            )
            return 0

        st["completed_rounds"] += 1
        st["estimated_tokens_used"] += tokens
        st["last_activity_at"] = io.now_iso()
        st["history"].append(
            {
                "round": current["round"],
                "status": "completed",
                "title": args.title or current.get("title"),
                "summary": args.summary or "",
                "commit_sha": args.commit_sha,
                "estimated_tokens": tokens,
                "candidate_id": current.get("candidate_id"),
                "finished_at": io.now_iso(),
            }
        )
        st["current_round"] = None
        state.save_state(repo, st)
        for cid in state.round_candidate_ids(current):
            state.update_candidate_status(repo, cid, "completed", current["round"])
        io.append_log(
            repo, "complete-round", "success",
            round=current["round"], commit_sha=args.commit_sha, estimated_tokens=tokens,
        )

        cfg = config.load_config(repo)
        if st.get("completed_rounds", 0) % 10 == 0:
            state.write_phase_report(repo, st, cfg)

        push_warning = None
        if cfg.get("push"):
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

        tokens = _resolve_tokens(args, repo, current.get("start_sha"))
        if getattr(args, "dry_run", False):
            print(
                "[DRY-RUN] Would mark round {} as blocked: {}.".format(current["round"], args.reason),
                file=sys.stderr,
            )
            return 0
        st["blocked_rounds"] += 1
        st["estimated_tokens_used"] += tokens
        st["last_activity_at"] = io.now_iso()
        st["history"].append(
            {
                "round": current["round"],
                "status": "blocked",
                "title": args.title or current.get("title"),
                "reason": args.reason or "",
                "candidate_id": current.get("candidate_id"),
                "finished_at": io.now_iso(),
            }
        )
        st["current_round"] = None
        state.save_state(repo, st)
        for cid in state.round_candidate_ids(current):
            state.update_candidate_status(repo, cid, "blocked", current["round"])
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

        tokens = _resolve_tokens(args, repo, current.get("start_sha"))
        if getattr(args, "dry_run", False):
            print(
                "[DRY-RUN] Would cancel round {}: {}.".format(current["round"], args.reason or ""),
                file=sys.stderr,
            )
            return 0
        st["estimated_tokens_used"] += tokens
        st["cancelled_rounds"] = st.get("cancelled_rounds", 0) + 1
        st["last_activity_at"] = io.now_iso()
        st["history"].append(
            {
                "round": current["round"],
                "status": "cancelled",
                "title": args.title or current.get("title"),
                "reason": args.reason or "",
                "candidate_id": current.get("candidate_id"),
                "finished_at": io.now_iso(),
            }
        )
        st["current_round"] = None
        state.save_state(repo, st)
        for cid in state.round_candidate_ids(current):
            state.update_candidate_status(repo, cid, "pending")
        io.append_log(repo, "cancel-round", "success", round=current["round"], reason=args.reason)
        return emit_result(args, True, "[OK] Round cancelled.")


def cmd_goal_met(args):
    repo = Path(args.repo).resolve()
    with io.run_lock(repo):
        st = state.load_state(repo)
        goal = args.goal
        if not goal:
            io.append_log(repo, "goal-met", "error", reason="goal required")
            return emit_result(args, False, "[ERROR] --goal is required.")
        if getattr(args, "dry_run", False):
            print("[DRY-RUN] Would mark goal as met: {}.".format(goal), file=sys.stderr)
            return 0
        if goal not in st["completed_goals"]:
            st["completed_goals"].append(goal)
            st["last_activity_at"] = io.now_iso()
        state.save_state(repo, st)
        io.append_log(repo, "goal-met", "success", goal=goal)
        return emit_result(args, True, "[OK] Goal marked met.")


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
            st["cancelled_rounds"] = st.get("cancelled_rounds", 0) + 1
            st["current_round"] = None
            st["history"].append(
                {
                    "round": open_round["round"],
                    "status": "cancelled",
                    "title": open_round.get("title"),
                    "reason": "auto-cancelled at finish",
                    "candidate_id": open_round.get("candidate_id"),
                    "finished_at": io.now_iso(),
                }
            )
            for cid in state.round_candidate_ids(open_round):
                state.update_candidate_status(repo, cid, "pending")
            io.append_log(repo, "cancel-round", "success", round=open_round.get("round"), reason="auto-cancelled at finish")

        st["finished_at"] = io.now_iso()
        st["stop_reason"] = args.reason or st.get("stop_reason") or "finished"
        state.save_state(repo, st)

        returned_to = None
        if not args.stay and cfg.get("branch_mode") == "feature":
            origin = st.get("origin_branch")
            if origin and origin != "HEAD" and io.branch_exists(repo, origin):
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
        message = "[OK] Autopilot run finished."
        data = {"returned_to": returned_to}
        return emit_result(args, True, message, data=data)


def cmd_backlog_add(args):
    repo = Path(args.repo).resolve()
    with io.run_lock(repo):
        backlog = state.load_backlog(repo)
        candidate_id = "candidate-{:03d}".format(backlog["next_id"])
        value = args.value if args.value is not None else config.LEGACY_IMPACT_SCORE.get(args.impact, 3)
        effort = args.effort if args.effort is not None else config.LEGACY_EFFORT_SCORE.get(args.effort_level, 3)
        if value < 1 or value > 5:
            io.append_log(repo, "backlog-add", "error", reason="value out of range")
            return emit_result(args, False, "[ERROR] --value must be between 1 and 5.")
        if effort < 1 or effort > 5:
            io.append_log(repo, "backlog-add", "error", reason="effort out of range")
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
            "created_at": io.now_iso(),
            "updated_at": io.now_iso(),
        }
        backlog["candidates"].append(candidate)
        backlog["next_id"] += 1
        state.save_backlog(repo, backlog)
        io.append_log(repo, "backlog-add", "success", candidate_id=candidate_id, title=args.title)
        if getattr(args, "json", False):
            return emit_result(args, True, "backlog candidate added", data={"id": candidate_id})
        print(candidate_id)
        return 0


def cmd_backlog_update(args):
    repo = Path(args.repo).resolve()
    with io.run_lock(repo):
        backlog = state.load_backlog(repo)
        candidate = state.find_candidate(backlog, args.id)
        if candidate is None:
            io.append_log(repo, "backlog-update", "error", reason="candidate not found")
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
        if args.status is not None:
            candidate["status"] = args.status
            changed.append("status")
        if not changed:
            io.append_log(repo, "backlog-update", "error", reason="nothing to update")
            return emit_result(args, False, "[ERROR] Nothing to update. Pass --title, --reason, --value, --effort, or --status.")
        candidate["updated_at"] = io.now_iso()
        state.save_backlog(repo, backlog)
        io.append_log(repo, "backlog-update", "success", candidate_id=args.id, fields=changed)
        return emit_result(args, True, "[OK] Updated candidate {}: {}".format(args.id, ", ".join(changed)))


def cmd_backlog_remove(args):
    repo = Path(args.repo).resolve()
    with io.run_lock(repo):
        backlog = state.load_backlog(repo)
        candidates = backlog.get("candidates", [])
        updated = [c for c in candidates if c.get("id") != args.id]
        if len(updated) == len(candidates):
            io.append_log(repo, "backlog-remove", "error", reason="candidate not found")
            return emit_result(args, False, "[ERROR] Candidate not found in backlog: {}".format(args.id))
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
    ranked = []
    for candidate in backlog.get("candidates", []):
        entry = dict(candidate)
        entry["score"] = round(state._candidate_score(candidate), 3)
        ranked.append(entry)
    ranked.sort(key=lambda item: (item.get("status") != "pending", -item.get("score", 0), item.get("id")))
    print(json.dumps(ranked, indent=2, ensure_ascii=False))
    return 0


def cmd_backlog_pick(args):
    repo = Path(args.repo).resolve()
    with io.run_lock(repo):
        backlog = state.load_backlog(repo)
        candidate = state.find_candidate(backlog, args.id)
        if candidate is None:
            io.append_log(repo, "backlog-pick", "error", reason="candidate not found")
            return emit_result(args, False, "[ERROR] Candidate not found in backlog: {}".format(args.id))
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
        branch = state.ensure_branch_impl(repo, st, cfg, to_stderr=getattr(args, "json", False))
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

        if cfg.get("allow_paths") or cfg.get("deny_paths"):
            names = io.run_git(repo, "diff", "--cached", "--name-only")
            blocked = [
                line for line in names.stdout.splitlines()
                if line.strip() and not _path_allowed(line, cfg.get("allow_paths"), cfg.get("deny_paths"))
            ]
            if blocked:
                io.append_log(repo, "commit", "error", reason="path whitelist violated", paths=blocked)
                return emit_result(
                    args, False,
                    "[ERROR] Staged files violate allow_paths/deny_paths: {}".format(", ".join(blocked)),
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
            if not cfg.get("allow_uncommitted_changes") and not open_round.get("start_clean"):
                io.append_log(repo, "commit", "error", reason="round started with dirty tree")
                return emit_result(
                    args, False,
                    "[ERROR] This round began with a dirty working tree and allow_uncommitted_changes "
                    "is false. Commit only the round's own changes, or set allow_uncommitted_changes: true.",
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
        io.append_log(repo, "commit", "success", commit_sha=sha, message=message, round=round_no)
        message = "[OK] Committed {}: {}".format(sha, message)
        if getattr(args, "json", False):
            return emit_result(args, True, message, data={"commit_sha": sha, "commit_message": message})
        print(message)
        return 0


def _staged_numstat_lines(repo):
    result = io.run_git(repo, "diff", "--cached", "--numstat")
    if result.returncode != 0:
        return 0
    text, binary = io._parse_numstat(result.stdout)
    return text + binary


def cmd_report(args):
    repo = Path(args.repo).resolve()
    st = state.load_state(repo)
    cfg = config.load_config(repo)
    lang = args.lang or cfg.get("report_lang", "zh")
    markdown = state.build_report(repo, st, cfg, lang)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(markdown, encoding="utf-8")
        return emit_result(args, True, "[OK] Report written to {}".format(output), data={"path": str(output)})
    print(markdown)
    return 0


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
        st["history"].append(
            {
                "round": round_number,
                "status": "revert",
                "title": args.title or "Undo commit {}".format(full_sha[:7]),
                "summary": args.summary or "Reverted {}".format(full_sha[:7]),
                "commit_sha": revert_sha,
                "reverted_sha": full_sha,
                "candidate_id": None,
                "finished_at": io.now_iso(),
            }
        )
        state.save_state(repo, st)
        io.append_log(repo, "undo-round", "success", round=round_number, commit_sha=revert_sha, reverted_sha=full_sha)
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
            io.append_log(repo, "detect-verify", "error", reason="nothing detected")
            return emit_result(args, False, "[ERROR] No verification commands detected; nothing to apply.")
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


def cmd_check(args):
    repo = Path(args.repo).resolve()
    st = state.load_state(repo)
    cfg = config.load_config(repo)
    stop_reason = state.compute_stop_reason(st, cfg)
    warnings = []

    if st.get("current_round") is not None:
        warnings.append("current_round is open; complete, block, or cancel it before starting a new round.")

    if not io.has_commits(repo):
        warnings.append("Repository has no commits yet; git log is unavailable and the first round creates the initial commit.")

    if io.is_detached_head(repo):
        warnings.append("Detached HEAD; consider checking out a branch before starting.")

    identity_ok, _, _ = io.git_identity_ok(repo)
    if not identity_ok:
        warnings.append("Git user.name/user.email not configured; commits will fail until set.")

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

    if cfg.get("push"):
        remotes = io.run_git(repo, "remote")
        if remotes.returncode == 0 and not remotes.stdout.strip():
            warnings.append("push is true but no git remote is configured; complete-round will warn on every push attempt.")

    payload = {
        "continue": stop_reason is None,
        "stop_reason": stop_reason,
        "warnings": warnings,
    }
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
    info = {
        "is_git_repo": True,
        "repo": str(repo),
        "has_commits": io.has_commits(repo),
        "current_branch": io.current_branch(repo),
        "detached_head": io.is_detached_head(repo),
        "dirty": io.working_tree_dirty(repo),
        "user_name": name,
        "user_email": email,
        "identity_ok": identity_ok,
        "has_pre_commit_hook": (git_dir / "hooks" / "pre-commit").exists(),
    }
    print(json.dumps(info, indent=2, ensure_ascii=False))
    return 0
