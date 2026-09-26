"""Argument parsing and dispatch for the autopilot state helper.

Each subparser registers its handler via ``set_defaults(func=...)`` — a single
registration point per command, no second name->handler table to keep in sync."""

import argparse
import io
import sys

from . import agent, commands, miner


def _force_utf8_stdio():
    """Windows pipes inherit the ANSI code page on py3.6: a zh report or a
    seed title with non-GBK characters would crash the loop mid-print with
    UnicodeEncodeError. Force UTF-8 with replacement on both streams.
    Idempotent: skip streams already wrapped as UTF-8 text."""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        buffer = getattr(stream, "buffer", None)
        if buffer is None:
            continue
        encoding = getattr(stream, "encoding", None)
        if encoding and encoding.lower().replace("-", "") == "utf8":
            continue
        setattr(
            sys,
            name,
            io.TextIOWrapper(buffer, encoding="utf-8", errors="replace", line_buffering=True),
        )


def build_parser():
    from . import __version__

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version",
                        version="auto-iterate-project {}".format(__version__))
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
    init_parser.add_argument("--smoke-commands", action="append", default=None)
    init_parser.add_argument("--max-expansion-waves", dest="max_expansion_waves", type=int, default=None)
    init_parser.add_argument("--push", action="store_true")
    init_parser.add_argument("--commit-message-prefix", default=None)
    init_parser.add_argument("--retries-per-round", type=int, default=None)
    init_parser.add_argument("--candidates-per-round", type=int, default=None, help="Backlog candidates to work per round (default 4)")
    init_parser.add_argument("--max-blocked-in-a-row", type=int, default=None)
    init_parser.add_argument("--commit-every-rounds", type=int, default=None,
                             help="Commit accumulated changes once per this many rounds (default 5)")
    init_parser.add_argument("--verify-every-rounds", type=int, default=None,
                             help="Run full verification once per this many rounds (default 3)")
    init_parser.add_argument("--checkpoint-every", type=int, default=None,
                             help="Pause and ask the user once per this many rounds (default off)")
    init_expand_group = init_parser.add_mutually_exclusive_group()
    init_expand_group.add_argument("--expand-after-goals", dest="expand_after_goals",
                                   action="store_true", default=None,
                                   help="Keep iterating after all goals are met (scout new candidates instead of stopping)")
    init_expand_group.add_argument("--no-expand-after-goals", dest="expand_after_goals",
                                   action="store_false", default=None,
                                   help="Stop when all goals are met (disable the expansion phase)")
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
    init_parser.add_argument("--max-predicted-per-round", type=int, default=None,
                             help="Anti-noise quota: at most this many predicted-origin candidates per recommended round (default 1; null disables)")
    init_parser.add_argument("--max-expansion-per-round", type=int, default=None,
                             help="Quota for expansion-origin candidates per recommended round (default null = uncapped; expansion work already passed the main agent's value gate)")
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

    dash_parser = subparsers.add_parser("dashboard", help="Run the read-only observation dashboard")
    dash_parser.add_argument("--repo", default=".")
    # --serve is parse-only (cmd_dashboard always serves); it exists so
    # spawn_server's command line reads explicitly and users can type it.
    dash_parser.add_argument("--serve", action="store_true", help="run the server in the foreground (default)")
    dash_parser.add_argument("--port", type=int, default=0)
    dash_parser.add_argument("--no-open", dest="auto_open", action="store_false", default=True)
    dash_parser.add_argument("--stop", action="store_true", help="stop a running dashboard")
    dash_parser.set_defaults(func=commands.cmd_dashboard)

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
    complete_parser.add_argument("--below-threshold", action="store_true",
                                 help="Record the round as completed even when --review-score is below review_threshold "
                                      "(the low score still feeds calibration; it does not count as blocked)")
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
    goal_parser.add_argument("--next-step", action="append", default=None,
                             help="Predicted follow-up direction; each occurrence creates one direction seed (repeatable)")
    goal_parser.add_argument("--unlocked-capability", action="append", default=None,
                             help="Capability the completed goal unlocked (repeatable, recorded on the goal event and seeds)")
    goal_parser.add_argument("--seed-type", default=None,
                             help="Type for created seeds (default: first non-saturated type)")
    goal_parser.add_argument("--seed-value", type=int, default=None, help="Seed value 1-5 (default 4)")
    goal_parser.add_argument("--seed-effort", type=int, default=None, help="Seed effort 1-5 (default 2)")
    goal_parser.add_argument("--round", type=int, default=None,
                             help="Round number that completed this goal (must match a completed history entry); "
                                  "omitting it marks the goal unverified and withholds the 'all goals met' stop")
    goal_parser.add_argument("--evidence", default=None,
                             help="Audit-only note on the evidence that the goal is met (recorded on the goal event)")
    goal_parser.add_argument("--no-auto-context", action="store_true",
                             help="Skip the automatic commit-topic/type-stats snapshot for the goal event")
    add_json(goal_parser)
    add_dry_run(goal_parser)
    goal_parser.set_defaults(func=commands.cmd_goal_met)

    seed_reject_parser = subparsers.add_parser(
        "seed-reject",
        help="Reject an open direction seed (value-gate / evidence check refused it)",
    )
    seed_reject_parser.add_argument("--repo", default=".")
    seed_reject_parser.add_argument("--id", required=True, help="Direction seed id (e.g. seed-003)")
    seed_reject_parser.add_argument("--reason", required=True, help="Why the hypothesis died (stored as the seed's outcome)")
    add_json(seed_reject_parser)
    add_dry_run(seed_reject_parser)
    seed_reject_parser.set_defaults(func=commands.cmd_seed_reject)

    finish_parser = subparsers.add_parser("finish", help="Finish the run")
    finish_parser.add_argument("--repo", default=".")
    finish_parser.add_argument("--reason")
    finish_parser.add_argument("--stay", action="store_true", help="Stay on the autopilot branch instead of returning to origin")
    finish_parser.add_argument("--force", action="store_true",
                               help="Finish even while no stop condition is reached and ready work remains (user-approved early stop)")
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
    directive_add_parser.add_argument("--text", "--directive", dest="text", required=True)
    add_json(directive_add_parser)
    add_dry_run(directive_add_parser)
    directive_add_parser.set_defaults(func=commands.cmd_directive_add)

    directive_list_parser = subparsers.add_parser("directive-list", help="List standing directives")
    directive_list_parser.add_argument("--repo", default=".")
    directive_list_parser.set_defaults(func=commands.cmd_directive_list)

    directive_remove_parser = subparsers.add_parser(
        "directive-remove",
        help="Remove a standing directive by its 1-based index in directive-list",
    )
    directive_remove_parser.add_argument("--repo", default=".")
    directive_remove_parser.add_argument("--index", type=int, required=True,
                                         help="1-based position from directive-list output")
    add_json(directive_remove_parser)
    add_dry_run(directive_remove_parser)
    directive_remove_parser.set_defaults(func=commands.cmd_directive_remove)

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

    mine_parser = subparsers.add_parser(
        "mine",
        help="Deterministic repo scan that yields backlog candidates (markers, swallowed, syntax, test-gap, hotspot, dead-export, docs-drift)",
    )
    mine_parser.add_argument("--repo", default=".")
    mine_parser.add_argument(
        "--kind",
        action="append",
        default=None,
        choices=list(miner.MINE_KINDS),
        help="Limit to one or more scanners (repeatable; default: all)",
    )
    mine_parser.add_argument("--limit", type=int, default=20, help="Max findings per scanner (default 20)")
    mine_parser.add_argument(
        "--apply",
        action="store_true",
        help="Write new findings into the backlog as candidates (deduped against existing evidence)",
    )
    add_json(mine_parser)
    add_dry_run(mine_parser)
    mine_parser.set_defaults(func=commands.cmd_mine)

    config_set_parser = subparsers.add_parser(
        "config-set",
        help="Update .autopilot/config.json at runtime and re-fingerprint state",
    )
    config_set_parser.add_argument("--repo", default=".")
    expand_group = config_set_parser.add_mutually_exclusive_group()
    expand_group.add_argument("--expand-after-goals", dest="expand_after_goals",
                              action="store_true", default=None,
                              help="Keep iterating after all goals are met (scout new candidates instead of stopping)")
    expand_group.add_argument("--no-expand-after-goals", dest="expand_after_goals",
                              action="store_false", default=None,
                              help="Stop when all goals are met (disable the expansion phase)")
    dash_group = config_set_parser.add_mutually_exclusive_group()
    dash_group.add_argument("--dashboard", dest="dashboard_enabled", action="store_true", default=None,
                            help="enable the read-only observation dashboard")
    dash_group.add_argument("--no-dashboard", dest="dashboard_enabled", action="store_false", default=None,
                            help="disable the observation dashboard")
    config_set_parser.add_argument("--dashboard-port", type=int, default=None, metavar="N",
                                   help="dashboard port (0 = random)")
    cadence = config_set_parser.add_argument_group("cadence fields")
    cadence.add_argument("--candidates-per-round", dest="candidates_per_round",
                         type=int, metavar="N",
                         help="Candidates worked per round (positive integer)")
    cadence.add_argument("--commit-every-rounds", dest="commit_every_rounds",
                         type=int, metavar="N",
                         help="Commit flush cadence in rounds (positive integer)")
    cadence.add_argument("--verify-every-rounds", dest="verify_every_rounds",
                         type=int, metavar="N",
                         help="Full verification cadence in rounds (positive integer)")
    cadence.add_argument("--checkpoint-every", dest="checkpoint_every",
                         type=int, metavar="N",
                         help="Human checkpoint cadence in rounds (positive integer)")
    budgets = config_set_parser.add_argument_group("budget fields")
    budgets.add_argument("--max-rounds", dest="max_rounds", type=int, metavar="N",
                         help="Round budget (positive integer)")
    budgets.add_argument("--clear-max-rounds", dest="clear_max_rounds",
                         action="store_true", default=False,
                         help="Remove the round budget")
    budgets.add_argument("--max-minutes", dest="max_minutes", type=int, metavar="N",
                         help="Idle-clock minute budget (positive integer)")
    budgets.add_argument("--clear-max-minutes", dest="clear_max_minutes",
                         action="store_true", default=False,
                         help="Remove the minute budget")
    budgets.add_argument("--max-tokens", dest="max_tokens", type=int, metavar="N",
                         help="Token budget (positive integer)")
    budgets.add_argument("--clear-max-tokens", dest="clear_max_tokens",
                         action="store_true", default=False,
                         help="Remove the token budget")
    budgets.add_argument("--max-expansion-waves", dest="max_expansion_waves", type=int, metavar="N",
                         help="Cap Deep Expansion waves for the run (non-negative integer)")
    budgets.add_argument("--clear-max-expansion-waves", dest="clear_max_expansion_waves",
                         action="store_true", default=False,
                         help="Remove the expansion wave cap")
    deadline = config_set_parser.add_mutually_exclusive_group()
    deadline.add_argument("--deadline", dest="deadline", metavar="EXPR",
                          help="Absolute stop time (ISO timestamp, +8h duration, or HH:MM)")
    deadline.add_argument("--clear-deadline", dest="clear_deadline",
                          action="store_true", default=False,
                          help="Remove the deadline")
    pushes = config_set_parser.add_mutually_exclusive_group()
    pushes.add_argument("--push", dest="push", action="store_true", default=None,
                        help="Allow complete-round to push the run branch")
    pushes.add_argument("--no-push", dest="push", action="store_false", default=None,
                        help="Forbid pushing (default)")
    scans = config_set_parser.add_mutually_exclusive_group()
    scans.add_argument("--scan-secrets", dest="scan_secrets", action="store_true", default=None,
                       help="Scan staged diffs for secret-like content on commit")
    scans.add_argument("--no-scan-secrets", dest="scan_secrets", action="store_false", default=None,
                       help="Disable the staged-diff secret scan")
    config_set_parser.add_argument("--check-commands", dest="check_commands",
                                   action="append", metavar="CMD",
                                   help="Verification command (repeatable; replaces the previous list)")
    config_set_parser.add_argument("--smoke-commands", dest="smoke_commands",
                                   action="append", metavar="CMD",
                                   help="Cheap between-verification check (repeatable; replaces the previous list)")
    config_set_parser.add_argument("--clear-smoke-commands", dest="clear_smoke_commands",
                                   action="store_true", default=False,
                                   help="Remove every declared smoke command")
    config_set_parser.add_argument("--clear-check-commands", dest="clear_check_commands",
                                   action="store_true", default=False,
                                   help="Remove all verification commands")
    config_set_parser.add_argument("--report-lang", dest="report_lang", choices=["zh", "en"],
                                   help="Language for phase reports and check warnings")
    add_json(config_set_parser)
    add_dry_run(config_set_parser)
    config_set_parser.set_defaults(func=commands.cmd_config_set)

    expansion_record_parser = subparsers.add_parser(
        "expansion-record",
        help="Record one Deep Expansion wave's lenses (rotation audit trail)",
    )
    expansion_record_parser.add_argument("--repo", default=".")
    expansion_record_parser.add_argument("--lens", action="append", required=True,
                                         help="Lens used by this wave (repeatable; must be one of the EXPANSION_LENSES)")
    add_json(expansion_record_parser)
    add_dry_run(expansion_record_parser)
    expansion_record_parser.set_defaults(func=commands.cmd_expansion_record)

    backlog_add_parser = subparsers.add_parser("backlog-add", help="Add a backlog candidate")
    backlog_add_parser.add_argument("--repo", default=".")
    backlog_add_parser.add_argument("--title", default=None,
                                    help="Candidate title (defaults to the seed title when --from-seed is used)")
    backlog_add_parser.add_argument("--reason")
    backlog_add_parser.add_argument("--from-seed", default=None,
                                    help="Promote a direction seed (seed id); title/type/value/effort/risk default to the seed")
    backlog_add_parser.add_argument("--value", type=int, default=None, help="Value 1-5 (preferred)")
    backlog_add_parser.add_argument("--effort", type=int, default=None, help="Effort 1-5 (preferred)")
    backlog_add_parser.add_argument("--type", default=None,
                                    help="bugfix|feature|refactor|perf|test|docs (default feature)")
    backlog_add_parser.add_argument("--risk", type=int, default=None, help="Risk 1-5 (default 1)")
    backlog_add_parser.add_argument("--depends-on", action="append", default=None,
                                    help="Candidate id that must be completed first (repeatable)")
    backlog_add_parser.add_argument("--origin", default=None,
                                    help="observed|predicted|expansion (predicted work is score-discounted and quota-limited; default observed, from-seed defaults predicted)")
    backlog_add_parser.add_argument("--confidence", type=float, default=None,
                                    help="Belief 0.5-1.0 for predicted/expansion work (default 0.75); observed is always 1.0")
    backlog_add_parser.add_argument("--based-on", default=None,
                                    help="Goal text this candidate was predicted from (goal-chain bonus <=1.08; from-seed defaults to the seed's source goal)")
    backlog_add_parser.add_argument("--evidence", default=None,
                                    help="Audit-only note on the supporting evidence (not scored)")
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
    backlog_remove_parser.add_argument("--id", "--candidate-id", dest="id", required=True)
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
    backlog_rank_parser.add_argument("--top", type=int, metavar="N", default=None,
                                     help="Print at most N entries (positive integer); the recommended batch stays in the head")
    backlog_rank_parser.add_argument("--pending-only", action="store_true",
                                     help="Drop completed/blocked history and print only pending/picked entries")
    backlog_rank_parser.add_argument("--brief", action="store_true",
                                     help="Compact entries: drop score_breakdown and free text, keep the loop-driving fields")
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

    prep_parser = subparsers.add_parser(
        "round-prep",
        help="One call for the loop's round start: check fields + suggested candidates + analysis + directives",
    )
    prep_parser.add_argument("--repo", default=".")
    prep_parser.add_argument("--top", type=int, metavar="N", default=None,
                             help="Candidates to include (positive integer; default candidates_per_round + 1)")
    prep_parser.set_defaults(func=commands.cmd_round_prep)

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
    _force_utf8_stdio()
    parser = build_parser()
    args = parser.parse_args()
    if args.command is None:
        parser.error("a command is required")
    try:
        code = args.func(args)
    except OSError as exc:
        # Malformed paths (e.g. shell-mangled \\?\ device paths) used to escape
        # as raw tracebacks from ~30 entry points; the contract is [ERROR]+2.
        print("[ERROR] OS-level failure: {}".format(exc), file=sys.stderr)
        code = 2
    except (ValueError, KeyError, TypeError) as exc:
        # Corrupt state payloads / bad numeric fields should also honor the
        # [ERROR]+2 contract instead of dumping a traceback into the loop.
        print("[ERROR] Invalid input or state: {}".format(exc), file=sys.stderr)
        code = 2
    sys.exit(code)
