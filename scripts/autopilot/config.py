"""Configuration schema, validation, and persistence for .autopilot/config.json.

This module is the single source of truth for field defaults (default_config);
references/config.md and README.md mirror it."""

import re
import sys

from . import io

LEGACY_IMPACT_SCORE = {"high": 5, "medium": 3, "low": 1}
LEGACY_EFFORT_SCORE = {"small": 1, "medium": 3, "large": 5}


def default_config(repo):
    return {
        "repo": str(repo),
        "goals": [],
        "max_rounds": io.DEFAULT_MAX_ROUNDS,
        "max_minutes": None,
        "deadline": None,
        "max_tokens": None,
        "max_round_scope": None,
        "allow_uncommitted_changes": False,
        "push": False,
        "branch_mode": "current",
        "commit_message_prefix": "autopilot",
        "retries_per_round": 3,
        "candidates_per_round": 3,
        "commit_every_rounds": 5,
        "verify_every_rounds": 3,
        "checkpoint_every": None,
        "expand_after_goals": False,
        "review_threshold": None,
        "max_blocked_in_a_row": 2,
        "check_commands": [],
        "track_state": False,
        "allow_paths": [],
        "deny_paths": [],
        "scan_secrets": True,
        "secret_patterns": [],
        "type_saturation_threshold": 2,
        "report_lang": "zh",
        "ranking_mode": "expected",
        "min_candidate_value": 3,
        "max_same_type_per_round": 2,
    }


def default_backlog():
    return {
        "next_id": 1,
        "candidates": [],
    }


def config_path_for(repo):
    return repo / io.AUTOPILOT_DIR / io.CONFIG_FILENAME


def state_path_for(repo):
    return repo / io.AUTOPILOT_DIR / io.STATE_FILENAME


def backlog_path_for(repo):
    return repo / io.AUTOPILOT_DIR / io.BACKLOG_FILENAME


def _config_error(path, message):
    print(
        "[ERROR] Invalid .autopilot/config.json: {}. Fix the value or delete the file and run init again.".format(message),
        file=sys.stderr,
    )
    raise SystemExit(2)


def load_config(repo):
    defaults = default_config(repo)
    config = io.load_json(config_path_for(repo), None)
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
    deadline = merged.get("deadline")
    if deadline is not None:
        if not isinstance(deadline, str):
            _config_error(config_path_for(repo), "'deadline' must be an ISO-8601 timestamp string or null")
        elif io.parse_time(deadline) is None:
            _config_error(
                config_path_for(repo),
                "'deadline' must be an absolute ISO-8601 timestamp (for example "
                "'2026-08-10T08:00:00'). Use init --deadline to resolve relative "
                "expressions like '+8h' or '08:00'.",
            )
    if merged.get("max_blocked_in_a_row") is not None and not isinstance(merged["max_blocked_in_a_row"], int):
        _config_error(config_path_for(repo), "'max_blocked_in_a_row' must be an integer or null")
    if merged.get("retries_per_round") is not None and not isinstance(merged["retries_per_round"], int):
        _config_error(config_path_for(repo), "'retries_per_round' must be an integer or null")
    cpr = merged.get("candidates_per_round")
    if cpr is not None and (not isinstance(cpr, int) or isinstance(cpr, bool) or cpr < 1):
        _config_error(config_path_for(repo), "'candidates_per_round' must be a positive integer")
    for key in ("commit_every_rounds", "verify_every_rounds"):
        value = merged.get(key)
        if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 1):
            _config_error(config_path_for(repo), "'{}' must be a positive integer".format(key))
    checkpoint = merged.get("checkpoint_every")
    if checkpoint is not None and (not isinstance(checkpoint, int) or isinstance(checkpoint, bool) or checkpoint < 1):
        _config_error(config_path_for(repo), "'checkpoint_every' must be a positive integer or null")
    review = merged.get("review_threshold")
    if review is not None and (not isinstance(review, int) or isinstance(review, bool) or review < 1 or review > 5):
        _config_error(config_path_for(repo), "'review_threshold' must be an integer 1-5 or null")
    threshold = merged.get("type_saturation_threshold")
    if threshold is not None and (not isinstance(threshold, int) or isinstance(threshold, bool) or threshold < 0):
        _config_error(config_path_for(repo), "'type_saturation_threshold' must be a non-negative integer")
    if merged.get("ranking_mode") not in ("expected", "classic"):
        _config_error(config_path_for(repo), "'ranking_mode' must be 'expected' or 'classic'")
    mcv = merged.get("min_candidate_value")
    if mcv is not None and (not isinstance(mcv, int) or isinstance(mcv, bool) or mcv < 1 or mcv > 5):
        _config_error(config_path_for(repo), "'min_candidate_value' must be an integer 1-5 or null")
    mst = merged.get("max_same_type_per_round")
    if mst is not None and (not isinstance(mst, int) or isinstance(mst, bool) or mst < 1):
        _config_error(config_path_for(repo), "'max_same_type_per_round' must be a positive integer or null")
    if not isinstance(merged["check_commands"], list) or not all(isinstance(c, str) for c in merged["check_commands"]):
        _config_error(config_path_for(repo), "'check_commands' must be an array of strings")
    for key in ("allow_paths", "deny_paths", "secret_patterns"):
        if not isinstance(merged[key], list) or not all(isinstance(p, str) for p in merged[key]):
            _config_error(config_path_for(repo), "'{}' must be an array of strings".format(key))
    for pattern in merged["secret_patterns"]:
        try:
            re.compile(pattern)
        except re.error as exc:
            _config_error(
                config_path_for(repo),
                "'secret_patterns' contains an invalid regex ({}): {}".format(pattern, exc),
            )
    if merged.get("report_lang") not in ("zh", "en"):
        _config_error(config_path_for(repo), "'report_lang' must be 'zh' or 'en'")
    if merged.get("branch_mode") not in ("current", "feature"):
        _config_error(config_path_for(repo), "'branch_mode' must be 'current' or 'feature'")
    for key in ("push", "allow_uncommitted_changes", "track_state", "scan_secrets", "expand_after_goals"):
        if not isinstance(merged[key], bool):
            _config_error(config_path_for(repo), "'{}' must be true or false".format(key))
    if not isinstance(merged["commit_message_prefix"], str):
        _config_error(config_path_for(repo), "'commit_message_prefix' must be a string")
    return merged


def save_config(repo, config):
    io.save_json(config_path_for(repo), config)
