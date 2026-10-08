"""Configuration schema, validation, and persistence for .autopilot/config.json.

This module is the single source of truth for field defaults (default_config)
and for per-field validation rules (_CONFIG_FIELD_RULES); references/config.md
and README.md mirror it."""

import math
import re
import sys
from pathlib import Path

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
        "candidates_per_round": 4,
        "commit_every_rounds": 5,
        "verify_every_rounds": 3,
        "checkpoint_every": None,
        "expand_after_goals": False,
        "review_threshold": None,
        "max_blocked_in_a_row": 2,
        "check_commands": [],
        "smoke_commands": [],
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
        "min_pending_candidates": 3,
        "max_predicted_per_round": 1,
        "max_expansion_per_round": None,
        "max_expansion_waves": None,
        "dashboard": {"enabled": False, "auto_open": True, "port": 0, "domain_map": None},
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


def _config_error(path, message, source=None):
    if source:
        # Values came from init flags: the config file does not exist yet, so
        # telling the user to "fix the file" would point at nothing.
        print(
            "[ERROR] Invalid configuration ({}): {}. Fix the offending init flag and run init again.".format(source, message),
            file=sys.stderr,
        )
    else:
        # path 参数自诘认知：非 init-flag 来源的错误把真实文件名带上
        # （轮 21 dead-code 候选 321——收了不用的参数要么用要么删）。
        print(
            "[ERROR] Invalid .autopilot/config.json: {}. Fix the value or delete the file and run init again. (file: {})".format(message, path),
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
    # A user-written "dashboard" section is partial: overlay it on the defaults
    # field-by-field. merged.update() above replaced the whole dict, so merging
    # from merged["dashboard"] would be a self-copy that loses auto_open/port.
    if isinstance(config.get("dashboard"), dict):
        dash = dict(defaults["dashboard"])
        dash.update(config["dashboard"])
        merged["dashboard"] = dash
    validate_config(merged)
    return merged


# ---- 字段注册表（candidate-314，v1.14.0）----------------------------------
# default_config 的每个旋钮在此声明校验规则：validate_config 按表序执行共享
# 谓词，新增字段 = default_config 加默认值 + 这里加一行 + config.md 镜像，
# 不再手写一段 if 链（1.12.x 的负数校验两套机制并存即此漂移的实录）。
# 守卫测试 ConfigFieldRegistryTests 锁定「默认键集合 == 注册表 ∪ 豁免」。
# 行序即历史校验顺序——多字段同时非法时的首报错误被测试锚定，勿重排。
_CONFIG_FIELD_RULES = (
    # goals 项级校验（轮 32 candidate-343）：[42] 这类项会变成 normalize 原样
    # 返回、all_goals_met 恒 False 的死目标。
    (("goals",), "string_array", None),
    (("max_rounds", "max_minutes", "max_tokens", "max_round_scope"), "finite_number", None),
    (("deadline",), "deadline", None),
    # 负数会以零轮静默停机（0/-1 比较恒真）；0 是合法的「任一 blocked 即停」。
    (
        ("max_blocked_in_a_row", "retries_per_round"), "int_range",
        (0, None, "must be an integer or null", "must be a non-negative integer or null"),
    ),
    # candidates_per_round 无 null 档（文档默认 4，消息无 or null；wave9
    # candidate-384：null 曾通过 int_range 的跳过语义流到四个读者各自回退
    # 3/1/1/5）——int_positive 连 null 一起拒。
    (("candidates_per_round",), "int_positive", (1, "must be a positive integer")),
    (
        ("commit_every_rounds", "verify_every_rounds"), "int_range",
        (1, None, "must be a positive integer", "must be a positive integer"),
    ),
    (
        ("checkpoint_every",), "int_range",
        (1, None, "must be a positive integer or null", "must be a positive integer or null"),
    ),
    (
        ("review_threshold",), "int_range",
        (1, 5, "must be an integer 1-5 or null", "must be an integer 1-5 or null"),
    ),
    (
        ("type_saturation_threshold",), "int_range",
        (0, None, "must be a non-negative integer", "must be a non-negative integer"),
    ),
    (("ranking_mode",), "choice", ("expected", "classic")),
    (
        ("min_candidate_value",), "int_range",
        (1, 5, "must be an integer 1-5 or null", "must be an integer 1-5 or null"),
    ),
    (
        ("max_same_type_per_round",), "int_range",
        (1, None, "must be a positive integer or null", "must be a positive integer or null"),
    ),
    (
        ("min_pending_candidates", "max_predicted_per_round",
         "max_expansion_per_round", "max_expansion_waves"), "int_range",
        (0, None, "must be a non-negative integer or null", "must be a non-negative integer or null"),
    ),
    # smoke_commands 与 check_commands 同形但分开成键：轮间的廉价检查不得被
    # 循环当作正确性证明（见 references/config.md）。
    (("check_commands",), "string_array", None),
    (("smoke_commands",), "string_array", None),
    (("allow_paths", "deny_paths"), "string_array", None),
    (("secret_patterns",), "regex_array", None),
    (("report_lang",), "choice", ("zh", "en")),
    (("branch_mode",), "choice", ("current", "feature")),
    (
        ("push", "allow_uncommitted_changes", "track_state", "scan_secrets",
         "expand_after_goals"), "bool", None,
    ),
    (("commit_message_prefix",), "string", None),
)

# 注册表豁免：repo 由 load_config 强制回填、dashboard 是嵌套对象且自带专项
# 校验，均不走机械谓词。
_REGISTRY_EXEMPT_FIELDS = ("repo", "dashboard")


def field_int_rule(name):
    """(lo, hi) bounds of an int-registered field (int_range or int_positive
    rows), or None when the field carries no int bound. cmd_config_set
    validates its --flags against the same bounds the validator enforces, so
    the two range mechanisms cannot drift apart again (candidate-314)."""
    for fields, kind, spec in _CONFIG_FIELD_RULES:
        if name not in fields:
            continue
        if kind == "int_range":
            return spec[0], spec[1]
        if kind == "int_positive":
            return spec[0], None
    return None


def _validate_registered_field(path, source, key, value, kind, spec):
    """One registry row, one field. Error texts are byte-identical to the
    hand-written checks this table replaced."""
    if kind == "int_range":
        lo, hi, type_msg, range_msg = spec
        if value is None:
            return
        if isinstance(value, bool) or not isinstance(value, int):
            _config_error(path, "'{}' {}".format(key, type_msg), source)
        if value < lo or (hi is not None and value > hi):
            _config_error(path, "'{}' {}".format(key, range_msg), source)
    elif kind == "int_positive":
        lo, msg = spec
        if isinstance(value, bool) or not isinstance(value, int) or value < lo:
            _config_error(path, "'{}' {}".format(key, msg), source)
    elif kind == "finite_number":
        if value is not None and not isinstance(value, (int, float)):
            _config_error(path, "'{}' must be a number or null".format(key), source)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and not math.isfinite(value):
            # JSON's NaN/Infinity literals (e.g. a hand-edited 1e999) parse as
            # float inf and would make every budget comparison meaningless.
            _config_error(path, "'{}' must be a finite number or null".format(key), source)
        if isinstance(value, bool) or (isinstance(value, (int, float)) and value < 0):
            _config_error(path, "'{}' must be a non-negative number or null".format(key), source)
    elif kind == "deadline":
        if value is not None:
            if not isinstance(value, str):
                _config_error(path, "'deadline' must be an ISO-8601 timestamp string or null", source)
            elif io.parse_time(value) is None:
                _config_error(
                    path,
                    "'deadline' must be an absolute ISO-8601 timestamp (for example "
                    "'2026-08-10T08:00:00'). Use init --deadline to resolve relative "
                    "expressions like '+8h' or '08:00'. Or fix in place with "
                    "config-set --deadline.",
                    source,
                )
    elif kind == "regex_array":
        if not isinstance(value, list) or not all(isinstance(p, str) for p in value):
            _config_error(path, "'{}' must be an array of strings".format(key), source)
            return
        for pattern in value:
            try:
                re.compile(pattern)
            except re.error as exc:
                _config_error(
                    path,
                    "'secret_patterns' contains an invalid regex ({}): {}".format(pattern, exc),
                    source,
                )
    elif kind == "choice":
        if value not in spec:
            _config_error(
                path,
                "'{}' must be {}".format(key, " or ".join("'{}'".format(c) for c in spec)),
                source,
            )
    elif kind == "string_array":
        if not isinstance(value, list) or not all(isinstance(c, str) for c in value):
            _config_error(path, "'{}' must be an array of strings".format(key), source)
    elif kind == "bool":
        if not isinstance(value, bool):
            _config_error(path, "'{}' must be true or false".format(key), source)
    elif kind == "string":
        if not isinstance(value, str):
            _config_error(path, "'{}' must be a string".format(key), source)


def validate_config(merged, source=None):
    """Validate one fully merged config dict (defaults included). Raises
    SystemExit(2) via _config_error on the first violation. Pure function:
    load_config runs it after reading disk, and cmd_init runs it on the
    command-line-built config BEFORE save_config, so an invalid init can never
    write a config the next load would reject. `source` labels config errors
    that trace back to init flags rather than an on-disk file.

    Mechanical checks are table-driven (_CONFIG_FIELD_RULES, candidate-314):
    the walk order is the historical check order, so the first-reported error
    for a multi-invalid config is unchanged."""
    path = config_path_for(Path(merged.get("repo") or "."))
    for fields, kind, spec in _CONFIG_FIELD_RULES:
        for key in fields:
            _validate_registered_field(path, source, key, merged.get(key), kind, spec)
    dash = merged.get("dashboard")
    if dash is not None:
        if not isinstance(dash, dict):
            _config_error(path, "'dashboard' must be an object or null", source)
        for key in ("enabled", "auto_open"):
            if not isinstance(dash[key], bool):
                _config_error(path, "'dashboard.{}' must be a boolean".format(key), source)
        port = dash["port"]
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            _config_error(path, "'dashboard.port' must be an integer in 0..65535", source)
        domain_map = dash["domain_map"]
        if domain_map is not None:
            if not isinstance(domain_map, dict):
                _config_error(path, "'dashboard.domain_map' must be an object or null", source)
            for prefix, value in domain_map.items():
                valid = (
                    isinstance(value, dict)
                    and isinstance(value.get("name"), str)
                    and (value.get("meaning") is None or isinstance(value.get("meaning"), str))
                )
                if not valid:
                    _config_error(
                        path,
                        "'dashboard.domain_map[{}]' must be {{\"name\": str, \"meaning\"?: str}}".format(prefix),
                        source,
                    )


def save_config(repo, config):
    io.save_json(config_path_for(repo), config)
