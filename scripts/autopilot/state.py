"""State, backlog, branch, stop-condition, scoring, and report logic."""

import json
import math
import re
import sys
import unicodedata
import uuid
from datetime import datetime, timezone

from . import config, io

_ZERO_WIDTH_CHARS = ("\u200b", "\u200c", "\u200d", "\ufeff")


def normalize_goal_text(text):
    """Canonical comparison form for goal strings: NFC + zero-width/BOM
    removal + whitespace strip. Guards the goal-met -> stop-condition chain:
    a zero-width space used to make `goal-met` report success while
    all_goals_met stayed False forever (the loop could never stop)."""
    if not isinstance(text, str):
        return text
    cleaned = unicodedata.normalize("NFC", text)
    for char in _ZERO_WIDTH_CHARS:
        cleaned = cleaned.replace(char, "")
    return cleaned.strip()


def table_cell(text):
    """Make arbitrary text safe inside a markdown table cell: physical
    newlines/tabs become spaces (they break the row into phantom rows) and
    pipes are escaped (they create phantom columns)."""
    if not isinstance(text, str):
        return text
    cleaned = text.replace("|", "\\|")
    return re.sub(r"[\r\n\t]+", " ", cleaned).strip()


def default_state(repo, goals=None, config_fingerprint=None):
    """Single authority for the state.json schema. cmd_init creates fresh state
    from this; migrate_state backfills the same field set for older files."""
    started_at = io.now_iso()
    return {
        "schema": io.SCHEMA_VERSION,
        "run_id": uuid.uuid4().hex[:io.RUN_ID_LENGTH],
        "repo": str(repo),
        "branch": None,
        "origin_branch": None,
        "created_at": started_at,
        "started_at": started_at,
        "last_activity_at": started_at,
        "round": 0,
        "round_seq": 0,
        "completed_rounds": 0,
        "blocked_rounds": 0,
        "cancelled_rounds": 0,
        "reverted_rounds": 0,
        "estimated_tokens_used": 0,
        "run_start_sha": None,
        "billed_text": 0,
        "billed_binary": 0,
        "type_stats": {},
        "goals": list(goals or []),
        "completed_goals": [],
        "goal_events": [],
        "goal_seeds": [],
        "expansion_waves": [],
        "current_round": None,
        "history": [],
        "stop_reason": None,
        "finished_at": None,
        "config_fingerprint": config_fingerprint,
    }


def migrate_state(state):
    """Backfill missing state fields. Returns True if anything changed."""
    defaults = {
        "run_id": uuid.uuid4().hex[:io.RUN_ID_LENGTH],
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
        "goal_events": [],
        "goal_seeds": [],
        "expansion_waves": [],
        "repo": None,
        "created_at": io.now_iso(),
        "started_at": None,
        "round": 0,
        "stop_reason": None,
        "finished_at": None,
        "config_fingerprint": None,
        "run_start_sha": None,
        "billed_text": 0,
        "billed_binary": 0,
    }
    changed = False
    for key, value in defaults.items():
        if key not in state:
            state[key] = value
            changed = True
    if "round_seq" not in state:
        # Round numbers must never be reused, but zero-work aborted rounds no
        # longer advance the round counters; backfill the sequence from the
        # counters of every round the old scheme actually counted.
        state["round_seq"] = (
            state.get("completed_rounds", 0)
            + state.get("blocked_rounds", 0)
            + state.get("cancelled_rounds", 0)
            + state.get("reverted_rounds", 0)
        )
        changed = True
    if state.get("started_at") is None:
        state["started_at"] = state.get("created_at")
        changed = True
    if state.get("last_activity_at") is None:
        state["last_activity_at"] = state.get("started_at")
        changed = True
    return changed


_STATE_INT_KEYS = (
    "schema",
    "round",
    "round_seq",
    "completed_rounds",
    "blocked_rounds",
    "cancelled_rounds",
    "reverted_rounds",
    "estimated_tokens_used",
)


def _state_type_error(path, message):
    # The integrity trail outlives this process's stderr: log before dying so
    # the first detection of corrupted state is findable after the fact.
    try:
        io.append_log(path.parent.parent, "integrity", "error",
                      file=str(path), message=message)
    except Exception:
        pass
    print(
        "[ERROR] Invalid .autopilot/state.json: {}. Fix or delete it and run init again.".format(message),
        file=sys.stderr,
    )
    raise SystemExit(2)


def _validate_state_types(repo, state):
    """Corrupted field types (e.g. a string counter) must fail cleanly instead of
    raising a TypeError mid-command."""
    path = config.state_path_for(repo)
    for key in _STATE_INT_KEYS:
        value = state.get(key)
        if value is not None and not isinstance(value, int):
            _state_type_error(path, "'{}' must be a number, got {}".format(key, type(value).__name__))
    if not isinstance(state.get("history", []), list):
        _state_type_error(path, "'history' must be an array")
    for entry in state.get("history", []):
        if not isinstance(entry, dict):
            _state_type_error(path, "'history' items must be objects")
    if not isinstance(state.get("completed_goals", []), list):
        _state_type_error(path, "'completed_goals' must be an array")
    if not isinstance(state.get("goals", []), list):
        _state_type_error(path, "'goals' must be an array")
    if not isinstance(state.get("type_stats", {}), dict):
        _state_type_error(path, "'type_stats' must be an object")
    for key, value in (state.get("type_stats") or {}).items():
        if not isinstance(value, dict):
            _state_type_error(
                path,
                "'type_stats.{}' must be an object, got {}".format(key, type(value).__name__),
            )
    if not isinstance(state.get("goal_events", []), list):
        _state_type_error(path, "'goal_events' must be an array")
    if not isinstance(state.get("goal_seeds", []), list):
        _state_type_error(path, "'goal_seeds' must be an array")
    current = state.get("current_round")
    if current is not None and not isinstance(current, dict):
        _state_type_error(path, "'current_round' must be an object or null")
    if isinstance(current, dict) and not isinstance(current.get("round"), int):
        # Without a valid round number every round-closing command would die in
        # a KeyError while begin-round refuses (round already open) — a bricked
        # run. Fail cleanly instead.
        _state_type_error(path, "'current_round.round' must be a number")


def load_state(repo):
    path = config.state_path_for(repo)
    # Sentinel default: load_json's None default cannot tell "file absent" from
    # "file contains JSON null" — both used to report the bogus "state.json not
    # found" for a null file and hide the actual corruption.
    missing = object()
    state = io.load_json(path, missing)
    if state is missing:
        print("[ERROR] state.json not found. Run init first.", file=sys.stderr)
        raise SystemExit(2)
    if not isinstance(state, dict):
        detail = "null" if state is None else "a JSON {}".format(type(state).__name__)
        print(
            "[ERROR] .autopilot/state.json exists but is not a valid JSON object ({}). "
            "Fix or delete it and run init again.".format(detail),
            file=sys.stderr,
        )
        raise SystemExit(2)
    old_schema = state.get("schema", 1)
    if not isinstance(old_schema, int):
        # Must be caught before any comparison: a null/string schema would
        # otherwise die with a bare TypeError on the `<` below.
        _state_type_error(path, "'schema' must be a number, got {}".format(
            "null" if old_schema is None else type(old_schema).__name__))
    if old_schema > io.SCHEMA_VERSION:
        # Written by a newer skill version: migrating down would silently drop
        # fields this build does not know about. Fail closed and mutate nothing.
        print(
            "[ERROR] .autopilot/state.json was written by a newer autopilot version "
            "(schema {} > {}). Downgrading is not supported: update this skill to the "
            "matching version, or restore the previous state file. Do not delete the "
            "file unless the run is abandoned.".format(old_schema, io.SCHEMA_VERSION),
            file=sys.stderr,
        )
        raise SystemExit(2)
    changed = migrate_state(state)
    if state.get("run_start_sha") is None:
        # Pre-anchor states must not fall back to EMPTY_TREE: that bills the
        # whole EMPTY_TREE..HEAD diff as one round and can fake-trigger
        # max_tokens. Anchor billing at first load instead — work committed
        # before the upgrade is unreconstructable and stays unbilled on
        # purpose. EMPTY_TREE (an existing value) is left alone so an unborn
        # repo does not churn the state file on every load.
        head = io.run_git(repo, "rev-parse", "--verify", "-q", "HEAD")
        state["run_start_sha"] = head.stdout.strip() if head.returncode == 0 else io.EMPTY_TREE
        changed = True
    _validate_state_types(repo, state)
    if old_schema < io.SCHEMA_VERSION:
        state["schema"] = io.SCHEMA_VERSION
        changed = True
    state["repo"] = str(repo)
    if changed:
        save_state(repo, state)
        # Schema upgrades and field backfills mutate the user's state file: an
        # audit event makes "who changed state.json" answerable (migration, not
        # corruption).
        io.append_log(repo, "state-migrate", "success",
                      from_schema=old_schema, to_schema=io.SCHEMA_VERSION)
    return state


def save_state(repo, state):
    io.save_json(config.state_path_for(repo), state)


def load_backlog(repo):
    backlog = io.load_json(config.backlog_path_for(repo), config.default_backlog())
    _validate_backlog(repo, backlog)
    return backlog


def _validate_backlog(repo, backlog):
    """Same fail-clean policy as state.json: a hand-corrupted backlog must die
    with a clear message, not with a TypeError somewhere inside ranking."""
    path = config.backlog_path_for(repo)

    def fail(message):
        try:
            io.append_log(repo, "integrity", "error", file=str(path), message=message)
        except Exception:
            pass
        print(
            "[ERROR] Invalid .autopilot/backlog.json: {}. Fix or delete it and run init again.".format(message),
            file=sys.stderr,
        )
        raise SystemExit(2)

    if not isinstance(backlog, dict):
        fail("must be a JSON object, got {}".format(type(backlog).__name__))
    candidates = backlog.get("candidates")
    if not isinstance(candidates, list):
        fail("'candidates' must be an array")
    for candidate in candidates:
        if not isinstance(candidate, dict):
            fail("every candidate must be an object")
        if not isinstance(candidate.get("id"), str) or not candidate.get("id"):
            fail("every candidate needs a non-empty string 'id'")
    next_id = backlog.get("next_id")
    if not isinstance(next_id, int) or isinstance(next_id, bool):
        fail("'next_id' must be a number")


def save_backlog(repo, backlog):
    io.save_json(config.backlog_path_for(repo), backlog)


def find_candidate(backlog, candidate_id):
    for candidate in backlog.get("candidates", []):
        if candidate.get("id") == candidate_id:
            return candidate
    return None


def update_candidates_status(repo, candidate_ids, status, round_number=None, backlog=None, extra_fields=None):
    """Update many candidates with a single backlog read/write cycle. When a
    preloaded backlog is passed it is mutated here; otherwise the backlog is
    loaded once for the whole batch. The backlog is saved once when anything
    changed. `extra_fields` is merged into every updated candidate (e.g. the
    round's self-review score for value calibration)."""
    ids = [cid for cid in (candidate_ids or []) if cid]
    if not ids:
        return
    if backlog is None:
        backlog = load_backlog(repo)
    changed = False
    for candidate_id in ids:
        candidate = find_candidate(backlog, candidate_id)
        if candidate is None:
            print("[WARN] Candidate not found in backlog: {}".format(candidate_id), file=sys.stderr)
            continue
        candidate["status"] = status
        candidate["updated_at"] = io.now_iso()
        if round_number is not None:
            candidate["round"] = round_number
        if extra_fields:
            candidate.update(extra_fields)
        changed = True
    if changed:
        save_backlog(repo, backlog)


def append_history(st, entry):
    """Append a round-history entry with bounded growth: text fields are capped
    and only the most recent io.HISTORY_LIMIT entries are kept, so long-running
    loops do not grow state.json without bound."""
    for key in ("title", "summary", "reason", "review_notes"):
        value = entry.get(key)
        if isinstance(value, str) and len(value) > io.HISTORY_TEXT_LIMIT:
            entry[key] = value[:io.HISTORY_TEXT_LIMIT]
    entry.setdefault("finished_at", io.now_iso())
    history = st.setdefault("history", [])
    history.append(entry)
    if len(history) > io.HISTORY_LIMIT:
        del history[: len(history) - io.HISTORY_LIMIT]
    return entry


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


# --- Post-goal direction prediction (goal events + direction seeds) ---------
# Seeds carry the "because we shipped X, Y is probably next" hypotheses. They
# live in state.json (no separate file) with bounded growth; completed_goals
# stays a plain string array for backward compatibility.

SEED_STATUSES = ("open", "promoted", "verified", "refuted", "rejected")


def saturated_types(state, threshold=2):
    """Types whose completed-candidate count reached the saturation threshold
    (from the state's type_stats snapshot). Deterministic order."""
    try:
        threshold = max(0, int(threshold or 0))
    except (TypeError, ValueError):
        threshold = 2
    result = []
    for candidate_type in sorted(state.get("type_stats") or {}):
        entry = (state.get("type_stats") or {}).get(candidate_type)
        if not isinstance(entry, dict):
            entry = {}
        try:
            completed = int(entry.get("completed") or 0)
        except (TypeError, ValueError, OverflowError):
            completed = 0
        if completed >= threshold:
            result.append(candidate_type)
    return result


def _next_sequential_id(state, key, prefix):
    """Next zero-padded id (`ge-003` / `seed-011`). Numbering is simply the
    highest suffix among existing entries + 1 — there is no persistent counter.
    Ids are still never reused in the normal flow: the append helpers number
    before appending, and the bounded lists only truncate from the head
    (``del events[:n]`` / ``del seeds[:n]``), so the maximum suffix survives
    every truncation and the next id is always fresh — a stale
    candidate.from_seed can never come to point at a newer, unrelated seed."""
    existing = set()
    max_counter = 0
    for item in state.get(key) or []:
        if isinstance(item, dict):
            item_id = str(item.get("id") or "")
            if item_id.startswith(prefix):
                existing.add(item_id)
                suffix = item_id[len(prefix):]
                if suffix.isdigit():
                    max_counter = max(max_counter, int(suffix))
    counter = max_counter + 1
    while "{}{:03d}".format(prefix, counter) in existing:
        counter += 1
    return "{}{:03d}".format(prefix, counter)


def _truncate_seed_text(text):
    if isinstance(text, str) and len(text) > io.SEED_TEXT_LIMIT:
        return text[:io.SEED_TEXT_LIMIT]
    return text


def find_seed(state, seed_id):
    for seed in state.get("goal_seeds") or []:
        if isinstance(seed, dict) and seed.get("id") == seed_id:
            return seed
    return None


def open_seeds(state):
    """Seeds still awaiting a value-gate / promote decision."""
    return [s for s in state.get("goal_seeds") or []
            if isinstance(s, dict) and s.get("status") == "open"]


def append_goal_event(st, event):
    """Append a structured goal-completion record with bounded growth."""
    event["id"] = _next_sequential_id(st, "goal_events", "ge-")
    event.setdefault("met_at", io.now_iso())
    events = st.setdefault("goal_events", [])
    events.append(event)
    if len(events) > io.GOAL_EVENTS_LIMIT:
        del events[: len(events) - io.GOAL_EVENTS_LIMIT]
    return event


def append_seed(st, seed):
    """Append a direction-seed hypothesis with bounded growth and capped text."""
    seed["id"] = _next_sequential_id(st, "goal_seeds", "seed-")
    seed.setdefault("created_at", io.now_iso())
    seed.setdefault("status", "open")
    for key in ("title", "hypothesis", "from_capability", "source_goal"):
        seed[key] = _truncate_seed_text(seed.get(key))
    seeds = st.setdefault("goal_seeds", [])
    seeds.append(seed)
    if len(seeds) > io.SEEDS_LIMIT:
        del seeds[: len(seeds) - io.SEEDS_LIMIT]
    return seed


# Deep Expansion lens rotation (single authority; SKILL.md's lens table mirrors
# this order). expansion-record refuses lenses outside this tuple, and check
# reports the unused remainder so the agent can rotate deliberately.
EXPANSION_LENSES = (
    "architecture",
    "tests",
    "security",
    "performance",
    "docs",
    "dead-code",
    "api-design",
    "concurrency",
    "i18n",
    "config",
    "observability",
    "packaging",
    "data-integrity",
    "ux-copy",
    "dependency-graph",
    "cross-cutting",
)


def append_expansion_wave(st, lenses):
    """Append one Deep Expansion wave record with bounded growth (oldest
    trimmed). Lenses are deduplicated and sorted — the record is a set
    snapshot used to verify lens rotation across waves; `added` counts the
    candidates the wave produced (0 at record time; the audit trail lives in
    log.jsonl's backlog-add events between the two waves)."""
    waves = st.setdefault("expansion_waves", [])
    wave = {
        "at": io.now_iso(),
        "lenses": sorted(set(lenses or [])),
        "added": 0,
    }
    waves.append(wave)
    if len(waves) > io.EXPANSION_WAVES_LIMIT:
        del waves[: len(waves) - io.EXPANSION_WAVES_LIMIT]
    return wave


def resolve_seed(st, seed_id, status, notes=None, candidate_id=None):
    """Move a seed along its state machine. Returns the seed or None.
      open -> promoted (backlog-add --from-seed) -> verified (complete-round)
                                                 -> refuted  (block-round)
      open -> rejected (seed-reject: value-gate / evidence check refused it)
      promoted -> open (cancel-round write-back)
    Terminal states (verified/refuted/rejected) are immutable — a cancelled
    round can never resurrect a seed the statistics already counted. Failed
    hypotheses never flow back silently: a refuted seed keeps its notes so the
    hit-rate statistics stay honest."""
    seed = find_seed(st, seed_id)
    if seed is None:
        return None
    if status not in SEED_STATUSES:
        return None
    if seed.get("status") in ("verified", "refuted", "rejected"):
        return None
    now = io.now_iso()
    seed["status"] = status
    seed["updated_at"] = now
    if status == "promoted":
        seed["promoted_at"] = now
        if candidate_id:
            seed["promoted_candidate_id"] = candidate_id
    elif status == "verified":
        seed["verified_at"] = now
        if candidate_id:
            seed["promoted_candidate_id"] = candidate_id
    elif status == "refuted":
        seed["refuted_at"] = now
        seed["outcome"] = notes or ""
    elif status == "rejected":
        seed["rejected_at"] = now
        seed["outcome"] = notes or ""
    elif status == "open":
        # Cancel write-back: clear the stale promotion markers so the seed can be
        # re-promoted cleanly (the candidate itself returns to pending too).
        seed.pop("promoted_at", None)
        seed.pop("promoted_candidate_id", None)
    return seed


def _assert_safe_ref_name(name, what="branch"):
    """Reject ref names git would parse as options or path traversal."""
    if not name or not isinstance(name, str):
        print("[ERROR] Invalid {} name: empty.".format(what), file=sys.stderr)
        raise SystemExit(2)
    if name.startswith("-") or name.startswith("/") or ".." in name or name != name.strip():
        print(
            "[ERROR] Invalid {} name {!r}: must not start with '-', '/', contain '..', "
            "or have edge whitespace.".format(what, name),
            file=sys.stderr,
        )
        raise SystemExit(2)
    return name


def ensure_branch(repo, state, cfg, to_stderr=False):
    def say(msg):
        if to_stderr:
            print(msg, file=sys.stderr)
        else:
            print(msg)

    if cfg.get("branch_mode") != "feature":
        say("[SKIP] branch_mode is current; no autopilot branch created.")
        return None

    if state.get("origin_branch") is None:
        try:
            state["origin_branch"] = io.current_branch(repo)
        except SystemExit:
            state["origin_branch"] = None
        save_state(repo, state)

    branch = state.get("branch")
    if branch and not str(branch).startswith("autopilot/"):
        print(
            "[ERROR] state.json branch {!r} is not an autopilot branch; refusing to check it out. "
            "Reset .autopilot/state.json or run init --force to recover.".format(branch),
            file=sys.stderr,
        )
        raise SystemExit(2)

    if not cfg.get("allow_uncommitted_changes") and io.tracked_changes(repo):
        print(
            "[ERROR] Working tree has tracked changes and allow_uncommitted_changes is false; "
            "refusing to switch branches so user changes are not carried over.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    if not branch:
        branch = "autopilot/" + state.get("run_id", uuid.uuid4().hex[:io.RUN_ID_LENGTH])
        _assert_safe_ref_name(branch)
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
        _assert_safe_ref_name(branch)
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
    delimiters, stripping leading connectors like 以及/并且. A dot between two
    digits is a decimal point (v1.3.3 / 3.5), not a sentence break."""
    if not text:
        return []
    goals = []
    for part in re.split(r"(?:(?<![0-9])\.(?![0-9])|[。．；;\n\r，,、])+", text):
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


def compute_stop_reason(state, cfg):
    """Return the stop reason (or None) based on state and config. Shared by check and begin-round."""
    stop_reason = None

    if state.get("finished_at"):
        stop_reason = "already finished"
    elif state.get("stop_reason"):
        stop_reason = state["stop_reason"]

    if stop_reason is None:
        if (
            all_goals_met(cfg, state)
            and not cfg.get("expand_after_goals")
            and not unverified_goals(state, cfg)
        ):
            stop_reason = "all goals met"

    if stop_reason is None:
        # Count every round that consumed a slot in the run (matches round numbers):
        # completed, blocked, cancelled, and reverted all advance the counter.
        total_rounds = (
            state.get("completed_rounds", 0)
            + state.get("blocked_rounds", 0)
            + state.get("cancelled_rounds", 0)
            + state.get("reverted_rounds", 0)
        )
        max_rounds = cfg.get("max_rounds")
        if max_rounds is not None and total_rounds >= max_rounds:
            stop_reason = "max_rounds reached"

    if stop_reason is None:
        consecutive_blocked = count_consecutive_blocked(state)
        max_blocked = cfg.get("max_blocked_in_a_row")
        if max_blocked is not None and consecutive_blocked >= max_blocked:
            stop_reason = (
                "max_blocked_in_a_row reached ({}/{}); quality-failed rounds can complete "
                "instead: complete-round --below-threshold records the low score without "
                "counting blocked".format(consecutive_blocked, max_blocked)
            )

    if stop_reason is None:
        max_minutes = cfg.get("max_minutes")
        reference = state.get("last_activity_at") or state.get("started_at")
        started_at = io.parse_time(reference)
        if max_minutes is not None and started_at is not None:
            elapsed_minutes = (datetime.now(timezone.utc) - started_at).total_seconds() / 60
            if elapsed_minutes >= max_minutes:
                stop_reason = "max_minutes reached ({:.1f}/{})".format(elapsed_minutes, max_minutes)

    if stop_reason is None:
        deadline = cfg.get("deadline")
        deadline_at = io.parse_time(deadline)
        if deadline_at is not None and datetime.now(timezone.utc) >= deadline_at:
            stop_reason = "deadline reached ({})".format(deadline)

    if stop_reason is None:
        max_tokens = cfg.get("max_tokens")
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


def all_goals_met(cfg, state):
    goals = cfg.get("goals") or state.get("goals") or []
    if not goals:
        return False
    completed = {normalize_goal_text(goal) for goal in state.get("completed_goals") or []}
    return all(normalize_goal_text(goal) in completed for goal in goals)


def unverified_goals(state, cfg):
    """Configured goals recorded in completed_goals whose latest matching
    goal_event was written without evidence (goal-met ran without --round).
    compute_stop_reason withholds "all goals met" while this list is non-empty:
    a goal-met claim not anchored to a completed round is an assertion, not
    evidence. Goals with no surviving goal_event (event list truncation,
    --no-auto-context) stay verified — blocking on absent data would brick
    migrated/truncated runs; only a positive unverified marker withholds the
    stop."""
    completed = {normalize_goal_text(goal) for goal in state.get("completed_goals") or []}
    if not completed:
        return []
    events = [event for event in state.get("goal_events") or [] if isinstance(event, dict)]
    result = []
    for goal in cfg.get("goals") or state.get("goals") or []:
        normalized = normalize_goal_text(goal)
        if normalized not in completed:
            continue
        latest = None
        for event in events:
            if normalize_goal_text(event.get("goal") or "") == normalized:
                latest = event
        if latest is not None and latest.get("unverified"):
            result.append(goal)
    return result


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
    except (TypeError, ValueError, OverflowError):
        value, effort = 3, 3
    if effort <= 0:
        effort = 1
    return float(value) / float(effort)


VALID_CANDIDATE_TYPES = ("bugfix", "feature", "refactor", "perf", "test", "docs")


def _resolved_value(candidate):
    """Numeric value 1-5 with legacy impact mapping and safe fallbacks."""
    value = candidate.get("value")
    if value is None:
        value = config.LEGACY_IMPACT_SCORE.get(candidate.get("impact"), 3)
    try:
        return max(1, min(5, int(value)))
    except (TypeError, ValueError, OverflowError):
        return 3


def _resolved_effort(candidate):
    effort = candidate.get("effort")
    if effort is None or isinstance(effort, str):
        effort = config.LEGACY_EFFORT_SCORE.get(effort, 3)
    try:
        effort = int(effort)
    except (TypeError, ValueError, OverflowError):
        return 3
    return max(1, min(5, effort))


def _candidate_type(candidate):
    candidate_type = candidate.get("type") or "feature"
    if candidate_type not in VALID_CANDIDATE_TYPES:
        return "other"
    return candidate_type


def compute_type_stats(backlog):
    """Per-type completion/block statistics derived from the backlog. Feeds ranking
    (expected value, saturation, prospective mix) and the retrospective report.
    Also learns a per-type value calibration from self-review scores: when at least
    three completed candidates carry a review_score, calibration compares the
    average review against the average predicted value (clamped 0.6-1.5) so the
    agent's own optimism or pessimism self-corrects over a run. Types outside the
    VALID_CANDIDATE_TYPES list are grouped under their own key.
    Predicted-origin candidates (`origin: "predicted"`) are excluded here — they
    are accounted in compute_predicted_account, so a failed prediction never
    drags down the success rate of observed work in the same type.
    Calibration cold start: a type with fewer than three reviewed candidates
    borrows the run-wide review/value ratio (clamped 0.6-1.5) once at least
    three completed candidates anywhere carry a review score — early reviews
    then already shape ranking instead of every type starting neutral."""
    stats = {}
    global_review_sum = 0.0
    global_review_n = 0
    global_value_sum = 0
    global_value_n = 0
    for candidate in backlog.get("candidates", []):
        if candidate.get("origin") == "predicted":
            continue
        candidate_type = _candidate_type(candidate)
        entry = stats.setdefault(
            candidate_type,
            {"completed": 0, "blocked": 0, "total": 0, "effort_sum": 0, "value_sum": 0,
             "review_sum": 0.0, "review_n": 0},
        )
        status = candidate.get("status")
        if status in ("completed", "blocked"):
            entry["total"] += 1
            if status == "completed":
                entry["completed"] += 1
                parsed_value = 0
                try:
                    parsed_value = int(candidate.get("value") or 0)
                    entry["effort_sum"] += int(candidate.get("effort") or 0)
                    entry["value_sum"] += parsed_value
                except (TypeError, ValueError):
                    pass
                # Global calibration samples accumulate here, BEFORE the per-type
                # sums are popped below — the fallback needs the same evidence.
                global_value_sum += parsed_value
                global_value_n += 1
                review = candidate.get("review_score")
                # `review == review` rejects NaN, which would poison the average
                # and the calibration factor.
                if isinstance(review, (int, float)) and not isinstance(review, bool) and review == review:
                    entry["review_sum"] += review
                    entry["review_n"] += 1
                    global_review_sum += review
                    global_review_n += 1
            else:
                entry["blocked"] += 1
    for entry in stats.values():
        done = entry["completed"] + entry["blocked"]
        entry["blocked_rate"] = round(entry["blocked"] / done, 3) if done else 0.0
        avg_effort = round(entry["effort_sum"] / entry["completed"], 2) if entry["completed"] else 0.0
        avg_value = round(entry["value_sum"] / entry["completed"], 2) if entry["completed"] else 0.0
        entry["avg_effort"] = avg_effort
        entry["avg_value"] = avg_value
        review_avg = round(entry["review_sum"] / entry["review_n"], 2) if entry["review_n"] else None
        entry["review_n"] = entry["review_n"]
        entry["review_avg"] = review_avg
        if entry["review_n"] >= 3 and avg_value > 0 and review_avg is not None:
            entry["calibration"] = round(max(0.6, min(1.5, review_avg / avg_value)), 2)
        elif (
            entry["review_n"] < 3
            and global_review_n >= 3
            and global_value_n > 0
            and global_value_sum > 0
        ):
            entry["calibration"] = round(
                max(0.6, min(1.5, (global_review_sum / global_review_n) / (global_value_sum / global_value_n))),
                2,
            )
        else:
            entry["calibration"] = 1.0
        entry.pop("effort_sum", None)
        entry.pop("value_sum", None)
        entry.pop("review_sum", None)
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


PREDICTED_SAMPLE_FLOOR = 3


def compute_predicted_account(backlog):
    """Blocked/review sub-account for predicted-origin candidates only (刀 B).
    Predictions keep their own ledger so consecutive failures sink future
    predictions without touching the observed work's statistics. Until
    PREDICTED_SAMPLE_FLOOR resolved predictions exist, scoring falls back to a
    conservative prior (type success_rate x 0.75)."""
    completed = blocked = 0
    review_sum = 0.0
    review_n = 0
    for candidate in backlog.get("candidates", []):
        if candidate.get("origin") != "predicted":
            continue
        status = candidate.get("status")
        if status == "completed":
            completed += 1
            review = candidate.get("review_score")
            if isinstance(review, (int, float)) and not isinstance(review, bool) and review == review:
                review_sum += review
                review_n += 1
        elif status == "blocked":
            blocked += 1
    done = completed + blocked
    blocked_rate = round(blocked / done, 3) if done else 0.0
    return {
        "completed": completed,
        "blocked": blocked,
        "done": done,
        "blocked_rate": blocked_rate,
        "success_rate": round(1.0 - blocked_rate, 3),
        "review_n": review_n,
        "review_avg": round(review_sum / review_n, 2) if review_n else None,
        "calibration": (
            round(max(0.6, min(1.5, (review_sum / review_n) / max(1, completed))), 2)
            if review_n >= 3 and completed else 1.0
        ),
    }


def _score_classic(candidate, type_stats=None, saturation_threshold=2):
    """Legacy value/effort ratio scoring (ranking_mode: classic)."""
    base = _candidate_score(candidate)
    try:
        risk = int(candidate.get("risk") or 1)
    except (TypeError, ValueError, OverflowError):
        risk = 1
    risk = max(1, min(5, risk))
    risk_factor = max(0.5, 1.0 - 0.08 * (risk - 1))

    candidate_type = _candidate_type(candidate)
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


def _score_expected(candidate, type_stats=None, saturation_threshold=2,
                    unlocks=0, pending_mix=0.0, risk_weight=0.08,
                    completed_goals=None, predicted_account=None, batch_width=3):
    """Expected-value-per-round scoring (ranking_mode: expected, default).

    The scarce resource in an autopilot run is rounds, not effort — per-round
    overhead (analysis, verify, commit) dominates and max_round_scope already
    bounds effort. So the score is expected VALUE delivered per round:

        value x P(round succeeds) x calibration     <- expected value
        x confidence                                <- predicted-origin work is
                                                       discounted by belief (刀 B)
        x goal-chain bonus (<=1.08)                 <- "based on a met goal" is a
                                                       tie-breaker, never more
                                                       than half a real unlock
        x dependency unlock bonus                   <- foundational work pays
        x budget-aware risk factor                  <- take swings early, play safe late
        x completed-type saturation                 <- stop grinding one area (floored
                                                       at 0.35: it dents the diversity
                                                       signal, never vetoes value)
        x prospective backlog-mix penalty           <- diversify BEFORE over-grinding
        / effort cost                               <- free within one round's batch
                                                       width, linear beyond it (the
                                                       formula's premise is that
                                                       per-round overhead dominates)

    Predicted-origin candidates draw their success rate from the predicted
    sub-account once it has PREDICTED_SAMPLE_FLOOR resolved samples; before that
    they pay a conservative prior (type success_rate x 0.75). Classic mode never
    sees any of these factors."""
    value = _resolved_value(candidate)
    origin = candidate.get("origin")
    if origin not in ("observed", "predicted", "expansion"):
        origin = "observed"
    entry = (type_stats or {}).get(_candidate_type(candidate), {})
    completed_n = entry.get("completed") or 0
    blocked_n = entry.get("blocked") or 0
    blocked_rate = entry.get("blocked_rate")
    if blocked_rate is None:
        blocked_rate = (blocked_n / (completed_n + blocked_n)) if (completed_n + blocked_n) else 0.0
    success_rate = 1.0 - blocked_rate
    calibration = entry.get("calibration") or 1.0

    predicted_account = predicted_account or {}
    if origin == "predicted":
        if predicted_account.get("done", 0) >= PREDICTED_SAMPLE_FLOOR:
            success_rate = predicted_account.get("success_rate", success_rate)
            if predicted_account.get("calibration") is not None:
                calibration = predicted_account["calibration"]
        else:
            success_rate = success_rate * 0.75

    if origin == "observed":
        confidence = 1.0
    else:
        confidence = candidate.get("confidence")
        if (
            not isinstance(confidence, (int, float))
            or isinstance(confidence, bool)
            # Chained range test: NaN and +-inf fail it and fall back to the
            # default instead of poisoning the discount (NaN bypasses min/max).
            or not (0.5 <= float(confidence) <= 1.0)
        ):
            confidence = 0.75
        confidence = float(confidence)
    confidence_factor = confidence

    based_on = candidate.get("based_on")
    goal_chain_factor = 1.08 if (based_on and based_on in (completed_goals or [])) else 1.0

    unlock_bonus = 1.0 + 0.15 * unlocks

    try:
        risk = int(candidate.get("risk") or 1)
    except (TypeError, ValueError, OverflowError):
        risk = 1
    risk = max(1, min(5, risk))
    risk_factor = max(0.3, 1.0 - risk_weight * (risk - 1))

    try:
        threshold = max(0, int(saturation_threshold or 0))
    except (TypeError, ValueError):
        threshold = 2
    # Logarithmic decay with a hard floor (0.7**k reaches 0.045 at k=9 and
    # 4.6e-05 at k=28): saturation dents the diversity signal but must never
    # veto a genuinely valuable candidate of the dominant type.
    over = max(0, completed_n - threshold)
    saturation_factor = max(0.35, 1.0 - 0.12 * math.log2(1 + over))

    mix_penalty = 1.0 - 0.3 * max(0.0, min(1.0, pending_mix))

    effort = _resolved_effort(candidate)
    try:
        width = int(batch_width)
    except (TypeError, ValueError, OverflowError):
        width = 3
    # Effort is free within one round's batch width (per-round overhead
    # dominates anyway) and linear beyond it — the score ranks value per round,
    # so a candidate that still fits this round's batch must not lose to a
    # lighter one that would leave the batch idle.
    effort_cost = 1.0 + 0.15 * max(0, effort - max(1, width))

    expected_value = value * success_rate * calibration
    score = (expected_value * unlock_bonus * risk_factor * saturation_factor * mix_penalty
             / effort_cost * confidence_factor * goal_chain_factor)
    return score, {
        "base_value": value,
        "origin": origin,
        "confidence": round(confidence, 3),
        "confidence_factor": round(confidence_factor, 3),
        "goal_chain_factor": round(goal_chain_factor, 3),
        "success_rate": round(success_rate, 3),
        "calibration": round(calibration, 3),
        "expected_value": round(expected_value, 3),
        "unlocks": unlocks,
        "unlock_bonus": round(unlock_bonus, 3),
        "risk": risk,
        "risk_weight": round(risk_weight, 3),
        "risk_factor": round(risk_factor, 3),
        "saturation_factor": round(saturation_factor, 3),
        "mix_penalty": round(mix_penalty, 3),
        "effort_cost": round(effort_cost, 3),
    }


def candidate_adjusted_score(candidate, type_stats=None, saturation_threshold=2,
                             cfg=None, unlocks=0, pending_mix=0.0, risk_weight=0.08,
                             completed_goals=None, predicted_account=None, batch_width=None):
    """Adjusted score for one candidate. Dispatches on cfg ranking_mode:
    'expected' (default) or 'classic'. Returns (score, breakdown). The
    prediction factors (completed_goals / predicted_account) only apply in
    expected mode — classic stays the legacy value/effort ratio. `batch_width`
    feeds the effort cost (falls back to _score_expected's default when None)."""
    if (cfg or {}).get("ranking_mode", "expected") == "classic":
        return _score_classic(candidate, type_stats, saturation_threshold)
    kwargs = {} if batch_width is None else {"batch_width": batch_width}
    return _score_expected(candidate, type_stats, saturation_threshold,
                           unlocks=unlocks, pending_mix=pending_mix, risk_weight=risk_weight,
                           completed_goals=completed_goals, predicted_account=predicted_account,
                           **kwargs)


def _unlocks_map(backlog):
    """candidate id -> number of pending candidates that depend on it."""
    counts = {}
    for candidate in backlog.get("candidates", []):
        if candidate.get("status") != "pending":
            continue
        for dep_id in candidate.get("depends_on") or []:
            counts[dep_id] = counts.get(dep_id, 0) + 1
    return counts


def _risk_weight_for(cfg, progress):
    """Risk aversion grows as the run's round budget is consumed: early rounds
    take swings at high-risk work, late rounds play it safe."""
    if progress is None:
        return 0.08
    return 0.05 + 0.10 * max(0.0, min(1.0, progress))


def _mark_selection(entries, cfg, progress=None):
    """Mark the recommended round batch (`selected: true`). Selection is a
    constrained pick over the ranked list, separate from scoring:
      - only pending + ready candidates are eligible
      - value below min_candidate_value is demoted (below_floor); such
        quick-wins only fill slots the main pool left open
      - at most max_same_type_per_round candidates of the same type per round
      - origin quotas are separate (刀 B anti-noise): predicted candidates are
        capped by max_predicted_per_round (default 1, unproven hypotheses),
        expansion candidates by max_expansion_per_round (None = uncapped,
        they already passed the main agent's value gate); observed wins ties
      - late run (progress > 0.7): predicted work is cut entirely while
        observed candidates are still ready; expansion is not cut
      - the 40% score cutoff uses the FIRST SELECTED entry as its base (the
        top-ranked entry may be quota-cut, which would raise the bar for
        everything else) and only prunes below-floor entries — above-floor
        candidates are always eligible until the batch is full
      - ready above-floor entries skipped by a constraint get `cut_reason`:
        'late_run' | 'quota' | 'type' | 'cutoff', or 'batch_full' when the
        batch is already full (so consumers can tell why nothing was picked)
    """
    n = cfg.get("candidates_per_round") or 3
    max_per_type = cfg.get("max_same_type_per_round") or 2
    max_predicted = cfg.get("max_predicted_per_round")
    if max_predicted is None:
        max_predicted = len(entries)
    max_expansion = cfg.get("max_expansion_per_round")
    if max_expansion is None:
        max_expansion = len(entries)
    late_run = progress is not None and progress > 0.7
    for entry in entries:
        entry["selected"] = False
        entry.pop("cut_reason", None)
    pool = [e for e in entries if e.get("status") == "pending" and e.get("ready") and not e.get("below_floor")]
    below = [e for e in entries if e.get("status") == "pending" and e.get("ready") and e.get("below_floor")]
    observed_ready = any((e.get("origin") or "observed") == "observed" for e in pool)
    predicted_quota = 0 if (late_run and observed_ready) else max_predicted
    selected = []
    type_counts = {}
    predicted_count = 0
    expansion_count = 0
    cutoff_base = None
    for entry in pool:
        origin = entry.get("origin") or "observed"
        if len(selected) >= n:
            entry["cut_reason"] = "batch_full"
            continue
        if origin == "predicted" and late_run and observed_ready:
            entry["cut_reason"] = "late_run"
            continue
        if origin == "predicted" and predicted_count >= predicted_quota:
            entry["cut_reason"] = "quota"
            continue
        if origin == "expansion" and expansion_count >= max_expansion:
            entry["cut_reason"] = "quota"
            continue
        candidate_type = entry.get("type") or "feature"
        if type_counts.get(candidate_type, 0) >= max_per_type:
            entry["cut_reason"] = "type"
            continue
        if cutoff_base is not None and entry["score"] < 0.4 * cutoff_base and entry.get("below_floor"):
            entry["cut_reason"] = "cutoff"
            break
        selected.append(entry)
        if cutoff_base is None:
            cutoff_base = entry["score"]
        type_counts[candidate_type] = type_counts.get(candidate_type, 0) + 1
        if origin == "predicted":
            predicted_count += 1
        elif origin == "expansion":
            expansion_count += 1
    if len(selected) < n and below:
        # The quick-win fallback fills the slots the main pool left open. It
        # obeys the same origin quotas / late-run cut (a below-floor predicted
        # candidate must not bypass either) and the 40% cutoff against the
        # batch's base — a quick-win far below it is noise, not work.
        for entry in below:
            if len(selected) >= n:
                break
            origin = entry.get("origin") or "observed"
            if origin == "predicted" and late_run and observed_ready:
                continue
            if origin == "predicted" and predicted_count >= predicted_quota:
                continue
            if origin == "expansion" and expansion_count >= max_expansion:
                continue
            if cutoff_base is not None and entry["score"] < 0.4 * cutoff_base and entry.get("below_floor"):
                break
            selected.append(entry)
            if origin == "predicted":
                predicted_count += 1
            elif origin == "expansion":
                expansion_count += 1
    for entry in selected:
        entry["selected"] = True


def progress_from_state(state, cfg):
    """Fraction of the round budget consumed (0-1), or None when max_rounds is unset."""
    max_rounds = (cfg or {}).get("max_rounds")
    if not max_rounds:
        return None
    used = (
        state.get("completed_rounds", 0)
        + state.get("blocked_rounds", 0)
        + state.get("cancelled_rounds", 0)
        + state.get("reverted_rounds", 0)
    )
    return used / max_rounds


def rank_candidates(backlog, cfg, progress=None, completed_goals=None):
    """Rank backlog candidates and mark the recommended round batch. Pending,
    dependency-ready candidates come first (by score desc), then pending-but-blocked
    candidates (with their blocked_by reasons), then picked/completed/blocked
    candidates. `progress` (0-1 fraction of max_rounds consumed, or None) shapes
    the risk weight and the late-run predicted quota. `completed_goals` enables
    the goal-chain bonus for candidates whose based_on matches a met goal."""
    cfg = cfg or {}
    type_stats = compute_type_stats(backlog)
    predicted_account = compute_predicted_account(backlog)
    threshold = cfg.get("type_saturation_threshold", 2)
    floor = cfg.get("min_candidate_value")
    risk_weight = _risk_weight_for(cfg, progress)

    pending = [c for c in backlog.get("candidates", []) if c.get("status") == "pending"]
    pending_total = len(pending)
    pending_by_type = {}
    for candidate in pending:
        pending_by_type[_candidate_type(candidate)] = pending_by_type.get(_candidate_type(candidate), 0) + 1
    unlocks = _unlocks_map(backlog)

    entries = []
    for candidate in backlog.get("candidates", []):
        entry = dict(candidate)
        candidate_type = _candidate_type(candidate)
        pending_mix = (pending_by_type.get(candidate_type, 0) / pending_total) if pending_total else 0.0
        score, breakdown = candidate_adjusted_score(
            candidate, type_stats, threshold, cfg,
            unlocks=unlocks.get(candidate.get("id"), 0),
            pending_mix=pending_mix,
            risk_weight=risk_weight,
            completed_goals=completed_goals,
            predicted_account=predicted_account,
            batch_width=cfg.get("candidates_per_round") or 1,
        )
        entry["score"] = round(score, 3)
        entry["score_breakdown"] = breakdown
        missing, ready = candidate_deps_status(backlog, candidate)
        entry["ready"] = ready
        entry["blocked_by"] = missing
        entry["unlocks"] = unlocks.get(candidate.get("id"), 0)
        entry["below_floor"] = bool(floor is not None and _resolved_value(candidate) < floor)
        entries.append(entry)
    entries.sort(
        key=lambda item: (
            item.get("status") != "pending",
            not item.get("ready"),
            -item.get("score", 0.0),
            (item.get("origin") or "observed") != "observed",
            item.get("id") or "",
        )
    )
    if cfg.get("ranking_mode", "expected") != "classic":
        _mark_selection(entries, cfg, progress=progress)
    return entries


def analysis_path_for(repo):
    return repo / io.AUTOPILOT_DIR / io.ANALYSIS_FILENAME


def directives_path_for(repo):
    return repo / io.AUTOPILOT_DIR / io.DIRECTIVES_FILENAME


def load_directives(repo):
    directives = io.load_json(directives_path_for(repo), {"directives": []})
    path = directives_path_for(repo)
    if not isinstance(directives, dict) or not isinstance(directives.get("directives"), list):
        print(
            "[ERROR] Invalid .autopilot/directives.json: must be an object with a "
            "'directives' array. Fix or delete it and run init again.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    for entry in directives["directives"]:
        if not isinstance(entry, dict):
            print(
                "[ERROR] Invalid .autopilot/directives.json: every directive must be an object. "
                "Fix or delete it and run init again.",
                file=sys.stderr,
            )
            raise SystemExit(2)
    return directives


def save_directives(repo, directives):
    io.save_json(directives_path_for(repo), directives)


def add_directive(repo, text):
    directives = load_directives(repo)
    entry = {"text": text, "added_at": io.now_iso()}
    directives.setdefault("directives", []).append(entry)
    save_directives(repo, directives)
    return len(directives["directives"])


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


def analysis_commits_behind(repo):
    """How many commits the cached analysis predates (None when there is no
    cache, no parseable git_head, or the count cannot be determined). Gives the
    staleness a magnitude so the agent can judge whether a rescan is due."""
    analysis = load_analysis(repo)
    if not isinstance(analysis, dict):
        return None
    cached_head = analysis.get("git_head")
    if not cached_head:
        return None
    result = io.run_git(repo, "rev-list", "--count", "{}..HEAD".format(cached_head))
    if result.returncode != 0:
        return None
    try:
        return int(result.stdout.strip())
    except (TypeError, ValueError, OverflowError):
        return None


def build_retrospective(repo, state, cfg, lang="zh"):
    """Run-level retrospective: per-type success stats, blocked rounds, and the
    verification setup, in the configured language."""
    zh = lang == "zh"
    backlog = load_backlog(repo)
    out = []
    # Feature-mode honesty line: the run's commits live on the autopilot
    # branch and finish does not merge (by design), so every report must say
    # where the work landed instead of leaving the user to discover it.
    feature_note = None
    if cfg.get("branch_mode") == "feature" and state.get("branch"):
        origin = state.get("origin_branch")
        if origin and origin != "HEAD":
            origin_label = "`{}`".format(origin)
        else:
            origin_label = "原" if zh else "the original"
        if zh:
            feature_note = "- 提交保留在分支 `{}`，未合并到 {} 分支".format(state["branch"], origin_label)
        else:
            feature_note = "- Commits remain on branch `{}`; not merged into {}.".format(
                state["branch"], origin_label
            )
    if zh:
        out.append("# 迭代复盘（Retrospective）")
        out.append("")
        out.append("- 仓库: `{}`".format(state.get("repo")))
        out.append("- run_id: `{}`".format(state.get("run_id")))
        out.append("- 完成轮次: {}".format(state.get("completed_rounds", 0)))
        out.append("- 受阻轮次: {}".format(state.get("blocked_rounds", 0)))
        out.append("- 回滚轮次: {}".format(state.get("reverted_rounds", 0)))
        out.append("- 估算 Token: {}".format(state.get("estimated_tokens_used", 0)))
        if feature_note:
            out.append(feature_note)
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
        if feature_note:
            out.append(feature_note)
        out.append("")
        out.append("## Stats by type")
        out.append("")
    stats = state.get("type_stats") or compute_type_stats(backlog)
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
                entry.get("round", "?"), table_cell(entry.get("title", "")), table_cell(entry.get("reason", ""))
            ))
    out.append("")

    checks = cfg.get("check_commands") or []
    out.append("## {}".format("验证命令" if zh else "Verification commands"))
    out.append("")
    if not checks:
        out.append("- {}".format("未配置" if zh else "none configured"))
    else:
        for command in checks:
            out.append("- `{}`".format(command))
    out.append("")

    ranked = rank_candidates(
        backlog, cfg, progress=progress_from_state(state, cfg),
        completed_goals=list(state.get("completed_goals") or []),
    )
    ready = [r for r in ranked if r.get("status") == "pending" and r.get("ready")]
    out.append("## {}".format("下一步建议" if zh else "Next likely improvement"))
    out.append("")
    if not ready:
        out.append("- {}".format("无" if zh else "none"))
    else:
        top = ready[0]
        out.append("- {} `{}` (value={}, effort={}, type={})".format(
            top.get("id"), table_cell(top.get("title")), top.get("value"), top.get("effort"), top.get("type") or "feature"
        ))
    out.append("")
    return "\n".join(out)


_STATUS_LABELS = {
    "zh": {
        "completed": "已完成", "blocked": "受阻", "cancelled": "已取消",
        "aborted": "空转取消",
        "revert": "已回滚", "picked": "进行中", "pending": "待处理",
        "run_report": "Auto Iterate 运行报告", "meta": "运行信息", "goals": "目标",
        "counts": "轮次统计", "history": "轮次历史", "backlog": "改进清单",
        "gitlog": "最近提交", "next": "下一步建议", "active": "活动分支",
        "tokens": "估算 Token", "repo": "仓库", "none": "无",
        "deadline": "定时截止", "seeds": "方向假设（种子）",
        "seed_status": {"open": "待处理", "promoted": "已立项", "verified": "已验证",
                        "refuted": "已证伪", "rejected": "已否决", "pending": "待处理",
                        "picked": "进行中", "completed": "已完成", "blocked": "受阻"},
    },
    "en": {
        "completed": "completed", "blocked": "blocked", "cancelled": "cancelled",
        "aborted": "aborted",
        "revert": "reverted", "picked": "in progress", "pending": "pending",
        "run_report": "Auto Iterate Run Report", "meta": "Run info", "goals": "Goals",
        "counts": "Round counts", "history": "Round history", "backlog": "Backlog",
        "gitlog": "Recent commits", "next": "Next likely improvement", "active": "Active branch",
        "tokens": "Estimated tokens", "repo": "Repo", "none": "none",
        "deadline": "Deadline timer", "seeds": "Direction seeds (predictions)",
        "seed_status": {"open": "open", "promoted": "promoted", "verified": "verified",
                        "refuted": "refuted", "rejected": "rejected", "pending": "pending",
                        "picked": "picked", "completed": "completed", "blocked": "blocked"},
    },
}


def build_report(repo, state, cfg, lang="en"):
    """Build a deterministic markdown report from state, config, and backlog."""
    zh = lang == "zh"
    backlog_data = load_backlog(repo)
    L = _STATUS_LABELS["zh" if zh else "en"]
    out = []
    out.append("# {}".format(L["run_report"]))
    out.append("")

    out.append("## {}".format(L["meta"]))
    out.append("")
    out.append("- {}: `{}`".format(L["repo"], state.get("repo")))
    out.append("- run_id: `{}`".format(state.get("run_id")))
    # branch/origin_branch are only set in feature mode; in current mode both
    # are None and the report used to show 无 despite git knowing the branch.
    active_branch = state.get("branch") or state.get("origin_branch") or io.current_branch(repo)
    out.append("- {}: `{}`".format(L["active"], active_branch))
    out.append("- started_at: `{}`".format(state.get("started_at")))
    out.append("- last_activity_at: `{}`".format(state.get("last_activity_at")))
    out.append("- {}: `{}`".format(L["deadline"], cfg.get("deadline") or L["none"]))
    out.append("- finished_at: `{}`".format(state.get("finished_at") or L["none"]))
    out.append("- stop_reason: `{}`".format(state.get("stop_reason") or L["none"]))
    out.append("")

    goals = cfg.get("goals") or state.get("goals") or []
    # Same normalization as all_goals_met: a goal recorded with (or without)
    # zero-width characters must not make the report contradict the stop logic.
    completed = {normalize_goal_text(goal) for goal in state.get("completed_goals") or []}
    out.append("## {}".format(L["goals"]))
    out.append("")
    if not goals:
        out.append("- {}".format(L["none"]))
    else:
        for goal in goals:
            mark = "x" if normalize_goal_text(goal) in completed else " "
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
                entry.get("round", ""), label, table_cell(entry.get("title", "")), sha, tokens
            ))
    out.append("")

    backlog = backlog_data.get("candidates") or []
    out.append("## {}".format(L["backlog"]))
    out.append("")
    if not backlog:
        out.append("- {}".format(L["none"]))
    else:
        out.append("| id | title | type | value/effort | status |")
        out.append("|---|---|---|---|---|")
        for c in backlog:
            label = _STATUS_LABELS["zh" if zh else "en"].get(c.get("status", "pending"), c.get("status", "pending"))
            title = c.get("title", "")
            if c.get("origin") == "predicted":
                title = "[P] " + title
            out.append("| `{}` | {} | {} | {}/{} | {} |".format(
                c.get("id", ""), table_cell(title), c.get("type") or "feature",
                c.get("value", ""), c.get("effort", ""), label,
            ))
    out.append("")

    # Direction seeds + predicted sub-account (刀 B): predictions carry their own
    # ledger so their hit rate is visible instead of melting into the type stats.
    seeds = [s for s in (state.get("goal_seeds") or []) if isinstance(s, dict)]
    predicted_account = compute_predicted_account(backlog_data)
    out.append("## {}".format(L["seeds"]))
    out.append("")
    if not seeds and not predicted_account.get("done"):
        out.append("- {}".format(L["none"]))
    else:
        if seeds:
            out.append("| id | status | type | title | source goal |")
            out.append("|---|---|---|---|---|")
            for seed in seeds:
                status_label = L["seed_status"].get(seed.get("status", "open"), seed.get("status", "open"))
                out.append("| `{}` | {} | {} | {} | {} |".format(
                    seed.get("id", ""), status_label, seed.get("type") or "feature",
                    table_cell(seed.get("title", "")), table_cell(seed.get("source_goal", "")),
                ))
        if predicted_account.get("done"):
            out.append("")
            if zh:
                out.append("- 预测子账：已完成 {} / 受阻 {}（受阻率 {}%）".format(
                    predicted_account["completed"], predicted_account["blocked"],
                    round(predicted_account["blocked_rate"] * 100),
                ))
            else:
                out.append("- Predicted sub-account: {} completed / {} blocked (blocked rate {}%)".format(
                    predicted_account["completed"], predicted_account["blocked"],
                    round(predicted_account["blocked_rate"] * 100),
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

    ranked = rank_candidates(
        backlog_data, cfg, progress=progress_from_state(state, cfg),
        completed_goals=list(state.get("completed_goals") or []),
    )
    ready = [r for r in ranked if r.get("status") == "pending" and r.get("ready")]
    if ready:
        top = ready[0]
        out.append("## {}".format(L["next"]))
        out.append("")
        out.append("- {} `{}` (value={}, effort={}, type={}, score={})".format(
            top.get("id"), table_cell(top.get("title")), top.get("value"), top.get("effort"),
            top.get("type") or "feature", top.get("score"),
        ))
        out.append("")

    return "\n".join(out)


def write_phase_report(repo, state, cfg):
    """Write a phase report (every io.PHASE_REPORT_INTERVAL completed rounds) in the
    configured language, keeping only the most recent io.PHASE_REPORT_KEEP files."""
    lang = cfg.get("report_lang", "zh")
    completed = state.get("completed_rounds", 0)
    markdown = build_report(repo, state, cfg, lang)
    path = io.autopilot_file_for(repo, "{}{}.md".format(io.PHASE_REPORT_PREFIX, completed))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(markdown, encoding="utf-8")
        reports = []
        for old in path.parent.glob(io.PHASE_REPORT_PREFIX + "*.md"):
            suffix = old.name[len(io.PHASE_REPORT_PREFIX):-3]
            if suffix.isdigit():
                reports.append((int(suffix), old))
        reports.sort()
        for _, old in reports[:-io.PHASE_REPORT_KEEP]:
            try:
                old.unlink()
            except OSError:
                pass
        print(
            "[PHASE] Completed {} rounds; phase report written to {} (lang={}).".format(completed, path, lang),
            file=sys.stderr,
        )
    except OSError as exc:
        print("[WARN] Could not write phase report: {}".format(exc), file=sys.stderr)
