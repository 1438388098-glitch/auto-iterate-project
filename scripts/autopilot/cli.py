"""Argument parsing and dispatch for the autopilot state helper.

Each subparser registers its handler via ``set_defaults(func=...)`` — a single
registration point per command, no second name->handler table to keep in sync."""

import argparse
import sys

from . import agent, commands


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
    init_parser.add_argument("--deadline", default=None,
                             help="Timer stop: ISO timestamp, relative '+8h'/'+30min'/'+1d', or local 'HH:MM' (tomorrow if passed)")
    init_parser.add_argument("--max-tokens", type=int, default=None)
    init_parser.add_argument("--max-round-scope", type=int, default=None)
    init_parser.add_argument("--branch-mode", choices=["current", "feature"], default=None)
    init_parser.add_argument("--allow-uncommitted-changes", action="store_true")
    init_parser.add_argument("--track-state", action="store_true")
    init_parser.add_argument("--check-commands", action="append", default=None)
    init_parser.add_argument("--push", action="store_true")
    init_parser.add_argument("--commit-message-prefix", default=None)
    init_parser.add_argument("--retries-per-round", type=int, default=None)
    init_parser.add_argument("--candidates-per-round", type=int, default=None, help="Backlog candidates to work per round (default 3)")
    init_parser.add_argument("--max-blocked-in-a-row", type=int, default=None)
    init_parser.add_argument("--commit-every-rounds", type=int, default=None,
                             help="Commit accumulated changes once per this many rounds (default 5)")
    init_parser.add_argument("--verify-every-rounds", type=int, default=None,
                             help="Run full verification once per this many rounds (default 3)")
    init_parser.add_argument("--checkpoint-every", type=int, default=None,
                             help="Pause and ask the user once per this many rounds (default off)")
    init_parser.add_argument("--expand-after-goals", action="store_true", default=None,
                             help="Keep iterating after all goals are met (scout new candidates instead of stopping)")
    init_parser.add_argument("--review-threshold", type=int, default=None,
                             help="complete-round requires --review-score >= this (1-5) to pass")
    scan_group = init_parser.add_mutually_exclusive_group()
    scan_group.add_argument("--scan-secrets", dest="scan_secrets", action="store_true", default=None,
                            help="Scan staged diffs for secret-like content before commit (default)")
    scan_group.add_argument("--no-scan-secrets", dest="scan_secrets", action="store_false", default=None)
    init_parser.add_argument("--secret-pattern", action="append", default=None,
                             help="Extra regex patterns for the commit-time secret scan (repeatable)")
    init_parser.add_argument("--type-saturation-threshold", type=int, default=None,
                             help="Completed same-type candidates before ranking downweights the type (default 2)")
    init_parser.add_argument("--ranking-mode", choices=["expected", "classic"], default=None,
                             help="Backlog scoring: 'expected' ranks by value x success rate per round (default); 'classic' is the legacy value/effort ratio")
    init_parser.add_argument("--min-candidate-value", type=int, default=None,
                             help="Candidates below this value are demoted in ranking (1-5, default 3; null disables)")
    init_parser.add_argument("--max-same-type-per-round", type=int, default=None,
                             help="Diversity quota: at most this many same-type candidates per recommended round (default 2)")
    init_parser.add_argument("--min-pending-candidates", type=int, default=None,
                             help="check action_hint=expand when pending backlog falls below this (default 3)")
    init_parser.add_argument("--allow-path", action="append", default=None, help="Glob of paths allowed in commits (repeatable)")
    init_parser.add_argument("--deny-path", action="append", default=None, help="Glob of paths never allowed in commits (repeatable)")
    init_parser.add_argument("--report-lang", choices=["zh", "en"], default=None)
    init_parser.add_argument("--force", action="store_true")
    add_json(init_parser)
    add_dry_run(init_parser)
    init_parser.set_defaults(func=commands.cmd_init)

    read_parser = subparsers.add_parser("read", help="Print current state")
    read_parser.add_argument("--repo", default=".")
    read_parser.set_defaults(func=commands.cmd_read)

    diagnose_parser = subparsers.add_parser("diagnose", help="Inspect repository health and git environment")
    diagnose_parser.add_argument("--repo", default=".")
    diagnose_parser.set_defaults(func=commands.cmd_diagnose)

    begin_parser = subparsers.add_parser("begin-round", help="Open a round")
    begin_parser.add_argument("--repo", default=".")
    begin_parser.add_argument("--title", required=True)
    begin_parser.add_argument("--reason", required=True)
    begin_parser.add_argument("--candidate-id", action="append", default=None,
                             help="Backlog candidate id for this round (repeatable for multi-candidate rounds)")
    add_json(begin_parser)
    add_dry_run(begin_parser)
    begin_parser.set_defaults(func=commands.cmd_begin_round)

    complete_parser = subparsers.add_parser("complete-round", help="Finish a round successfully")
    complete_parser.add_argument("--repo", default=".")
    complete_parser.add_argument("--title")
    complete_parser.add_argument("--summary", required=True)
    complete_parser.add_argument("--commit-sha", default=None,
                                 help="Commit SHA recorded by the commit command (omit when deferring commits in batched mode)")
    complete_parser.add_argument("--tokens", type=int, default=None)
    complete_parser.add_argument("--review-score", type=int, default=None,
                                 help="Self-review score 1-5 (required when config review_threshold is set)")
    complete_parser.add_argument("--review-notes", default=None, help="Optional self-review notes")
    add_json(complete_parser)
    add_dry_run(complete_parser)
    complete_parser.set_defaults(func=commands.cmd_complete_round)

    block_parser = subparsers.add_parser("block-round", help="Mark a round blocked")
    block_parser.add_argument("--repo", default=".")
    block_parser.add_argument("--title")
    block_parser.add_argument("--reason", required=True)
    block_parser.add_argument("--tokens", type=int, default=None)
    add_json(block_parser)
    add_dry_run(block_parser)
    block_parser.set_defaults(func=commands.cmd_block_round)

    cancel_parser = subparsers.add_parser("cancel-round", help="Abandon an open round without counting it blocked")
    cancel_parser.add_argument("--repo", default=".")
    cancel_parser.add_argument("--title")
    cancel_parser.add_argument("--reason")
    cancel_parser.add_argument("--tokens", type=int, default=None)
    add_json(cancel_parser)
    add_dry_run(cancel_parser)
    cancel_parser.set_defaults(func=commands.cmd_cancel_round)

    commit_parser = subparsers.add_parser("commit", help="Stage-verified commit with prefix message and scope guard")
    commit_parser.add_argument("--repo", default=".")
    commit_parser.add_argument("--round", type=int, default=None)
    commit_parser.add_argument("--summary", required=True)
    commit_parser.add_argument("--allow-secrets", action="store_true",
                               help="Skip the built-in secret scan for this commit")
    add_json(commit_parser)
    add_dry_run(commit_parser)
    commit_parser.set_defaults(func=commands.cmd_commit)

    undo_parser = subparsers.add_parser("undo-round", help="Revert a bad commit and record it as a revert round")
    undo_parser.add_argument("--repo", default=".")
    undo_parser.add_argument("--sha", required=True)
    undo_parser.add_argument("--title")
    undo_parser.add_argument("--summary")
    add_json(undo_parser)
    add_dry_run(undo_parser)
    undo_parser.set_defaults(func=commands.cmd_undo_round)

    goal_parser = subparsers.add_parser("goal-met", help="Mark a configured goal met")
    goal_parser.add_argument("--repo", default=".")
    goal_parser.add_argument("--goal", required=True)
    add_json(goal_parser)
    add_dry_run(goal_parser)
    goal_parser.set_defaults(func=commands.cmd_goal_met)

    finish_parser = subparsers.add_parser("finish", help="Finish the run")
    finish_parser.add_argument("--repo", default=".")
    finish_parser.add_argument("--reason")
    finish_parser.add_argument("--stay", action="store_true", help="Stay on the autopilot branch instead of returning to origin")
    add_json(finish_parser)
    add_dry_run(finish_parser)
    finish_parser.set_defaults(func=commands.cmd_finish)

    report_parser = subparsers.add_parser("report", help="Print or write a markdown run report")
    report_parser.add_argument("--repo", default=".")
    report_parser.add_argument("--output", default=None, help="Write the report to a file instead of stdout")
    report_parser.add_argument("--force", action="store_true",
                               help="Allow --output paths outside the target repository")
    report_parser.add_argument("--lang", choices=["zh", "en"], default=None, help="Report language (default: config report_lang)")
    add_json(report_parser)
    report_parser.set_defaults(func=commands.cmd_report)

    retrospective_parser = subparsers.add_parser("retrospective", help="Print or write a run-level retrospective (type stats, blocked rounds)")
    retrospective_parser.add_argument("--repo", default=".")
    retrospective_parser.add_argument("--output", default=None, help="Write the retrospective to a file instead of stdout")
    retrospective_parser.add_argument("--force", action="store_true",
                                      help="Allow --output paths outside the target repository")
    retrospective_parser.add_argument("--lang", choices=["zh", "en"], default=None)
    add_json(retrospective_parser)
    retrospective_parser.set_defaults(func=commands.cmd_retrospective)

    analysis_save_parser = subparsers.add_parser("analysis-save", help="Cache a repository analysis snapshot (invalidated on HEAD/config change)")
    analysis_save_parser.add_argument("--repo", default=".")
    analysis_save_parser.add_argument("--content", default=None, help="The analysis as a JSON value")
    analysis_save_parser.add_argument("--name", default=None, help="Optional label for the cache entry")
    add_json(analysis_save_parser)
    add_dry_run(analysis_save_parser)
    analysis_save_parser.set_defaults(func=commands.cmd_analysis_save)

    analysis_load_parser = subparsers.add_parser("analysis-load", help="Read the cached analysis and report whether it is still valid")
    analysis_load_parser.add_argument("--repo", default=".")
    analysis_load_parser.set_defaults(func=commands.cmd_analysis_load)

    directive_add_parser = subparsers.add_parser("directive-add", help="Add a standing directive the loop must honor in every future round")
    directive_add_parser.add_argument("--repo", default=".")
    directive_add_parser.add_argument("--text", required=True)
    add_json(directive_add_parser)
    add_dry_run(directive_add_parser)
    directive_add_parser.set_defaults(func=commands.cmd_directive_add)

    directive_list_parser = subparsers.add_parser("directive-list", help="List standing directives")
    directive_list_parser.add_argument("--repo", default=".")
    directive_list_parser.set_defaults(func=commands.cmd_directive_list)

    secret_scan_parser = subparsers.add_parser("secret-scan", help="Scan the staged diff for secret-like content")
    secret_scan_parser.add_argument("--repo", default=".")
    add_json(secret_scan_parser)
    secret_scan_parser.set_defaults(func=commands.cmd_secret_scan)

    detect_verify_parser = subparsers.add_parser("detect-verify", help="Detect test/build commands from repo entry points")
    detect_verify_parser.add_argument("--repo", default=".")
    detect_verify_parser.add_argument("--apply", action="store_true", help="Write detected commands into config check_commands")
    add_json(detect_verify_parser)
    add_dry_run(detect_verify_parser)
    detect_verify_parser.set_defaults(func=commands.cmd_detect_verify)

    backlog_add_parser = subparsers.add_parser("backlog-add", help="Add a backlog candidate")
    backlog_add_parser.add_argument("--repo", default=".")
    backlog_add_parser.add_argument("--title", required=True)
    backlog_add_parser.add_argument("--reason")
    backlog_add_parser.add_argument("--value", type=int, default=None, help="Value 1-5 (preferred)")
    backlog_add_parser.add_argument("--effort", type=int, default=None, help="Effort 1-5 (preferred)")
    backlog_add_parser.add_argument("--type", default=None,
                                    help="bugfix|feature|refactor|perf|test|docs (default feature)")
    backlog_add_parser.add_argument("--risk", type=int, default=None, help="Risk 1-5 (default 1)")
    backlog_add_parser.add_argument("--depends-on", action="append", default=None,
                                    help="Candidate id that must be completed first (repeatable)")
    backlog_add_parser.add_argument("--impact", choices=["high", "medium", "low"], default=None, help="Legacy")
    backlog_add_parser.add_argument("--effort-level", choices=["small", "medium", "large"], default=None, help="Legacy")
    add_json(backlog_add_parser)
    add_dry_run(backlog_add_parser)
    backlog_add_parser.set_defaults(func=commands.cmd_backlog_add)

    backlog_update_parser = subparsers.add_parser("backlog-update", help="Update a backlog candidate's fields or status")
    backlog_update_parser.add_argument("--repo", default=".")
    backlog_update_parser.add_argument("--id", required=True)
    backlog_update_parser.add_argument("--title")
    backlog_update_parser.add_argument("--reason")
    backlog_update_parser.add_argument("--value", type=int, default=None, help="Value 1-5")
    backlog_update_parser.add_argument("--effort", type=int, default=None, help="Effort 1-5")
    backlog_update_parser.add_argument("--type", default=None)
    backlog_update_parser.add_argument("--risk", type=int, default=None, help="Risk 1-5")
    backlog_update_parser.add_argument("--depends-on", action="append", default=None)
    backlog_update_parser.add_argument("--status", choices=["pending", "picked", "completed", "blocked"])
    add_json(backlog_update_parser)
    add_dry_run(backlog_update_parser)
    backlog_update_parser.set_defaults(func=commands.cmd_backlog_update)

    backlog_remove_parser = subparsers.add_parser("backlog-remove", help="Remove a backlog candidate")
    backlog_remove_parser.add_argument("--repo", default=".")
    backlog_remove_parser.add_argument("--id", required=True)
    add_json(backlog_remove_parser)
    add_dry_run(backlog_remove_parser)
    backlog_remove_parser.set_defaults(func=commands.cmd_backlog_remove)

    backlog_list_parser = subparsers.add_parser("backlog-list", help="List backlog candidates")
    backlog_list_parser.add_argument("--repo", default=".")
    backlog_list_parser.set_defaults(func=commands.cmd_backlog_list)

    backlog_rank_parser = subparsers.add_parser(
        "backlog-rank",
        help="List candidates sorted by expected value per round (ranking_mode; classic is value/effort)",
    )
    backlog_rank_parser.add_argument("--repo", default=".")
    backlog_rank_parser.set_defaults(func=commands.cmd_backlog_rank)

    backlog_pick_parser = subparsers.add_parser("backlog-pick", help="Mark a candidate picked")
    backlog_pick_parser.add_argument("--repo", default=".")
    backlog_pick_parser.add_argument("--id", required=True)
    add_json(backlog_pick_parser)
    add_dry_run(backlog_pick_parser)
    backlog_pick_parser.set_defaults(func=commands.cmd_backlog_pick)

    ensure_branch_parser = subparsers.add_parser("ensure-branch", help="Create or check out the autopilot feature branch")
    ensure_branch_parser.add_argument("--repo", default=".")
    add_json(ensure_branch_parser)
    add_dry_run(ensure_branch_parser)
    ensure_branch_parser.set_defaults(func=commands.cmd_ensure_branch)

    push_parser = subparsers.add_parser("push", help="Push the current branch to its remote (never force)")
    push_parser.add_argument("--repo", default=".")
    add_json(push_parser)
    add_dry_run(push_parser)
    push_parser.set_defaults(func=commands.cmd_push)

    check_parser = subparsers.add_parser("check", help="Check stop conditions")
    check_parser.add_argument("--repo", default=".")
    check_parser.add_argument("--brief", action="store_true", help="Return loop-driving fields only (continue/stop_reason/warnings/backlog/action_hint/schedule) without full state and config")
    check_parser.set_defaults(func=commands.cmd_check)

    detect_parser = subparsers.add_parser("detect-agent", help="Detect the runtime agent and print adaptation context")
    detect_parser.add_argument("--repo", default=".")
    detect_parser.add_argument("--home", default=None, help="Override the agent home directory for detection")
    detect_parser.set_defaults(func=agent.cmd_detect_agent)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    if args.command is None:
        parser.error("a command is required")
    sys.exit(args.func(args))
