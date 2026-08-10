"""Argument parsing and command dispatch for the autopilot state helper."""

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
    init_parser.add_argument("--candidates-per-round", type=int, default=None, help="Backlog candidates to work per round (default 1)")
    init_parser.add_argument("--max-blocked-in-a-row", type=int, default=None)
    init_parser.add_argument("--commit-every-rounds", type=int, default=None,
                             help="Commit accumulated changes once per this many rounds (default 5)")
    init_parser.add_argument("--verify-every-rounds", type=int, default=None,
                             help="Run full verification once per this many rounds (default 3)")
    scan_group = init_parser.add_mutually_exclusive_group()
    scan_group.add_argument("--scan-secrets", dest="scan_secrets", action="store_true", default=None,
                            help="Scan staged diffs for secret-like content before commit (default)")
    scan_group.add_argument("--no-scan-secrets", dest="scan_secrets", action="store_false", default=None)
    init_parser.add_argument("--secret-pattern", action="append", default=None,
                             help="Extra regex patterns for the commit-time secret scan (repeatable)")
    init_parser.add_argument("--type-saturation-threshold", type=int, default=None,
                             help="Completed same-type candidates before ranking downweights the type (default 2)")
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
    complete_parser.add_argument("--commit-sha", default=None,
                                 help="Commit SHA recorded by the commit command (omit when deferring commits in batched mode)")
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
    commit_parser.add_argument("--allow-secrets", action="store_true",
                               help="Skip the built-in secret scan for this commit")
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

    retrospective_parser = subparsers.add_parser("retrospective", help="Print or write a run-level retrospective (type stats, blocked rounds)")
    retrospective_parser.add_argument("--repo", default=".")
    retrospective_parser.add_argument("--output", default=None, help="Write the retrospective to a file instead of stdout")
    retrospective_parser.add_argument("--lang", choices=["zh", "en"], default=None)
    add_json(retrospective_parser)

    analysis_save_parser = subparsers.add_parser("analysis-save", help="Cache a repository analysis snapshot (invalidated on HEAD/config change)")
    analysis_save_parser.add_argument("--repo", default=".")
    analysis_save_parser.add_argument("--content", default=None, help="The analysis as a JSON value")
    analysis_save_parser.add_argument("--name", default=None, help="Optional label for the cache entry")
    add_json(analysis_save_parser)
    add_dry_run(analysis_save_parser)

    analysis_load_parser = subparsers.add_parser("analysis-load", help="Read the cached analysis and report whether it is still valid")
    analysis_load_parser.add_argument("--repo", default=".")

    secret_scan_parser = subparsers.add_parser("secret-scan", help="Scan the staged diff for secret-like content")
    secret_scan_parser.add_argument("--repo", default=".")
    add_json(secret_scan_parser)

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
    backlog_add_parser.add_argument("--type", default=None,
                                    help="bugfix|feature|refactor|perf|test|docs (default feature)")
    backlog_add_parser.add_argument("--risk", type=int, default=None, help="Risk 1-5 (default 1)")
    backlog_add_parser.add_argument("--depends-on", action="append", default=None,
                                    help="Candidate id that must be completed first (repeatable)")
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
    backlog_update_parser.add_argument("--type", default=None)
    backlog_update_parser.add_argument("--risk", type=int, default=None, help="Risk 1-5")
    backlog_update_parser.add_argument("--depends-on", action="append", default=None)
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
        "init": commands.cmd_init,
        "read": commands.cmd_read,
        "diagnose": commands.cmd_diagnose,
        "begin-round": commands.cmd_begin_round,
        "complete-round": commands.cmd_complete_round,
        "block-round": commands.cmd_block_round,
        "cancel-round": commands.cmd_cancel_round,
        "commit": commands.cmd_commit,
        "undo-round": commands.cmd_undo_round,
        "goal-met": commands.cmd_goal_met,
        "finish": commands.cmd_finish,
        "report": commands.cmd_report,
        "retrospective": commands.cmd_retrospective,
        "analysis-save": commands.cmd_analysis_save,
        "analysis-load": commands.cmd_analysis_load,
        "secret-scan": commands.cmd_secret_scan,
        "detect-verify": commands.cmd_detect_verify,
        "backlog-add": commands.cmd_backlog_add,
        "backlog-update": commands.cmd_backlog_update,
        "backlog-remove": commands.cmd_backlog_remove,
        "backlog-list": commands.cmd_backlog_list,
        "backlog-rank": commands.cmd_backlog_rank,
        "backlog-pick": commands.cmd_backlog_pick,
        "ensure-branch": commands.cmd_ensure_branch,
        "push": commands.cmd_push,
        "check": commands.cmd_check,
        "detect-agent": agent.cmd_detect_agent,
    }
    sys.exit(handlers[args.command](args))
