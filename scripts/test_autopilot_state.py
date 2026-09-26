#!/usr/bin/env python3
"""Unit + integration tests for autopilot_state.py (uses throwaway git repos)."""

import json
import os
import py_compile
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from io import StringIO
from unittest import mock
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "autopilot_state.py"

# Direct import for pure-function adversarial tests (resolve_seed, scoring,
# selection): the package lives next to this file.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import autopilot  # noqa: E402
from autopilot import state as ap_state  # noqa: E402
from autopilot import io as ap_io  # noqa: E402
from autopilot import commands as commands_module  # noqa: E402
from autopilot import config as config_module  # noqa: E402
from autopilot.cli import build_parser  # noqa: E402
from autopilot.guard import path_allowed  # noqa: E402
from autopilot.secrets import SECRET_PATTERNS  # noqa: E402

import re  # noqa: E402


def _secret_hit(text):
    return any(re.search(pattern, text) for pattern in SECRET_PATTERNS)


class RunResult(object):
    """Minimal subprocess.CompletedProcess stand-in for in-process runs."""

    def __init__(self, returncode, stdout, stderr):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.args = []


class AutopilotTestBase(unittest.TestCase):
    script = SCRIPT

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="autopilot-test-")
        self.repo = Path(self.tmp) / "repo"
        self.repo.mkdir()
        self.env = dict(os.environ)
        # Isolate from the developer's git environment: repo-local config only,
        # no inherited GIT_* plumbing, no global gpgsign/hooks interference.
        for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_NAMESPACE",
                    "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR",
                    "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_AUTHOR_DATE",
                    "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "GIT_COMMITTER_DATE",
                    "GIT_CONFIG_COUNT"):
            self.env.pop(var, None)
        self.global_config = Path(self.tmp) / "global-gitconfig"
        self.global_config.write_text("", encoding="utf-8")
        self.env["GIT_CONFIG_GLOBAL"] = str(self.global_config)
        self.env["GIT_CONFIG_SYSTEM"] = str(self.global_config)
        self.env["PYTHONIOENCODING"] = "utf-8"
        self.env["LC_ALL"] = "C"
        self.env["GIT_CEILING_DIRECTORIES"] = str(Path(self.tmp).parent).replace("\\", "/")
        self.git("init", "-q")
        # Repo-local identity written straight into .git/config: 3 fewer git
        # subprocesses per test (~250 tests) without changing behavior.
        (self.repo / ".git" / "config").write_text(
            "[user]\n\tname = Test User\n\temail = test@example.com\n"
            "[commit]\n\tgpgsign = false\n",
            encoding="utf-8",
        )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.repo)] + list(args),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            encoding="utf-8",
            errors="replace",
            env=self.env,
        )

    def run_state(self, command, *args):
        """In-process invocation of the CLI. ~589 subprocess calls at ~0.33s
        interpreter startup each dominated the suite runtime; dispatching
        through build_parser keeps every call site unchanged. Real-subprocess
        behavior (exit codes through the entry point, UTF-8 stdio) stays
        covered by the explicit subprocess contract tests. self.env is applied
        to os.environ for the duration — in-process code would otherwise
        inherit the developer's git identity/global config."""
        parser = build_parser()
        old_env = {key: os.environ.get(key) for key in self.env}
        for key, value in self.env.items():
            os.environ[key] = value
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout = StringIO()
        sys.stderr = StringIO()
        try:
            try:
                ns = parser.parse_args([command, "--repo", str(self.repo)] + list(args))
                code = ns.func(ns)
            except SystemExit as exc:
                code = exc.code
        finally:
            out, err = sys.stdout.getvalue(), sys.stderr.getvalue()
            sys.stdout, sys.stderr = old_out, old_err
            for key, value in old_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        if not isinstance(code, int):
            code = 0
        return RunResult(code, out, err)

    def read_json(self, name):
        return json.loads((self.repo / ".autopilot" / name).read_text(encoding="utf-8"))


class RepoTest(AutopilotTestBase):
    """A committed git repo with a stable branch name."""

    def setUp(self):
        super().setUp()
        (self.repo / "README.md").write_text("# Test\n", encoding="utf-8")
        self.git("add", "README.md")
        self.git("commit", "-q", "-m", "initial")
        # Read HEAD directly (ref: refs/heads/<name>) — no subprocess, and
        # independent of the git version's default-branch name.
        head = (self.repo / ".git" / "HEAD").read_text(encoding="utf-8").strip()
        self.initial_branch = head.split("/")[-1] if "/" in head else head

    def add_file(self, name="feature.py", content="x = 1\n"):
        (self.repo / name).write_text(content, encoding="utf-8")
        self.git("add", name)


class ScriptSmokeTests(unittest.TestCase):
    def test_script_compiles(self):
        py_compile.compile(str(SCRIPT), doraise=True)


class InitTests(RepoTest):
    def test_init_creates_state_and_config(self):
        result = self.run_state("init")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.repo / ".autopilot" / "state.json").exists())
        self.assertTrue((self.repo / ".autopilot" / "config.json").exists())
        config = self.read_json("config.json")
        self.assertEqual(config["max_rounds"], 10)
        self.assertFalse(config["track_state"])
        self.assertEqual(config["check_commands"], [])
        self.assertEqual(config["candidates_per_round"], 4)
        self.assertEqual(config["commit_every_rounds"], 5)
        self.assertEqual(config["verify_every_rounds"], 3)
        self.assertTrue(config["scan_secrets"])
        self.assertEqual(config["ranking_mode"], "expected")
        self.assertEqual(config["min_candidate_value"], 3)
        self.assertEqual(config["max_same_type_per_round"], 2)

    def test_init_adds_exclude(self):
        self.run_state("init")
        exclude = (self.repo / ".git" / "info" / "exclude").read_text(encoding="utf-8")
        self.assertIn(".autopilot/", exclude)

    def test_track_state_skips_exclude(self):
        self.run_state("init", "--track-state")
        exclude = (self.repo / ".git" / "info" / "exclude").read_text(encoding="utf-8")
        self.assertNotIn(".autopilot/", exclude)

    def test_goals_and_limits(self):
        self.run_state("init", "--goal", "Make tests pass", "--max-rounds", "2", "--max-minutes", "10")
        config = self.read_json("config.json")
        self.assertEqual(config["goals"], ["Make tests pass"])
        self.assertEqual(config["max_rounds"], 2)
        self.assertEqual(config["max_minutes"], 10)

    def test_check_commands_stored(self):
        self.run_state("init", "--check-commands", "pytest", "--check-commands", "python -m py_compile .")
        config = self.read_json("config.json")
        self.assertEqual(config["check_commands"], ["pytest", "python -m py_compile ."])

    def test_init_resume_warns(self):
        self.run_state("init")
        # Non-zero: an exit 0 used to silently drop the new init flags.
        result = self.run_state("init")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already exists", result.stderr)

    def test_init_requires_git_repo(self):
        plain = Path(self.tmp) / "notgit"
        plain.mkdir()
        result = subprocess.run(
            [sys.executable, str(self.script), "init", "--repo", str(plain)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            encoding="utf-8",
            errors="replace",
            env=self.env,
        )
        self.assertNotEqual(result.returncode, 0)

    def test_no_leftover_tmp_files(self):
        self.run_state("init")
        self.run_state("backlog-add", "--title", "T", "--reason", "r")
        leftovers = list((self.repo / ".autopilot").glob("*.tmp*"))
        self.assertEqual(leftovers, [])


class UnbornBranchTests(AutopilotTestBase):
    def test_init_on_unborn_branch(self):
        result = self.run_state("init")
        self.assertEqual(result.returncode, 0, result.stderr)
        state = self.read_json("state.json")
        self.assertEqual(state["schema"], 6)

    def test_diagnose_reports_unborn(self):
        result = self.run_state("diagnose")
        data = json.loads(result.stdout)
        self.assertFalse(data["has_commits"])


class DiagnoseTests(RepoTest):
    def test_diagnose_reports(self):
        result = self.run_state("diagnose")
        data = json.loads(result.stdout)
        self.assertTrue(data["is_git_repo"])
        self.assertTrue(data["has_commits"])
        self.assertTrue(data["identity_ok"])
        self.assertFalse(data["dirty"])

    def test_diagnose_detects_missing_identity(self):
        self.git("config", "user.name", "")
        self.git("config", "user.email", "")
        result = self.run_state("diagnose")
        data = json.loads(result.stdout)
        self.assertFalse(data["identity_ok"])


class RoundFlowTests(RepoTest):
    def test_full_round_flow(self):
        self.run_state("init")
        self.run_state("backlog-add", "--title", "Add feature", "--reason", "useful", "--value", "4", "--effort", "2")
        cid = self.read_json("backlog.json")["candidates"][0]["id"]

        result = self.run_state("begin-round", "--title", "Add feature", "--reason", "useful", "--candidate-id", cid)
        self.assertEqual(result.returncode, 0, result.stderr)

        self.add_file()
        result = self.run_state("commit", "--summary", "add feature")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("[OK]", result.stdout)

        sha = self.git("rev-parse", "HEAD").stdout.strip()
        result = self.run_state("complete-round", "--summary", "added feature", "--commit-sha", sha)
        self.assertEqual(result.returncode, 0, result.stderr)

        self.assertEqual(self.read_json("backlog.json")["candidates"][0]["status"], "completed")

        result = self.run_state("check")
        data = json.loads(result.stdout)
        self.assertTrue(data["continue"])

    def test_multi_candidate_round(self):
        self.run_state("init", "--candidates-per-round", "3")
        config = self.read_json("config.json")
        self.assertEqual(config["candidates_per_round"], 3)
        self.run_state("backlog-add", "--title", "A", "--reason", "r", "--value", "5", "--effort", "1")
        self.run_state("backlog-add", "--title", "B", "--reason", "r", "--value", "4", "--effort", "1")
        ids = [c["id"] for c in self.read_json("backlog.json")["candidates"]]

        result = self.run_state("begin-round", "--title", "A+B", "--reason", "batch",
                                "--candidate-id", ids[0], "--candidate-id", ids[1])
        self.assertEqual(result.returncode, 0, result.stderr)
        state = self.read_json("state.json")
        self.assertEqual(state["current_round"]["candidate_ids"], ids)
        self.assertEqual(state["current_round"]["candidate_id"], ids[0])

        self.add_file("a.py", "a = 1\n")
        result = self.run_state("commit", "--summary", "change a")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.add_file("b.py", "b = 2\n")
        result = self.run_state("commit", "--summary", "change b")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        log = self.git("log", "--pretty=%s", "-2").stdout.strip().splitlines()
        self.assertTrue(log[0].startswith("autopilot(round-1): change b"), log)
        self.assertTrue(log[1].startswith("autopilot(round-1): change a"), log)

        sha = self.git("rev-parse", "HEAD").stdout.strip()
        result = self.run_state("complete-round", "--summary", "batched", "--commit-sha", sha)
        self.assertEqual(result.returncode, 0, result.stderr)

        statuses = [c["status"] for c in self.read_json("backlog.json")["candidates"]]
        self.assertEqual(statuses, ["completed", "completed"])
        self.assertEqual(self.read_json("state.json")["completed_rounds"], 1)

    def test_multi_candidate_missing_id_refused(self):
        self.run_state("init")
        self.run_state("backlog-add", "--title", "A", "--reason", "r")
        cid = self.read_json("backlog.json")["candidates"][0]["id"]
        result = self.run_state("begin-round", "--title", "t", "--reason", "x",
                                "--candidate-id", cid, "--candidate-id", "candidate-999")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not found", result.stderr.lower())
        self.assertIsNone(self.read_json("state.json")["current_round"])

    def test_multi_candidate_cancel_restores_all(self):
        self.run_state("init")
        self.run_state("backlog-add", "--title", "A", "--reason", "r")
        self.run_state("backlog-add", "--title", "B", "--reason", "r")
        ids = [c["id"] for c in self.read_json("backlog.json")["candidates"]]
        self.run_state("begin-round", "--title", "A+B", "--reason", "batch",
                       "--candidate-id", ids[0], "--candidate-id", ids[1])
        result = self.run_state("cancel-round", "--reason", "changed mind")
        self.assertEqual(result.returncode, 0, result.stderr)
        statuses = [c["status"] for c in self.read_json("backlog.json")["candidates"]]
        self.assertEqual(statuses, ["pending", "pending"])

    def test_candidates_per_round_validation(self):
        self.run_state("init")
        config_path = self.repo / ".autopilot" / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["candidates_per_round"] = 0
        config_path.write_text(json.dumps(config), encoding="utf-8")
        result = self.run_state("check")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("candidates_per_round", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_commit_message_prefix(self):
        self.run_state("init")
        config_path = self.repo / ".autopilot" / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["commit_message_prefix"] = "chore"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        self.run_state("begin-round", "--title", "prefix", "--reason", "x")
        self.add_file()
        self.run_state("commit", "--summary", "add feature")
        log = self.git("log", "-1", "--pretty=%s").stdout.strip()
        self.assertTrue(log.startswith("chore(round-1):"), log)

    def test_max_round_scope_enforced(self):
        self.run_state("init")
        config_path = self.repo / ".autopilot" / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["max_round_scope"] = 2
        config_path.write_text(json.dumps(config), encoding="utf-8")
        self.add_file("big.py", "a\nb\nc\nd\n")
        result = self.run_state("commit", "--summary", "too big")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("max_round_scope", result.stderr)

    def test_commit_requires_identity(self):
        self.run_state("init")
        self.git("config", "user.name", "")
        self.git("config", "user.email", "")
        self.add_file()
        result = self.run_state("commit", "--summary", "add feature")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("identity", result.stderr.lower())

    def test_commit_requires_staged_files(self):
        self.run_state("init")
        (self.repo / "unstaged.py").write_text("y = 2\n", encoding="utf-8")
        result = self.run_state("commit", "--summary", "nothing staged")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Nothing is staged", result.stderr)

    def test_max_rounds_stop(self):
        self.run_state("init", "--max-rounds", "1")
        self.run_state("begin-round", "--title", "r1", "--reason", "x")
        self.add_file()
        self.run_state("commit", "--summary", "add feature")
        sha = self.git("rev-parse", "HEAD").stdout.strip()
        self.run_state("complete-round", "--summary", "done", "--commit-sha", sha)
        result = self.run_state("check")
        data = json.loads(result.stdout)
        self.assertFalse(data["continue"])
        self.assertIn("max_rounds", data["stop_reason"])

    def test_goal_stop(self):
        self.run_state("init", "--goal", "Ship feature")
        self.run_state("begin-round", "--title", "r1", "--reason", "x")
        self.add_file()
        self.run_state("commit", "--summary", "add feature")
        sha = self.git("rev-parse", "HEAD").stdout.strip()
        self.run_state("complete-round", "--summary", "done", "--commit-sha", sha)
        self.run_state("goal-met", "--goal", "Ship feature", "--round", "1")
        result = self.run_state("check")
        data = json.loads(result.stdout)
        self.assertFalse(data["continue"])
        self.assertEqual(data["stop_reason"], "all goals met")

    def test_max_blocked_in_a_row(self):
        self.run_state("init")
        for _ in range(2):
            self.run_state("begin-round", "--title", "b", "--reason", "x")
            result = self.run_state("block-round", "--reason", "could not do it")
            self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_state("check")
        data = json.loads(result.stdout)
        self.assertFalse(data["continue"])
        self.assertIn("max_blocked_in_a_row", data["stop_reason"])

    def test_cancel_round_restores_candidate(self):
        self.run_state("init")
        self.run_state("backlog-add", "--title", "T", "--reason", "r")
        cid = self.read_json("backlog.json")["candidates"][0]["id"]
        self.run_state("begin-round", "--title", "T", "--reason", "r", "--candidate-id", cid)
        result = self.run_state("cancel-round", "--reason", "changed mind")
        self.assertEqual(result.returncode, 0, result.stderr)
        state = self.read_json("state.json")
        self.assertIsNone(state["current_round"])
        self.assertEqual(state["blocked_rounds"], 0)
        self.assertEqual(self.read_json("backlog.json")["candidates"][0]["status"], "pending")

    def test_tokens_auto_estimated(self):
        self.run_state("init")
        self.run_state("begin-round", "--title", "r", "--reason", "x")
        self.add_file()
        self.run_state("commit", "--summary", "add feature")
        sha = self.git("rev-parse", "HEAD").stdout.strip()
        self.run_state("complete-round", "--summary", "done", "--commit-sha", sha)
        state = self.read_json("state.json")
        self.assertGreater(state["estimated_tokens_used"], 0)

    def test_schema_migration(self):
        self.run_state("init")
        state_path = self.repo / ".autopilot" / "state.json"
        old = json.loads(state_path.read_text(encoding="utf-8"))
        old["schema"] = 1
        state_path.write_text(json.dumps(old), encoding="utf-8")
        result = self.run_state("read")
        data = json.loads(result.stdout)
        self.assertEqual(data["schema"], 6)
        self.assertIn("origin_branch", data)


class BacklogRankTests(RepoTest):
    def test_backlog_rank(self):
        self.run_state("init")
        self.run_state("backlog-add", "--title", "A", "--reason", "r", "--value", "5", "--effort", "1")
        self.run_state("backlog-add", "--title", "B", "--reason", "r", "--value", "1", "--effort", "5")
        result = self.run_state("backlog-rank")
        ranked = json.loads(result.stdout)
        self.assertEqual(ranked[0]["title"], "A")
        self.assertGreater(ranked[0]["score"], ranked[1]["score"])

    def test_legacy_impact_maps_to_value(self):
        self.run_state("init")
        self.run_state("backlog-add", "--title", "L", "--reason", "r", "--impact", "high", "--effort-level", "small")
        result = self.run_state("backlog-rank")
        ranked = json.loads(result.stdout)
        self.assertEqual(ranked[0]["value"], 5)
        self.assertEqual(ranked[0]["effort"], 1)

    def test_value_range_validation(self):
        self.run_state("init")
        result = self.run_state("backlog-add", "--title", "X", "--reason", "r", "--value", "9", "--effort", "1")
        self.assertNotEqual(result.returncode, 0)


class LockTests(RepoTest):
    def test_lock_blocks_concurrent_run(self):
        self.run_state("init")
        lock_path = self.repo / ".autopilot" / "lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(
            json.dumps({"pid": os.getpid(), "started_at": "2026-01-01T00:00:00+00:00"}),
            encoding="utf-8",
        )
        result = self.run_state("begin-round", "--title", "r", "--reason", "x")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("another autopilot run", result.stderr.lower())

    def test_stale_lock_is_removed(self):
        self.run_state("init")
        lock_path = self.repo / ".autopilot" / "lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(
            json.dumps({"pid": 99999999, "started_at": "2026-01-01T00:00:00+00:00"}),
            encoding="utf-8",
        )
        result = self.run_state("begin-round", "--title", "r", "--reason", "x")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(lock_path.exists())


class BranchTests(RepoTest):
    def test_finish_returns_to_origin_branch(self):
        result = self.run_state("init", "--branch-mode", "feature")
        self.assertEqual(result.returncode, 0, result.stderr)
        state = self.read_json("state.json")
        self.assertEqual(state["origin_branch"], self.initial_branch)
        current = self.git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        self.assertNotEqual(current, self.initial_branch)
        self.run_state("finish", "--force")
        current = self.git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        self.assertEqual(current, self.initial_branch)

    def test_finish_stay_keeps_branch(self):
        self.run_state("init", "--branch-mode", "feature")
        self.run_state("finish", "--force", "--stay")
        current = self.git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        self.assertNotEqual(current, self.initial_branch)


class BeginRoundStopTests(RepoTest):
    def _complete_one_round(self, summary="done"):
        self.run_state("begin-round", "--title", "r", "--reason", "x")
        self.add_file()
        self.run_state("commit", "--summary", "add feature")
        sha = self.git("rev-parse", "HEAD").stdout.strip()
        self.run_state("complete-round", "--summary", summary, "--commit-sha", sha)

    def test_refuses_after_max_rounds(self):
        self.run_state("init", "--max-rounds", "1")
        self._complete_one_round()
        result = self.run_state("begin-round", "--title", "r2", "--reason", "y")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("stopped", result.stderr.lower())

    def test_refuses_after_goals_met(self):
        self.run_state("init", "--goal", "Ship")
        self._complete_one_round()
        self.run_state("goal-met", "--goal", "Ship", "--round", "1")
        result = self.run_state("begin-round", "--title", "r2", "--reason", "y")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("stopped", result.stderr.lower())

    def test_refuses_after_max_blocked_in_a_row(self):
        self.run_state("init")
        for _ in range(2):
            self.run_state("begin-round", "--title", "b", "--reason", "x")
            self.run_state("block-round", "--reason", "nope")
        result = self.run_state("begin-round", "--title", "b3", "--reason", "x")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("stopped", result.stderr.lower())

    def test_refuses_when_already_finished(self):
        self.run_state("init")
        self.run_state("finish", "--force", "--reason", "done")
        result = self.run_state("begin-round", "--title", "r", "--reason", "x")
        self.assertNotEqual(result.returncode, 0)


class FinishGateTests(RepoTest):
    """P0 anti-early-stop gate: finish is refused while no stop condition is
    reached and work remains; --force overrides; a real stop still finishes."""

    def _add_candidate(self):
        result = self.run_state("backlog-add", "--title", "A", "--reason", "r",
                                "--value", "3", "--effort", "2")
        self.assertEqual(result.returncode, 0, result.stderr)

    def _log_events(self):
        log_path = self.repo / ".autopilot" / "log.jsonl"
        return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()
                if line.strip()]

    def test_finish_refuses_while_ready_work_and_no_stop_condition(self):
        self.run_state("init")
        self._add_candidate()
        result = self.run_state("finish", "--reason", "x")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Refusing to finish", result.stderr)
        self.assertIn("Refusing to finish", result.stderr)
        self.assertIn("ready_floor=1", result.stderr)
        self.assertIn("pass --force", result.stderr)
        state = self.read_json("state.json")
        self.assertIsNone(state["finished_at"])
        data = json.loads(self.run_state("check", "--brief").stdout)
        self.assertTrue(data["continue"])

    def test_finish_force_overrides_gate(self):
        self.run_state("init")
        self._add_candidate()
        result = self.run_state("finish", "--force", "--reason", "user asked to stop")
        self.assertEqual(result.returncode, 0, result.stderr)
        state = self.read_json("state.json")
        self.assertIsNotNone(state["finished_at"])
        forced = [e for e in self._log_events() if e.get("event") == "finish-forced"]
        self.assertEqual(len(forced), 1)
        self.assertEqual(forced[-1].get("reason"), "user asked to stop")
        self.assertEqual(forced[-1].get("ready"), 1)

    def test_finish_allowed_after_stop_condition(self):
        self.run_state("init", "--max-rounds", "1")
        self.run_state("begin-round", "--title", "r", "--reason", "x")
        self.add_file()
        self.run_state("commit", "--summary", "add feature")
        sha = self.git("rev-parse", "HEAD").stdout.strip()
        self.run_state("complete-round", "--summary", "done", "--commit-sha", sha)
        result = self.run_state("finish", "--reason", "max rounds reached")
        self.assertEqual(result.returncode, 0, result.stderr)
        state = self.read_json("state.json")
        self.assertIsNotNone(state["finished_at"])
        self.assertEqual([e for e in self._log_events() if e.get("event") == "finish-forced"], [])


class GoalEvidenceTests(RepoTest):
    """goal-met without --round is an unevidenced claim: recorded, but unverified
    goals withhold the 'all goals met' stop until evidence lands."""

    def _complete_one_round(self):
        self.run_state("begin-round", "--title", "r", "--reason", "x")
        self.add_file()
        self.run_state("commit", "--summary", "add feature")
        sha = self.git("rev-parse", "HEAD").stdout.strip()
        self.run_state("complete-round", "--summary", "done", "--commit-sha", sha)

    def test_goal_met_without_round_marks_unverified(self):
        self.run_state("init", "--goal", "G")
        result = self.run_state("goal-met", "--goal", "G")
        self.assertEqual(result.returncode, 0, result.stderr)
        state = self.read_json("state.json")
        self.assertTrue(state["goal_events"][-1]["unverified"])
        data = json.loads(self.run_state("check", "--brief").stdout)
        self.assertIn("G", data["goals_unverified"])
        self.assertTrue(data["goals_met"])
        self.assertIsNone(data["stop_reason"])
        self.assertTrue(data["continue"])
        # With valuable ready work left, the warning names the unverified goal.
        self.run_state("backlog-add", "--title", "A", "--reason", "r", "--value", "3", "--effort", "2")
        data = json.loads(self.run_state("check", "--brief").stdout)
        self.assertTrue(any("G" in w and "不得 finish" in w for w in data["warnings"]))

    def test_goal_met_with_round_verifies(self):
        self.run_state("init", "--goal", "G")
        self._complete_one_round()
        result = self.run_state("goal-met", "--goal", "G", "--round", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        state = self.read_json("state.json")
        self.assertFalse(state["goal_events"][-1]["unverified"])
        self.assertEqual(state["goal_events"][-1]["round"], 1)
        data = json.loads(self.run_state("check", "--brief").stdout)
        self.assertEqual(data["goals_unverified"], [])
        self.assertIn("all goals met", data["stop_reason"])
        self.assertFalse(data["continue"])

    def test_goal_met_round_without_completed_history_refused(self):
        self.run_state("init", "--goal", "G")
        result = self.run_state("goal-met", "--goal", "G", "--round", "2")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--round 2", result.stderr)
        self.assertIn("completed round", result.stderr)
        state = self.read_json("state.json")
        self.assertEqual(state["completed_goals"], [])


class RankingBatchTests(unittest.TestCase):
    """Direct tests of the rewritten batch selection and scoring factors
    (expansion quota split, late-run scope, cutoff base, calibration cold start)."""

    @staticmethod
    def _entry(entry_id, origin=None, below=False, score=5.0, ctype="refactor"):
        entry = {"id": entry_id, "title": entry_id, "type": ctype, "status": "pending",
                 "ready": True, "score": score, "below_floor": below}
        if origin:
            entry["origin"] = origin
        return entry

    @staticmethod
    def _cfg(**overrides):
        cfg = {"candidates_per_round": 3, "max_same_type_per_round": 2,
               "max_predicted_per_round": 1, "max_expansion_per_round": None}
        cfg.update(overrides)
        return cfg

    def test_expansion_origin_not_capped_by_predicted_quota(self):
        entries = [
            self._entry("p", origin="predicted", score=5.0, ctype="refactor"),
            self._entry("x", origin="expansion", score=4.0, ctype="bugfix"),
            self._entry("o", score=3.0, ctype="perf"),
        ]
        ap_state._mark_selection(entries, self._cfg(max_predicted_per_round=1))
        self.assertEqual([(e["id"], e["selected"]) for e in entries],
                         [("p", True), ("x", True), ("o", True)])

    def test_expansion_quota_zero_still_blocks_expansion(self):
        entries = [
            self._entry("x", origin="expansion", score=4.0, ctype="bugfix"),
            self._entry("o", score=3.0, ctype="perf"),
        ]
        ap_state._mark_selection(entries, self._cfg(max_predicted_per_round=1,
                                                    max_expansion_per_round=0))
        by_id = {e["id"]: e for e in entries}
        self.assertFalse(by_id["x"].get("selected"))
        self.assertEqual(by_id["x"].get("cut_reason"), "quota")
        self.assertTrue(by_id["o"].get("selected"))

    def test_late_run_cuts_predicted_not_expansion(self):
        entries = [
            self._entry("p1", origin="predicted", score=5.0, ctype="refactor"),
            self._entry("p2", origin="predicted", score=4.0, ctype="bugfix"),
            self._entry("o", score=3.0, ctype="perf"),
            self._entry("x", origin="expansion", score=2.0, ctype="docs"),
        ]
        ap_state._mark_selection(entries, self._cfg(), progress=0.8)
        by_id = {e["id"]: e for e in entries}
        self.assertEqual(by_id["p1"].get("cut_reason"), "late_run")
        self.assertEqual(by_id["p2"].get("cut_reason"), "late_run")
        self.assertFalse(by_id["p1"].get("selected"))
        self.assertFalse(by_id["p2"].get("selected"))
        self.assertTrue(by_id["o"].get("selected"))
        self.assertTrue(by_id["x"].get("selected"))

    def test_cutoff_only_prunes_below_floor(self):
        # ES-4 scenario, hardened: the top entry sets the cutoff base; an
        # above-floor entry below the 40% line stays eligible (only the batch
        # width can stop it), while a below-floor entry under the line is
        # pruned from the quick-win fallback.
        entries = [
            self._entry("top", score=4.25, ctype="refactor"),
            self._entry("above", score=1.5, ctype="bugfix"),
            self._entry("floorish", below=True, score=1.609, ctype="perf"),
        ]
        ap_state._mark_selection(entries, self._cfg())
        by_id = {e["id"]: e for e in entries}
        self.assertTrue(by_id["top"].get("selected"))
        self.assertTrue(by_id["above"].get("selected"))
        self.assertFalse(by_id["floorish"].get("selected"))

        # All-below-floor pool with the predicted quota at zero: the fallback
        # loop must still fill the batch from observed quick-wins.
        entries = [
            self._entry("bp", origin="predicted", below=True, score=2.0, ctype="refactor"),
            self._entry("bo1", below=True, score=1.5, ctype="bugfix"),
            self._entry("bo2", below=True, score=1.2, ctype="perf"),
        ]
        ap_state._mark_selection(entries, self._cfg(max_predicted_per_round=0))
        selected = [e["id"] for e in entries if e.get("selected")]
        self.assertEqual(selected, ["bo1", "bo2"])

    def test_saturation_factor_floor(self):
        score, breakdown = ap_state._score_expected(
            {"id": "c", "title": "t", "value": 4, "effort": 2, "type": "feature"},
            type_stats={"feature": {"completed": 30, "blocked": 0, "blocked_rate": 0.0,
                                    "calibration": 1.0}},
            saturation_threshold=2,
        )
        # 1 - 0.12*log2(1+28) = 0.417 (the old 0.7**28 was 4.6e-05).
        self.assertEqual(breakdown["saturation_factor"], 0.417)

    def test_effort_cost_batch_width(self):
        for effort, expected in ((1, 1.0), (2, 1.0), (3, 1.0), (4, 1.0), (5, 1.15)):
            _, breakdown = ap_state._score_expected(
                {"id": "c", "title": "t", "value": 5, "effort": effort},
                type_stats={}, batch_width=4,
            )
            self.assertEqual(breakdown["effort_cost"], expected, effort)
        # rank_candidates feeds cfg's candidates_per_round as the batch width.
        backlog = {"candidates": [{"id": "c1", "title": "t", "status": "pending",
                                   "value": 5, "effort": 5}]}
        ranked = ap_state.rank_candidates(backlog, {"candidates_per_round": 4})
        self.assertEqual(ranked[0]["score_breakdown"]["effort_cost"], 1.15)
        ranked = ap_state.rank_candidates(backlog, {"candidates_per_round": 3})
        self.assertEqual(ranked[0]["score_breakdown"]["effort_cost"], 1.3)

    def test_score_breakdown_table(self):
        # mix_penalty: clamped to [0, 1] on the pending_mix input.
        for mix, expected in ((0.0, 1.0), (1.0, 0.7), (1.7, 0.7)):
            _, breakdown = ap_state._score_expected(
                {"id": "c", "title": "t", "value": 3, "effort": 1},
                type_stats={}, pending_mix=mix,
            )
            self.assertEqual(breakdown["mix_penalty"], expected, mix)
        # Predicted sub-account: below PREDICTED_SAMPLE_FLOOR the type rate is
        # discounted (x0.75); at/above it the sub-account value is used as-is.
        type_stats = {"perf": {"completed": 3, "blocked": 1, "blocked_rate": 0.25,
                               "calibration": 1.0}}
        _, warm = ap_state._score_expected(
            {"id": "c", "title": "t", "value": 3, "effort": 1, "origin": "predicted",
             "type": "perf"},
            type_stats=type_stats,
            predicted_account={"done": 2, "success_rate": 0.9},
        )
        self.assertEqual(warm["success_rate"], round(0.75 * 0.75, 3))
        _, mature = ap_state._score_expected(
            {"id": "c", "title": "t", "value": 3, "effort": 1, "origin": "predicted",
             "type": "perf"},
            type_stats=type_stats,
            predicted_account={"done": 3, "success_rate": 0.9},
        )
        self.assertEqual(mature["success_rate"], 0.9)
        # Risk weights: flat when progress is unknown, then 0.05 + 0.10*progress.
        for progress, expected in ((None, 0.08), (0.0, 0.05), (0.5, 0.10), (1.0, 0.15)):
            self.assertAlmostEqual(ap_state._risk_weight_for({}, progress), expected, places=3)
        # A late run punishes risk=5 harder than an early one.
        _, early = ap_state._score_expected(
            {"id": "c", "title": "t", "value": 3, "effort": 1, "risk": 5},
            type_stats={}, risk_weight=0.05,
        )
        _, late = ap_state._score_expected(
            {"id": "c", "title": "t", "value": 3, "effort": 1, "risk": 5},
            type_stats={}, risk_weight=0.15,
        )
        self.assertEqual(early["risk_factor"], 0.8)
        self.assertEqual(late["risk_factor"], 0.4)
        # The goal-chain bonus requires the based_on goal to actually be met.
        _, unmet = ap_state._score_expected(
            {"id": "c", "title": "t", "value": 3, "effort": 1, "based_on": "unmet goal"},
            type_stats={}, completed_goals=["met goal"],
        )
        self.assertEqual(unmet["goal_chain_factor"], 1.0)

    def test_calibration_falls_back_to_global(self):
        backlog = {"candidates": [
            {"type": "docs", "status": "completed", "value": 4, "review_score": 5},
            {"type": "docs", "status": "completed", "value": 4, "review_score": 5},
            {"type": "perf", "status": "completed", "value": 5, "review_score": 2},
            {"type": "perf", "status": "completed", "value": 5, "review_score": 2},
            {"type": "perf", "status": "completed", "value": 5, "review_score": 2},
        ]}
        stats = ap_state.compute_type_stats(backlog)
        # docs has only 2 reviews of its own, but the run-wide ratio exists:
        # global review avg 3.2 / global value avg 4.6 = 0.6957 -> 0.7.
        self.assertEqual(stats["docs"]["review_n"], 2)
        self.assertEqual(stats["docs"]["calibration"], 0.7)
        # perf has 3 reviews of its own and keeps its own (clamped) ratio.
        self.assertEqual(stats["perf"]["calibration"], 0.6)

    def test_stop_reason_precedence(self):
        base_state = {
            "finished_at": None, "stop_reason": None, "goals": [], "completed_goals": [],
            "goal_events": [], "completed_rounds": 0, "blocked_rounds": 0,
            "cancelled_rounds": 0, "reverted_rounds": 0, "history": [],
            "estimated_tokens_used": 0, "last_activity_at": None, "started_at": None,
        }
        base_cfg = {"goals": [], "max_rounds": None, "max_minutes": None, "max_tokens": None,
                    "max_blocked_in_a_row": None, "deadline": None, "expand_after_goals": False}
        # finished_at wins over everything.
        st = dict(base_state, finished_at="T", completed_goals=["A"], completed_rounds=5)
        cfg = dict(base_cfg, goals=["A"], max_rounds=1)
        self.assertEqual(ap_state.compute_stop_reason(st, cfg), "already finished")
        # Verified goals beat max_rounds and a blocked streak.
        st = dict(base_state, completed_goals=["A"], completed_rounds=5,
                  blocked_rounds=2, history=[{"round": 1, "status": "blocked"}])
        cfg = dict(base_cfg, goals=["A"], max_rounds=1, max_blocked_in_a_row=1)
        self.assertEqual(ap_state.compute_stop_reason(st, cfg), "all goals met")
        # max_rounds beats a blocked streak.
        st = dict(base_state, completed_rounds=5, blocked_rounds=2,
                  history=[{"round": 1, "status": "blocked"}])
        cfg = dict(base_cfg, max_rounds=1, max_blocked_in_a_row=1)
        self.assertEqual(ap_state.compute_stop_reason(st, cfg), "max_rounds reached")
        # A blocked streak beats max_minutes.
        st = dict(base_state, blocked_rounds=2,
                  history=[{"round": 1, "status": "blocked"},
                           {"round": 2, "status": "blocked"}],
                  last_activity_at="2020-01-01T00:00:00+00:00",
                  started_at="2020-01-01T00:00:00+00:00")
        cfg = dict(base_cfg, max_blocked_in_a_row=2, max_minutes=1)
        self.assertIn("max_blocked_in_a_row", ap_state.compute_stop_reason(st, cfg))
        # max_minutes beats deadline.
        st = dict(base_state, last_activity_at="2020-01-01T00:00:00+00:00",
                  started_at="2020-01-01T00:00:00+00:00")
        cfg = dict(base_cfg, max_minutes=1, deadline="2000-01-01T00:00:00+00:00")
        self.assertIn("max_minutes", ap_state.compute_stop_reason(st, cfg))


class BatchContractTests(RepoTest):
    """CLI-level contracts tying check's hints to what ranking actually picks."""

    def _configure(self, **overrides):
        cfg_path = self.repo / ".autopilot" / "config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg.update(overrides)
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

    def test_selected_empty_reason_surfaced(self):
        # TG-3 hardened: every ready above-floor candidate is predicted and the
        # predicted quota is 0 -> the batch is empty. min_pending_candidates=0
        # keeps needs_expansion false, so ONLY the batch contract can turn the
        # hint away from "work".
        self.run_state("init", "--min-pending-candidates", "0")
        self._configure(max_predicted_per_round=0)
        self.run_state("backlog-add", "--title", "pred-a", "--reason", "r", "--value", "5",
                       "--effort", "1", "--origin", "predicted", "--confidence", "0.9",
                       "--type", "refactor")
        self.run_state("backlog-add", "--title", "pred-b", "--reason", "r", "--value", "4",
                       "--effort", "2", "--origin", "predicted", "--confidence", "0.9",
                       "--type", "perf")
        data = json.loads(self.run_state("check", "--brief").stdout)
        # Quota-cut with a ready pool stays expand (not mine) — diversity problem.
        self.assertEqual(data["action_hint"], "expand")
        self.assertEqual(data["selected_count"], 0)
        self.assertEqual(data["selected_empty_reason"], "quota")
        self.assertEqual(data["backlog"]["ready"], 2)

        # TG-3's original shape, now fixed: a value>=floor observed candidate is
        # always eligible until the batch is full, quota or not.
        self._configure(max_predicted_per_round=0)
        self.run_state("backlog-add", "--title", "obs", "--reason", "r", "--value", "3",
                       "--effort", "5", "--type", "bugfix")
        data = json.loads(self.run_state("check", "--brief").stdout)
        self.assertEqual(data["selected_count"], 1)
        self.assertIsNone(data["selected_empty_reason"])

    def test_type_stats_consistent_after_backlog_update(self):
        self.run_state("init")
        self.run_state("backlog-add", "--title", "A", "--reason", "r", "--value", "4",
                       "--effort", "2", "--type", "docs")
        self.run_state("backlog-add", "--title", "B", "--reason", "r", "--value", "4",
                       "--effort", "2", "--type", "docs")
        ids = [c["id"] for c in self.read_json("backlog.json")["candidates"]]
        result = self.run_state("backlog-update", "--id", ids[0], "--status", "completed")
        self.assertEqual(result.returncode, 0, result.stderr)
        expected = ap_state.compute_type_stats(self.read_json("backlog.json"))
        self.assertEqual(self.read_json("state.json")["type_stats"], expected)
        result = self.run_state("backlog-remove", "--id", ids[1])
        self.assertEqual(result.returncode, 0, result.stderr)
        expected = ap_state.compute_type_stats(self.read_json("backlog.json"))
        self.assertEqual(self.read_json("state.json")["type_stats"], expected)

    def test_expansion_brief_content(self):
        self.run_state("init", "--goal", "G", "--expand-after-goals",
                       "--type-saturation-threshold", "2")
        for i in range(3):
            self.run_state("backlog-add", "--title", "d{}".format(i), "--reason", "r",
                           "--value", "4", "--effort", "2", "--type", "docs")
        backlog_path = self.repo / ".autopilot" / "backlog.json"
        backlog = json.loads(backlog_path.read_text(encoding="utf-8"))
        for c in backlog["candidates"]:
            c["status"] = "completed"
        backlog_path.write_text(json.dumps(backlog), encoding="utf-8")
        # backlog-update refreshes state.type_stats from the backlog.
        ids = [c["id"] for c in self.read_json("backlog.json")["candidates"]]
        self.run_state("backlog-update", "--id", ids[0], "--title", "d0")
        self.run_state("begin-round", "--title", "r", "--reason", "x")
        self.add_file()
        self.run_state("commit", "--summary", "add feature")
        sha = self.git("rev-parse", "HEAD").stdout.strip()
        self.run_state("complete-round", "--summary", "done", "--commit-sha", sha)
        self.run_state("goal-met", "--goal", "G", "--round", "1")
        data = json.loads(self.run_state("check", "--brief").stdout)
        self.assertEqual(data["phase"], "expand")
        self.assertEqual(data["expansion"]["saturated_types"], ["docs"])

    def test_suggested_themes_content(self):
        self.run_state("init", "--goal", "G", "--expand-after-goals")
        self.add_file()
        self.git("add", "feature.py")
        self.git("commit", "-q", "-m", "add feature")
        self.run_state("goal-met", "--goal", "G")
        self.run_state("goal-met", "--goal", "G")
        data = json.loads(self.run_state("check", "--brief").stdout)
        self.assertEqual(
            data["expansion"]["suggested_themes"],
            ["follow up on goal: G", "initial", "add feature"],
        )

    def test_begin_round_error_names_below_floor_pool(self):
        self.run_state("init")
        self.run_state("backlog-add", "--title", "Chore", "--reason", "r",
                       "--value", "2", "--effort", "1")
        result = self.run_state("begin-round", "--title", "r", "--reason", "x")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("below-floor", result.stderr)
        self.assertIn("Deep Expansion", result.stderr)
        self.assertIn("--candidate-id", result.stderr)

    def test_default_seed_type_skips_bugfix(self):
        self.assertEqual(ap_state.VALID_CANDIDATE_TYPES[0], "bugfix")  # order sanity
        self.assertEqual(commands_module._default_seed_type([]), "feature")
        self.assertEqual(commands_module._default_seed_type(["bugfix"]), "feature")
        self.assertEqual(
            commands_module._default_seed_type(["bugfix", "feature", "refactor", "perf", "test"]),
            "docs",
        )
        # Everything saturated -> the feature fallback.
        self.assertEqual(
            commands_module._default_seed_type(list(ap_state.VALID_CANDIDATE_TYPES)),
            "feature",
        )

    def test_seed_defaults_risk_two(self):
        self.run_state("init", "--goal", "G")
        result = self.run_state("goal-met", "--goal", "G", "--next-step", "N")
        self.assertEqual(result.returncode, 0, result.stderr)
        state = self.read_json("state.json")
        self.assertEqual(state["goal_seeds"][0]["risk"], 2)


class ExpansionWaveTests(RepoTest):
    """Deep Expansion waves are recorded and observable: lens rotation via
    expansion_waves + check's lenses_unused, cache staleness via analysis payload."""

    def _record(self, *lenses):
        args = ["expansion-record"]
        for lens in lenses:
            args.extend(["--lens", lens])
        return self.run_state(*args)

    def _log_events(self):
        log_path = self.repo / ".autopilot" / "log.jsonl"
        return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()
                if line.strip()]

    def test_expansion_wave_recorded(self):
        self.run_state("init")
        result = self._record("tests", "performance")
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self._record("security", "tests")
        self.assertEqual(result.returncode, 0, result.stderr)
        state = self.read_json("state.json")
        waves = state["expansion_waves"]
        self.assertEqual(len(waves), 2)
        self.assertEqual(waves[0]["lenses"], ["performance", "tests"])
        self.assertEqual(waves[1]["lenses"], ["security", "tests"])
        data = json.loads(self.run_state("check", "--brief").stdout)
        self.assertEqual(data["wave_no"], 2)
        # Union across waves, in wave order (each wave's lenses are stored as a
        # sorted set snapshot).
        self.assertEqual(data["lenses_used"], ["performance", "tests", "security"])
        expected_unused = [lens for lens in ap_state.EXPANSION_LENSES
                           if lens not in data["lenses_used"]]
        self.assertEqual(data["lenses_unused"], expected_unused)
        self.assertNotIn("tests", data["lenses_unused"])
        self.assertIn("architecture", data["lenses_unused"])

    def test_consecutive_same_lens_waves_warn(self):
        self.run_state("init")
        first = self._record("tests", "performance")
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self._record("performance", "tests")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("[WARN]", second.stderr)
        self.assertIn("same lens set", second.stderr)
        warns = [e for e in self._log_events()
                 if e.get("event") == "expansion-wave" and e.get("status") == "warn"]
        self.assertEqual(len(warns), 1)
        self.assertEqual(warns[-1].get("reason"), "same lens set as previous wave")
        # A different set afterwards does not warn again.
        third = self._record("security")
        self.assertEqual(third.returncode, 0, third.stderr)
        self.assertNotIn("[WARN]", third.stderr)
        self.assertEqual(len(warns), 1)

    def test_expansion_record_rejects_unknown_lens(self):
        self.run_state("init")
        result = self._record("tests", "vibes")
        self.assertEqual(result.returncode, 2)
        self.assertIn("Unknown expansion lens", result.stderr)
        self.assertIn("vibes", result.stderr)
        self.assertEqual(self.read_json("state.json")["expansion_waves"], [])

    def test_check_surfaces_stale_analysis(self):
        self.run_state("init")
        result = self.run_state("analysis-save", "--content", '{"summary": "fresh scan"}')
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(self.run_state("check", "--brief").stdout)
        self.assertEqual(data["analysis"]["status"], "fresh")
        # Three commits push the cache past the stale-warning threshold.
        for i in range(3):
            (self.repo / "file{}.py".format(i)).write_text("x = {}\n".format(i), encoding="utf-8")
            self.git("add", "file{}.py".format(i))
            self.git("commit", "-q", "-m", "commit {}".format(i))
        data = json.loads(self.run_state("check", "--brief").stdout)
        self.assertEqual(data["analysis"]["status"], "stale")
        self.assertGreaterEqual(data["analysis"]["commits_behind"], 3)
        self.assertTrue(any("analysis cache is" in w and "commits stale" in w
                            for w in data["warnings"]))
        # Cache staleness is a hint, never a stop.
        self.assertTrue(data["continue"])
        self.assertIsNone(data["stop_reason"])

    def test_check_works_without_analysis(self):
        self.run_state("init")
        data = json.loads(self.run_state("check", "--brief").stdout)
        self.assertEqual(data["analysis"]["status"], "missing")
        self.assertIsNone(data["analysis"]["commits_behind"])
        # A missing cache must not stop the loop; never-mined empty backlog → mine.
        self.assertEqual(data["action_hint"], "mine")
        self.assertTrue(data["continue"])

    def test_analysis_staleness_tracks_each_commit(self):
        self.run_state("init")
        self.run_state("analysis-save", "--content", '{"summary": "fresh scan"}')
        data = json.loads(self.run_state("check", "--brief").stdout)
        self.assertEqual(data["analysis"]["status"], "fresh")
        self.assertEqual(data["analysis"]["commits_behind"], 0)
        (self.repo / "a.py").write_text("a = 1\n", encoding="utf-8")
        self.git("add", "a.py")
        self.git("commit", "-q", "-m", "first")
        first = json.loads(self.run_state("check", "--brief").stdout)
        self.assertEqual(first["analysis"]["status"], "stale")
        self.assertEqual(first["analysis"]["commits_behind"], 1)
        (self.repo / "b.py").write_text("b = 2\n", encoding="utf-8")
        self.git("add", "b.py")
        self.git("commit", "-q", "-m", "second")
        second = json.loads(self.run_state("check", "--brief").stdout)
        self.assertEqual(second["analysis"]["status"], "stale")
        self.assertEqual(second["analysis"]["commits_behind"], 2)
        self.assertGreater(second["analysis"]["commits_behind"],
                           first["analysis"]["commits_behind"])


class ConfigSetTests(RepoTest):
    """config-set reopens an 'all goals met' stop and re-fingerprints state so
    the deliberate change is not flagged as config-drift."""

    def _complete_one_round(self):
        self.run_state("begin-round", "--title", "r", "--reason", "x")
        self.add_file()
        self.run_state("commit", "--summary", "add feature")
        sha = self.git("rev-parse", "HEAD").stdout.strip()
        self.run_state("complete-round", "--summary", "done", "--commit-sha", sha)

    def test_config_set_expands_after_goals(self):
        self.run_state("init", "--goal", "G")
        self._complete_one_round()
        self.run_state("goal-met", "--goal", "G", "--round", "1")
        refused = self.run_state("begin-round", "--title", "r2", "--reason", "y")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("all goals met", refused.stderr)
        self.assertIn("config-set --expand-after-goals", refused.stderr)
        result = self.run_state("config-set", "--expand-after-goals")
        self.assertEqual(result.returncode, 0, result.stderr)
        config = self.read_json("config.json")
        self.assertTrue(config["expand_after_goals"])
        opened = self.run_state("begin-round", "--title", "r2", "--reason", "y")
        self.assertEqual(opened.returncode, 0, opened.stderr)

    def test_config_set_refreshes_fingerprint(self):
        self.run_state("init")
        config_path = self.repo / ".autopilot" / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["min_pending_candidates"] = 5
        config_path.write_text(json.dumps(config), encoding="utf-8")
        drifted = json.loads(self.run_state("check", "--brief").stdout)
        self.assertTrue(any("config.json changed since init" in w for w in drifted["warnings"]))
        result = self.run_state("config-set", "--expand-after-goals")
        self.assertEqual(result.returncode, 0, result.stderr)
        state = self.read_json("state.json")
        self.assertEqual(
            state["config_fingerprint"],
            ap_io.file_sha256(self.repo / ".autopilot" / "config.json"),
        )
        data = json.loads(self.run_state("check", "--brief").stdout)
        self.assertFalse(any("config.json changed since init" in w for w in data["warnings"]))

    def test_config_set_multiple_fields_roundtrip(self):
        self.run_state("init")
        result = self.run_state(
            "config-set", "--candidates-per-round", "6", "--commit-every-rounds", "3",
            "--max-minutes", "90",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        config = self.read_json("config.json")
        self.assertEqual(config["candidates_per_round"], 6)
        self.assertEqual(config["commit_every_rounds"], 3)
        self.assertEqual(config["max_minutes"], 90)
        state = self.read_json("state.json")
        self.assertEqual(
            state["config_fingerprint"],
            ap_io.file_sha256(self.repo / ".autopilot" / "config.json"),
        )
        data = json.loads(self.run_state("check", "--brief").stdout)
        self.assertFalse(any("config.json changed since init" in w for w in data["warnings"]))

    def test_config_set_clear_budget_stores_null(self):
        self.run_state("init", "--max-minutes", "45")
        result = self.run_state("config-set", "--clear-max-minutes")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(self.read_json("config.json")["max_minutes"])

    def test_config_set_deadline_resolves_relative_expression(self):
        self.run_state("init")
        result = self.run_state("config-set", "--deadline", "+2h")
        self.assertEqual(result.returncode, 0, result.stderr)
        stored = self.read_json("config.json")["deadline"]
        self.assertIsNotNone(ap_io.parse_time(stored))

    def test_config_set_rejects_nonpositive_and_value_clear_conflicts(self):
        self.run_state("init")
        bad = self.run_state("config-set", "--candidates-per-round", "0")
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("positive integer", bad.stderr)
        conflict = self.run_state("config-set", "--max-rounds", "5", "--clear-max-rounds")
        self.assertNotEqual(conflict.returncode, 0)
        self.assertIn("mutually exclusive", conflict.stderr)
        unparsable = self.run_state("config-set", "--deadline", "昨天")
        self.assertNotEqual(unparsable.returncode, 0)
        self.assertIn("Could not parse --deadline", unparsable.stderr)


class BudgetAccountingTests(RepoTest):
    """P1 budget/round accounting: zero-work cancels do not consume a round,
    tokens are billed monotonically per run, blocked streaks and budgets are
    observable in check."""

    def _touch_staged(self, name, lines=10):
        (self.repo / name).write_text("x = 1\n" * lines, encoding="utf-8")
        self.git("add", name)

    def test_zero_work_cancel_does_not_consume_round(self):
        self.run_state("init")
        self.run_state("begin-round", "--title", "probe", "--reason", "x")
        result = self.run_state("cancel-round", "--reason", "wrong flag")
        self.assertEqual(result.returncode, 0, result.stderr)
        state = self.read_json("state.json")
        self.assertEqual(
            (state["completed_rounds"], state["blocked_rounds"], state["cancelled_rounds"]),
            (0, 0, 0),
        )
        self.assertEqual(state["history"][-1]["status"], "aborted")
        self.assertEqual(state["estimated_tokens_used"], 0)
        # The next round gets a fresh number from round_seq (never reuses 1).
        self.run_state("begin-round", "--title", "real", "--reason", "x")
        state = self.read_json("state.json")
        self.assertEqual(state["current_round"]["round"], 2)
        self.assertEqual(state["round_seq"], 2)
        # aborted does not advance the max_rounds denominator: with
        # max_rounds 2 the one real completed round still allows round 3.
        self.run_state("complete-round", "--summary", "done")
        result = self.run_state("begin-round", "--title", "r3", "--reason", "x")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.read_json("state.json")["current_round"]["round"], 3)

    def test_zero_work_cancel_thresholds(self):
        self.run_state("init")
        # Working-tree changes exist -> still a billed cancelled round.
        self.run_state("begin-round", "--title", "a", "--reason", "x")
        self.add_file()
        self.run_state("cancel-round", "--reason", "real work pending")
        state = self.read_json("state.json")
        self.assertEqual(state["history"][-1]["status"], "cancelled")
        self.assertEqual(state["cancelled_rounds"], 1)
        # A commit happened during the round -> cancelled.
        self.run_state("begin-round", "--title", "b", "--reason", "x")
        self.add_file("b.py")
        self.git("add", "b.py")
        self.git("commit", "-q", "-m", "work")
        self.run_state("cancel-round", "--reason", "after commit")
        state = self.read_json("state.json")
        self.assertEqual(state["history"][-1]["status"], "cancelled")
        self.assertEqual(state["cancelled_rounds"], 2)
        # Old probe (>10 minutes) with no changes -> cancelled again.
        self.run_state("begin-round", "--title", "c", "--reason", "x")
        state_path = self.repo / ".autopilot" / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["current_round"]["started_at"] = "2020-01-01T00:00:00+00:00"
        state_path.write_text(json.dumps(state), encoding="utf-8")
        self.run_state("cancel-round", "--reason", "stale probe")
        state = self.read_json("state.json")
        self.assertEqual(state["history"][-1]["status"], "cancelled")
        self.assertEqual(state["cancelled_rounds"], 3)

    def test_token_accounting_monotonic(self):
        # QA-3: two staged rounds committed in round 3 used to bill the same
        # 20 lines twice ([620, 620, 740] = 1980); the run-level water marks
        # bill them once. A commit-only batch flush with zero new units is free
        # (no TOKEN_BASE) so max_tokens is not drained by re-committing already
        # billed work ([620, 620, 0] = 1240).
        self.run_state("init")
        self.run_state("begin-round", "--title", "r1", "--reason", "x")
        self._touch_staged("w1.py")
        self.run_state("complete-round", "--summary", "r1")
        self.run_state("begin-round", "--title", "r2", "--reason", "x")
        self._touch_staged("w2.py")
        self.run_state("complete-round", "--summary", "r2")
        self.run_state("begin-round", "--title", "r3", "--reason", "x")
        self.git("commit", "-q", "-m", "batch")
        sha = self.git("rev-parse", "HEAD").stdout.strip()
        self.run_state("complete-round", "--summary", "r3", "--commit-sha", sha)
        state = self.read_json("state.json")
        tokens = [entry["estimated_tokens"] for entry in state["history"]]
        self.assertEqual(tokens, [620, 620, 0])
        self.assertEqual(state["estimated_tokens_used"], 1240)

    def test_check_reports_blocked_streak(self):
        self.run_state("init")
        self.run_state("begin-round", "--title", "a", "--reason", "x")
        self.run_state("block-round", "--reason", "b1")
        data = json.loads(self.run_state("check", "--brief").stdout)
        self.assertEqual(data["blocked_streak"], 1)
        self.assertTrue(any("one more blocked round stops the run" in w for w in data["warnings"]))
        # Second blocked round fires max_blocked_in_a_row (default 2).
        self.run_state("begin-round", "--title", "b", "--reason", "x")
        self.run_state("block-round", "--reason", "b2")
        data = json.loads(self.run_state("check", "--brief").stdout)
        self.assertEqual(data["blocked_streak"], 2)
        self.assertTrue(data["stop_reason"].startswith("max_blocked_in_a_row"))

    def test_complete_round_below_threshold_records_score(self):
        self.run_state("init", "--review-threshold", "4", "--goal", "G")
        self.run_state("backlog-add", "--title", "A", "--reason", "r", "--value", "4",
                       "--effort", "2", "--type", "docs")
        cid = self.read_json("backlog.json")["candidates"][0]["id"]
        self.run_state("begin-round", "--title", "A", "--reason", "r", "--candidate-id", cid)
        self.add_file()
        self.git("add", "feature.py")
        self.git("commit", "-q", "-m", "work")
        sha = self.git("rev-parse", "HEAD").stdout.strip()
        # Without the flag the refusal stands (original behavior locked).
        refused = self.run_state("complete-round", "--summary", "s", "--commit-sha", sha,
                                 "--review-score", "3")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("below review_threshold", refused.stderr)
        result = self.run_state("complete-round", "--summary", "s", "--commit-sha", sha,
                                "--review-score", "3", "--below-threshold")
        self.assertEqual(result.returncode, 0, result.stderr)
        state = self.read_json("state.json")
        self.assertEqual(state["history"][-1]["review_score"], 3)
        self.assertTrue(state["history"][-1]["below_threshold"])
        self.assertEqual(state["history"][-1]["status"], "completed")
        self.assertEqual(state["blocked_rounds"], 0)
        # The low score still lands in the calibration ledger.
        self.assertGreaterEqual(self.read_json("state.json")["type_stats"]["docs"]["review_n"], 1)

    def test_check_reports_budget_remaining(self):
        self.run_state("init", "--max-minutes", "60")
        state = self.read_json("state.json")
        data = json.loads(self.run_state("check", "--brief").stdout)
        self.assertEqual(data["budget"]["max_minutes"], 60)
        self.assertEqual(data["budget"]["last_activity_at"], state["last_activity_at"])
        self.assertIsNotNone(data["budget"]["remaining_minutes"])
        self.assertGreater(data["budget"]["remaining_minutes"], 0)
        self.assertLessEqual(data["budget"]["remaining_minutes"], 60)

    def test_resume_after_crash_mid_round(self):
        self.run_state("init")
        self.run_state("begin-round", "--title", "r", "--reason", "x")
        # Simulate a crash that killed the process holding the lock.
        lock = self.repo / ".autopilot" / "lock"
        if lock.exists():
            lock.unlink()
        data = json.loads(self.run_state("check", "--brief").stdout)
        self.assertTrue(any("current_round is open" in w for w in data["warnings"]))
        self.assertTrue(data["continue"])
        self.add_file()
        self.run_state("commit", "--summary", "add feature")
        sha = self.git("rev-parse", "HEAD").stdout.strip()
        result = self.run_state("complete-round", "--summary", "done", "--commit-sha", sha)
        self.assertEqual(result.returncode, 0, result.stderr)
        state = self.read_json("state.json")
        self.assertEqual(state["completed_rounds"], 1)
        self.assertEqual(state["history"][-1]["status"], "completed")

    def test_paused_run_stops_on_first_check(self):
        self.run_state("init", "--max-minutes", "1")
        self.run_state("begin-round", "--title", "r", "--reason", "x")
        state_path = self.repo / ".autopilot" / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["last_activity_at"] = "2020-01-01T00:00:00+00:00"
        state_path.write_text(json.dumps(state), encoding="utf-8")
        data = json.loads(self.run_state("check", "--brief").stdout)
        self.assertFalse(data["continue"])
        self.assertIn("max_minutes", data["stop_reason"])

    def test_resume_after_deadline_pass(self):
        self.run_state("init", "--deadline", "2020-01-01T00:00:00")
        data = json.loads(self.run_state("check", "--brief").stdout)
        self.assertFalse(data["continue"])
        self.assertIn("deadline", data["stop_reason"])

    def test_schedule_hints_include_boundary_round(self):
        self.run_state("init", "--commit-every-rounds", "5", "--verify-every-rounds", "3",
                       "--checkpoint-every", "2")
        state_path = self.repo / ".autopilot" / "state.json"
        for completed, expected_commit in ((4, 5), (5, 5), (6, 10)):
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["completed_rounds"] = completed
            state_path.write_text(json.dumps(state), encoding="utf-8")
            data = json.loads(self.run_state("check", "--brief").stdout)
            self.assertEqual(data["next_commit_round"], expected_commit, completed)
            self.assertEqual(data["next_verify_round"], ((max(1, completed) - 1) // 3 + 1) * 3)
            self.assertEqual(data["next_checkpoint_round"], ((max(1, completed) - 1) // 2 + 1) * 2)
        # A brand-new run (current_number 0) still points at the first boundary.
        self.run_state("init", "--force", "--checkpoint-every", "2")
        data = json.loads(self.run_state("check", "--brief").stdout)
        self.assertEqual(data["next_checkpoint_round"], 2)


class RobustnessTests(RepoTest):
    """P2 hardening: config validation mirrors (QA-1/2/6), binary staged-diff
    findings (QA-5), goal-text normalization (QA-7/8), and IO fail-closed
    behaviors (QA-9/10)."""

    def _write_config(self, payload):
        config_path = self.repo / ".autopilot" / "config.json"
        config_path.parent.mkdir(exist_ok=True)
        config_path.write_text(json.dumps(payload), encoding="utf-8")

    def _assert_load_config_exits(self, repo, expected_fragment):
        from autopilot import config as config_module
        with self.assertRaises(SystemExit) as ctx:
            config_module.load_config(repo)
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn(expected_fragment, ctx.exception.__class__.__name__ + str(ctx.exception))

    def test_negative_blocked_and_retries_rejected(self):
        self.run_state("init")
        self._write_config({"max_blocked_in_a_row": -1})
        from autopilot import config as config_module
        with self.assertRaises(SystemExit) as ctx:
            config_module.load_config(self.repo)
        self.assertEqual(ctx.exception.code, 2)
        # The error prints to stderr; capture the message via save of stderr.
        result = self.run_state("check")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("'max_blocked_in_a_row'", result.stderr)
        self._write_config({"retries_per_round": -1})
        result = self.run_state("check")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("'retries_per_round'", result.stderr)

    def test_nan_budget_rejected(self):
        result = self.run_state("init", "--force", "--max-minutes", "nan")
        self.assertEqual(result.returncode, 2)
        self.assertIn("finite", result.stderr)
        self.run_state("init", "--force")
        self._write_config({"max_minutes": 1e999})
        from autopilot import config as config_module
        with self.assertRaises(SystemExit) as ctx:
            config_module.load_config(self.repo)
        self.assertEqual(ctx.exception.code, 2)
        result = self.run_state("check")
        self.assertIn("finite", result.stderr)

    def test_binary_staged_produces_finding(self):
        self.run_state("init")
        self.run_state("begin-round", "--title", "r", "--reason", "x")
        # A real binary blob whose (unscannable) content carries a token shape.
        payload = b"\x00\x01\x02ghp_" + b"A" * 36 + b"\xff\xfe"
        (self.repo / "keystore.bin").write_bytes(payload)
        self.git("add", "keystore.bin")
        from autopilot import secrets as secrets_module
        findings = secrets_module.scan_staged_diff(self.repo)
        binary = [f for f in findings if f.get("pattern") == "binary-staged"]
        self.assertEqual(len(binary), 1)
        self.assertEqual(binary[0]["file"], "keystore.bin")
        self.assertIn("not scannable", binary[0]["text"])
        # The commit helper refuses without --allow-secrets.
        refused = self.run_state("commit", "--summary", "add keystore")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("secret", refused.stderr.lower())
        allowed = self.run_state("commit", "--summary", "add keystore", "--allow-secrets")
        self.assertEqual(allowed.returncode, 0, allowed.stderr)

    def test_init_force_rebuilds_config_fresh(self):
        self.run_state("init")
        self._write_config({"ranking_mode": "bogus"})
        # init without --force still inherits and refuses to save; with --force
        # the defaults + CLI win and the config becomes loadable again.
        inherited = self.run_state("init", "--force")  # sanity: force is accepted
        self.assertEqual(inherited.returncode, 0, inherited.stderr)
        self._write_config({"ranking_mode": "bogus", "max_rounds": 3})
        result = self.run_state("init", "--force")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.read_json("config.json")["ranking_mode"], "expected")
        self.assertEqual(self.read_json("config.json")["max_rounds"], 10)
        self.assertEqual(self.run_state("check").returncode, 0)

    def test_split_goals_keeps_version_numbers(self):
        self.assertEqual(
            ap_state.split_goals("升级到 v1.3.3 并修复崩溃"),
            ["升级到 v1.3.3 并修复崩溃"],
        )
        self.assertEqual(ap_state.split_goals("支持 3.5 版本"), ["支持 3.5 版本"])
        # Sentence dots still split.
        self.assertEqual(ap_state.split_goals("修复崩溃.加测试"), ["修复崩溃", "加测试"])

    def test_report_goal_checkbox_normalizes(self):
        self.run_state("init", "--force", "--goal", "发布 v1.3.3")
        state_path = self.repo / ".autopilot" / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["completed_goals"] = ["发布 v1.3.3"]
        state_path.write_text(json.dumps(state), encoding="utf-8")
        cfg_path = self.repo / ".autopilot" / "config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg["goals"] = ["发布 v1.3.3\u200b"]
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        # all_goals_met normalizes; the report checkbox must agree with it.
        self.assertTrue(ap_state.all_goals_met(self.read_json("config.json"), state))
        report = self.run_state("report")
        self.assertEqual(report.returncode, 0, report.stderr)
        self.assertIn("- [x]", report.stdout)

    def test_pid_alive_permission_error_is_alive(self):
        import os as os_module
        import autopilot.io as ap_io_module

        # EPERM means the holder EXISTS: the lock must fail closed to "alive"
        # so a cross-user live lock is never deleted. _pid_alive probes with
        # os.kill on POSIX and with the tasklist subprocess on Windows, so the
        # test has to patch whichever probe the current platform actually
        # uses — patching subprocess.run on POSIX leaves os.kill in place and
        # asserts nothing (it used to fail outright on a nonexistent PID).
        if os_module.name == "nt":
            original_run = ap_io_module.subprocess.run

            def refuse_tasklist(*args, **kwargs):
                raise PermissionError("EPERM: operation not permitted")

            try:
                ap_io_module.subprocess.run = refuse_tasklist
                self.assertTrue(ap_io_module._pid_alive(12345))
            finally:
                ap_io_module.subprocess.run = original_run
        else:
            original_kill = ap_io_module.os.kill

            def refuse_kill(pid, sig):
                raise PermissionError("EPERM: operation not permitted")

            try:
                ap_io_module.os.kill = refuse_kill
                self.assertTrue(ap_io_module._pid_alive(12345))
            finally:
                ap_io_module.os.kill = original_kill

    def test_pid_alive_missing_process_is_dead(self):
        """The counterpart guard: a PID that is genuinely gone must read as
        dead, so a stale lock still gets cleaned up (the EPERM branch above
        must not swallow ProcessLookupError).

        Both platforms are driven through a stubbed probe rather than a real
        one: an empty tasklist result for Windows (no matching row) and
        ProcessLookupError for POSIX. Relying on the host's real tasklist
        would make this test depend on machine state, and CI cannot be
        trusted to catch that here (runs on this repo sit queued)."""
        import os as os_module
        import autopilot.io as ap_io_module

        if os_module.name == "nt":
            class _NoMatch:
                stdout = "INFO: No tasks are running which match the specified criteria.\r\n"

            original_run = ap_io_module.subprocess.run
            try:
                ap_io_module.subprocess.run = lambda *args, **kwargs: _NoMatch()
                self.assertFalse(ap_io_module._pid_alive(12345))
            finally:
                ap_io_module.subprocess.run = original_run
            return

        original_kill = ap_io_module.os.kill

        def missing(pid, sig):
            raise ProcessLookupError("ESRCH: no such process")

        try:
            ap_io_module.os.kill = missing
            self.assertFalse(ap_io_module._pid_alive(12345))
        finally:
            ap_io_module.os.kill = original_kill


class OrphanCommitTests(RepoTest):
    def test_commit_refuses_without_open_round(self):
        self.run_state("init")
        self.add_file()
        result = self.run_state("commit", "--summary", "orphan")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no round is open", result.stderr.lower())

    def test_commit_round_override_allows_orphan(self):
        # --round flushes staged changes for an ALREADY-completed round, so the
        # number must exist in completed history (any orphan number used to be
        # accepted and absorbed whatever was staged).
        self.run_state("init")
        self.run_state("begin-round", "--title", "r", "--reason", "x")
        self.run_state("complete-round", "--summary", "done")
        self.add_file()
        result = self.run_state("commit", "--round", "1", "--summary", "orphan")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        log = self.git("log", "-1", "--pretty=%s").stdout.strip()
        self.assertEqual(log, "autopilot(round-1): orphan")


class GoalBudgetTests(RepoTest):
    def test_goal_met_updates_last_activity(self):
        self.run_state("init", "--max-minutes", "1")
        state_path = self.repo / ".autopilot" / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["last_activity_at"] = "2020-01-01T00:00:00+00:00"
        state_path.write_text(json.dumps(state), encoding="utf-8")
        self.run_state("goal-met", "--goal", "G")
        result = self.run_state("check")
        data = json.loads(result.stdout)
        self.assertTrue(data["continue"], data["stop_reason"])


class PredictedOriginTests(RepoTest):
    """刀 B anti-noise: predicted-origin candidates are confidence-discounted,
    quota-limited per batch, accounted separately, and cut in the late run.
    Acceptance matrix from docs/post-goal-prediction-proposal.md §8."""

    def _add(self, *args):
        result = self.run_state("backlog-add", *args)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip().splitlines()[-1]

    def test_old_backlog_scores_unchanged(self):
        """No origin field -> observed defaults; confidence/goal-chain factors neutral."""
        self.run_state("init")
        self._add("--title", "A", "--reason", "r", "--value", "4", "--effort", "2")
        ranked = json.loads(self.run_state("backlog-rank").stdout)
        breakdown = ranked[0]["score_breakdown"]
        self.assertEqual(breakdown["origin"], "observed")
        self.assertEqual(breakdown["confidence_factor"], 1.0)
        self.assertEqual(breakdown["goal_chain_factor"], 1.0)
        self.assertTrue(ranked[0]["selected"])

    def test_confidence_discounts_score(self):
        """Same value/effort/type: predicted confidence 0.6 scores ~0.6x observed
        (with >=3 predicted samples so the 0.75 prior is inactive)."""
        self.run_state("init")
        # Three resolved predicted candidates activate the predicted sub-account
        # (success_rate 1.0 -> prior x0.75 no longer applies).
        for _ in range(3):
            cid = self._add("--title", "hist", "--value", "3", "--effort", "3",
                            "--origin", "predicted", "--confidence", "0.8")
            backlog_path = self.repo / ".autopilot" / "backlog.json"
            backlog = json.loads(backlog_path.read_text(encoding="utf-8"))
            for c in backlog["candidates"]:
                if c["id"] == cid:
                    c["status"] = "completed"
            backlog_path.write_text(json.dumps(backlog), encoding="utf-8")
        obs = self._add("--title", "observed", "--value", "4", "--effort", "2")
        pred = self._add("--title", "predicted", "--value", "4", "--effort", "2",
                         "--origin", "predicted", "--confidence", "0.6",
                         "--based-on", "some goal", "--evidence", "e")
        self.run_state("goal-met", "--goal", "some goal")  # activates the goal-chain bonus
        ranked = json.loads(self.run_state("backlog-rank").stdout)
        by_id = {entry["id"]: entry for entry in ranked}
        ratio = by_id[pred]["score"] / by_id[obs]["score"]
        self.assertAlmostEqual(ratio, 0.6 * 1.08, places=2)  # confidence x goal-chain
        self.assertEqual(by_id[pred]["score_breakdown"]["confidence"], 0.6)
        self.assertEqual(by_id[pred]["score_breakdown"]["goal_chain_factor"], 1.08)

    def test_confidence_validation(self):
        self.run_state("init")
        result = self.run_state("backlog-add", "--title", "p", "--origin", "predicted",
                                "--confidence", "0.3")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("0.5 and 1.0", result.stderr)
        result = self.run_state("backlog-add", "--title", "p", "--origin", "bogus")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("observed|predicted|expansion", result.stderr)

    def test_predicted_quota_one_per_batch(self):
        """Three high-value ready predicted candidates, quota default 1: only one
        predicted enters the selected batch."""
        self.run_state("init", "--max-rounds", "10")
        for i in range(3):
            self._add("--title", "pred{}".format(i), "--value", "5", "--effort", "1",
                      "--origin", "predicted", "--confidence", "0.9",
                      "--type", "refactor")
        self._add("--title", "obs", "--value", "5", "--effort", "1", "--type", "bugfix")
        ranked = json.loads(self.run_state("backlog-rank").stdout)
        selected = [e for e in ranked if e.get("selected")]
        predicted_selected = [e for e in selected if e.get("origin") == "predicted"]
        self.assertEqual(len(predicted_selected), 1)
        self.assertTrue(any(e.get("origin") is None for e in selected))

    def test_quota_zero_disables_predicted_selection(self):
        self.run_state("init", "--max-rounds", "10")
        cfg_path = self.repo / ".autopilot" / "config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg["max_predicted_per_round"] = 0
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        for i in range(2):
            self._add("--title", "pred{}".format(i), "--value", "5", "--effort", "1",
                      "--origin", "predicted", "--type", "refactor")
        self._add("--title", "obs", "--value", "3", "--effort", "3", "--type", "bugfix")
        ranked = json.loads(self.run_state("backlog-rank").stdout)
        selected = [e for e in ranked if e.get("selected")]
        self.assertFalse(any(e.get("origin") == "predicted" for e in selected))

    def test_consecutive_blocked_predictions_sink_without_contaminating_observed(self):
        """Three blocked predictions: the next predicted sinks via its sub-account;
        the observed candidate of the same type keeps a clean success rate. With a
        one-slot batch the sunk predicted no longer competes with the observed
        candidate (above-floor candidates only fill slots until the batch is full)."""
        self.run_state("init", "--candidates-per-round", "1")
        for i in range(3):
            self._add("--title", "p{}".format(i), "--value", "5", "--effort", "1",
                      "--origin", "predicted", "--confidence", "0.9", "--type", "refactor")
        pred4 = self._add("--title", "p4", "--value", "5", "--effort", "1",
                          "--origin", "predicted", "--confidence", "0.9", "--type", "refactor")
        obs = self._add("--title", "obs", "--value", "5", "--effort", "1", "--type", "refactor")
        backlog_path = self.repo / ".autopilot" / "backlog.json"
        backlog = json.loads(backlog_path.read_text(encoding="utf-8"))
        for c in backlog["candidates"]:
            if c["id"] in ("candidate-001", "candidate-002", "candidate-003"):
                c["status"] = "blocked"
        backlog_path.write_text(json.dumps(backlog), encoding="utf-8")
        ranked = json.loads(self.run_state("backlog-rank").stdout)
        by_id = {entry["id"]: entry for entry in ranked}
        self.assertEqual(by_id[pred4]["score_breakdown"]["success_rate"], 0.0)
        self.assertEqual(by_id[obs]["score_breakdown"]["success_rate"], 1.0)
        self.assertFalse(by_id[pred4].get("selected"))
        self.assertEqual(by_id[pred4].get("cut_reason"), "batch_full")
        self.assertTrue(by_id[obs].get("selected"))

    def test_late_run_cuts_predicted_when_observed_ready(self):
        self.run_state("init", "--max-rounds", "10")
        # 8 rounds consumed -> progress 0.8 > 0.7.
        state_path = self.repo / ".autopilot" / "state.json"
        st = json.loads(state_path.read_text(encoding="utf-8"))
        st["completed_rounds"] = 8
        state_path.write_text(json.dumps(st), encoding="utf-8")
        self._add("--title", "pred", "--value", "5", "--effort", "1",
                  "--origin", "predicted", "--confidence", "0.95", "--type", "refactor")
        self._add("--title", "obs", "--value", "3", "--effort", "3", "--type", "bugfix")
        ranked = json.loads(self.run_state("backlog-rank").stdout)
        selected = [e for e in ranked if e.get("selected")]
        self.assertFalse(any(e.get("origin") == "predicted" for e in selected))
        self.assertTrue(any(e.get("origin") is None for e in selected))

    def test_late_run_allows_predicted_when_no_observed_ready(self):
        self.run_state("init", "--max-rounds", "10")
        state_path = self.repo / ".autopilot" / "state.json"
        st = json.loads(state_path.read_text(encoding="utf-8"))
        st["completed_rounds"] = 8
        state_path.write_text(json.dumps(st), encoding="utf-8")
        self._add("--title", "pred", "--value", "4", "--effort", "2",
                  "--origin", "predicted", "--confidence", "0.9", "--type", "refactor")
        ranked = json.loads(self.run_state("backlog-rank").stdout)
        selected = [e for e in ranked if e.get("selected")]
        self.assertTrue(any(e.get("origin") == "predicted" for e in selected))

    def test_classic_mode_ignores_prediction_factors(self):
        self.run_state("init", "--max-rounds", "10")
        cfg_path = self.repo / ".autopilot" / "config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg["ranking_mode"] = "classic"
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        obs = self._add("--title", "obs", "--value", "4", "--effort", "2")
        pred = self._add("--title", "pred", "--value", "4", "--effort", "2",
                         "--origin", "predicted", "--confidence", "0.5")
        ranked = json.loads(self.run_state("backlog-rank").stdout)
        by_id = {entry["id"]: entry for entry in ranked}
        self.assertAlmostEqual(by_id[pred]["score"], by_id[obs]["score"], places=3)
        self.assertNotIn("confidence_factor", by_id[pred]["score_breakdown"])

    def test_from_seed_defaults_predicted_origin(self):
        self.run_state("init")
        self.run_state("goal-met", "--goal", "G1", "--next-step", "follow up")
        seed_id = self.read_json("state.json")["goal_seeds"][0]["id"]
        cid = self._add("--from-seed", seed_id)
        ranked = json.loads(self.run_state("backlog-rank").stdout)
        entry = [e for e in ranked if e["id"] == cid][0]
        self.assertEqual(entry["origin"], "predicted")
        self.assertEqual(entry["based_on"], "G1")
        self.assertEqual(entry["confidence"], 0.75)
        self.assertEqual(entry["evidence"], "")

    def test_report_lists_seeds_and_predicted_account(self):
        self.run_state("init")
        self.run_state("goal-met", "--goal", "G1", "--next-step", "follow up A",
                       "--next-step", "follow up B")
        result = self.run_state("report")
        output = result.stdout
        self.assertIn("方向假设", output)
        self.assertIn("follow up A", output)
        cid = self._add("--from-seed", "seed-001")
        backlog_path = self.repo / ".autopilot" / "backlog.json"
        backlog = json.loads(backlog_path.read_text(encoding="utf-8"))
        for c in backlog["candidates"]:
            if c["id"] == cid:
                c["status"] = "blocked"
        backlog_path.write_text(json.dumps(backlog), encoding="utf-8")
        output = self.run_state("report").stdout
        self.assertIn("预测子账", output)
        self.assertIn("受阻 1", output)


class SeedScoringHelperTests(unittest.TestCase):
    """Adversarial direct tests of pure helpers (no subprocess, no temp repo)."""

    # -- resolve_seed state machine ------------------------------------------
    def _seed(self, status="open", **extra):
        state = {"goal_seeds": [dict({"id": "seed-001", "status": status}, **extra)]}
        return state, state["goal_seeds"][0]

    def test_resolve_seed_missing_and_invalid(self):
        st = {"goal_seeds": []}
        self.assertIsNone(ap_state.resolve_seed(st, "seed-999", "promoted"))
        self.assertIsNone(ap_state.resolve_seed(st, "seed-001", "bogus"))
        self.assertEqual(st, {"goal_seeds": []})

    def test_resolve_seed_terminal_is_immutable(self):
        for terminal in ("verified", "refuted", "rejected"):
            st, seed = self._seed(status=terminal)
            for target in ("open", "promoted", "verified", "refuted", "rejected"):
                self.assertIsNone(ap_state.resolve_seed(st, "seed-001", target), (terminal, target))
            self.assertEqual(seed["status"], terminal)

    def test_resolve_seed_reopen_clears_promotion_keeps_outcome(self):
        st, seed = self._seed(status="promoted", promoted_at="T", promoted_candidate_id="candidate-005",
                              outcome="")
        seed["outcome"] = "earlier attempt"
        self.assertIsNotNone(ap_state.resolve_seed(st, "seed-001", "open"))
        self.assertEqual(seed["status"], "open")
        self.assertNotIn("promoted_at", seed)
        self.assertNotIn("promoted_candidate_id", seed)
        self.assertEqual(seed["outcome"], "earlier attempt")

    def test_resolve_seed_refuted_keeps_ledger_honest(self):
        st, seed = self._seed(status="promoted", promoted_at="T", promoted_candidate_id="candidate-005")
        self.assertIsNotNone(ap_state.resolve_seed(st, "seed-001", "refuted", notes="evidence gone"))
        self.assertEqual(seed["status"], "refuted")
        self.assertEqual(seed["outcome"], "evidence gone")
        self.assertIn("promoted_at", seed)
        self.assertIn("refuted_at", seed)

    # -- _score_expected confidence adversarial -------------------------------
    def _score_confidence(self, confidence, origin="predicted"):
        candidate = {"id": "c", "title": "t", "value": 4, "effort": 2,
                     "origin": origin, "confidence": confidence}
        score, breakdown = ap_state._score_expected(candidate, type_stats={})
        return breakdown["confidence_factor"]

    def test_confidence_adversarial_table(self):
        fallback = 0.75
        cases = [
            (True, fallback), (False, fallback), ("high", fallback), (None, fallback),
            (1.7, fallback), (-0.2, fallback), (0.5, 0.5), (1.0, 1.0),
        ]
        for confidence, expected in cases:
            self.assertEqual(self._score_confidence(confidence), expected, confidence)

    def test_confidence_nan_and_inf_fall_back(self):
        # NaN fails the chained range test (min/max would return 1.0); inf too.
        self.assertEqual(self._score_confidence(float("nan")), 0.75)
        self.assertEqual(self._score_confidence(float("inf")), 0.75)

    def test_observed_confidence_forced_to_one(self):
        self.assertEqual(self._score_confidence(0.1, origin="observed"), 1.0)

    def test_nan_review_score_never_poisons_calibration(self):
        backlog = {"candidates": [
            {"id": "a", "title": "t", "type": "bugfix", "status": "completed",
             "value": 4, "effort": 2, "review_score": float("nan")},
        ]}
        stats = ap_state.compute_type_stats(backlog)
        self.assertEqual(stats["bugfix"]["review_n"], 0)
        self.assertEqual(stats["bugfix"]["calibration"], 1.0)
        account = ap_state.compute_predicted_account({"candidates": [
            {"id": "b", "title": "t", "status": "completed", "origin": "predicted",
             "review_score": float("nan")},
        ]})
        self.assertEqual(account["review_n"], 0)

    def test_overflow_values_do_not_crash_resolvers(self):
        self.assertEqual(ap_state._resolved_value({"value": float("inf")}), 3)
        self.assertEqual(ap_state._resolved_effort({"effort": float("inf")}), 3)

    # -- _next_sequential_id: fresh ids survive head truncation ----------------
    def test_sequential_id_never_reuses_truncated_ids(self):
        st = {"goal_seeds": [{"id": "seed-{:03d}".format(i), "title": str(i)} for i in range(1, 51)]}
        self.assertEqual(ap_state._next_sequential_id(st, "goal_seeds", "seed-"), "seed-051")
        st["goal_seeds"] = st["goal_seeds"][1:]  # seed-001 truncated away
        # Numbering is max(existing suffix) + 1 and the bounded lists only
        # truncate from the head, so the maximum suffix always survives: the
        # next id is again seed-051 — fresh, never a reuse of seed-001.
        self.assertEqual(ap_state._next_sequential_id(st, "goal_seeds", "seed-"), "seed-051")

    # -- _mark_selection quota edges ------------------------------------------
    @staticmethod
    def _entry(entry_id, origin=None, below=False, score=5.0):
        entry = {"id": entry_id, "title": entry_id, "type": "refactor", "status": "pending",
                 "ready": True, "score": score, "below_floor": below}
        if origin:
            entry["origin"] = origin
        return entry

    def _cfg(self, quota=1):
        return {"candidates_per_round": 3, "max_same_type_per_round": 2,
                "max_predicted_per_round": quota}

    def test_below_floor_predicted_respects_quota(self):
        entries = [
            self._entry("p1", origin="predicted", score=5.0),
            self._entry("p2", origin="predicted", score=4.0),
            self._entry("qp", origin="predicted", below=True, score=1.0),
        ]
        ap_state._mark_selection(entries, self._cfg(quota=0))
        self.assertFalse(any(e.get("selected") for e in entries))

        entries = [
            self._entry("p1", origin="predicted", score=5.0),
            self._entry("p2", origin="predicted", score=4.0),
            self._entry("qp", origin="predicted", below=True, score=1.0),
        ]
        ap_state._mark_selection(entries, self._cfg(quota=1))
        selected = [e["id"] for e in entries if e.get("selected")]
        self.assertEqual(selected, ["p1"])  # main-loop slot consumed; below skipped

    def test_below_floor_observed_still_fills_slot(self):
        entries = [
            self._entry("p1", origin="predicted", score=5.0),
            self._entry("p2", origin="predicted", score=4.0),
            self._entry("qo", below=True, score=1.0),
        ]
        ap_state._mark_selection(entries, self._cfg(quota=0))
        selected = [e["id"] for e in entries if e.get("selected")]
        self.assertEqual(selected, ["qo"])

    def test_all_predicted_pool_selects_single_top(self):
        entries = [
            self._entry("p1", origin="predicted", score=5.0),
            self._entry("p2", origin="predicted", score=4.0),
            self._entry("p3", origin="predicted", score=3.0),
        ]
        ap_state._mark_selection(entries, self._cfg(quota=1))
        selected = [e["id"] for e in entries if e.get("selected")]
        self.assertEqual(selected, ["p1"])

    def test_late_run_cuts_predicted_including_below(self):
        entries = [
            self._entry("p1", origin="predicted", score=5.0),
            self._entry("qp", origin="predicted", below=True, score=1.0),
            self._entry("obs", score=2.0),
        ]
        ap_state._mark_selection(entries, self._cfg(quota=1), progress=0.8)
        selected = [e["id"] for e in entries if e.get("selected")]
        self.assertEqual(selected, ["obs"])


class PredictedHardeningTests(RepoTest):
    """Subprocess regressions for the v1.3.2 hardening (audit-driven)."""

    def test_seed_reject_command(self):
        self.run_state("init", "--goal", "G", "--expand-after-goals", "--max-rounds", "50")
        self.run_state("goal-met", "--goal", "G")
        self.run_state("goal-met", "--goal", "G2", "--next-step", "N")
        result = self.run_state("seed-reject", "--id", "seed-001", "--reason", "evidence gone")
        self.assertEqual(result.returncode, 0, result.stderr)
        state = self.read_json("state.json")
        seed = state["goal_seeds"][0]
        self.assertEqual(seed["status"], "rejected")
        self.assertEqual(seed["outcome"], "evidence gone")
        # Rejected seeds leave the open-seeds list: the Wave 0 exception can fire.
        brief = json.loads(self.run_state("check", "--brief").stdout)
        self.assertEqual(brief["expansion"]["seeds"], [])
        # promote / re-reject must fail
        result = self.run_state("backlog-add", "--from-seed", "seed-001")
        self.assertNotEqual(result.returncode, 0)
        result = self.run_state("seed-reject", "--id", "seed-001", "--reason", "again")
        self.assertNotEqual(result.returncode, 0)

    def test_check_expansion_tolerates_corrupt_seed_entries(self):
        self.run_state("init")
        self.run_state("goal-met", "--goal", "G", "--next-step", "real seed")
        state_path = self.repo / ".autopilot" / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["goal_seeds"] = ["oops", 42, None] + state["goal_seeds"]
        state["goal_events"] = ["bad-event"] + state["goal_events"]
        state_path.write_text(json.dumps(state), encoding="utf-8")
        data = json.loads(self.run_state("check", "--brief").stdout)
        self.assertEqual(data["phase"], "iterate")  # goals not met here; sanity

    def test_corrupt_type_stats_clean_error(self):
        self.run_state("init")
        state_path = self.repo / ".autopilot" / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["type_stats"] = {"bugfix": "oops"}
        state_path.write_text(json.dumps(state), encoding="utf-8")
        result = self.run_state("check", "--brief")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("type_stats", result.stderr)

    def test_corrupt_backlog_clean_error(self):
        self.run_state("init")
        backlog_path = self.repo / ".autopilot" / "backlog.json"
        backlog_path.write_text(json.dumps({"next_id": 2, "candidates": ["oops"]}), encoding="utf-8")
        result = self.run_state("backlog-rank")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("backlog.json", result.stderr)
        result = self.run_state("check")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Traceback", result.stderr)

    def test_goal_met_dedupes_repeated_seeds(self):
        self.run_state("init")
        for _ in range(2):
            result = self.run_state("goal-met", "--goal", "G", "--next-step", "same idea", "--json")
            self.assertEqual(result.returncode, 0, result.stderr)
        state = self.read_json("state.json")
        self.assertEqual(len(state["goal_seeds"]), 1)

    def test_goal_event_records_commit_sha_and_source_event_id(self):
        self.run_state("init")
        self.git("commit", "--allow-empty", "-q", "-m", "base")
        self.run_state("begin-round", "--title", "t", "--reason", "r")
        self.run_state("complete-round", "--commit-sha", "HEAD", "--summary", "s")
        result = self.run_state("goal-met", "--goal", "G", "--next-step", "N", "--json")
        payload = json.loads(result.stdout)
        state = self.read_json("state.json")
        event = state["goal_events"][0]
        self.assertEqual(len(event["commit_shas"]), 1)
        self.assertEqual(state["goal_seeds"][0]["source_event_id"], event["id"])

    def test_multi_seed_multi_candidate_round_resolution(self):
        self.run_state("init")
        self.run_state("goal-met", "--goal", "G", "--next-step", "A", "--next-step", "B")
        self.run_state("backlog-add", "--from-seed", "seed-001")
        self.run_state("backlog-add", "--from-seed", "seed-002")
        self.git("commit", "--allow-empty", "-q", "-m", "base")
        self.run_state("begin-round", "--title", "t", "--reason", "r",
                       "--candidate-id", "candidate-001", "--candidate-id", "candidate-002")
        self.run_state("complete-round", "--summary", "both")
        state = self.read_json("state.json")
        self.assertEqual([s["status"] for s in state["goal_seeds"]], ["verified", "verified"])

    def test_seed_writeback_warns_on_missing_seed(self):
        self.run_state("init")
        self.run_state("goal-met", "--goal", "G", "--next-step", "N")
        self.run_state("backlog-add", "--from-seed", "seed-001")
        state_path = self.repo / ".autopilot" / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["goal_seeds"] = []  # seed vanished (as if truncated)
        state_path.write_text(json.dumps(state), encoding="utf-8")
        self.git("commit", "--allow-empty", "-q", "-m", "base")
        self.run_state("begin-round", "--title", "t", "--reason", "r", "--candidate-id", "candidate-001")
        result = self.run_state("complete-round", "--summary", "s")
        self.assertEqual(result.returncode, 0, result.stderr)  # no crash
        log = (self.repo / ".autopilot" / "log.jsonl").read_text(encoding="utf-8")
        self.assertIn("seed-writeback", log)
        self.assertIn("missing", log)

    def test_goal_met_text_mode_echoes_seed_ids(self):
        self.run_state("init")
        result = self.run_state("goal-met", "--goal", "G", "--next-step", "A", "--next-step", "B")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Created direction seeds: seed-001, seed-002", result.stdout)

    def test_backlog_add_requires_init(self):
        result = self.run_state("backlog-add", "--title", "t", "--value", "3", "--effort", "1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not initialized", result.stderr)
        self.assertFalse((self.repo / ".autopilot" / "backlog.json").exists())
        self.assertNotIn("candidate-", result.stdout)

    def test_backlog_add_fails_closed_on_corrupt_state(self):
        self.run_state("init")
        (self.repo / ".autopilot" / "state.json").write_text("{ not json", encoding="utf-8")
        result = self.run_state("backlog-add", "--title", "t", "--value", "3", "--effort", "1")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn("candidate-", result.stdout)

    def test_goal_invisible_chars_cannot_fake_success(self):
        self.run_state("init", "--goal", "提升测试质量", "--max-rounds", "5")
        self.run_state("begin-round", "--title", "r", "--reason", "x")
        self.add_file()
        self.run_state("commit", "--summary", "add feature")
        sha = self.git("rev-parse", "HEAD").stdout.strip()
        self.run_state("complete-round", "--summary", "done", "--commit-sha", sha)
        result = self.run_state("goal-met", "--goal", "提升测试质量\u200b", "--round", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        # The near-duplicate must not leave the run unable to stop.
        data = json.loads(self.run_state("check", "--brief").stdout)
        self.assertTrue(data["goals_met"])
        self.assertFalse(data["continue"])

    def test_review_score_range_without_threshold(self):
        self.run_state("init")
        self.git("commit", "--allow-empty", "-q", "-m", "base")
        self.run_state("begin-round", "--title", "t", "--reason", "r")
        result = self.run_state("complete-round", "--summary", "s", "--review-score", "99")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("between 1 and 5", result.stderr)
        state = self.read_json("state.json")
        self.assertEqual(state["history"], [])

    def test_depends_on_warns_forward_ref_and_bans_self(self):
        self.run_state("init")
        # Forward reference: allowed, but loudly warned (it blocks until the
        # target exists and completes).
        result = self.run_state("backlog-add", "--title", "t", "--depends-on", "candidate-099")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("unknown candidate", result.stderr)
        self.assertTrue((self.repo / ".autopilot" / "backlog.json").exists())
        self.run_state("backlog-add", "--title", "c1")
        result = self.run_state("backlog-update", "--id", "candidate-001", "--depends-on", "candidate-001")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot depend on itself", result.stderr)

    def test_init_rejects_negative_knobs(self):
        """Negative init knobs used to fake success (config load failed later)
        or — worse — silently stop the loop on the first check
        (max_blocked_in_a_row: 0 >= -1)."""
        for knob in ("--max-rounds", "--max-tokens", "--max-round-scope",
                     "--retries-per-round", "--max-blocked-in-a-row", "--max-minutes"):
            result = self.run_state("init", knob, "-5")
            self.assertNotEqual(result.returncode, 0, knob)
            self.assertIn("must be a non-negative integer", result.stderr)
            self.assertNotIn("Traceback", result.stderr)
        result = self.run_state("init", "--max-rounds", "3")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_goal_mismatch_warns(self):
        self.run_state("init", "--goal", "real goal")
        result = self.run_state("goal-met", "--goal", "totally different")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("does not match any configured goal", result.stderr)

    def test_report_tables_survive_hostile_titles(self):
        self.run_state("init")
        self.run_state("backlog-add", "--title", "line1\nline2|PIPE\tTAB", "--value", "3", "--effort", "1")
        self.run_state("goal-met", "--goal", "G", "--next-step", "stepA\nstepB|X")
        output = self.run_state("report").stdout
        self.assertNotIn("line1\nline2", output)
        self.assertNotIn("stepA\nstepB", output)
        self.assertIn("line1 line2\\|PIPE TAB", output)
        self.assertIn("stepA stepB\\|X", output)

    def test_guard_backslash_deny_matches(self):
        # A Windows-style deny rule used to silently never match (fail-open).
        self.assertFalse(path_allowed("secrets/prod/keys.txt", [], ["secrets\\"]))
        self.assertFalse(path_allowed("secrets/prod/keys.txt", [], ["secrets/"]))

    def test_guard_globstar_matches_zero_directories(self):
        self.assertFalse(path_allowed("src/a.py", [], ["src/**/*.py"]))
        self.assertFalse(path_allowed("src/deep/b.py", [], ["src/**/*.py"]))
        self.assertTrue(path_allowed("src/a.py", ["src/**/*.py"], []))
        # Bare "*.log" matches by basename under subdirectories.
        self.assertFalse(path_allowed("notes/a.log", [], ["*.log"]))
        self.assertTrue(path_allowed("notes/a.log", ["*.log"], []))

    def test_secret_patterns_pkcs8_and_github_variants(self):
        self.assertTrue(_secret_hit("-----BEGIN ENCRYPTED PRIVATE KEY-----"))
        self.assertTrue(_secret_hit("-----BEGIN OPENSSH PRIVATE KEY-----"))
        self.assertTrue(_secret_hit("token: gho_" + "a" * 36))
        self.assertTrue(_secret_hit("token: ghs_" + "b" * 36))
        self.assertTrue(_secret_hit("key = " + "sk-proj-" + "c" * 30))

    def test_secret_sk_pattern_word_boundary(self):
        self.assertFalse(_secret_hit("the task-runner-configuration-for-nightly job"))
        self.assertFalse(_secret_hit("disk-utility-backup-script-v2 archive"))
        self.assertTrue(_secret_hit('"sk-live-abcdefghijklmnopqrst"'))

    def test_version_consistency_across_files(self):
        """Single version authority (scripts/autopilot/__init__.py __version__)
        must match SKILL.md frontmatter, agents/openai.yaml, references/overview.md,
        and the newest CHANGELOG section — drift fails here instead of at release
        time. (Root README.md also carries the version header since the owner
        reinstated it as the repo face on 2026-09-26.)"""
        import autopilot

        repo_root = Path(__file__).resolve().parent.parent
        version = autopilot.__version__
        skill = (repo_root / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("version: {}".format(version), skill)
        yaml_text = (repo_root / "agents" / "openai.yaml").read_text(encoding="utf-8")
        self.assertIn("version: {}".format(version), yaml_text)
        overview = (repo_root / "references" / "overview.md").read_text(encoding="utf-8")
        self.assertIn("Version {}".format(version), overview)
        changelog = (repo_root / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIn("## {} (".format(version), changelog)
        readme = (repo_root / "README.md").read_text(encoding="utf-8")
        self.assertIn("Version {}".format(version), readme)

    def test_parse_deadline_overflow_returns_none(self):
        from autopilot import io as ap_io
        self.assertIsNone(ap_io.parse_deadline("+" + "9" * 30 + "w"))
        self.assertIsNone(ap_io.parse_deadline("+not-a-duration"))
        self.assertIsNotNone(ap_io.parse_deadline("+1h"))

    def test_git_push_prefers_origin_over_alphabetical_first(self):
        """Multi-remote repos (fork + origin, standard contribution setup) must
        not push to the alphabetically-first remote when no upstream is set."""
        origin = Path(self.tmp) / "origin.git"
        fork = Path(self.tmp) / "a-fork.git"  # sorts before "origin"
        for remote in (origin, fork):
            self.git("init", "-q", "--bare", str(remote))
        self.git("remote", "add", "fork", str(fork))
        self.git("remote", "add", "origin", str(origin))
        self.add_file("f.py")
        self.git("commit", "-q", "-m", "x")
        branch = (self.repo / ".git" / "HEAD").read_text(encoding="utf-8").strip().split("/")[-1]
        output, err = ap_io.git_push(self.repo)
        self.assertIsNone(err, err)
        fork_heads = self.git("ls-remote", str(fork), "refs/heads/" + branch).stdout.strip()
        origin_heads = self.git("ls-remote", str(origin), "refs/heads/" + branch).stdout.strip()
        self.assertNotEqual(origin_heads, "", "origin (preferred) must receive the push")
        self.assertEqual(fork_heads, "", "the alphabetically-first fork must be skipped")

    def test_directive_list_shows_index(self):
        self.run_state("init")
        self.run_state("directive-add", "--text", "rule A")
        self.run_state("directive-add", "--text", "rule B")
        data = json.loads(self.run_state("directive-list").stdout)
        self.assertEqual([d["index"] for d in data["directives"]], [1, 2])
        # The remove error message points at directive-list: keep them in sync.
        result = self.run_state("directive-remove", "--index", "9")
        self.assertIn("as shown by directive-list", result.stderr)

    def test_invalid_override_warns(self):
        env = dict(self.env)
        for var in ("OPENCODE", "CLAUDE_CODE", "CODEX", "SKILL_DIR"):
            env.pop(var, None)
        env["AUTOPILOT_AGENT"] = "ClaudeCode"
        result = subprocess.run(
            [sys.executable, str(self.script), "detect-agent", "--repo", str(self.repo), "--home", str(self.tmp)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("not one of", result.stderr)
        self.assertIn("generic", result.stderr)

    def test_directive_remove_by_index(self):
        self.run_state("init")
        self.run_state("directive-add", "--text", "rule one")
        self.run_state("directive-add", "--text", "rule two")
        result = self.run_state("directive-remove", "--index", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("rule one", result.stdout)
        directives = json.loads(self.run_state("directive-list").stdout)
        self.assertEqual([d["text"] for d in directives["directives"]], ["rule two"])
        result = self.run_state("directive-remove", "--index", "5")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("between 1 and 1", result.stderr)

    def test_state_migration_is_logged(self):
        self.run_state("init")
        state_path = self.repo / ".autopilot" / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["schema"] = 5
        state.pop("goal_seeds", None)
        state_path.write_text(json.dumps(state), encoding="utf-8")
        self.run_state("read")
        disk = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(disk["schema"], 6)
        log = (self.repo / ".autopilot" / "log.jsonl").read_text(encoding="utf-8")
        self.assertIn("state-migrate", log)

    def test_real_subprocess_exit_code_and_utf8(self):
        """Contract for the real entry point that in-process runs can't cover:
        process exit code, and UTF-8 stdio without PYTHONIOENCODING (the CLI
        wraps its own streams)."""
        self.run_state("init")
        self.run_state("goal-met", "--goal", "G", "--next-step", "seed \U0001f680 title")
        env = dict(self.env)
        env.pop("PYTHONIOENCODING", None)
        result = subprocess.run(
            [sys.executable, str(self.script), "check", "--brief", "--repo", str(self.repo)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, encoding="utf-8", errors="replace",
            env=env, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        result = subprocess.run(
            [sys.executable, str(self.script), "check", "--repo", str(self.repo.parent)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, encoding="utf-8", errors="replace",
            env=env, timeout=60,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Traceback", result.stderr)

    def test_corrupt_directives_clean_error(self):
        self.run_state("init")
        path = self.repo / ".autopilot" / "directives.json"
        path.write_text("[]", encoding="utf-8")
        for command, args in (("directive-add", ("--text", "t")),
                              ("directive-list", ()),
                              ("directive-remove", ("--index", "1"))):
            result = self.run_state(command, *args)
            self.assertNotEqual(result.returncode, 0, command)
            self.assertNotIn("Traceback", result.stderr)
            self.assertIn("directives.json", result.stderr)

    def test_current_round_missing_round_key_clean_error(self):
        self.run_state("init")
        state_path = self.repo / ".autopilot" / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["current_round"] = {"title": "broken"}
        state_path.write_text(json.dumps(state), encoding="utf-8")
        for command, args in (("complete-round", ("--summary", "s")),
                              ("block-round", ("--reason", "r")),
                              ("cancel-round", ("--reason", "r"))):
            result = self.run_state(command, *args)
            self.assertNotEqual(result.returncode, 0, command)
            self.assertNotIn("Traceback", result.stderr)
            self.assertIn("current_round.round", result.stderr)

    def test_history_junk_clean_error(self):
        self.run_state("init")
        state_path = self.repo / ".autopilot" / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["history"] = ["junk"]
        state_path.write_text(json.dumps(state), encoding="utf-8")
        result = self.run_state("check", "--brief")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("history", result.stderr)

    def test_init_rejects_negative_max_predicted(self):
        result = self.run_state("init", "--max-predicted-per-round", "-1")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Traceback", result.stderr)
        result = self.run_state("init", "--max-predicted-per-round", "0")
        self.assertEqual(result.returncode, 0, result.stderr)
        cfg = json.loads((self.repo / ".autopilot" / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(cfg["max_predicted_per_round"], 0)

    def test_seed_field_junk_falls_back(self):
        self.run_state("init")
        state_path = self.repo / ".autopilot" / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["goal_seeds"] = [{"id": "seed-001", "status": "open", "title": "junk",
                                "type": "refactor", "value": "4", "effort": "oops", "risk": "2"}]
        state_path.write_text(json.dumps(state), encoding="utf-8")
        result = self.run_state("backlog-add", "--from-seed", "seed-001")
        self.assertEqual(result.returncode, 0, result.stderr)
        backlog = self.read_json("backlog.json")
        candidate = backlog["candidates"][0]
        self.assertEqual(candidate["value"], 4)   # "4" coerced
        self.assertEqual(candidate["effort"], 3)  # junk -> legacy default
        self.assertEqual(candidate["risk"], 2)    # "2" coerced


    def test_undo_round_merge_commit_points_at_mainline(self):
        self.run_state("init")
        self.git("checkout", "-q", "-b", "side")
        self.add_file("conflict.txt", "side\n")
        self.git("commit", "-q", "-m", "side change")
        self.git("checkout", "-q", self.initial_branch)
        self.add_file("other.txt", "main\n")
        self.git("commit", "-q", "-m", "main change")
        self.git("merge", "-q", "--no-edit", "side")
        merge_sha = self.git("rev-parse", "HEAD").stdout.strip()
        result = self.run_state("undo-round", "--sha", merge_sha)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("merge commit", result.stderr)
        self.assertIn("-m 1", result.stderr)
        state = self.read_json("state.json")
        self.assertEqual(state["reverted_rounds"], 0)

    def test_goal_met_seed_value_effort_boundaries(self):
        self.run_state("init", "--goal", "G", "--expand-after-goals", "--max-rounds", "50")
        for bad in ("0", "6"):
            result = self.run_state("goal-met", "--goal", "G", "--next-step", "N", "--seed-value", bad)
            self.assertNotEqual(result.returncode, 0, bad)
            self.assertIn("must be between 1 and 5", result.stderr)
        result = self.run_state("goal-met", "--goal", "G", "--next-step", "N",
                                "--seed-value", "1", "--seed-effort", "5", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        state = self.read_json("state.json")
        self.assertEqual(state["goal_seeds"][0]["value"], 1)
        self.assertEqual(state["goal_seeds"][0]["effort"], 5)
        result = self.run_state("goal-met", "--goal", "G", "--next-step", "N2", "--seed-type", "bogus")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("bugfix|feature|refactor|perf|test|docs", result.stderr)

    def test_push_no_remote_json_contract(self):
        self.run_state("init", "--push")
        result = self.run_state("push", "--json")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "", "JSON mode must keep errors on stdout")
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertIn("No git remote is configured", payload["message"])
        log = (self.repo / ".autopilot" / "log.jsonl").read_text(encoding="utf-8")
        self.assertIn('"event": "push"', log)

    def test_report_relative_output_resolves_against_repo(self):
        self.run_state("init")
        outside_cwd = Path(self.tmp) / "elsewhere"
        outside_cwd.mkdir()
        result = subprocess.run(
            [sys.executable, str(self.script), "report", "--repo", str(self.repo),
             "--output", "out.md"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, encoding="utf-8", errors="replace",
            env=self.env, cwd=str(outside_cwd),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.repo / "out.md").exists())
        self.assertFalse((outside_cwd / "out.md").exists())

    def test_dry_run_mutates_nothing(self):
        """Table-driven zero-mutation scan over the dry-run branches the matrix
        found untested: state bytes, backlog bytes, directive bytes, and git
        HEAD must be identical before and after."""
        self.run_state("init", "--push")
        self.git("commit", "--allow-empty", "-q", "-m", "base")
        self.run_state("directive-add", "--text", "rule")
        self.run_state("backlog-add", "--title", "seeded", "--value", "3", "--effort", "1")
        self.run_state("backlog-add", "--title", "work", "--value", "4", "--effort", "2")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "work")
        sha = self.git("rev-parse", "HEAD").stdout.strip()
        self.run_state("begin-round", "--title", "t", "--reason", "r", "--candidate-id", "candidate-002")
        # seed-reject validates the id before its dry-run gate: give it a real seed.
        self.run_state("goal-met", "--goal", "G", "--next-step", "N")

        def snapshot():
            directives_path = self.repo / ".autopilot" / "directives.json"
            return (
                (self.repo / ".autopilot" / "state.json").read_text(encoding="utf-8"),
                (self.repo / ".autopilot" / "backlog.json").read_text(encoding="utf-8"),
                directives_path.read_text(encoding="utf-8") if directives_path.exists() else "",
                self.git("rev-parse", "HEAD").stdout,
            )

        # Group A: commands that require an open round (dry-run while open).
        before = snapshot()
        for command, args in (
            ("complete-round", ("--summary", "s")),
            ("block-round", ("--reason", "b")),
            ("cancel-round", ("--reason", "c")),
        ):
            result = self.run_state(command, *args, "--dry-run")
            self.assertEqual(result.returncode, 0, (command, result.stderr))
            self.assertIn("[DRY-RUN]", result.stderr, command)
            self.assertEqual(snapshot(), before, "dry-run mutated state: " + command)
        # Close the round for group B (undo-round refuses while one is open).
        self.run_state("cancel-round", "--reason", "close group A")
        self.run_state("goal-met", "--goal", "G", "--next-step", "N")
        before = snapshot()
        for command, args in (
            ("goal-met", ("--goal", "G", "--next-step", "N")),
            ("seed-reject", ("--id", "seed-001", "--reason", "x")),
            ("finish", ()),
            ("undo-round", ("--sha", sha)),
            ("analysis-save", ("--content", "{}")),
            ("directive-remove", ("--index", "1")),
            ("backlog-update", ("--id", "candidate-001", "--value", "5")),
            ("ensure-branch", ()),
            ("push", ()),
        ):
            result = self.run_state(command, *args, "--dry-run")
            self.assertEqual(result.returncode, 0, (command, result.stderr))
            self.assertIn("[DRY-RUN]", result.stderr, command)
            self.assertEqual(snapshot(), before, "dry-run mutated state: " + command)
        # finish --dry-run must not set finished_at (covered by the snapshot),
        # and undo-round --dry-run must leave no REVERT_HEAD litter.
        self.assertFalse((self.repo / ".git" / "REVERT_HEAD").exists())


    def test_uninitialized_guard_matrix(self):
        """Two message families on an uninitialized repo: explicit guards say
        'not initialized'; load_state-based commands say 'state.json not
        found'. Both exit 2 with no traceback — drift between them fails here."""
        explicit = ["directive-list", "secret-scan", "retrospective", "analysis-load"]
        for command in explicit:
            result = self.run_state(command)
            self.assertNotEqual(result.returncode, 0, command)
            self.assertNotIn("Traceback", result.stderr, command)
            self.assertIn("not initialized", result.stderr, command)
        loaders = ["read", ("goal-met", ("--goal", "G")), ("seed-reject", ("--id", "seed-001", "--reason", "r")),
                   ("report", ())]
        for item in loaders:
            command, args = item if isinstance(item, tuple) else (item, ())
            result = self.run_state(command, *args)
            self.assertNotEqual(result.returncode, 0, command)
            self.assertNotIn("Traceback", result.stderr, command)
            self.assertIn("state.json not found", result.stderr, command)

    def test_commit_identity_gate_json(self):
        self.run_state("init")
        self.run_state("begin-round", "--title", "t", "--reason", "r")
        self.add_file("f.py")
        self.git("config", "--local", "--unset", "user.name")
        self.git("config", "--local", "--unset", "user.email")
        result = self.run_state("commit", "--summary", "s", "--json")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertIn("Git identity is not configured", payload["message"])

    def test_complete_round_push_fail_warns_but_succeeds(self):
        self.run_state("init", "--push")
        missing = Path(self.tmp) / "no-such-origin.git"
        self.git("remote", "add", "origin", str(missing))
        self.run_state("backlog-add", "--title", "t", "--value", "3", "--effort", "1")
        self.run_state("begin-round", "--title", "t", "--reason", "r", "--candidate-id", "candidate-001")
        self.add_file("f.py")
        self.git("add", "-A")
        r = self.run_state("commit", "--summary", "s")
        sha = [l.split()[2].rstrip(":") for l in r.stdout.splitlines() if l.startswith("[OK] Committed ")][0]
        result = self.run_state("complete-round", "--summary", "done", "--commit-sha", sha)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("push failed", result.stdout + result.stderr)
        state = self.read_json("state.json")
        self.assertEqual(state["history"][-1]["status"], "completed")

    def test_agent_empty_env_signal_counts_as_unset(self):
        env = dict(self.env)
        for var in ("OPENCODE", "CLAUDE_CODE", "CODEX", "SKILL_DIR", "AUTOPILOT_AGENT"):
            env.pop(var, None)
        env["OPENCODE"] = ""
        result = subprocess.run(
            [sys.executable, str(self.script), "detect-agent", "--repo", str(self.repo), "--home", str(self.tmp)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, encoding="utf-8", errors="replace", env=env,
        )
        data = json.loads(result.stdout)
        self.assertEqual(data["agent"], "generic")
        self.assertEqual(data["detected_by"], "fallback")

    def test_commit_round_zero_and_negative_rejected(self):
        self.run_state("init")
        self.run_state("begin-round", "--title", "t", "--reason", "r")
        self.run_state("complete-round", "--summary", "done")
        self.add_file("f.py")
        self.git("add", "-A")
        for bad in ("0", "-5"):
            result = self.run_state("commit", "--round", bad, "--summary", "s")
            self.assertNotEqual(result.returncode, 0, bad)
            self.assertIn("positive integer", result.stderr)
        # --round must reference a completed round in history (any orphan
        # number used to be accepted).
        result = self.run_state("commit", "--round", "2", "--summary", "s")
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("no completed round", result.stderr)
        result = self.run_state("commit", "--round", "1", "--summary", "s")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_negative_tokens_rejected(self):
        self.run_state("init")
        self.git("commit", "--allow-empty", "-q", "-m", "base")
        self.run_state("begin-round", "--title", "t", "--reason", "r")
        result = self.run_state("complete-round", "--summary", "s", "--tokens", "-5")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("non-negative", result.stderr)

    def test_detect_verify_json_shape_stable_without_apply(self):
        (self.repo / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
        result = self.run_state("detect-verify", "--json")
        data = json.loads(result.stdout)
        self.assertTrue(data["ok"])
        self.assertIn("message", data)
        self.assertIn("detected", data)


class DirectionSeedTests(RepoTest):
    """Post-goal direction prediction: goal events + direction seeds live in
    state.json; completed_goals stays a plain string array (schema v6)."""

    def test_v5_state_migrates_with_seed_fields(self):
        self.run_state("init")
        state_path = self.repo / ".autopilot" / "state.json"
        old = json.loads(state_path.read_text(encoding="utf-8"))
        old["schema"] = 5
        old.pop("goal_events", None)
        old.pop("goal_seeds", None)
        old["goals"] = ["ship it"]
        old["completed_goals"] = ["ship it"]
        state_path.write_text(json.dumps(old), encoding="utf-8")
        data = json.loads(self.run_state("read").stdout)
        self.assertEqual(data["schema"], 6)
        self.assertEqual(data["goal_events"], [])
        self.assertEqual(data["goal_seeds"], [])
        self.assertEqual(data["completed_goals"], ["ship it"])

    def test_goal_met_creates_event_and_seeds(self):
        self.run_state("init")
        result = self.run_state(
            "goal-met", "--goal", "export module works",
            "--next-step", "wire export into CLI",
            "--next-step", "document export usage",
            "--unlocked-capability", "export module importable",
            "--json",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertIn("goal_event", payload)
        self.assertIn("seeds", payload)
        self.assertEqual(len(payload["seeds"]), 2)
        state = self.read_json("state.json")
        # completed_goals stays a plain string array (invariant).
        self.assertEqual(state["completed_goals"], ["export module works"])
        self.assertEqual(len(state["goal_events"]), 1)
        event = state["goal_events"][0]
        self.assertEqual(event["goal"], "export module works")
        self.assertEqual(event["unlocked_capabilities"], ["export module importable"])
        self.assertEqual(event["seed_ids"], [payload["seeds"][0]["id"], payload["seeds"][1]["id"]])
        self.assertEqual(len(state["goal_seeds"]), 2)
        for seed in state["goal_seeds"]:
            self.assertEqual(seed["status"], "open")
            self.assertEqual(seed["source_goal"], "export module works")
            self.assertEqual(seed["value"], 4)
            self.assertEqual(seed["effort"], 2)
            self.assertIn("hypothesis", seed)

    def test_goal_met_plain_call_stays_compatible(self):
        self.run_state("init")
        self.run_state("goal-met", "--goal", "G")
        state = self.read_json("state.json")
        self.assertEqual(state["completed_goals"], ["G"])
        # Plain call still records the structured goal event (auto context),
        # but creates no seeds without --next-step.
        self.assertEqual(len(state["goal_events"]), 1)
        self.assertEqual(state["goal_events"][0]["seed_ids"], [])
        self.assertEqual(state["goal_seeds"], [])

    def test_goal_met_no_auto_context_skips_event(self):
        self.run_state("init")
        self.run_state("goal-met", "--goal", "G", "--no-auto-context")
        state = self.read_json("state.json")
        self.assertEqual(state["completed_goals"], ["G"])
        self.assertEqual(state["goal_events"], [])
        self.assertEqual(state["goal_seeds"], [])

    def test_seed_type_avoids_saturated_types(self):
        self.run_state("init")
        state_path = self.repo / ".autopilot" / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["type_stats"] = {"feature": {"completed": 5}, "docs": {"completed": 0}}
        state_path.write_text(json.dumps(state), encoding="utf-8")
        self.run_state("goal-met", "--goal", "G", "--next-step", "N")
        state = self.read_json("state.json")
        self.assertNotEqual(state["goal_seeds"][0]["type"], "feature")

    def test_backlog_add_from_seed_promotes(self):
        self.run_state("init")
        self.run_state("goal-met", "--goal", "G", "--next-step", "wire CLI", "--seed-type", "feature")
        state = self.read_json("state.json")
        seed_id = state["goal_seeds"][0]["id"]
        result = self.run_state("backlog-add", "--from-seed", seed_id, "--reason", "r")
        self.assertEqual(result.returncode, 0, result.stderr)
        candidate_id = result.stdout.strip().splitlines()[-1]
        backlog = self.read_json("backlog.json")
        candidate = [c for c in backlog["candidates"] if c["id"] == candidate_id][0]
        self.assertEqual(candidate["from_seed"], seed_id)
        self.assertEqual(candidate["title"], "wire CLI")
        self.assertEqual(candidate["type"], "feature")
        self.assertEqual(candidate["value"], 4)
        state = self.read_json("state.json")
        seed = [s for s in state["goal_seeds"] if s["id"] == seed_id][0]
        self.assertEqual(seed["status"], "promoted")
        self.assertEqual(seed["promoted_candidate_id"], candidate_id)

    def test_backlog_add_from_seed_requires_open(self):
        self.run_state("init")
        self.run_state("goal-met", "--goal", "G", "--next-step", "N")
        seed_id = self.read_json("state.json")["goal_seeds"][0]["id"]
        # First promote consumes the seed.
        self.run_state("backlog-add", "--from-seed", seed_id)
        result = self.run_state("backlog-add", "--from-seed", seed_id)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("only 'open' seeds", result.stderr)

    def test_complete_round_verifies_seed(self):
        self.run_state("init")
        self.run_state("goal-met", "--goal", "G", "--next-step", "N")
        seed_id = self.read_json("state.json")["goal_seeds"][0]["id"]
        cid = self.run_state("backlog-add", "--from-seed", seed_id).stdout.strip().splitlines()[-1]
        self.git("commit", "--allow-empty", "-q", "-m", "base")
        self.run_state("begin-round", "--title", "t", "--reason", "r", "--candidate-id", cid)
        self.run_state("complete-round", "--summary", "done")
        state = self.read_json("state.json")
        seed = [s for s in state["goal_seeds"] if s["id"] == seed_id][0]
        self.assertEqual(seed["status"], "verified")
        self.assertIn("verified_at", seed)

    def test_block_round_refutes_seed_with_notes(self):
        self.run_state("init")
        self.run_state("goal-met", "--goal", "G", "--next-step", "N")
        seed_id = self.read_json("state.json")["goal_seeds"][0]["id"]
        cid = self.run_state("backlog-add", "--from-seed", seed_id).stdout.strip().splitlines()[-1]
        self.git("commit", "--allow-empty", "-q", "-m", "base")
        self.run_state("begin-round", "--title", "t", "--reason", "r", "--candidate-id", cid)
        self.run_state("block-round", "--reason", "assumption wrong: no entry point")
        state = self.read_json("state.json")
        seed = [s for s in state["goal_seeds"] if s["id"] == seed_id][0]
        self.assertEqual(seed["status"], "refuted")
        self.assertIn("assumption wrong", seed["outcome"])

    def test_cancel_round_returns_seed_to_open(self):
        self.run_state("init")
        self.run_state("goal-met", "--goal", "G", "--next-step", "N")
        seed_id = self.read_json("state.json")["goal_seeds"][0]["id"]
        cid = self.run_state("backlog-add", "--from-seed", seed_id).stdout.strip().splitlines()[-1]
        self.git("commit", "--allow-empty", "-q", "-m", "base")
        self.run_state("begin-round", "--title", "t", "--reason", "r", "--candidate-id", cid)
        self.run_state("cancel-round", "--reason", "reprioritized")
        state = self.read_json("state.json")
        seed = [s for s in state["goal_seeds"] if s["id"] == seed_id][0]
        self.assertEqual(seed["status"], "open")
        self.assertNotIn("promoted_at", seed)

    def test_seed_lists_bounded_and_text_capped(self):
        self.run_state("init")
        state_path = self.repo / ".autopilot" / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["goal_seeds"] = [
            {"id": "seed-{:03d}".format(i), "status": "refuted", "title": "t{}".format(i)}
            for i in range(1, 51)
        ]
        state_path.write_text(json.dumps(state), encoding="utf-8")
        long_title = "x" * 2000
        self.run_state("goal-met", "--goal", "G", "--next-step", long_title)
        state = self.read_json("state.json")
        self.assertLessEqual(len(state["goal_seeds"]), 50)
        newest = [s for s in state["goal_seeds"] if s["title"].startswith("x")]
        self.assertEqual(len(newest), 1)
        self.assertLessEqual(len(newest[0]["title"]), 500)


class MigrationTests(RepoTest):
    def test_migration_persists_fields_on_read(self):
        self.run_state("init")
        state_path = self.repo / ".autopilot" / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["schema"], 6)
        state.pop("estimated_tokens_used", None)
        state_path.write_text(json.dumps(state), encoding="utf-8")
        self.run_state("read")
        disk = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertIn("estimated_tokens_used", disk)

    def test_migrated_state_anchors_tokens_at_load_not_empty_tree(self):
        self.run_state("init")
        state_path = self.repo / ".autopilot" / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state.pop("run_start_sha", None)
        state_path.write_text(json.dumps(state), encoding="utf-8")
        self.run_state("read")
        anchor = json.loads(state_path.read_text(encoding="utf-8"))["run_start_sha"]
        head = self.git("rev-parse", "HEAD").stdout.strip()
        self.assertEqual(anchor, head)
        # Anchored at HEAD: the pre-upgrade commit history must not be billed
        # (EMPTY_TREE would bill the whole repo and fake-trigger max_tokens).
        _, total_text, total_binary = ap_io.estimate_tokens_for_round(self.repo, anchor, 0, 0)
        self.assertEqual((total_text, total_binary), (0, 0))


class TokenEstimateTests(RepoTest):
    def test_estimate_anchors_on_round_start(self):
        self.run_state("init")
        self.run_state("begin-round", "--title", "r1", "--reason", "x")
        self.add_file("a.py", "1\n2\n3\n")
        self.run_state("commit", "--summary", "a")
        sha = self.git("rev-parse", "HEAD").stdout.strip()
        self.run_state("complete-round", "--summary", "a", "--commit-sha", sha)
        after_r1 = self.read_json("state.json")["estimated_tokens_used"]

        self.run_state("begin-round", "--title", "r2", "--reason", "x")
        self.add_file("b.py", "4\n5\n")
        self.run_state("commit", "--summary", "b")
        sha = self.git("rev-parse", "HEAD").stdout.strip()
        self.run_state("complete-round", "--summary", "b", "--commit-sha", sha)
        after_r2 = self.read_json("state.json")["estimated_tokens_used"]

        round2 = after_r2 - after_r1
        self.assertGreaterEqual(round2, 500)
        self.assertLess(round2, 550)


class BacklogManageTests(RepoTest):
    def test_backlog_update_and_remove(self):
        self.run_state("init")
        self.run_state("backlog-add", "--title", "T", "--reason", "r", "--value", "3", "--effort", "3")
        cid = self.read_json("backlog.json")["candidates"][0]["id"]
        result = self.run_state("backlog-update", "--id", cid, "--value", "5", "--effort", "1", "--title", "T2")
        self.assertEqual(result.returncode, 0, result.stderr)
        c = self.read_json("backlog.json")["candidates"][0]
        self.assertEqual(c["value"], 5)
        self.assertEqual(c["effort"], 1)
        self.assertEqual(c["title"], "T2")
        result = self.run_state("backlog-remove", "--id", cid)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.read_json("backlog.json")["candidates"], [])

    def test_backlog_update_validation(self):
        self.run_state("init")
        self.run_state("backlog-add", "--title", "T", "--reason", "r")
        cid = self.read_json("backlog.json")["candidates"][0]["id"]
        result = self.run_state("backlog-update", "--id", cid, "--value", "9")
        self.assertNotEqual(result.returncode, 0)

    def test_backlog_update_status(self):
        self.run_state("init")
        self.run_state("backlog-add", "--title", "T", "--reason", "r")
        cid = self.read_json("backlog.json")["candidates"][0]["id"]
        result = self.run_state("backlog-update", "--id", cid, "--status", "blocked")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.read_json("backlog.json")["candidates"][0]["status"], "blocked")

    def test_backlog_list_requires_init(self):
        result = self.run_state("backlog-list")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not initialized", result.stderr.lower())


class AuditLogTests(RepoTest):
    def test_log_records_events(self):
        self.run_state("init")
        self.run_state("backlog-add", "--title", "T", "--reason", "r")
        self.run_state("begin-round", "--title", "r", "--reason", "x")
        log_path = self.repo / ".autopilot" / "log.jsonl"
        self.assertTrue(log_path.exists())
        events = [
            json.loads(line)
            for line in log_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(events[0]["event"], "init")
        self.assertIn("backlog-add", [e["event"] for e in events])
        self.assertIn("begin-round", [e["event"] for e in events])


class JsonOutputTests(RepoTest):
    def test_json_output_shape(self):
        self.run_state("init")
        result = self.run_state("begin-round", "--title", "r", "--reason", "x", "--json")
        data = json.loads(result.stdout)
        self.assertTrue(data["ok"])
        self.assertIn("message", data)
        result = self.run_state("cancel-round", "--json")
        data = json.loads(result.stdout)
        self.assertTrue(data["ok"])
        result = self.run_state("cancel-round", "--json")
        data = json.loads(result.stdout)
        self.assertFalse(data["ok"])


class ForceInitTests(RepoTest):
    def test_init_force_reinitializes(self):
        self.run_state("init")
        first = self.read_json("state.json")["run_id"]
        result = self.run_state("init", "--force")
        self.assertEqual(result.returncode, 0, result.stderr)
        second = self.read_json("state.json")["run_id"]
        self.assertNotEqual(first, second)


class DetectAgentTests(unittest.TestCase):
    script = SCRIPT

    def _clean_env(self):
        env = {k: v for k, v in os.environ.items() if k not in ("OPENCODE", "CLAUDE_CODE", "CODEX", "AUTOPILOT_AGENT", "SKILL_DIR")}
        env["PYTHONIOENCODING"] = "utf-8"
        return env

    def _run(self, env, *extra):
        return subprocess.run(
            [sys.executable, str(self.script), "detect-agent"] + list(extra),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )

    def _payload(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_env_signal_opencode(self):
        env = self._clean_env()
        env["OPENCODE"] = "1"
        data = self._payload(self._run(env))
        self.assertEqual(data["agent"], "opencode")
        self.assertIn("env:OPENCODE", data["detected_by"])

    def test_env_signal_claude_code(self):
        env = self._clean_env()
        env["CLAUDE_CODE"] = "1"
        data = self._payload(self._run(env))
        self.assertEqual(data["agent"], "claude-code")
        self.assertEqual(data["shell"], "bash")

    def test_env_signal_codex(self):
        env = self._clean_env()
        env["CODEX"] = "1"
        data = self._payload(self._run(env))
        self.assertEqual(data["agent"], "codex")
        self.assertEqual(data["shell"], "bash")

    def test_override_env_wins(self):
        env = self._clean_env()
        env["OPENCODE"] = "1"
        env["AUTOPILOT_AGENT"] = "codex"
        data = self._payload(self._run(env))
        self.assertEqual(data["agent"], "codex")

    def test_cwd_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp) / "proj"
            proj.mkdir()
            (proj / "CLAUDE.md").write_text("# x\n", encoding="utf-8")
            env = self._clean_env()
            data = self._payload(self._run(env, "--repo", str(proj)))
            self.assertEqual(data["agent"], "claude-code")

    def test_home_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            (home / "AGENTS.md").write_text("# x\n", encoding="utf-8")
            env = self._clean_env()
            data = self._payload(self._run(env, "--home", str(home)))
            self.assertEqual(data["agent"], "codex")

    def test_fallback_generic(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "empty"
            empty.mkdir()
            env = self._clean_env()
            data = self._payload(self._run(env, "--repo", str(empty), "--home", str(empty)))
            self.assertEqual(data["agent"], "generic")

    def test_python_cmd_present(self):
        env = self._clean_env()
        env["OPENCODE"] = "1"
        data = self._payload(self._run(env))
        self.assertIn(data["python_cmd"], ("python", "python3", "py"))

    def test_skill_dir_env_override(self):
        env = self._clean_env()
        env["OPENCODE"] = "1"
        env["SKILL_DIR"] = "C:\\skills\\custom"
        data = self._payload(self._run(env))
        self.assertEqual(data["skill_dir"], "C:\\skills\\custom")

    def test_cwd_opencode_subdir_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp) / "proj"
            proj.mkdir()
            (proj / ".opencode").mkdir()
            (proj / ".opencode" / "opencode.json").write_text("{}", encoding="utf-8")
            env = self._clean_env()
            data = self._payload(self._run(env, "--repo", str(proj)))
            self.assertEqual(data["agent"], "opencode")

    def test_invalid_override_falls_back_to_generic(self):
        env = self._clean_env()
        env["AUTOPILOT_AGENT"] = "bogus"
        data = self._payload(self._run(env))
        self.assertEqual(data["agent"], "generic")


class PureHelperUnitTests(unittest.TestCase):
    """Direct unit tests for pure helpers (import the module, no subprocess/git setup)."""

    script = SCRIPT

    @classmethod
    def setUpClass(cls):
        import sys as _sys
        if str(SCRIPT.parent) not in _sys.path:
            _sys.path.insert(0, str(SCRIPT.parent))
        import autopilot_state as ap
        cls.ap = ap

    def test_parse_time_handles_colon_offset(self):
        ap = self.ap
        self.assertIsNotNone(ap.parse_time("2020-01-01T00:00:00+00:00"))
        self.assertIsNotNone(ap.parse_time("2020-01-01T00:00:00+0000"))
        self.assertIsNotNone(ap.parse_time("2020-01-01T00:00:00Z"))
        self.assertIsNotNone(ap.parse_time("2020-01-01T00:00:00+08:00"))
        self.assertIsNone(ap.parse_time("not-a-time"))
        self.assertIsNone(ap.parse_time(None))

    def test_candidate_score_tolerance(self):
        ap = self.ap
        self.assertEqual(ap._candidate_score({"value": "x", "effort": "y"}), 1.0)
        self.assertEqual(ap._candidate_score({"value": 5, "effort": 0}), 5.0)
        self.assertEqual(ap._candidate_score({"value": 5, "effort": 2}), 2.5)
        self.assertEqual(ap._candidate_score({"impact": "high", "effort": "small"}), 5.0)

    def test_parse_numstat_binary(self):
        ap = self.ap
        text, binary = ap._parse_numstat("1\t2\tfile.py\n3\t4\tfile2.py\n")
        self.assertEqual(text, 10)
        self.assertEqual(binary, 0)
        text, binary = ap._parse_numstat("-\t-\tblob.bin\n-\t-\timg.png\n")
        self.assertEqual(text, 0)
        self.assertEqual(binary, 2)

    def test_compute_stop_reason_max_tokens(self):
        ap = self.ap
        state = {
            "finished_at": None, "stop_reason": None, "goals": [], "completed_goals": [],
            "completed_rounds": 0, "blocked_rounds": 0, "history": [],
            "estimated_tokens_used": 50, "last_activity_at": None, "started_at": None,
        }
        config = {"goals": [], "max_rounds": None, "max_minutes": None, "max_tokens": 10, "max_blocked_in_a_row": None}
        self.assertIn("max_tokens", ap.compute_stop_reason(state, config))

    def test_compute_stop_reason_max_minutes_parses_colon(self):
        ap = self.ap
        state = {
            "finished_at": None, "stop_reason": None, "goals": [], "completed_goals": [],
            "completed_rounds": 0, "blocked_rounds": 0, "history": [],
            "estimated_tokens_used": 0,
            "last_activity_at": "2020-01-01T00:00:00+00:00", "started_at": "2020-01-01T00:00:00+00:00",
        }
        config = {"goals": [], "max_rounds": None, "max_minutes": 1, "max_tokens": None, "max_blocked_in_a_row": None}
        self.assertIn("max_minutes", ap.compute_stop_reason(state, config))

    def test_compute_stop_reason_deadline(self):
        ap = self.ap
        state = {
            "finished_at": None, "stop_reason": None, "goals": [], "completed_goals": [],
            "completed_rounds": 0, "blocked_rounds": 0, "history": [],
            "estimated_tokens_used": 0, "last_activity_at": None, "started_at": None,
        }
        config = {
            "goals": [], "max_rounds": None, "max_minutes": None, "max_tokens": None,
            "max_blocked_in_a_row": None, "deadline": "2000-01-01T00:00:00+00:00",
        }
        self.assertIn("deadline", ap.compute_stop_reason(state, config))
        config["deadline"] = "2999-01-01T00:00:00+00:00"
        self.assertIsNone(ap.compute_stop_reason(state, config))

    def test_all_goals_met_and_expand(self):
        ap = self.ap
        state = {"goals": [], "completed_goals": []}
        config = {"goals": ["A", "B"], "expand_after_goals": False}
        self.assertFalse(ap.all_goals_met(config, state))
        state["completed_goals"] = ["A"]
        self.assertFalse(ap.all_goals_met(config, state))
        state["completed_goals"] = ["A", "B"]
        self.assertTrue(ap.all_goals_met(config, state))
        # expand_after_goals: goals met but the loop keeps running.
        base = {
            "finished_at": None, "stop_reason": None, "goals": ["A", "B"],
            "completed_goals": ["A", "B"], "completed_rounds": 1, "blocked_rounds": 0,
            "history": [], "estimated_tokens_used": 0, "last_activity_at": None,
            "started_at": None,
        }
        cfg = {"goals": ["A", "B"], "max_rounds": None, "max_minutes": None,
               "max_tokens": None, "max_blocked_in_a_row": None, "deadline": None,
               "expand_after_goals": True}
        self.assertIsNone(ap.compute_stop_reason(base, cfg))
        cfg["expand_after_goals"] = False
        self.assertEqual(ap.compute_stop_reason(base, cfg), "all goals met")

    def test_parse_deadline(self):
        ap = self.ap
        self.assertIsNone(ap.parse_deadline(None))
        self.assertIsNone(ap.parse_deadline(""))
        self.assertIsNone(ap.parse_deadline("not-a-time"))
        # Absolute ISO with offset and naive (treated as UTC).
        self.assertIn("2026-08-10T08:00:00", ap.parse_deadline("2026-08-10T08:00:00+08:00"))
        self.assertTrue(ap.parse_deadline("2026-08-10T08:00:00").endswith("+00:00"))
        self.assertTrue(ap.parse_deadline("2026-08-10T08:00:00Z").endswith("+00:00"))
        # Relative durations resolve to a future absolute UTC timestamp.
        self.assertIsNotNone(ap.parse_deadline("+30min"))
        self.assertIsNotNone(ap.parse_deadline("+8h"))
        self.assertIsNotNone(ap.parse_deadline("+1d"))
        self.assertIsNotNone(ap.parse_deadline("+2w"))
        self.assertIsNotNone(ap.parse_deadline("+90minutes"))
        # Wall-clock HH:MM resolves to an absolute timestamp (today or tomorrow).
        self.assertIsNotNone(ap.parse_deadline("08:00"))

    def test_autopilot_path(self):
        ap = self.ap
        self.assertTrue(ap._is_autopilot_path(".autopilot"))
        self.assertTrue(ap._is_autopilot_path(".autopilot/state.json"))
        self.assertTrue(ap._is_autopilot_path(".autopilot\\state.json"))
        self.assertFalse(ap._is_autopilot_path(".autopilothack"))
        self.assertFalse(ap._is_autopilot_path("src/main.py"))

    def test_candidate_adjusted_score_risk(self):
        ap = self.ap
        low, low_bd = ap.candidate_adjusted_score({"value": 5, "effort": 1, "risk": 1})
        high, high_bd = ap.candidate_adjusted_score({"value": 5, "effort": 1, "risk": 5})
        self.assertGreater(low, high)
        self.assertEqual(low_bd["risk_factor"], 1.0)
        self.assertAlmostEqual(high_bd["risk_factor"], 0.68, places=2)

    def test_candidate_adjusted_score_saturation(self):
        ap = self.ap
        score, breakdown = ap.candidate_adjusted_score(
            {"value": 5, "effort": 1, "type": "docs"},
            {"docs": {"completed": 3, "blocked": 0}},
            2,
        )
        # Logarithmic decay, floored at 0.35: one completed over threshold
        # costs 0.12 (1 - 0.12*log2(2) = 0.88), not the old exponential 0.7.
        self.assertAlmostEqual(score, 5.0 * 0.88, places=3)
        self.assertAlmostEqual(breakdown["saturation_factor"], 0.88, places=3)

    def test_candidate_adjusted_score_blocked_history(self):
        ap = self.ap
        score, breakdown = ap.candidate_adjusted_score(
            {"value": 5, "effort": 1, "type": "perf"},
            {"perf": {"completed": 1, "blocked": 2}},
            2,
        )
        # success rate 1/3 from the type's blocked history (2 of 3 attempts blocked)
        self.assertAlmostEqual(breakdown["success_rate"], 1 / 3, places=3)
        self.assertAlmostEqual(score, 5.0 * (1 / 3), places=3)

    def test_expected_value_reverses_ratio_ranking(self):
        """The core behavior fix: a cheap trivial candidate must no longer beat an
        expensive valuable one — rounds are the scarce resource, not effort."""
        ap = self.ap
        backlog = {
            "candidates": [
                {"id": "candidate-001", "title": "Big feature", "status": "pending", "value": 5, "effort": 3},
                {"id": "candidate-002", "title": "Tiny chore", "status": "pending", "value": 2, "effort": 1},
            ]
        }
        ranked = ap.rank_candidates(backlog, {})
        self.assertEqual(ranked[0]["title"], "Big feature")

    def test_rank_candidates_classic_mode_keeps_ratio(self):
        ap = self.ap
        backlog = {
            "candidates": [
                {"id": "candidate-001", "title": "Big feature", "status": "pending", "value": 5, "effort": 3},
                {"id": "candidate-002", "title": "Tiny chore", "status": "pending", "value": 2, "effort": 1},
            ]
        }
        ranked = ap.rank_candidates(backlog, {"ranking_mode": "classic"})
        self.assertEqual(ranked[0]["title"], "Tiny chore")
        self.assertFalse(ranked[0]["below_floor"])
        self.assertNotIn("selected", ranked[0])

    def test_rank_candidates_unlock_bonus(self):
        ap = self.ap
        backlog = {
            "candidates": [
                {"id": "candidate-001", "title": "Standalone", "status": "pending", "value": 3, "effort": 1},
                {"id": "candidate-002", "title": "Foundation", "status": "pending", "value": 3, "effort": 1},
                {"id": "candidate-003", "title": "Dependent", "status": "pending", "value": 3, "effort": 1,
                 "depends_on": ["candidate-002"]},
            ]
        }
        ranked = ap.rank_candidates(backlog, {})
        by_title = {r["title"]: r for r in ranked}
        self.assertEqual(by_title["Foundation"]["unlocks"], 1)
        self.assertGreater(by_title["Foundation"]["score"], by_title["Standalone"]["score"])

    def test_rank_candidates_marks_selected_batch(self):
        ap = self.ap
        backlog = {
            "candidates": [
                {"id": "candidate-001", "title": "DocsA", "status": "pending", "value": 5, "effort": 1, "type": "docs"},
                {"id": "candidate-002", "title": "DocsB", "status": "pending", "value": 5, "effort": 1, "type": "docs"},
                {"id": "candidate-003", "title": "Feature", "status": "pending", "value": 4, "effort": 1, "type": "feature"},
                {"id": "candidate-004", "title": "Quickwin", "status": "pending", "value": 2, "effort": 1, "type": "docs"},
            ]
        }
        cfg = {"candidates_per_round": 3, "max_same_type_per_round": 2, "min_candidate_value": 3}
        ranked = ap.rank_candidates(backlog, cfg)
        by_title = {r["title"]: r for r in ranked}
        self.assertTrue(by_title["DocsA"]["selected"])
        self.assertTrue(by_title["DocsB"]["selected"])
        self.assertTrue(by_title["Feature"]["selected"])
        self.assertFalse(by_title["Quickwin"]["selected"])
        self.assertTrue(by_title["Quickwin"]["below_floor"])
        # tighter quota: only one docs candidate per round -> Feature is pulled in
        # and the below-floor quick-win fills the last slot.
        ranked2 = ap.rank_candidates(backlog, dict(cfg, max_same_type_per_round=1))
        by_title2 = {r["title"]: r for r in ranked2}
        self.assertTrue(by_title2["DocsA"]["selected"])
        self.assertFalse(by_title2["DocsB"]["selected"])
        self.assertTrue(by_title2["Feature"]["selected"])
        self.assertTrue(by_title2["Quickwin"]["selected"])

    def test_compute_type_stats_calibration(self):
        ap = self.ap
        backlog = {
            "candidates": [
                {"type": "docs", "status": "completed", "value": 4, "effort": 2, "review_score": 4},
                {"type": "docs", "status": "completed", "value": 4, "effort": 2, "review_score": 4},
                {"type": "docs", "status": "completed", "value": 4, "effort": 2, "review_score": 4},
                {"type": "perf", "status": "completed", "value": 5, "effort": 3, "review_score": 2},
                {"type": "perf", "status": "completed", "value": 5, "effort": 3, "review_score": 2},
                {"type": "perf", "status": "completed", "value": 5, "effort": 3, "review_score": 2},
            ]
        }
        stats = ap.compute_type_stats(backlog)
        self.assertEqual(stats["docs"]["review_n"], 3)
        self.assertEqual(stats["docs"]["calibration"], 1.0)
        self.assertEqual(stats["perf"]["calibration"], 0.6)

    def test_compute_type_stats(self):
        ap = self.ap
        backlog = {
            "candidates": [
                {"type": "docs", "status": "completed", "effort": 2, "value": 4},
                {"type": "docs", "status": "completed", "effort": 3, "value": 5},
                {"type": "docs", "status": "blocked", "effort": 1, "value": 2},
                {"type": "refactor", "status": "pending", "effort": 3, "value": 5},
            ]
        }
        stats = ap.compute_type_stats(backlog)
        self.assertEqual(stats["docs"]["completed"], 2)
        self.assertEqual(stats["docs"]["blocked"], 1)
        self.assertAlmostEqual(stats["docs"]["blocked_rate"], 1 / 3, places=3)
        self.assertEqual(stats["docs"]["avg_effort"], 2.5)
        self.assertEqual(stats["refactor"]["completed"], 0)

    def test_candidate_deps_status(self):
        ap = self.ap
        backlog = {
            "candidates": [
                {"id": "candidate-001", "status": "completed"},
                {"id": "candidate-002", "status": "pending"},
            ]
        }
        missing, ready = ap.candidate_deps_status(backlog, {"depends_on": ["candidate-001"]})
        self.assertTrue(ready)
        self.assertEqual(missing, [])
        missing, ready = ap.candidate_deps_status(backlog, {"depends_on": ["candidate-002"]})
        self.assertFalse(ready)
        self.assertIn("candidate-002", missing[0])
        missing, ready = ap.candidate_deps_status(backlog, {"depends_on": ["candidate-999"]})
        self.assertFalse(ready)
        self.assertIn("missing", missing[0])


class OptimizationTests(RepoTest):
    def _complete_round(self, summary="done"):
        self.run_state("begin-round", "--title", "r", "--reason", "x")
        self.add_file()
        self.run_state("commit", "--summary", "add feature")
        sha = self.git("rev-parse", "HEAD").stdout.strip()
        self.run_state("complete-round", "--summary", summary, "--commit-sha", sha)

    def test_init_refuses_dirty_tree(self):
        (self.repo / "user.txt").write_text("u\n", encoding="utf-8")
        result = self.run_state("init")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("dirty", result.stderr.lower())

    def test_init_allows_dirty_with_flag(self):
        (self.repo / "user.txt").write_text("u\n", encoding="utf-8")
        result = self.run_state("init", "--allow-uncommitted-changes")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_begin_round_refuses_dirty_first_round(self):
        self.run_state("init")
        (self.repo / "user.txt").write_text("u\n", encoding="utf-8")
        result = self.run_state("begin-round", "--title", "r", "--reason", "x")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("dirty", result.stderr.lower())

    def test_begin_round_allows_dirty_with_allow_uncommitted(self):
        self.run_state("init", "--allow-uncommitted-changes")
        (self.repo / "user.txt").write_text("u\n", encoding="utf-8")
        result = self.run_state("begin-round", "--title", "r", "--reason", "x")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_track_state_does_not_block_begin(self):
        self.run_state("init", "--track-state")
        result = self.run_state("begin-round", "--title", "r", "--reason", "x")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_cancel_then_round_numbers_advance(self):
        self.run_state("init")
        self.run_state("begin-round", "--title", "c", "--reason", "x")
        # Real work happened, so this is a billed cancelled round (a zero-work
        # probe would be aborted: no counters, but the round number still
        # advances via round_seq).
        self.add_file()
        self.run_state("cancel-round", "--reason", "changed mind")
        self.run_state("begin-round", "--title", "r2", "--reason", "x")
        state = self.read_json("state.json")
        self.assertEqual(state["round"], 2)
        self.assertEqual(state["cancelled_rounds"], 1)

    def test_max_tokens_stop(self):
        self.run_state("init", "--max-tokens", "1")
        self.run_state("begin-round", "--title", "r", "--reason", "x")
        self.add_file()
        self.run_state("commit", "--summary", "add feature")
        sha = self.git("rev-parse", "HEAD").stdout.strip()
        self.run_state("complete-round", "--summary", "done", "--commit-sha", sha, "--tokens", "10")
        data = json.loads(self.run_state("check").stdout)
        self.assertFalse(data["continue"])
        self.assertIn("max_tokens", data["stop_reason"])

    def test_max_minutes_stop(self):
        self.run_state("init", "--max-minutes", "1")
        state_path = self.repo / ".autopilot" / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["last_activity_at"] = "2020-01-01T00:00:00+00:00"
        state["started_at"] = "2020-01-01T00:00:00+00:00"
        state_path.write_text(json.dumps(state), encoding="utf-8")
        data = json.loads(self.run_state("check").stdout)
        self.assertFalse(data["continue"])
        self.assertIn("max_minutes", data["stop_reason"])

    def test_deadline_stop(self):
        self.run_state("init", "--deadline", "2000-01-01T00:00:00")
        config = self.read_json("config.json")
        self.assertIn("deadline", config)
        self.assertTrue(config["deadline"].endswith("+00:00"))
        data = json.loads(self.run_state("check", "--brief").stdout)
        self.assertFalse(data["continue"])
        self.assertIn("deadline", data["stop_reason"])
        result = self.run_state("begin-round", "--title", "r", "--reason", "x")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("stopped", result.stderr.lower())

    def test_deadline_relative_resolves_to_absolute(self):
        self.run_state("init", "--deadline", "+1h")
        config = self.read_json("config.json")
        self.assertTrue(config["deadline"].startswith("20") or config["deadline"].startswith("19"))
        self.assertIn("+00:00", config["deadline"])
        data = json.loads(self.run_state("check", "--brief").stdout)
        self.assertTrue(data["continue"])

    def test_deadline_future_allows_rounds(self):
        self.run_state("init", "--deadline", "2999-01-01T00:00:00")
        data = json.loads(self.run_state("check", "--brief").stdout)
        self.assertTrue(data["continue"])

    def test_deadline_invalid_rejected(self):
        result = self.run_state("init", "--deadline", "not-a-time")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("deadline", result.stderr.lower())
        self.assertFalse((self.repo / ".autopilot" / "state.json").exists())

    def test_deadline_in_config_is_stop_condition(self):
        self.run_state("init", "--deadline", "2999-01-01T00:00:00")
        data = json.loads(self.run_state("check", "--brief").stdout)
        no_stop_warnings = [w for w in data["warnings"] if "stop condition" in w.lower()]
        self.assertEqual(no_stop_warnings, [])

    def test_check_brief(self):
        self.run_state("init")
        data = json.loads(self.run_state("check", "--brief").stdout)
        self.assertIn("continue", data)
        self.assertIn("stop_reason", data)
        self.assertIn("warnings", data)
        self.assertNotIn("state", data)
        self.assertNotIn("config", data)

    def test_commit_json_shape(self):
        self.run_state("init")
        self.run_state("begin-round", "--title", "r", "--reason", "x")
        self.add_file()
        result = self.run_state("commit", "--summary", "add feature", "--json")
        data = json.loads(result.stdout)
        self.assertTrue(data["ok"])
        self.assertIn("commit_sha", data)

    def test_init_json_shape(self):
        result = self.run_state("init", "--json")
        data = json.loads(result.stdout)
        self.assertTrue(data["ok"])
        self.assertIn("run_id", data)

    def test_complete_round_rejects_invalid_sha(self):
        self.run_state("init")
        self.run_state("begin-round", "--title", "r", "--reason", "x")
        self.add_file()
        self.run_state("commit", "--summary", "add feature")
        result = self.run_state("complete-round", "--summary", "x", "--commit-sha", "not-a-commit")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("commit-sha", result.stderr.lower())

    def test_finish_auto_cancels_open_round(self):
        self.run_state("init")
        self.run_state("begin-round", "--title", "r", "--reason", "x")
        result = self.run_state("finish", "--force", "--reason", "done")
        self.assertEqual(result.returncode, 0, result.stderr)
        state = self.read_json("state.json")
        self.assertIsNone(state["current_round"])
        self.assertEqual(state["cancelled_rounds"], 1)

    def test_push_refuses_when_disabled(self):
        self.run_state("init")
        result = self.run_state("push")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("push", result.stderr.lower())

    def test_push_enabled_pushes_to_remote(self):
        remote = Path(self.tmp) / "remote.git"
        self.git("init", "--bare", str(remote))
        self.git("remote", "add", "origin", str(remote))
        self.run_state("init", "--push")
        self.run_state("begin-round", "--title", "r", "--reason", "x")
        self.add_file()
        self.run_state("commit", "--summary", "add feature")
        sha = self.git("rev-parse", "HEAD").stdout.strip()
        result = self.run_state("complete-round", "--summary", "done", "--commit-sha", sha)
        self.assertEqual(result.returncode, 0, result.stderr)
        branch = self.git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        out = subprocess.run(
            ["git", "--git-dir", str(remote), "rev-parse", branch],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            encoding="utf-8",
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn(sha[:7], out.stdout)

    def test_bad_config_type_clean_error(self):
        self.run_state("init")
        config_path = self.repo / ".autopilot" / "config.json"
        config_path.write_text("[]", encoding="utf-8")
        result = self.run_state("check")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("config", result.stderr.lower())
        self.assertNotIn("Traceback", result.stderr)

    def test_string_max_rounds_clean_error(self):
        self.run_state("init")
        config_path = self.repo / ".autopilot" / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["max_rounds"] = "10"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        result = self.run_state("check")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Traceback", result.stderr)

    def test_invalid_json_clean_error(self):
        self.run_state("init")
        (self.repo / ".autopilot" / "state.json").write_text("{invalid", encoding="utf-8")
        result = self.run_state("check")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Failed to read", result.stderr)

    def test_binary_file_counts_toward_scope(self):
        self.run_state("init")
        config_path = self.repo / ".autopilot" / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["max_round_scope"] = 0
        config_path.write_text(json.dumps(config), encoding="utf-8")
        (self.repo / "blob.bin").write_bytes(b"\x00\x01\x02")
        self.git("add", "blob.bin")
        # The unscannable binary first trips the secret scan (binary-staged);
        # --allow-secrets passes it so the scope guard can do its own refusal.
        blocked = self.run_state("commit", "--summary", "bin")
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn("binary file staged", blocked.stderr)
        result = self.run_state("commit", "--summary", "bin", "--allow-secrets")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("max_round_scope", result.stderr)

    def test_init_new_flags(self):
        self.run_state("init", "--push", "--commit-message-prefix", "chore",
                       "--retries-per-round", "5", "--max-blocked-in-a-row", "3")
        config = self.read_json("config.json")
        self.assertTrue(config["push"])
        self.assertEqual(config["commit_message_prefix"], "chore")
        self.assertEqual(config["retries_per_round"], 5)
        self.assertEqual(config["max_blocked_in_a_row"], 3)

    def test_ensure_branch_rejects_unexpected_branch(self):
        self.run_state("init", "--branch-mode", "feature")
        state_path = self.repo / ".autopilot" / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["branch"] = "master"
        state_path.write_text(json.dumps(state), encoding="utf-8")
        result = self.run_state("ensure-branch")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("branch", result.stderr.lower())


class FeatureTests(RepoTest):
    def _complete_round(self):
        self.run_state("begin-round", "--title", "r", "--reason", "x")
        self.add_file()
        self.run_state("commit", "--summary", "add feature")
        sha = self.git("rev-parse", "HEAD").stdout.strip()
        self.run_state("complete-round", "--summary", "done", "--commit-sha", sha)

    def test_goals_from_prompt_split(self):
        self.run_state("init", "--goals-from-prompt", "修复测试；添加文档，以及支持中文")
        config = self.read_json("config.json")
        self.assertEqual(config["goals"], ["修复测试", "添加文档", "支持中文"])

    def test_detect_verify_python(self):
        (self.repo / "pyproject.toml").write_text("[tool.pytest]\n", encoding="utf-8")
        self.run_state("init")
        result = self.run_state("detect-verify", "--json")
        data = json.loads(result.stdout)
        self.assertIn("pytest", data["commands"])

    def test_detect_verify_apply(self):
        (self.repo / "pyproject.toml").write_text("[tool.pytest]\n", encoding="utf-8")
        self.run_state("init")
        result = self.run_state("detect-verify", "--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        config = self.read_json("config.json")
        self.assertIn("pytest", config["check_commands"])

    def test_report_stdout(self):
        self.run_state("init")
        self.run_state("backlog-add", "--title", "Add docs", "--reason", "r", "--value", "4", "--effort", "2")
        result = self.run_state("report", "--lang", "en")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Run Report", result.stdout)
        self.assertIn("Add docs", result.stdout)

    def test_report_output_file_default_zh(self):
        self.run_state("init")
        out = self.repo / "report.md"
        result = self.run_state("report", "--output", str(out))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(out.exists())
        content = out.read_text(encoding="utf-8")
        self.assertIn("运行报告", content)
        self.assertIn("已完成", content)

    def test_undo_round(self):
        self.run_state("init")
        self._complete_round()
        bad_sha = self.git("rev-parse", "HEAD").stdout.strip()
        result = self.run_state("undo-round", "--sha", bad_sha)
        self.assertEqual(result.returncode, 0, result.stderr)
        state = self.read_json("state.json")
        self.assertEqual(state["reverted_rounds"], 1)
        self.assertEqual(state["history"][-1]["status"], "revert")

    def test_undo_round_invalid_sha(self):
        self.run_state("init")
        result = self.run_state("undo-round", "--sha", "not-a-commit")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("commit", result.stderr.lower())

    def test_undo_round_refuses_with_open_round(self):
        self.run_state("init")
        self.run_state("begin-round", "--title", "r", "--reason", "x")
        result = self.run_state("undo-round", "--sha", "HEAD")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("round is open", result.stderr.lower())

    def test_undo_round_advances_round_number(self):
        self.run_state("init")
        self._complete_round()
        bad_sha = self.git("rev-parse", "HEAD").stdout.strip()
        self.run_state("undo-round", "--sha", bad_sha)
        self.run_state("begin-round", "--title", "r2", "--reason", "x")
        state = self.read_json("state.json")
        self.assertEqual(state["round"], 3)

    def test_allow_paths_block(self):
        self.run_state("init")
        config_path = self.repo / ".autopilot" / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["allow_paths"] = ["src/*"]
        config_path.write_text(json.dumps(config), encoding="utf-8")
        self.run_state("begin-round", "--title", "r", "--reason", "x")
        self.add_file("other.py")
        result = self.run_state("commit", "--summary", "oops")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("allow_paths", result.stderr)

    def test_allow_paths_pass(self):
        self.run_state("init")
        config_path = self.repo / ".autopilot" / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["allow_paths"] = ["src/*"]
        config_path.write_text(json.dumps(config), encoding="utf-8")
        self.run_state("begin-round", "--title", "r", "--reason", "x")
        (self.repo / "src").mkdir()
        (self.repo / "src" / "ok.py").write_text("x = 1\n", encoding="utf-8")
        self.git("add", "src/ok.py")
        result = self.run_state("commit", "--summary", "ok")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_deny_paths_block(self):
        self.run_state("init")
        config_path = self.repo / ".autopilot" / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["deny_paths"] = ["*.secret"]
        config_path.write_text(json.dumps(config), encoding="utf-8")
        self.run_state("begin-round", "--title", "r", "--reason", "x")
        (self.repo / "creds.secret").write_text("k", encoding="utf-8")
        self.git("add", "creds.secret")
        result = self.run_state("commit", "--summary", "oops")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("deny_paths", result.stderr)

    def test_dry_run_init(self):
        result = self.run_state("init", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.repo / ".autopilot" / "state.json").exists())

    def test_dry_run_begin_no_state_change(self):
        self.run_state("init")
        result = self.run_state("begin-round", "--title", "r", "--reason", "x", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        state = self.read_json("state.json")
        self.assertIsNone(state["current_round"])

    def test_dry_run_commit_no_commit(self):
        self.run_state("init")
        self.run_state("begin-round", "--title", "r", "--reason", "x")
        self.add_file()
        before = self.git("rev-parse", "HEAD").stdout.strip()
        result = self.run_state("commit", "--summary", "add feature", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        after = self.git("rev-parse", "HEAD").stdout.strip()
        self.assertEqual(before, after)

    def test_phase_report_at_ten_rounds(self):
        self.run_state("init")
        for _ in range(10):
            self._complete_round()
        path = self.repo / ".autopilot" / "phase-report-round-10.md"
        self.assertTrue(path.exists(), "phase report should exist at 10 completed rounds")
        content = path.read_text(encoding="utf-8")
        self.assertIn("运行报告", content)
        self.assertIn("已完成", content)

    def test_no_phase_report_before_ten(self):
        self.run_state("init")
        for _ in range(3):
            self._complete_round()
        phase = list((self.repo / ".autopilot").glob("phase-report-round-*.md"))
        self.assertEqual(phase, [])


class BacklogScoreTests(RepoTest):
    def test_backlog_add_type_risk_deps_stored(self):
        self.run_state("init")
        self.run_state("backlog-add", "--title", "A", "--reason", "r", "--value", "4",
                       "--effort", "2", "--type", "refactor", "--risk", "3",
                       "--depends-on", "candidate-001")
        c = self.read_json("backlog.json")["candidates"][0]
        self.assertEqual(c["type"], "refactor")
        self.assertEqual(c["risk"], 3)
        self.assertEqual(c["depends_on"], ["candidate-001"])

    def test_backlog_add_invalid_type_refused(self):
        self.run_state("init")
        result = self.run_state("backlog-add", "--title", "A", "--reason", "r", "--type", "nope")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--type", result.stderr)

    def test_backlog_add_invalid_risk_refused(self):
        self.run_state("init")
        result = self.run_state("backlog-add", "--title", "A", "--reason", "r", "--risk", "9")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--risk", result.stderr)

    def test_backlog_rank_respects_deps(self):
        self.run_state("init")
        self.run_state("backlog-add", "--title", "Prereq", "--reason", "r", "--value", "5", "--effort", "1")
        self.run_state("backlog-add", "--title", "Dep", "--reason", "r", "--value", "5", "--effort", "1",
                       "--depends-on", "candidate-001")
        result = self.run_state("backlog-rank")
        ranked = json.loads(result.stdout)
        self.assertEqual(ranked[0]["title"], "Prereq")
        self.assertTrue(ranked[0]["ready"])
        self.assertFalse(ranked[1]["ready"])
        self.assertIn("candidate-001", ranked[1]["blocked_by"][0])

    def test_begin_round_refuses_unresolved_deps(self):
        self.run_state("init")
        self.run_state("backlog-add", "--title", "Dep", "--reason", "r", "--value", "5", "--effort", "1",
                       "--depends-on", "candidate-999")
        cid = self.read_json("backlog.json")["candidates"][0]["id"]
        result = self.run_state("begin-round", "--title", "t", "--reason", "x", "--candidate-id", cid)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("depends", result.stderr.lower())
        self.assertIsNone(self.read_json("state.json")["current_round"])

    def test_begin_round_allows_resolved_deps(self):
        self.run_state("init")
        self.run_state("backlog-add", "--title", "Prereq", "--reason", "r", "--value", "5", "--effort", "1")
        prereq = self.read_json("backlog.json")["candidates"][0]["id"]
        self.run_state("backlog-add", "--title", "Dep", "--reason", "r", "--value", "5", "--effort", "1",
                       "--depends-on", prereq)
        dep = self.read_json("backlog.json")["candidates"][1]["id"]
        self.run_state("begin-round", "--title", "p", "--reason", "x", "--candidate-id", prereq)
        self.add_file("prereq.py", "a = 1\n")
        self.run_state("commit", "--summary", "prereq")
        sha = self.git("rev-parse", "HEAD").stdout.strip()
        self.run_state("complete-round", "--summary", "p", "--commit-sha", sha)
        result = self.run_state("begin-round", "--title", "d", "--reason", "x", "--candidate-id", dep)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_backlog_rank_type_saturation(self):
        self.run_state("init")
        config_path = self.repo / ".autopilot" / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["type_saturation_threshold"] = 0
        config_path.write_text(json.dumps(config), encoding="utf-8")
        self.run_state("backlog-add", "--title", "DocA", "--reason", "r", "--value", "5", "--effort", "1", "--type", "docs")
        self.run_state("backlog-add", "--title", "DocB", "--reason", "r", "--value", "4", "--effort", "1", "--type", "docs")
        ids = [c["id"] for c in self.read_json("backlog.json")["candidates"]]
        for cid in ids:
            self.run_state("begin-round", "--title", "d", "--reason", "x", "--candidate-id", cid)
            self.add_file("f.py", "x = 1\n")
            sha = self.git("rev-parse", "HEAD").stdout.strip()
            self.run_state("commit", "--summary", "doc")
            self.run_state("complete-round", "--summary", "d", "--commit-sha", sha)
        self.run_state("backlog-add", "--title", "DocC", "--reason", "r", "--value", "5", "--effort", "1", "--type", "docs")
        self.run_state("backlog-add", "--title", "Feat", "--reason", "r", "--value", "5", "--effort", "1", "--type", "feature")
        result = self.run_state("backlog-rank")
        ranked = [r for r in json.loads(result.stdout) if r["status"] == "pending"]
        titles = [r["title"] for r in ranked]
        self.assertEqual(titles, ["Feat", "DocC"])
        self.assertGreater(ranked[0]["score"], ranked[1]["score"])


class SecretScanTests(RepoTest):
    def test_commit_refuses_secret(self):
        self.run_state("init")
        self.run_state("begin-round", "--title", "r", "--reason", "x")
        self.add_file("creds.py", 'KEY = "AKIAIOSFODNN7EXAMPLE"\n')
        result = self.run_state("commit", "--summary", "oops")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("secret", result.stderr.lower())

    def test_commit_allow_secrets_bypass(self):
        self.run_state("init")
        self.run_state("begin-round", "--title", "r", "--reason", "x")
        self.add_file("creds.py", 'KEY = "AKIAIOSFODNN7EXAMPLE"\n')
        result = self.run_state("commit", "--summary", "force", "--allow-secrets")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_commit_scan_secrets_disabled(self):
        self.run_state("init")
        config_path = self.repo / ".autopilot" / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["scan_secrets"] = False
        config_path.write_text(json.dumps(config), encoding="utf-8")
        self.run_state("begin-round", "--title", "r", "--reason", "x")
        self.add_file("creds.py", 'KEY = "AKIAIOSFODNN7EXAMPLE"\n')
        result = self.run_state("commit", "--summary", "ok")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_commit_refuses_custom_pattern(self):
        self.run_state("init")
        config_path = self.repo / ".autopilot" / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["secret_patterns"] = [r"MYTOKEN[0-9]{6}"]
        config_path.write_text(json.dumps(config), encoding="utf-8")
        self.run_state("begin-round", "--title", "r", "--reason", "x")
        self.add_file("t.py", "t = 'MYTOKEN123456'\n")
        result = self.run_state("commit", "--summary", "oops")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("secret", result.stderr.lower())

    def test_secret_scan_command(self):
        self.run_state("init")
        self.run_state("begin-round", "--title", "r", "--reason", "x")
        self.add_file("creds.py", 'KEY = "AKIAIOSFODNN7EXAMPLE"\n')
        result = self.run_state("secret-scan", "--json")
        self.assertNotEqual(result.returncode, 0)
        data = json.loads(result.stdout)
        self.assertFalse(data["clean"])
        self.assertTrue(data["findings"])


class AnalysisCacheTests(RepoTest):
    def test_analysis_missing(self):
        self.run_state("init")
        result = self.run_state("analysis-load")
        data = json.loads(result.stdout)
        self.assertFalse(data["valid"])
        self.assertEqual(data["status"], "missing")

    def test_analysis_save_load_fresh(self):
        self.run_state("init")
        result = self.run_state("analysis-save", "--content", '{"tree": ["src/"], "lang": "py"}')
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_state("analysis-load")
        data = json.loads(result.stdout)
        self.assertTrue(data["valid"])
        self.assertEqual(data["status"], "fresh")
        self.assertEqual(data["analysis"]["tree"], ["src/"])

    def test_analysis_invalid_content_refused(self):
        self.run_state("init")
        result = self.run_state("analysis-save", "--content", "not-json")
        self.assertNotEqual(result.returncode, 0)

    def test_analysis_stale_after_commit(self):
        self.run_state("init")
        self.run_state("analysis-save", "--content", "{}")
        self.add_file("new.py")
        self.git("commit", "-q", "-m", "new")
        result = self.run_state("analysis-load")
        data = json.loads(result.stdout)
        self.assertFalse(data["valid"])
        self.assertEqual(data["status"], "stale")

    def test_analysis_stale_after_config_change(self):
        self.run_state("init")
        self.run_state("analysis-save", "--content", "{}")
        config_path = self.repo / ".autopilot" / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["max_rounds"] = 3
        config_path.write_text(json.dumps(config), encoding="utf-8")
        result = self.run_state("analysis-load")
        data = json.loads(result.stdout)
        self.assertFalse(data["valid"])
        self.assertIn("config", data["reason"])


class BatchCommitTests(RepoTest):
    def test_complete_round_without_sha(self):
        self.run_state("init")
        self.run_state("begin-round", "--title", "r", "--reason", "x")
        self.add_file("a.py", "a = 1\n")
        result = self.run_state("complete-round", "--summary", "no commit yet")
        self.assertEqual(result.returncode, 0, result.stderr)
        state = self.read_json("state.json")
        self.assertEqual(state["completed_rounds"], 1)
        self.assertIsNone(state["history"][-1]["commit_sha"])

    def test_token_no_double_count_batch(self):
        self.run_state("init")
        self.run_state("begin-round", "--title", "r1", "--reason", "x")
        self.add_file("a.py", "1\n2\n")
        self.run_state("complete-round", "--summary", "a")
        after_r1 = self.read_json("state.json")["estimated_tokens_used"]

        self.run_state("begin-round", "--title", "r2", "--reason", "x")
        self.add_file("b.py", "3\n4\n5\n")
        self.run_state("complete-round", "--summary", "b")
        after_r2 = self.read_json("state.json")["estimated_tokens_used"]

        round2 = after_r2 - after_r1
        self.assertGreaterEqual(round2, 500)
        self.assertLess(round2, 550)

    def test_batch_commit_accumulates_changes(self):
        self.run_state("init")
        self.run_state("begin-round", "--title", "r1", "--reason", "x")
        self.add_file("a.py", "a = 1\n")
        self.run_state("complete-round", "--summary", "a")

        self.run_state("begin-round", "--title", "r2", "--reason", "x")
        self.add_file("b.py", "b = 2\n")
        result = self.run_state("commit", "--summary", "batched")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        sha = self.git("rev-parse", "HEAD").stdout.strip()
        self.run_state("complete-round", "--summary", "b", "--commit-sha", sha)

        files = self.git("show", "--stat", "--name-only", "--pretty=", "HEAD").stdout.strip().splitlines()
        self.assertIn("a.py", files)
        self.assertIn("b.py", files)


class VerifyScheduleTests(RepoTest):
    def test_check_reports_schedule(self):
        self.run_state("init")
        data = json.loads(self.run_state("check", "--brief").stdout)
        self.assertEqual(data["next_verify_round"], 3)
        self.assertEqual(data["next_commit_round"], 5)

    def test_init_batching_flags(self):
        self.run_state("init", "--commit-every-rounds", "2", "--verify-every-rounds", "1",
                       "--type-saturation-threshold", "1", "--no-scan-secrets")
        config = self.read_json("config.json")
        self.assertEqual(config["commit_every_rounds"], 2)
        self.assertEqual(config["verify_every_rounds"], 1)
        self.assertEqual(config["type_saturation_threshold"], 1)
        self.assertFalse(config["scan_secrets"])


class RetrospectiveTests(RepoTest):
    def test_retrospective_command(self):
        self.run_state("init")
        self.run_state("backlog-add", "--title", "A", "--reason", "r", "--value", "4",
                       "--effort", "2", "--type", "feature")
        cid = self.read_json("backlog.json")["candidates"][0]["id"]
        self.run_state("begin-round", "--title", "A", "--reason", "r", "--candidate-id", cid)
        self.add_file()
        sha = self.git("rev-parse", "HEAD").stdout.strip()
        self.run_state("commit", "--summary", "add feature")
        self.run_state("complete-round", "--summary", "done", "--commit-sha", sha)
        state = self.read_json("state.json")
        self.assertEqual(state["type_stats"]["feature"]["completed"], 1)
        result = self.run_state("retrospective", "--lang", "zh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("复盘", result.stdout)
        self.assertIn("| feature | 1 | 0 |", result.stdout)

    def test_type_stats_refreshed_after_complete(self):
        self.run_state("init")
        self.run_state("backlog-add", "--title", "A", "--reason", "r", "--value", "4",
                       "--effort", "2", "--type", "docs")
        cid = self.read_json("backlog.json")["candidates"][0]["id"]
        self.run_state("begin-round", "--title", "A", "--reason", "r", "--candidate-id", cid)
        self.add_file()
        sha = self.git("rev-parse", "HEAD").stdout.strip()
        self.run_state("commit", "--summary", "add feature")
        self.run_state("complete-round", "--summary", "done", "--commit-sha", sha)
        stats = self.read_json("state.json")["type_stats"]
        self.assertEqual(stats["docs"]["completed"], 1)
        self.assertEqual(stats["docs"]["avg_effort"], 2.0)

    def test_finish_writes_retrospective(self):
        self.run_state("init")
        self.run_state("begin-round", "--title", "r", "--reason", "x")
        self.add_file()
        sha = self.git("rev-parse", "HEAD").stdout.strip()
        self.run_state("commit", "--summary", "add feature")
        self.run_state("complete-round", "--summary", "done", "--commit-sha", sha)
        result = self.run_state("finish", "--force", "--reason", "done", "--json")
        data = json.loads(result.stdout)
        self.assertTrue(data["ok"])
        self.assertTrue((self.repo / ".autopilot" / "retrospective.md").exists())


class CheckpointTests(RepoTest):
    def test_checkpoint_round_off_by_default(self):
        self.run_state("init")
        data = json.loads(self.run_state("check", "--brief").stdout)
        self.assertIsNone(data["next_checkpoint_round"])

    def test_checkpoint_round_reported(self):
        self.run_state("init", "--checkpoint-every", "2")
        data = json.loads(self.run_state("check", "--brief").stdout)
        self.assertEqual(data["next_checkpoint_round"], 2)
        config = self.read_json("config.json")
        self.assertEqual(config["checkpoint_every"], 2)


class ExpandPhaseTests(RepoTest):
    def _finish_goal(self):
        self.run_state("begin-round", "--title", "r", "--reason", "x")
        self.add_file()
        self.run_state("commit", "--summary", "add feature")
        sha = self.git("rev-parse", "HEAD").stdout.strip()
        self.run_state("complete-round", "--summary", "done", "--commit-sha", sha)
        self.run_state("goal-met", "--goal", "G", "--round", "1")

    def test_default_stops_after_goals_met(self):
        self.run_state("init", "--goal", "G")
        self._finish_goal()
        data = json.loads(self.run_state("check", "--brief").stdout)
        self.assertFalse(data["continue"])
        self.assertIn("all goals met", data["stop_reason"])
        result = self.run_state("begin-round", "--title", "r2", "--reason", "x")
        self.assertNotEqual(result.returncode, 0)

    def test_expand_after_goals_continues(self):
        self.run_state("init", "--goal", "G", "--expand-after-goals")
        self._finish_goal()
        data = json.loads(self.run_state("check", "--brief").stdout)
        self.assertTrue(data["continue"], data["stop_reason"])
        self.assertTrue(data["goals_met"])
        self.assertEqual(data["phase"], "expand")
        result = self.run_state("begin-round", "--title", "r2", "--reason", "x")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_expand_warns_without_other_stop_condition(self):
        self.run_state("init", "--goal", "G", "--expand-after-goals")
        self._finish_goal()
        config_path = self.repo / ".autopilot" / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["max_rounds"] = None
        config["max_blocked_in_a_row"] = None
        config_path.write_text(json.dumps(config), encoding="utf-8")
        data = json.loads(self.run_state("check", "--brief").stdout)
        self.assertTrue(any("expand_after_goals" in w for w in data["warnings"]))


class ReviewGateTests(RepoTest):
    def _begin_round(self):
        self.run_state("init", "--review-threshold", "3")
        self.run_state("begin-round", "--title", "r", "--reason", "x")

    def test_review_score_required_when_threshold_set(self):
        self._begin_round()
        self.add_file()
        sha = self.git("rev-parse", "HEAD").stdout.strip()
        self.run_state("commit", "--summary", "add feature")
        result = self.run_state("complete-round", "--summary", "done", "--commit-sha", sha)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("review-score", result.stderr)

    def test_review_score_below_threshold_refused(self):
        self._begin_round()
        self.add_file()
        sha = self.git("rev-parse", "HEAD").stdout.strip()
        self.run_state("commit", "--summary", "add feature")
        result = self.run_state("complete-round", "--summary", "done", "--commit-sha", sha,
                                "--review-score", "2", "--review-notes", "hacky")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("below review_threshold", result.stderr)
        self.assertIsNotNone(self.read_json("state.json")["current_round"])

    def test_review_score_meets_threshold(self):
        self._begin_round()
        self.add_file()
        sha = self.git("rev-parse", "HEAD").stdout.strip()
        self.run_state("commit", "--summary", "add feature")
        result = self.run_state("complete-round", "--summary", "done", "--commit-sha", sha,
                                "--review-score", "4", "--review-notes", "solid")
        self.assertEqual(result.returncode, 0, result.stderr)
        history = self.read_json("state.json")["history"][-1]
        self.assertEqual(history["review_score"], 4)
        self.assertEqual(history["review_notes"], "solid")


class RankingModeTests(RepoTest):
    def test_classic_mode_via_cli_restores_ratio(self):
        self.run_state("init", "--ranking-mode", "classic")
        config = self.read_json("config.json")
        self.assertEqual(config["ranking_mode"], "classic")
        self.run_state("backlog-add", "--title", "Big", "--reason", "r", "--value", "5", "--effort", "3")
        self.run_state("backlog-add", "--title", "Tiny", "--reason", "r", "--value", "2", "--effort", "1")
        ranked = json.loads(self.run_state("backlog-rank").stdout)
        self.assertEqual(ranked[0]["title"], "Tiny")

    def test_expected_mode_prefers_value(self):
        self.run_state("init")
        self.run_state("backlog-add", "--title", "Big", "--reason", "r", "--value", "5", "--effort", "3")
        self.run_state("backlog-add", "--title", "Tiny", "--reason", "r", "--value", "2", "--effort", "1")
        ranked = json.loads(self.run_state("backlog-rank").stdout)
        self.assertEqual(ranked[0]["title"], "Big")
        # With only two candidates and candidates_per_round=3, the below-floor
        # quick-win legitimately fills the remaining recommended slot.
        self.assertTrue(ranked[0]["selected"])
        self.assertTrue(ranked[1]["below_floor"])
        self.assertFalse(ranked[0]["below_floor"])

    def test_begin_round_warns_on_multiple_below_floor(self):
        self.run_state("init")
        self.run_state("backlog-add", "--title", "Chore1", "--reason", "r", "--value", "2", "--effort", "1")
        self.run_state("backlog-add", "--title", "Chore2", "--reason", "r", "--value", "2", "--effort", "1")
        ids = [c["id"] for c in self.read_json("backlog.json")["candidates"]]
        result = self.run_state("begin-round", "--title", "chores", "--reason", "x",
                                "--candidate-id", ids[0], "--candidate-id", ids[1])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("below min_candidate_value", result.stderr)

    def test_invalid_ranking_config_clean_error(self):
        self.run_state("init")
        config_path = self.repo / ".autopilot" / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["ranking_mode"] = "bogus"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        result = self.run_state("check")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ranking_mode", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


class DirectiveTests(RepoTest):
    def test_requires_init(self):
        result = self.run_state("directive-add", "--text", "x")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not initialized", result.stderr.lower())

    def test_add_and_list(self):
        self.run_state("init")
        result = self.run_state("directive-add", "--text", "优先保证向后兼容")
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_state("directive-add", "--text", "改动公共 API 前先 grep 调用方")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(self.run_state("directive-list").stdout)
        self.assertEqual(len(data["directives"]), 2)
        self.assertEqual(data["directives"][0]["text"], "优先保证向后兼容")

    def test_directive_add_requires_text(self):
        self.run_state("init")
        result = self.run_state("directive-add", "--text", "")
        self.assertNotEqual(result.returncode, 0)

    def test_directive_add_dry_run(self):
        self.run_state("init")
        result = self.run_state("directive-add", "--text", "dry", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[DRY-RUN]", result.stderr)
        data = json.loads(self.run_state("directive-list").stdout)
        self.assertEqual(data["directives"], [])


class BacklogPickTests(RepoTest):
    def test_backlog_pick_marks_candidate(self):
        self.run_state("init")
        self.run_state("backlog-add", "--title", "T", "--reason", "r")
        cid = self.read_json("backlog.json")["candidates"][0]["id"]
        result = self.run_state("backlog-pick", "--id", cid)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.read_json("backlog.json")["candidates"][0]["status"], "picked")

    def test_backlog_pick_unknown_id_refused(self):
        self.run_state("init")
        result = self.run_state("backlog-pick", "--id", "candidate-999")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not found", result.stderr.lower())

    def test_backlog_pick_dry_run(self):
        self.run_state("init")
        self.run_state("backlog-add", "--title", "T", "--reason", "r")
        cid = self.read_json("backlog.json")["candidates"][0]["id"]
        result = self.run_state("backlog-pick", "--id", cid, "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[DRY-RUN]", result.stderr)
        self.assertEqual(self.read_json("backlog.json")["candidates"][0]["status"], "pending")

    def test_backlog_add_dry_run(self):
        self.run_state("init")
        result = self.run_state("backlog-add", "--title", "T", "--reason", "r", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[DRY-RUN]", result.stderr)
        # backlog.json is created lazily on first write; verify via backlog-list.
        data = json.loads(self.run_state("backlog-list").stdout)
        self.assertEqual(data["candidates"], [])

    def test_backlog_remove_dry_run(self):
        self.run_state("init")
        self.run_state("backlog-add", "--title", "T", "--reason", "r")
        cid = self.read_json("backlog.json")["candidates"][0]["id"]
        result = self.run_state("backlog-remove", "--id", cid, "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.read_json("backlog.json")["candidates"]), 1)


class SecretPatternCoverageTests(RepoTest):
    """Every built-in secret pattern must catch a realistic sample (and stay quiet
    on benign content). One staged file per subTest, verified via secret-scan."""

    SAMPLES = [
        ("aws_access_key", 'KEY = "AKIAIOSFODNN7EXAMPLE"\n'),
        ("private_key", "-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----\n"),
        ("github_classic", 'TOKEN = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"\n'),
        ("github_fine_grained", 'TOKEN = "github_pat_abcdefghijklmnopqrstuvwxyz0123456789"\n'),
        ("slack", 'TOKEN = "xoxb-abcdefghij"\n'),
        ("google_api", 'KEY = "AIza' + "a" * 35 + '"\n'),
        ("openai_sk", 'KEY = "sk-proj-' + "a" * 30 + '"\n'),
        ("jwt", 'TOKEN = "eyJ' + "a" * 10 + "." + "b" * 10 + "." + "c" * 10 + '"\n'),
    ]

    def test_builtin_patterns_catch_samples(self):
        self.run_state("init")
        self.run_state("begin-round", "--title", "r", "--reason", "x")
        for name, content in self.SAMPLES:
            with self.subTest(pattern=name):
                filename = "secret-sample-{}.py".format(name)
                self.add_file(filename, content)
                result = self.run_state("secret-scan", "--json")
                self.assertNotEqual(result.returncode, 0, result.stdout)
                data = json.loads(result.stdout)
                self.assertFalse(data["clean"], name)
                self.assertTrue(data["findings"], name)
                # Findings must be masked: no full secret text in the output.
                for finding in data["findings"]:
                    self.assertNotIn("AKIAIOSFODNN7EXAMPLE", finding["text"])
                self.git("reset", "-q", "HEAD", "--", filename)
                (self.repo / filename).unlink()

    def test_benign_content_not_flagged(self):
        self.run_state("init")
        self.run_state("begin-round", "--title", "r", "--reason", "x")
        self.add_file("ok.py", 'key = "short"\nconfig = {"retries": 3}\n')
        result = self.run_state("secret-scan", "--json")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        data = json.loads(result.stdout)
        self.assertTrue(data["clean"])

    def test_allow_secrets_bypass_is_audited(self):
        self.run_state("init")
        self.run_state("begin-round", "--title", "r", "--reason", "x")
        self.add_file("creds.py", 'KEY = "AKIAIOSFODNN7EXAMPLE"\n')
        result = self.run_state("commit", "--summary", "force", "--allow-secrets")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        log_path = self.repo / ".autopilot" / "log.jsonl"
        events = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        commit_events = [e for e in events if e["event"] == "commit" and e.get("status") == "success"]
        self.assertTrue(commit_events)
        self.assertTrue(commit_events[-1].get("secrets_bypassed"))

    def test_invalid_secret_pattern_clean_error(self):
        self.run_state("init")
        self.run_state("begin-round", "--title", "r", "--reason", "x")
        config_path = self.repo / ".autopilot" / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["secret_patterns"] = ["([unclosed"]
        config_path.write_text(json.dumps(config), encoding="utf-8")
        self.add_file("t.py", "x = 1\n")
        result = self.run_state("commit", "--summary", "x")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Traceback", result.stderr)


class UndoRoundConflictTests(RepoTest):
    def test_undo_round_conflict_refused(self):
        self.run_state("init")
        (self.repo / "f.txt").write_text("a\n", encoding="utf-8")
        self.git("add", "f.txt")
        self.git("commit", "-q", "-m", "change a")
        a_sha = self.git("rev-parse", "HEAD").stdout.strip()
        (self.repo / "f.txt").write_text("b\n", encoding="utf-8")
        self.git("add", "f.txt")
        self.git("commit", "-q", "-m", "change b")
        result = self.run_state("undo-round", "--sha", a_sha)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("revert failed", result.stderr)


class FailurePathTests(RepoTest):
    def test_corrupt_lock_is_removed(self):
        self.run_state("init")
        lock_path = self.repo / ".autopilot" / "lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text("not-json", encoding="utf-8")
        result = self.run_state("begin-round", "--title", "r", "--reason", "x")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(lock_path.exists())

    def test_state_non_object_clean_error(self):
        self.run_state("init")
        (self.repo / ".autopilot" / "state.json").write_text("[]", encoding="utf-8")
        result = self.run_state("check")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("state.json", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_state_corrupt_field_type_clean_error(self):
        self.run_state("init")
        state_path = self.repo / ".autopilot" / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["completed_rounds"] = "many"
        state_path.write_text(json.dumps(state), encoding="utf-8")
        result = self.run_state("check")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("completed_rounds", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_corrupt_analysis_reports_stale(self):
        self.run_state("init")
        self.run_state("analysis-save", "--content", "{}")
        (self.repo / ".autopilot" / "analysis.json").write_text("[]", encoding="utf-8")
        result = self.run_state("analysis-load")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["status"], "stale")

    def test_diagnose_reports_non_git_directory(self):
        plain = Path(self.tmp) / "notgit"
        plain.mkdir()
        result = subprocess.run(
            [sys.executable, str(self.script), "diagnose", "--repo", str(plain)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            encoding="utf-8",
            errors="replace",
            env=self.env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertFalse(data["is_git_repo"])

    def test_check_warns_on_detached_head(self):
        self.run_state("init")
        self.git("checkout", "--detach", "-q")
        data = json.loads(self.run_state("check", "--brief").stdout)
        self.assertTrue(any("Detached HEAD" in w for w in data["warnings"]))

    def test_push_failure_reported(self):
        missing_remote = Path(self.tmp) / "missing-remote.git"
        self.git("remote", "add", "origin", str(missing_remote))
        self.run_state("init", "--push")
        result = self.run_state("push")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("push failed", result.stderr)

    def test_ensure_branch_refuses_tracked_changes(self):
        self.run_state("init", "--branch-mode", "feature")
        self.run_state("finish", "--force")
        (self.repo / "README.md").write_text("# changed by user\n", encoding="utf-8")
        result = self.run_state("ensure-branch")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("tracked changes", result.stderr)


class AllowPathsDirectoryPrefixTests(RepoTest):
    """Regression: the documented directory-prefix form (`src/`, `src`) from
    references/config.md must actually match files under the directory."""

    def test_allow_paths_directory_form_passes(self):
        self.run_state("init")
        config_path = self.repo / ".autopilot" / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["allow_paths"] = ["src/"]
        config_path.write_text(json.dumps(config), encoding="utf-8")
        self.run_state("begin-round", "--title", "r", "--reason", "x")
        (self.repo / "src").mkdir()
        (self.repo / "src" / "ok.py").write_text("x = 1\n", encoding="utf-8")
        self.git("add", "src/ok.py")
        result = self.run_state("commit", "--summary", "ok")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_allow_paths_bare_name_form_passes(self):
        self.run_state("init")
        config_path = self.repo / ".autopilot" / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["allow_paths"] = ["src"]
        config_path.write_text(json.dumps(config), encoding="utf-8")
        self.run_state("begin-round", "--title", "r", "--reason", "x")
        (self.repo / "src").mkdir()
        (self.repo / "src" / "ok.py").write_text("x = 1\n", encoding="utf-8")
        self.git("add", "src/ok.py")
        result = self.run_state("commit", "--summary", "ok")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_deny_paths_non_ascii_filename_blocked(self):
        self.run_state("init")
        config_path = self.repo / ".autopilot" / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["deny_paths"] = ["*.pem"]
        config_path.write_text(json.dumps(config), encoding="utf-8")
        self.run_state("begin-round", "--title", "r", "--reason", "x")
        (self.repo / "密钥.pem").write_text("k", encoding="utf-8")
        self.git("add", "密钥.pem")
        result = self.run_state("commit", "--summary", "oops")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("deny_paths", result.stderr)


class ConfigFingerprintTests(RepoTest):
    def test_no_warning_right_after_init(self):
        self.run_state("init")
        data = json.loads(self.run_state("check", "--brief").stdout)
        self.assertFalse(any("config.json changed" in w for w in data["warnings"]))

    def test_warning_after_config_change(self):
        self.run_state("init")
        config_path = self.repo / ".autopilot" / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["max_rounds"] = 3
        config_path.write_text(json.dumps(config), encoding="utf-8")
        data = json.loads(self.run_state("check", "--brief").stdout)
        self.assertTrue(any("config.json changed" in w for w in data["warnings"]))


class OutputGuardTests(RepoTest):
    def test_report_output_outside_repo_refused(self):
        self.run_state("init")
        outside = Path(self.tmp) / "outside" / "report.md"
        result = self.run_state("report", "--output", str(outside))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(outside.exists())

    def test_report_output_outside_repo_with_force(self):
        self.run_state("init")
        outside_dir = Path(self.tmp) / "outside"
        outside = outside_dir / "report.md"
        result = self.run_state("report", "--output", str(outside), "--force")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(outside.exists())


class ContractTests(RepoTest):
    """JSON output is the machine interface agents consume: assert exact key sets
    so accidental field removal fails loudly."""

    def test_check_brief_contract(self):
        self.run_state("init")
        data = json.loads(self.run_state("check", "--brief").stdout)
        self.assertEqual(
            set(data),
            {
                "continue", "stop_reason", "warnings", "goals_met", "goals_unverified",
                "phase", "next_verify_round", "next_commit_round", "next_checkpoint_round",
                "backlog", "action_hint", "selected_count", "selected_empty_reason",
                "wave_no", "lenses_used", "lenses_unused", "analysis", "mining",
                "blocked_streak", "budget", "expansion_budget",
            },
        )
        self.assertEqual(
            set(data["backlog"]),
            {"pending", "ready", "min_pending_candidates", "needs_expansion"},
        )
        self.assertEqual(
            set(data["expansion_budget"]),
            {"waves_used", "max_waves", "waves_left"},
        )
        self.assertIn(data["action_hint"], ("work", "expand", "mine", "stop"))

    def test_detect_agent_contract_with_autopilot_state(self):
        """Cross-contract (seed-001): detect-agent's key set must not drift when
        the repo already carries .autopilot state (the common mid-run case)."""
        self.run_state("init")
        self.run_state("goal-met", "--goal", "G", "--next-step", "N")
        env = dict(self.env)
        for var in ("OPENCODE", "CLAUDE_CODE", "CODEX", "AUTOPILOT_AGENT", "SKILL_DIR"):
            env.pop(var, None)
        result = subprocess.run(
            [sys.executable, str(self.script), "detect-agent", "--repo", str(self.repo), "--home", str(self.tmp)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(
            set(data),
            {"agent", "label", "detected_by", "shell", "python_cmd", "skill_dir",
             "project_marker", "agent_config", "adaptation"},
        )
        # Seed/expand context must not leak into the agent-detection payload.
        self.assertNotIn("expansion", data)
        self.assertNotIn("goal_seeds", data)

    def test_detect_agent_contract(self):
        env = dict(self.env)
        for var in ("OPENCODE", "CLAUDE_CODE", "CODEX", "AUTOPILOT_AGENT", "SKILL_DIR"):
            env.pop(var, None)
        result = subprocess.run(
            [sys.executable, str(self.script), "detect-agent", "--repo", str(self.repo), "--home", str(self.tmp)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(
            set(data),
            {"agent", "label", "detected_by", "shell", "python_cmd", "skill_dir",
             "project_marker", "agent_config", "adaptation"},
        )

    def test_backlog_rank_entry_contract(self):
        self.run_state("init")
        self.run_state("backlog-add", "--title", "A", "--reason", "r", "--value", "4", "--effort", "2")
        ranked = json.loads(self.run_state("backlog-rank").stdout)
        self.assertEqual(
            set(ranked[0]),
            {
                "id", "title", "reason", "type", "risk", "depends_on", "impact",
                "value", "effort", "status", "round", "created_at", "updated_at",
                "score", "score_breakdown", "ready", "blocked_by",
                "unlocks", "below_floor", "selected",
            },
        )
        self.assertEqual(
            set(ranked[0]["score_breakdown"]),
            {
                "base_value", "origin", "confidence", "confidence_factor",
                "goal_chain_factor", "success_rate", "calibration", "expected_value",
                "unlocks", "unlock_bonus", "risk", "risk_weight", "risk_factor",
                "saturation_factor", "mix_penalty", "effort_cost",
            },
        )
        # Neutral prediction factors for a plain (observed) candidate — 刀 B
        # must not move legacy scores.
        self.assertEqual(ranked[0]["score_breakdown"]["origin"], "observed")
        self.assertEqual(ranked[0]["score_breakdown"]["confidence_factor"], 1.0)
        self.assertEqual(ranked[0]["score_breakdown"]["goal_chain_factor"], 1.0)

    def test_state_json_required_keys(self):
        self.run_state("init")
        state_data = self.read_json("state.json")
        for key in (
            "schema", "run_id", "repo", "branch", "origin_branch", "created_at",
            "started_at", "last_activity_at", "round", "completed_rounds",
            "blocked_rounds", "cancelled_rounds", "reverted_rounds",
            "estimated_tokens_used", "type_stats", "goals", "completed_goals",
            "goal_events", "goal_seeds", "expansion_waves",
            "current_round", "history", "stop_reason", "finished_at",
            "config_fingerprint",
        ):
            self.assertIn(key, state_data)

    def test_check_brief_expansion_contract(self):
        """Expand phase adds a stable-key `expansion` object; iterate phase must
        NOT carry it (exact-set contract above). `seeds` is [] rather than a
        missing key even when no goal has produced seeds yet."""
        self.run_state("init", "--goal", "G", "--expand-after-goals", "--max-rounds", "50")
        self.run_state("goal-met", "--goal", "G")
        data = json.loads(self.run_state("check", "--brief").stdout)
        self.assertEqual(data["phase"], "expand")
        self.assertEqual(
            set(data["expansion"]),
            {
                "seeds", "completed_goals", "recent_types",
                "saturated_types", "underused_types", "suggested_themes",
                "min_pending_candidates",
            },
        )
        self.assertEqual(data["expansion"]["seeds"], [])
        self.assertEqual(data["expansion"]["completed_goals"], ["G"])
        self.assertIn("bugfix", data["expansion"]["underused_types"])
        # With a seed present, the seed summary carries the stable field set.
        self.run_state("goal-met", "--goal", "G2", "--next-step", "next thing")
        data = json.loads(self.run_state("check", "--brief").stdout)
        seeds = data["expansion"]["seeds"]
        self.assertEqual(len(seeds), 1)
        self.assertEqual(
            set(seeds[0]),
            {"id", "title", "type", "from_capability", "source_goal", "status"},
        )


class ConfigValidationMatrixTests(RepoTest):
    """load_config has ~20 validation branches; cover them with one table."""

    CASES = [
        ("goals_type", {"goals": "single"}, "'goals' must be an array"),
        ("branch_mode", {"branch_mode": "nope"}, "'branch_mode' must be"),
        ("report_lang", {"report_lang": "fr"}, "'report_lang' must be"),
        ("review_threshold_range", {"review_threshold": 9}, "'review_threshold' must be"),
        ("checkpoint_type", {"checkpoint_every": "2"}, "'checkpoint_every' must be"),
        ("push_type", {"push": "yes"}, "'push' must be true or false"),
        ("check_commands_type", {"check_commands": "pytest"}, "'check_commands' must be an array"),
        ("allow_paths_type", {"allow_paths": "src/"}, "'allow_paths' must be an array"),
        ("deadline_type", {"deadline": 12345}, "'deadline' must be"),
        ("secret_pattern_regex", {"secret_patterns": ["([unclosed"]}, "invalid regex"),
        ("ranking_mode", {"ranking_mode": "bogus"}, "'ranking_mode' must be"),
        ("min_candidate_value", {"min_candidate_value": 9}, "'min_candidate_value' must be"),
        ("max_same_type_per_round", {"max_same_type_per_round": 0}, "'max_same_type_per_round' must be"),
        ("min_pending_candidates", {"min_pending_candidates": -1}, "'min_pending_candidates' must be"),
        ("max_predicted_per_round", {"max_predicted_per_round": -1}, "'max_predicted_per_round' must be"),
        ("max_predicted_per_round_bool", {"max_predicted_per_round": True}, "'max_predicted_per_round' must be"),
    ]

    def test_validation_matrix(self):
        for name, mutation, expected in self.CASES:
            with self.subTest(case=name):
                shutil.rmtree(self.repo / ".autopilot", ignore_errors=True)
                self.run_state("init")
                config_path = self.repo / ".autopilot" / "config.json"
                config = json.loads(config_path.read_text(encoding="utf-8"))
                config.update(mutation)
                config_path.write_text(json.dumps(config), encoding="utf-8")
                result = self.run_state("check")
                self.assertNotEqual(result.returncode, 0, name)
                self.assertIn(expected, result.stderr)
                self.assertNotIn("Traceback", result.stderr)


class ExpansionWatchTests(RepoTest):
    """Empty/thin backlog must push the agent to Deep Expansion, never idle or stop."""

    def test_empty_backlog_expands_not_stops(self):
        self.run_state("init")
        data = json.loads(self.run_state("check", "--brief").stdout)
        self.assertTrue(data["continue"])
        self.assertIsNone(data["stop_reason"])
        # Never-mined empty backlog: deterministic mining is the first supply.
        self.assertEqual(data["action_hint"], "mine")
        self.assertTrue(data["backlog"]["needs_expansion"])
        self.assertEqual(data["backlog"]["pending"], 0)
        joined = " ".join(data["warnings"]).lower()
        self.assertIn("mine --apply", joined)
        self.assertIn("do not idle", joined)

    def test_thin_backlog_warns_below_min_pending(self):
        self.run_state("init", "--min-pending-candidates", "3")
        self.run_state("backlog-add", "--title", "A", "--reason", "r", "--value", "4", "--effort", "2")
        data = json.loads(self.run_state("check", "--brief").stdout)
        self.assertTrue(data["continue"])
        # Thin + never mined → mine first (then expand if still thin).
        self.assertEqual(data["action_hint"], "mine")
        self.assertEqual(data["backlog"]["pending"], 1)
        self.assertTrue(any("min_pending_candidates" in w for w in data["warnings"]))

    def test_healthy_backlog_action_hint_work(self):
        self.run_state("init", "--min-pending-candidates", "2")
        self.run_state("backlog-add", "--title", "A", "--reason", "r", "--value", "4", "--effort", "2")
        self.run_state("backlog-add", "--title", "B", "--reason", "r", "--value", "4", "--effort", "2")
        data = json.loads(self.run_state("check", "--brief").stdout)
        self.assertTrue(data["continue"])
        self.assertEqual(data["action_hint"], "work")
        self.assertFalse(data["backlog"]["needs_expansion"])
        expansion_warnings = [w for w in data["warnings"] if "Deep Expansion" in w or "min_pending_candidates" in w]
        self.assertEqual(expansion_warnings, [])

    def test_stopped_run_action_hint_stop(self):
        self.run_state("init", "--max-rounds", "0")
        data = json.loads(self.run_state("check", "--brief").stdout)
        self.assertFalse(data["continue"])
        self.assertEqual(data["action_hint"], "stop")

    def test_init_min_pending_flag(self):
        self.run_state("init", "--min-pending-candidates", "5")
        cfg = json.loads((self.repo / ".autopilot" / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(cfg["min_pending_candidates"], 5)

    def test_ready_zero_forces_expand_even_when_pending_full(self):
        """pending>=min but all dependency-blocked must still action_hint=expand."""
        self.run_state("init", "--min-pending-candidates", "2")
        self.run_state("backlog-add", "--title", "Base", "--reason", "r", "--value", "4", "--effort", "2")
        base = json.loads((self.repo / ".autopilot" / "backlog.json").read_text(encoding="utf-8"))["candidates"][0]["id"]
        self.run_state(
            "backlog-add", "--title", "Dep1", "--reason", "r", "--value", "4", "--effort", "2",
            "--depends-on", "candidate-999",
        )
        self.run_state(
            "backlog-add", "--title", "Dep2", "--reason", "r", "--value", "4", "--effort", "2",
            "--depends-on", "candidate-999",
        )
        # Remove the ready base so only dependency-blocked pending items remain.
        self.run_state("backlog-remove", "--id", base)
        data = json.loads(self.run_state("check", "--brief").stdout)
        self.assertEqual(data["backlog"]["pending"], 2)
        self.assertEqual(data["backlog"]["ready"], 0)
        # ready==0 + never mined → mine independent supply first.
        self.assertEqual(data["action_hint"], "mine")
        self.assertTrue(any("dependency-ready" in w or "mine --apply" in w or "Deep Expansion" in w for w in data["warnings"]))

    def test_begin_round_refuses_empty_when_ready_backlog_exists(self):
        self.run_state("init")
        self.run_state("backlog-add", "--title", "A", "--reason", "r", "--value", "4", "--effort", "2")
        result = self.run_state("begin-round", "--title", "r", "--reason", "x")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("candidate-id", result.stderr.lower())

    def test_begin_round_allows_empty_when_no_ready_backlog(self):
        self.run_state("init")
        result = self.run_state("begin-round", "--title", "r", "--reason", "x")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_backlog_add_refreshes_last_activity(self):
        self.run_state("init", "--max-minutes", "1")
        state_path = self.repo / ".autopilot" / "state.json"
        st = json.loads(state_path.read_text(encoding="utf-8"))
        st["last_activity_at"] = "2020-01-01T00:00:00+00:00"
        state_path.write_text(json.dumps(st), encoding="utf-8")
        self.run_state("backlog-add", "--title", "A", "--reason", "r", "--value", "4", "--effort", "2")
        st = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertNotEqual(st["last_activity_at"], "2020-01-01T00:00:00+00:00")

    def test_report_json_wraps_markdown(self):
        self.run_state("init")
        data = json.loads(self.run_state("report", "--json").stdout)
        self.assertTrue(data["ok"])
        self.assertIn("markdown", data)
        self.assertTrue(data["markdown"])

    def test_retrospective_json_wraps_markdown(self):
        self.run_state("init")
        data = json.loads(self.run_state("retrospective", "--json").stdout)
        self.assertTrue(data["ok"])
        self.assertIn("markdown", data)


class MaxRoundsCountingTests(RepoTest):
    def test_cancelled_rounds_count_toward_max_rounds(self):
        self.run_state("init", "--max-rounds", "1")
        self.run_state("begin-round", "--title", "r", "--reason", "x")
        # Real work happened: this cancel is a billed cancelled round (a
        # zero-work probe would be aborted and consume no budget).
        self.add_file()
        self.run_state("cancel-round", "--reason", "nope")
        result = self.run_state("begin-round", "--title", "r2", "--reason", "y")
        self.assertNotEqual(result.returncode, 0)
        data = json.loads(self.run_state("check", "--brief").stdout)
        self.assertFalse(data["continue"])
        self.assertIn("max_rounds", data["stop_reason"])


class RefNameGuardTests(RepoTest):
    def test_assert_safe_ref_name_rejects_dash_prefix(self):
        import sys as _sys
        if str(SCRIPT.parent) not in _sys.path:
            _sys.path.insert(0, str(SCRIPT.parent))
        import autopilot.state as apstate
        with self.assertRaises(SystemExit):
            apstate._assert_safe_ref_name("-f")
        with self.assertRaises(SystemExit):
            apstate._assert_safe_ref_name("../evil")
        self.assertEqual(apstate._assert_safe_ref_name("autopilot/abc"), "autopilot/abc")


class LockHostTests(RepoTest):
    def test_other_host_lock_is_not_deleted(self):
        self.run_state("init")
        lock = self.repo / ".autopilot" / "lock"
        lock.write_text(
            json.dumps({"pid": os.getpid(), "hostname": "other-host-not-this-machine"}),
            encoding="utf-8",
        )
        result = self.run_state("backlog-add", "--title", "A", "--reason", "r", "--value", "4", "--effort", "2")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("another host", result.stderr.lower())
        self.assertTrue(lock.exists())


class HistoryBoundTests(unittest.TestCase):
    """Unit test for the bounded history append (no git needed)."""

    @classmethod
    def setUpClass(cls):
        import sys as _sys
        if str(SCRIPT.parent) not in _sys.path:
            _sys.path.insert(0, str(SCRIPT.parent))
        import autopilot.state as apstate
        cls.apstate = apstate

    def test_append_history_trims_to_limit(self):
        st = {"history": []}
        for i in range(105):
            self.apstate.append_history(st, {"round": i, "status": "completed", "title": "t" * 3000})
        self.assertEqual(len(st["history"]), self.apstate.io.HISTORY_LIMIT)
        self.assertEqual(st["history"][-1]["round"], 104)
        self.assertEqual(st["history"][0]["round"], 105 - self.apstate.io.HISTORY_LIMIT)
        self.assertEqual(len(st["history"][0]["title"]), self.apstate.io.HISTORY_TEXT_LIMIT)


class ReviewFixRegressionTests(RepoTest):
    """Lock the 1.4.0 review fixes: token base only on real work, config-set can
    disable expand_after_goals, parse_time trailing-Z only, dir/** path guard,
    and the 16-lens catalog stays in lockstep with SKILL.md / references."""

    def test_token_base_skipped_without_new_units(self):
        self.run_state("init")
        # Anchor on HEAD so the fixture's initial commit is not billed as run work.
        head = self.git("rev-parse", "HEAD").stdout.strip()
        tokens, total_text, total_binary = ap_io.estimate_tokens_for_round(self.repo, head, 0, 0)
        self.assertEqual(tokens, 0, "no changed units must not charge TOKEN_BASE")
        self.assertEqual((total_text, total_binary), (0, 0))

        self.add_file("a.py", "1\n2\n3\n")
        tokens, total_text, total_binary = ap_io.estimate_tokens_for_round(self.repo, head, 0, 0)
        self.assertGreaterEqual(tokens, ap_io.TOKEN_BASE)
        self.assertEqual(total_text, 3)

        # Re-billing the same units (water marks already at the totals) is free.
        tokens, _, _ = ap_io.estimate_tokens_for_round(self.repo, head, total_text, total_binary)
        self.assertEqual(tokens, 0)

    def test_config_set_disables_expand_after_goals(self):
        self.run_state("init", "--goal", "G", "--expand-after-goals")
        config = self.read_json("config.json")
        self.assertTrue(config["expand_after_goals"])
        result = self.run_state("config-set", "--no-expand-after-goals")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.read_json("config.json")["expand_after_goals"])
        none = self.run_state("config-set")
        self.assertNotEqual(none.returncode, 0)
        self.assertIn("expand-after-goals", none.stderr)
        both = self.run_state("config-set", "--expand-after-goals", "--no-expand-after-goals")
        self.assertNotEqual(both.returncode, 0)

    def test_parse_time_only_rewrites_trailing_z(self):
        self.assertIsNotNone(ap_io.parse_time("2026-08-10T08:00:00Z"))
        self.assertIsNotNone(ap_io.parse_time("2026-08-10T08:00:00+08:00"))
        # A mid-string Z is not the UTC designator and must not be rewritten.
        self.assertIsNone(ap_io.parse_time("2026-08-10T08:00:00Z+08:00"))
        self.assertIsNone(ap_io.parse_time("not-a-timestamp"))

    def test_guard_doublestar_directory_subtree(self):
        self.assertTrue(path_allowed("docs/a.md", ["docs/**"], []))
        self.assertTrue(path_allowed("docs/a/b.md", ["docs/**"], []))
        self.assertTrue(path_allowed("docs", ["docs/**"], []))
        self.assertFalse(path_allowed("src/a.py", ["docs/**"], []))
        self.assertFalse(path_allowed("docs/a.md", [], ["docs/**"]))
        self.assertTrue(path_allowed("src/a.py", ["src/**/*.py"], []))
        self.assertTrue(path_allowed("src/nested/a.py", ["src/**/*.py"], []))
        self.assertFalse(path_allowed("other/a.py", ["src/**/*.py"], []))

    def test_expansion_lenses_match_skill_docs(self):
        self.assertEqual(len(ap_state.EXPANSION_LENSES), 16)
        self.assertEqual(len(set(ap_state.EXPANSION_LENSES)), 16)
        repo_root = Path(__file__).resolve().parent.parent
        skill = (repo_root / "SKILL.md").read_text(encoding="utf-8")
        lenses_doc = (repo_root / "references" / "expansion-lenses.md").read_text(encoding="utf-8")
        self.assertIn("16", skill)
        for lens in ap_state.EXPANSION_LENSES:
            self.assertIn(lens, lenses_doc, "references/expansion-lenses.md missing lens " + lens)
        # Merged legacy aliases must stay gone or expansion-record will reject them.
        self.assertNotIn("docs / ux-copy", skill)
        self.assertNotIn("| docs / ux-copy |", lenses_doc)

    def test_skill_ship_surface_docs_present(self):
        """The repo carries a root README.md as its GitHub face (owner decision
        2026-09-26, superseding the earlier "no root README" rule), while the
        agent-facing overview still lives under references/ — the skill entry
        point remains SKILL.md either way."""
        repo_root = Path(__file__).resolve().parent.parent
        self.assertTrue((repo_root / "README.md").exists())
        for name in (
            "overview.md",
            "config.md",
            "expansion-lenses.md",
            "wave0-prediction.md",
            "troubleshooting.md",
        ):
            self.assertTrue((repo_root / "references" / name).exists(), name)

    def test_log_rotation_keeps_generations(self):
        self.run_state("init")
        log_path = self.repo / ".autopilot" / "log.jsonl"
        log_path.write_text("x" * (ap_io.LOG_ROTATE_BYTES + 1), encoding="utf-8")
        ap_io.append_log(self.repo, "rotate-probe", "ok")
        self.assertTrue(log_path.exists())
        self.assertTrue(Path(str(log_path) + ".1").exists())
        # Second rotation shifts .1 -> .2 instead of overwriting history.
        log_path.write_text("y" * (ap_io.LOG_ROTATE_BYTES + 1), encoding="utf-8")
        ap_io.append_log(self.repo, "rotate-probe", "ok")
        self.assertTrue(Path(str(log_path) + ".2").exists())


class MiningAndFinishGateTests(RepoTest):
    """P0: deterministic mining supplies candidates; the finish gate refuses
    while below-floor ready work or untried mining remains (the old gate only
    counted value>=floor and let runs stop with a full selected batch)."""

    def test_mine_finds_markers_and_applies(self):
        (self.repo / "app.py").write_text(
            "def main():\n    # TODO: handle empty input\n    return 1\n",
            encoding="utf-8",
        )
        self.git("add", "app.py")
        self.git("commit", "-q", "-m", "add app")
        self.run_state("init")
        result = self.run_state("mine", "--kind", "markers", "--apply", "--json")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        data = json.loads(result.stdout)
        self.assertGreaterEqual(data["found"], 1)
        self.assertGreaterEqual(data["applied"], 1)
        backlog = self.read_json("backlog.json")
        titles = [c["title"] for c in backlog["candidates"]]
        self.assertTrue(any("TODO" in t for t in titles), titles)
        # Second mine must dedup against existing evidence.
        again = json.loads(self.run_state("mine", "--kind", "markers", "--apply", "--json").stdout)
        self.assertEqual(again["new"], 0)
        self.assertEqual(again["applied"], 0)

    def test_mine_records_mining_runs_for_exhaustion(self):
        self.run_state("init")
        first = json.loads(self.run_state("mine", "--kind", "docs-drift", "--json").stdout)
        self.assertIn("exhausted", first)
        self.assertFalse(first["exhausted"], "one empty mine is not exhaustion")
        second = json.loads(self.run_state("mine", "--kind", "docs-drift", "--json").stdout)
        # Two zero-finding runs in a row (docs-drift on a clean README) → exhausted.
        self.assertTrue(second["exhausted"] or second["found"] > 0)
        runs = self.read_json("state.json").get("mining_runs") or []
        self.assertTrue(runs and all("new" in r for r in runs), runs)

    def test_mine_exhaustion_counts_new_findings_not_raw(self):
        """Resident findings kept the recorded count above zero forever, so
        `exhausted` was unreachable and finish always needed --force."""
        (self.repo / "app.py").write_text("# TODO: resident\n", encoding="utf-8")
        self.git("add", "app.py")
        self.git("commit", "-q", "-m", "add app")
        self.run_state("init")
        first = json.loads(self.run_state("mine", "--kind", "markers", "--apply", "--json").stdout)
        self.assertGreaterEqual(first["applied"], 1)
        self.assertFalse(first["exhausted"])
        second = json.loads(self.run_state("mine", "--kind", "markers", "--apply", "--json").stdout)
        # Raw findings stay > 0 (the TODO is still in the file) but new == 0.
        self.assertEqual(second["new"], 0)
        self.assertFalse(second["exhausted"], "one zero-new run is not exhaustion")
        third = json.loads(self.run_state("mine", "--kind", "markers", "--apply", "--json").stdout)
        self.assertEqual(third["new"], 0)
        self.assertTrue(third["exhausted"], "two consecutive zero-new runs are exhaustion")

    def test_mine_exhaustion_ignores_readonly_probes(self):
        """A read-only mine (no --apply) never touches the backlog, so its
        new-count is always the raw count — it must not reset the exhaustion
        sequence the way an applying run would."""
        (self.repo / "app.py").write_text("# TODO: resident\n", encoding="utf-8")
        self.git("add", "app.py")
        self.git("commit", "-q", "-m", "add app")
        self.run_state("init")
        first = json.loads(self.run_state("mine", "--kind", "markers", "--apply", "--json").stdout)
        self.assertGreaterEqual(first["applied"], 1)
        self.assertFalse(first["exhausted"])
        second = json.loads(self.run_state("mine", "--kind", "markers", "--apply", "--json").stdout)
        self.assertEqual(second["new"], 0)
        self.assertFalse(second["exhausted"])
        # Two read-only probes in between: without the apply-run filter these
        # would show new > 0 and reset the two-consecutive-zero-new sequence.
        probe_a = json.loads(self.run_state("mine", "--kind", "markers", "--json").stdout)
        self.assertGreaterEqual(probe_a["new"], 0)
        probe_b = json.loads(self.run_state("mine", "--kind", "markers", "--json").stdout)
        self.assertFalse(probe_b["exhausted"])
        third = json.loads(self.run_state("mine", "--kind", "markers", "--apply", "--json").stdout)
        self.assertEqual(third["new"], 0)
        self.assertTrue(third["exhausted"], "read-only probes must not reset the exhaustion sequence")

    def test_finish_refuses_below_floor_ready_work(self):
        """The proven early-stop hole: ready work below min_candidate_value used
        to leave the finish gate open while check still said work."""
        self.run_state("init", "--min-candidate-value", "3", "--min-pending-candidates", "3")
        for i in range(3):
            self.run_state(
                "backlog-add", "--title", "low{}".format(i), "--reason", "r",
                "--value", "2", "--effort", "1", "--type", "docs",
            )
        # Isolate from max_rounds so only the gate is under test.
        cfg_path = self.repo / ".autopilot" / "config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg["max_rounds"] = None
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        st = self.read_json("state.json")
        st["config_fingerprint"] = ap_io.file_sha256(cfg_path)
        (self.repo / ".autopilot" / "state.json").write_text(json.dumps(st), encoding="utf-8")

        result = self.run_state("finish", "--reason", "premature")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(
            "Refusing to finish" in (result.stdout + result.stderr),
            result.stdout + result.stderr,
        )
        forced = self.run_state("finish", "--reason", "user-stop", "--force")
        self.assertEqual(forced.returncode, 0, forced.stderr)

    def test_finish_refuses_when_mining_untried(self):
        self.run_state("init")
        cfg_path = self.repo / ".autopilot" / "config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg["max_rounds"] = None
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        st = self.read_json("state.json")
        st["config_fingerprint"] = ap_io.file_sha256(cfg_path)
        (self.repo / ".autopilot" / "state.json").write_text(json.dumps(st), encoding="utf-8")
        result = self.run_state("finish", "--reason", "skip-mine")
        self.assertNotEqual(result.returncode, 0)
        combined = result.stdout + result.stderr
        self.assertTrue("Refusing to finish" in combined, combined)
        self.assertTrue("mining_exhausted=False" in combined or "mine" in combined.lower(), combined)

    def test_check_action_hint_mine_when_thin(self):
        self.run_state("init")
        data = json.loads(self.run_state("check", "--brief").stdout)
        self.assertEqual(data["action_hint"], "mine")
        self.assertIn("mining", data)
        self.assertTrue(any("mine --apply" in w for w in data["warnings"]))

    def test_mine_dry_run_touches_nothing(self):
        (self.repo / "app.py").write_text("# TODO: x\n", encoding="utf-8")
        self.git("add", "app.py")
        self.git("commit", "-q", "-m", "add app")
        self.run_state("init")
        self.run_state("backlog-add", "--title", "seed-candidate", "--reason", "r", "--value", "3", "--effort", "1")
        before = self.read_json("backlog.json")
        result = self.run_state("mine", "--kind", "markers", "--apply", "--dry-run", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        after = self.read_json("backlog.json")
        self.assertEqual(before["candidates"], after["candidates"])

    def test_mine_dedup_ignores_marker_line_drift(self):
        (self.repo / "app.py").write_text(
            "x = 1\n# TODO: handle empty input\n",
            encoding="utf-8",
        )
        self.git("add", "app.py")
        self.git("commit", "-q", "-m", "add app")
        self.run_state("init")
        first = json.loads(self.run_state("mine", "--kind", "markers", "--apply", "--json").stdout)
        self.assertGreaterEqual(first["applied"], 1)
        # The marker drifts down a file edit later: same problem, not a new one.
        (self.repo / "app.py").write_text(
            "x = 1\ny = 2\n# TODO: handle empty input\n",
            encoding="utf-8",
        )
        self.git("add", "app.py")
        self.git("commit", "-q", "-m", "grow app")
        again = json.loads(self.run_state("mine", "--kind", "markers", "--apply", "--json").stdout)
        self.assertEqual(again["new"], 0)
        self.assertEqual(again["applied"], 0)

    def test_mine_hotspot_churn_change_not_deduplicated(self):
        """Hotspot identity is the file, not the churn count in the title."""
        (self.repo / "app.py").write_text("x = 1\n")
        self.git("add", "app.py")
        self.git("commit", "-q", "-m", "one")
        (self.repo / "app.py").write_text("x = 2\n")
        self.git("add", "app.py")
        self.git("commit", "-q", "-m", "two")
        self.run_state("init")
        first = json.loads(self.run_state("mine", "--kind", "hotspot", "--apply", "--json").stdout)
        # README.md has a single commit: churn noise, not a hotspot.
        self.assertEqual(first["applied"], 1, first["findings"])
        (self.repo / "app.py").write_text("x = 3\n")
        self.git("add", "app.py")
        self.git("commit", "-q", "-m", "three")
        second = json.loads(self.run_state("mine", "--kind", "hotspot", "--apply", "--json").stdout)
        self.assertEqual(second["new"], 0)
        self.assertEqual(second["applied"], 0)

    def test_mine_hotspot_handles_non_ascii_paths(self):
        (self.repo / "模块.py").write_text("x = 1\n", encoding="utf-8")
        self.git("add", "模块.py")
        self.git("commit", "-q", "-m", "one")
        (self.repo / "模块.py").write_text("x = 2\n", encoding="utf-8")
        self.git("add", "模块.py")
        self.git("commit", "-q", "-m", "two")
        self.run_state("init")
        result = json.loads(self.run_state("mine", "--kind", "hotspot", "--apply", "--json").stdout)
        self.assertGreaterEqual(result["applied"], 1)
        files = [
            c["file"] for c in self.read_json("backlog.json")["candidates"]
            if c.get("from_mine") == "hotspot"
        ]
        # core.quotepath=false: no git octal escapes in mined paths.
        self.assertTrue(any("模块" in (f or "") for f in files), files)

    def test_mine_swallowed_catches_comment_and_oneline_forms(self):
        (self.repo / "app.py").write_text(
            "def run():\n    return 1\n"
            "try:\n    run()\nexcept Exception:  # deliberate\n    pass\n"
            "try:\n    run()\nexcept ValueError: pass\n"
            "try:\n    run()\nexcept Exception:\n    pass  # keep\n"
            "try:\n    run()\nexcept Exception:\n    return None\n",
            encoding="utf-8",
        )
        self.git("add", "app.py")
        self.git("commit", "-q", "-m", "add app")
        self.run_state("init")
        result = json.loads(self.run_state("mine", "--kind", "swallowed", "--json").stdout)
        # Three swallow shapes fire; the real handler (return None) does not.
        self.assertEqual(result["found"], 3, result["findings"])
        evidence = " ".join(f["evidence"] for f in result["findings"])
        self.assertIn("# deliberate", evidence)
        self.assertIn("except ValueError: pass", evidence)

    def test_mine_test_gap_does_not_mistake_test_substring_names(self):
        (self.repo / "contest.py").write_text("def grade():\n    return 1\n", encoding="utf-8")
        self.git("add", "contest.py")
        self.git("commit", "-q", "-m", "add contest")
        self.run_state("init")
        result = json.loads(self.run_state("mine", "--kind", "test-gap", "--json").stdout)
        # contest.py is source, not a test file: the suite-level gap must fire.
        self.assertGreaterEqual(result["found"], 1)
        self.assertTrue(
            any("test suite" in f["title"] for f in result["findings"]),
            result["findings"],
        )

    def test_mine_test_gap_reports_uncovered_symbols(self):
        (self.repo / "contest.py").write_text("def grade():\n    return 1\n", encoding="utf-8")
        tests = self.repo / "tests"
        tests.mkdir()
        (tests / "test_basic.py").write_text("x = 1\n", encoding="utf-8")
        self.git("add", "contest.py", "tests")
        self.git("commit", "-q", "-m", "add sources")
        self.run_state("init")
        result = json.loads(self.run_state("mine", "--kind", "test-gap", "--json").stdout)
        # tests/ counts as the suite; contest.py is a source with an uncovered symbol.
        self.assertTrue(
            any(f["file"] == "contest.py" and "grade" in f["evidence"] for f in result["findings"]),
            result["findings"],
        )

    def test_mine_limit_zero_returns_nothing(self):
        (self.repo / "app.py").write_text("# TODO: x\n", encoding="utf-8")
        self.git("add", "app.py")
        self.git("commit", "-q", "-m", "add app")
        self.run_state("init")
        result = json.loads(self.run_state("mine", "--kind", "markers", "--limit", "0", "--json").stdout)
        self.assertEqual(result["found"], 0)
        self.assertEqual(result["by_kind"], {"markers": 0})

    def test_mine_utf16_python_reported_as_encoding(self):
        (self.repo / "broken.py").write_bytes("# comment\nx = 1\n".encode("utf-16"))
        self.git("add", "broken.py")
        self.git("commit", "-q", "-m", "add utf16")
        self.run_state("init")
        result = json.loads(self.run_state("mine", "--kind", "syntax", "--json").stdout)
        self.assertGreaterEqual(result["found"], 1)
        finding = result["findings"][0]
        self.assertIn("encoding", finding["title"])
        self.assertIsNone(finding["line"])
        self.assertNotIn(":0:", finding["evidence"])

    def test_mine_marker_types_follow_file_kind(self):
        (self.repo / "app.py").write_text(
            "# TODO: split module\n# FIXME: crash on empty\n",
            encoding="utf-8",
        )
        self.git("add", "app.py")
        self.git("commit", "-q", "-m", "add app")
        self.run_state("init")
        result = json.loads(self.run_state("mine", "--kind", "markers", "--apply", "--json").stdout)
        self.assertEqual(result["applied"], 2, result["findings"])
        types = {}
        for candidate in self.read_json("backlog.json")["candidates"]:
            if candidate.get("from_mine") != "markers":
                continue
            if "TODO" in candidate["title"]:
                types["todo"] = candidate["type"]
            elif "FIXME" in candidate["title"]:
                types["fixme"] = candidate["type"]
        # Code-file TODOs are refactor work, not docs; FIXME stays bugfix.
        self.assertEqual(types, {"todo": "refactor", "fixme": "bugfix"})

    def test_mine_skips_case_variants_of_ignored_dirs(self):
        nested = self.repo / "Node_Modules"
        nested.mkdir()
        (nested / "dep.py").write_text("# TODO: x\n", encoding="utf-8")
        self.git("add", "Node_Modules")
        self.git("commit", "-q", "-m", "add deps")
        self.run_state("init")
        result = json.loads(self.run_state("mine", "--kind", "markers", "--json").stdout)
        self.assertEqual(result["found"], 0)

    def test_mine_test_gap_skips_cli_dispatch_handlers(self):
        pkg = self.repo / "pkg"
        pkg.mkdir()
        (pkg / "cli.py").write_text(
            "def register(sub):\n"
            "    sub.set_defaults(func=handlers.cmd_greet)\n"
            "    sub.set_defaults(func=cmd_bye)\n",
            encoding="utf-8",
        )
        (pkg / "handlers.py").write_text(
            "def cmd_greet():\n    pass\n",
            encoding="utf-8",
        )
        (pkg / "other.py").write_text(
            "def plain_function():\n    pass\n",
            encoding="utf-8",
        )
        (self.repo / "test_cli_pkg.py").write_text("import pkg\n", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "pkg")
        self.run_state("init")
        result = json.loads(self.run_state("mine", "--kind", "test-gap", "--json").stdout)
        names = [f["title"] for f in result["findings"]]
        self.assertFalse(any("cmd_greet" in n or "cmd_bye" in n for n in names),
                         "CLI dispatch handlers must not be reported as test gaps")
        self.assertTrue(any("plain_function" in n for n in names))

    def test_mine_dead_export_skips_testcase_subclasses(self):
        pkg = self.repo / "pkg"
        pkg.mkdir()
        (pkg / "tests_a.py").write_text(
            "import unittest\n"
            "class Base(unittest.TestCase):\n    pass\n"
            "class Middle(Base):\n    pass\n"
            "class Covered(Middle):\n    pass\n",
            encoding="utf-8",
        )
        (pkg / "lib.py").write_text(
            "def really_dead():\n    pass\n",
            encoding="utf-8",
        )
        (self.repo / "test_pkg.py").write_text("import pkg\n", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "pkg")
        self.run_state("init")
        result = json.loads(self.run_state("mine", "--kind", "dead-export", "--json").stdout)
        names = [f["title"] for f in result["findings"]]
        self.assertFalse(any("Covered" in n or "Middle" in n or "Base" in n for n in names),
                         "TestCase subclasses (incl. via intermediate bases) are not dead code")
        self.assertTrue(any("really_dead" in n for n in names))

    def test_mine_hotspot_skips_deleted_paths(self):
        (self.repo / "gone.py").write_text("x = 1\n", encoding="utf-8")
        self.git("add", "gone.py")
        self.git("commit", "-q", "-m", "add gone")
        (self.repo / "gone.py").unlink()
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "delete gone")
        (self.repo / "keep.py").write_text("y = 2\n", encoding="utf-8")
        self.git("add", "keep.py")
        self.git("commit", "-q", "-m", "add keep")
        self.run_state("init")
        result = json.loads(self.run_state("mine", "--kind", "hotspot", "--json").stdout)
        titles = [f["title"] for f in result["findings"]]
        self.assertFalse(any("gone.py" in t for t in titles),
                         "deleted files must not be suggested as hotspots")

    def test_mine_markers_and_swallowed_skip_test_fixtures(self):
        (self.repo / "app.py").write_text(
            "# TODO: real debt in product code\n"
            "def f():\n    pass\n",
            encoding="utf-8",
        )
        (self.repo / "test_app.py").write_text(
            "# TODO: fixture sample string\n"
            "try:\n    run()\nexcept Exception:\n    pass\n",
            encoding="utf-8",
        )
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "seed")
        self.run_state("init")
        markers = json.loads(self.run_state("mine", "--kind", "markers", "--json").stdout)
        marker_files = [f["file"] for f in markers["findings"]]
        self.assertEqual(marker_files, ["app.py"])
        swallowed = json.loads(self.run_state("mine", "--kind", "swallowed", "--json").stdout)
        self.assertEqual(swallowed["findings"], [])

    def test_diagnose_non_git_reports_finding_not_error(self):
        plain = Path(self.tmp) / "plain"
        plain.mkdir()
        result = self.run_state("diagnose", "--repo", str(plain))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("[ERROR]", result.stderr)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["is_git_repo"])


class LifecycleStateFixTests(RepoTest):
    """Regression pack for the 1.5.1 lifecycle/state/IO fixes: refused commands
    must not leak state mutations or create .autopilot/, corrupt state must be
    named precisely, and finish/report must tell the truth about branch and
    uncommitted work."""

    def _subproc(self, *argv):
        return subprocess.run(
            [sys.executable, str(self.script)] + list(argv),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            encoding="utf-8",
            errors="replace",
            env=self.env,
        )

    # --- begin-round validates before it mutates (candidates stay pending) ---
    def test_begin_round_dirty_refusal_keeps_candidates_pending(self):
        self.run_state("init")
        self.run_state("backlog-add", "--title", "A", "--reason", "r", "--value", "4", "--effort", "2")
        cid = self.read_json("backlog.json")["candidates"][0]["id"]
        (self.repo / "user.txt").write_text("u\n", encoding="utf-8")
        result = self.run_state("begin-round", "--title", "t", "--reason", "r", "--candidate-id", cid)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("dirty", result.stderr.lower())
        candidate = self.read_json("backlog.json")["candidates"][0]
        self.assertEqual(candidate["status"], "pending")
        self.assertIsNone(candidate.get("round"))
        self.assertIsNone(self.read_json("state.json")["current_round"])
        # The candidate never leaked to picked: after the tree is clean, the
        # no-candidate-id guard still fires (a leaked 'picked' would silence it).
        self.git("add", "user.txt")
        self.git("commit", "-q", "-m", "user file")
        result = self.run_state("begin-round", "--title", "t", "--reason", "r")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("candidate-id", result.stderr.lower())

    # --- init validates before any filesystem side effect ---
    def test_init_dry_run_zero_filesystem_side_effects(self):
        result = self.run_state("init", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.repo / ".autopilot").exists())

    def test_init_non_git_repo_leaves_no_autopilot_dir(self):
        plain = Path(self.tmp) / "notgit-lifecycle"
        plain.mkdir()
        result = self._subproc("init", "--repo", str(plain))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((plain / ".autopilot").exists())

    def test_init_deadline_rejection_leaves_no_autopilot_dir(self):
        result = self.run_state("init", "--deadline", "not-a-time")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.repo / ".autopilot").exists())

    def test_duplicate_init_exits_nonzero_json_ok_false(self):
        self.run_state("init")
        result = self.run_state("init", "--json")
        self.assertNotEqual(result.returncode, 0)
        data = json.loads(result.stdout)
        self.assertFalse(data["ok"])

    # --- state.json corruption is named precisely ---
    def test_state_json_null_is_invalid_object_not_missing(self):
        self.run_state("init")
        (self.repo / ".autopilot" / "state.json").write_text("null", encoding="utf-8")
        result = self.run_state("check")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not a valid JSON object", result.stderr)
        self.assertIn("null", result.stderr)
        self.assertNotIn("not found", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_future_schema_refused_without_mutation(self):
        self.run_state("init")
        state_path = self.repo / ".autopilot" / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["schema"] = ap_io.SCHEMA_VERSION + 5
        state_path.write_text(json.dumps(state), encoding="utf-8")
        result = self.run_state("check")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("newer autopilot version", result.stderr)
        self.assertIn("Downgrading is not supported", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        # Fail-closed: the on-disk file must not be rewritten or migrated.
        self.assertEqual(
            json.loads(state_path.read_text(encoding="utf-8"))["schema"],
            ap_io.SCHEMA_VERSION + 5,
        )

    # --- check/read on a non-git path blame git, not the missing state ---
    def test_check_and_read_report_not_a_git_repository(self):
        plain = Path(self.tmp) / "notgit-lifecycle2"
        plain.mkdir()
        for command in ("check", "read"):
            result = self._subproc(command, "--repo", str(plain))
            self.assertNotEqual(result.returncode, 0, command)
            self.assertIn("Not a git repository", result.stderr, command)
            self.assertNotIn("state.json not found", result.stderr, command)

    # --- finish warns (never refuses) on uncommitted files ---
    def test_finish_warns_with_uncommitted_file_list(self):
        self.run_state("init")
        (self.repo / "dirty1.txt").write_text("a\n", encoding="utf-8")
        (self.repo / "dirty2.txt").write_text("b\n", encoding="utf-8")
        result = self.run_state("finish", "--force", "--reason", "done")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("uncommitted", result.stderr)
        self.assertIn("dirty1.txt", result.stderr)
        self.assertIn("dirty2.txt", result.stderr)

    def test_finish_ignores_autopilot_paths_in_dirty_warning(self):
        self.run_state("init")
        (self.repo / ".autopilot" / "stray.txt").write_text("x\n", encoding="utf-8")
        result = self.run_state("finish", "--force", "--reason", "done")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("uncommitted", result.stderr)

    # --- feature-mode finish says where the commits landed ---
    def test_finish_feature_branch_reports_unmerged_commits(self):
        self.run_state("init", "--branch-mode", "feature")
        self.run_state("begin-round", "--title", "r", "--reason", "x")
        self.add_file()
        self.run_state("commit", "--summary", "add feature")
        sha = self.git("rev-parse", "HEAD").stdout.strip()
        self.run_state("complete-round", "--summary", "done", "--commit-sha", sha)
        result = self.run_state("finish", "--force", "--reason", "done")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        combined = result.stdout + result.stderr
        self.assertIn("提交保留在分支", combined)
        self.assertIn("未合并", combined)
        retrospective = (self.repo / ".autopilot" / "retrospective.md").read_text(encoding="utf-8")
        self.assertIn("提交保留在分支", retrospective)
        self.assertIn("autopilot/", retrospective)

    def test_finish_feature_branch_note_english(self):
        self.run_state("init", "--branch-mode", "feature", "--report-lang", "en")
        result = self.run_state("finish", "--force", "--reason", "done")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        combined = result.stdout + result.stderr
        self.assertIn("Commits remain on branch", combined)
        self.assertIn("not merged into", combined)
        retrospective = (self.repo / ".autopilot" / "retrospective.md").read_text(encoding="utf-8")
        self.assertIn("Commits remain on branch", retrospective)

    # --- check's dirty warning is scoped and correctly worded ---
    def test_check_dirty_warning_scoped_and_worded(self):
        self.run_state("init")
        (self.repo / "user.txt").write_text("u\n", encoding="utf-8")
        data = json.loads(self.run_state("check", "--brief").stdout)
        self.assertTrue(any(
            "first begin-round refuses a dirty tree" in w and "later rounds only warn" in w
            for w in data["warnings"]
        ))

    def test_check_dirty_warning_absent_while_round_open(self):
        self.run_state("init", "--allow-uncommitted-changes")
        (self.repo / "user.txt").write_text("u\n", encoding="utf-8")
        self.run_state("begin-round", "--title", "t", "--reason", "r")
        data = json.loads(self.run_state("check", "--brief").stdout)
        self.assertFalse(any("Working tree is dirty" in w for w in data["warnings"]))

    # --- check names disabled secret scanning ---
    def test_check_warns_when_secret_scanning_disabled_zh(self):
        self.run_state("init")
        config_path = self.repo / ".autopilot" / "config.json"
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
        cfg["scan_secrets"] = False
        config_path.write_text(json.dumps(cfg), encoding="utf-8")
        data = json.loads(self.run_state("check", "--brief").stdout)
        self.assertTrue(any("scan_secrets: false" in w for w in data["warnings"]))

    def test_check_warns_when_secret_scanning_disabled_en(self):
        self.run_state("init", "--report-lang", "en")
        config_path = self.repo / ".autopilot" / "config.json"
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
        cfg["scan_secrets"] = False
        config_path.write_text(json.dumps(cfg), encoding="utf-8")
        data = json.loads(self.run_state("check", "--brief").stdout)
        self.assertTrue(any("Secret scanning is disabled" in w for w in data["warnings"]))

    # --- report shows the real branch in current mode ---
    def test_report_active_branch_falls_back_to_current_branch(self):
        self.run_state("init")
        branch = self.git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        report = self.run_state("report").stdout
        self.assertIn("`{}`".format(branch), report)
        report_en = self.run_state("report", "--lang", "en").stdout
        self.assertIn("`{}`".format(branch), report_en)

    # --- goal-met says unverified without a --round anchor ---
    def test_goal_met_without_round_says_unverified(self):
        self.run_state("init", "--goal", "G")
        result = self.run_state("goal-met", "--goal", "G")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("unverified", result.stdout)
        self.assertIn("将阻止 all-goals-met 停止", result.stdout)

    def test_check_unverified_goal_warning_blames_missing_round(self):
        self.run_state("init", "--goal", "G")
        self.run_state("goal-met", "--goal", "G")
        self.run_state("backlog-add", "--title", "A", "--reason", "r", "--value", "3", "--effort", "2")
        data = json.loads(self.run_state("check", "--brief").stdout)
        self.assertTrue(any(
            "G" in w and "未关联到已完成轮次" in w and "不得 finish" in w
            for w in data["warnings"]
        ))

    # --- one actionable path for a thin backlog (no self-contradiction) ---
    def test_check_single_supply_path_when_deterministic_side_stale(self):
        self.run_state("init")
        data = json.loads(self.run_state("check", "--brief").stdout)
        self.assertEqual(data["action_hint"], "mine")
        joined = " ".join(data["warnings"])
        self.assertIn("mine --apply", joined)
        self.assertIn("Do not idle", joined)
        self.assertNotIn("run Deep Expansion now", joined)

    def test_check_single_supply_path_english(self):
        self.run_state("init", "--report-lang", "en")
        data = json.loads(self.run_state("check", "--brief").stdout)
        self.assertEqual(data["action_hint"], "mine")
        joined = " ".join(data["warnings"])
        self.assertIn("run `mine --apply` first", joined)
        self.assertIn("only if the backlog is still thin after mining", joined)

    def test_check_expansion_now_only_when_deterministic_side_fresh(self):
        self.run_state("init")
        # Two zero-finding deterministic runs == supply side exhausted: only
        # then may check tell the agent to expand right away.
        state_path = self.repo / ".autopilot" / "state.json"
        st = json.loads(state_path.read_text(encoding="utf-8"))
        st["mining_runs"] = [
            {"at": "2026-01-01T00:00:00+00:00", "findings": 0, "applied": 0, "kinds": ["docs-drift"]},
            {"at": "2026-01-01T00:01:00+00:00", "findings": 0, "applied": 0, "kinds": ["docs-drift"]},
        ]
        state_path.write_text(json.dumps(st), encoding="utf-8")
        data = json.loads(self.run_state("check", "--brief").stdout)
        self.assertEqual(data["action_hint"], "expand")
        joined = " ".join(data["warnings"])
        self.assertIn("run Deep Expansion now", joined)

    # --- refusals on an uninitialized repo must not create .autopilot/ ---
    def test_refusals_on_uninitialized_repo_create_no_directory(self):
        for command, args in (
            ("backlog-add", ("--title", "t", "--value", "3", "--effort", "1")),
            ("backlog-list", ()),
            ("backlog-pick", ("--id", "candidate-001")),
            ("begin-round", ("--title", "t", "--reason", "r")),
            ("finish", ()),
            ("commit", ("--summary", "s")),
            ("push", ()),
            ("config-set", ("--expand-after-goals",)),
            ("directive-add", ("--text", "x")),
            ("mine", ("--kind", "markers")),
        ):
            result = self.run_state(command, *args)
            self.assertNotEqual(result.returncode, 0, command)
            self.assertNotIn("Traceback", result.stderr, command)
            self.assertFalse((self.repo / ".autopilot").exists(), command)

    # --- detect-agent reports the real skill root and a working python ---
    def test_detect_agent_skill_dir_points_at_skill_root(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("OPENCODE", "CLAUDE_CODE", "CODEX", "AUTOPILOT_AGENT", "SKILL_DIR")}
        result = self._subproc("detect-agent", "--repo", str(self.repo))
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(
            Path(data["skill_dir"]).resolve(),
            (Path(__file__).resolve().parent.parent).resolve(),
        )

    def test_detect_agent_python_cmd_actually_runs(self):
        result = self._subproc("detect-agent", "--repo", str(self.repo))
        data = json.loads(result.stdout)
        probe = subprocess.run(
            [data["python_cmd"], "-V"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=30,
        )
        self.assertEqual(probe.returncode, 0)


class SecurityFixRegressionTests(RepoTest):
    """Locks the security-audit fixes: branch guard in feature mode, the
    secret-scan file-header state machine, the pre-existing-changes
    interception (batch mode included), --round history validation, the
    pattern name in secret refusals, and the .autopilot/ forced-add guard."""

    # ---------- fix: secret-scan "++" prefix bypass ----------

    def test_commit_catches_plus_prefix_secret(self):
        # Content whose text itself starts with "++" shows up as "+++..." in
        # the staged diff — the old header match skipped the line entirely.
        self.run_state("init")
        self.run_state("begin-round", "--title", "r", "--reason", "x")
        (self.repo / "leak.py").write_text("++ghp_" + "a" * 36 + "\n", encoding="utf-8")
        self.git("add", "leak.py")
        result = self.run_state("commit", "--summary", "oops")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("secret", result.stderr.lower())

    def test_commit_catches_spaced_plus_prefix_secret(self):
        # "++ ghp_..." renders as "+++ ghp_..." — the exact "+++ " header
        # shape, which used to corrupt file attribution AND skip the scan.
        self.run_state("init")
        self.run_state("begin-round", "--title", "r", "--reason", "x")
        (self.repo / "leak.py").write_text("++ ghp_" + "b" * 36 + "\n", encoding="utf-8")
        self.git("add", "leak.py")
        result = self.run_state("commit", "--summary", "oops")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("secret", result.stderr.lower())

    def test_secret_scan_header_attribution_intact(self):
        # A real "+++ b/<file>" header still sets the attribution, and the
        # header lines themselves never become findings.
        self.run_state("init")
        self.run_state("begin-round", "--title", "r", "--reason", "x")
        self.add_file("creds.py", 'KEY = "ghp_' + "c" * 36 + '"\n')
        result = self.run_state("secret-scan", "--json")
        self.assertNotEqual(result.returncode, 0)
        data = json.loads(result.stdout)
        self.assertFalse(data["clean"])
        self.assertEqual(data["findings"][0]["file"], "creds.py")
        self.assertTrue(all("+++ b/" not in f["text"] for f in data["findings"]))

    def test_commit_refusal_names_matched_pattern(self):
        self.run_state("init")
        self.run_state("begin-round", "--title", "r", "--reason", "x")
        self.add_file("creds.py", 'KEY = "AKIAIOSFODNN7EXAMPLE"\n')
        result = self.run_state("commit", "--summary", "oops")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("(matched AKIA", result.stderr)

    # ---------- fix: branch guard (feature mode) ----------

    def _init_feature(self):
        self.run_state("init", "--branch-mode", "feature")
        return self.read_json("state.json")["branch"]

    def test_commit_refuses_after_manual_branch_switch(self):
        expected = self._init_feature()
        self.git("checkout", "-q", self.initial_branch)
        self.add_file()
        result = self.run_state("commit", "--summary", "wrong branch")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Branch drift", result.stderr)
        self.assertIn(self.initial_branch, result.stderr)
        self.assertIn(expected, result.stderr)
        self.assertIn("ensure-branch", result.stderr)
        # Nothing was committed on the drifted branch.
        log = self.git("log", "-1", "--pretty=%s").stdout.strip()
        self.assertEqual(log, "initial")

    def test_begin_round_refuses_after_manual_branch_switch(self):
        self._init_feature()
        self.git("checkout", "-q", self.initial_branch)
        result = self.run_state("begin-round", "--title", "r", "--reason", "x")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Branch drift", result.stderr)
        self.assertIsNone(self.read_json("state.json")["current_round"])

    def test_complete_round_refuses_after_manual_branch_switch(self):
        self._init_feature()
        self.run_state("begin-round", "--title", "r", "--reason", "x")
        self.git("checkout", "-q", self.initial_branch)
        result = self.run_state("complete-round", "--summary", "done")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Branch drift", result.stderr)
        self.assertIsNotNone(self.read_json("state.json")["current_round"])

    def test_commit_refuses_on_detached_head_feature(self):
        self._init_feature()
        self.git("checkout", "--detach", "-q")
        self.add_file()
        result = self.run_state("commit", "--summary", "dangling")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Detached HEAD", result.stderr)

    def test_current_mode_has_no_branch_guard(self):
        self.run_state("init")
        self.git("checkout", "-q", "-b", "side-branch")
        self.run_state("begin-round", "--title", "r", "--reason", "x")
        self.add_file()
        result = self.run_state("commit", "--summary", "current mode is free")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_check_warns_branch_drift_zh(self):
        self._init_feature()
        self.git("checkout", "-q", self.initial_branch)
        data = json.loads(self.run_state("check", "--brief").stdout)
        self.assertTrue(any("分支漂移" in w and "ensure-branch" in w for w in data["warnings"]))

    def test_check_warns_branch_drift_en(self):
        self.run_state("init", "--branch-mode", "feature", "--report-lang", "en")
        self.git("checkout", "-q", self.initial_branch)
        data = json.loads(self.run_state("check", "--brief").stdout)
        self.assertTrue(any("Branch drift" in w for w in data["warnings"]))

    def test_check_warns_detached_head_in_feature_mode(self):
        self._init_feature()
        self.git("checkout", "--detach", "-q")
        data = json.loads(self.run_state("check", "--brief").stdout)
        self.assertTrue(any("detached" in w and "ensure-branch" in w for w in data["warnings"]))

    def test_ensure_branch_repairs_drift_then_commit_works(self):
        expected = self._init_feature()
        self.git("checkout", "-q", self.initial_branch)
        result = self.run_state("ensure-branch")
        self.assertEqual(result.returncode, 0, result.stderr)
        current = self.git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        self.assertEqual(current, expected)
        self.run_state("begin-round", "--title", "r", "--reason", "x")
        self.add_file()
        result = self.run_state("commit", "--summary", "back on track")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    # ---------- fix: pre-existing changes interception ----------

    def test_batch_commit_refuses_user_changes_staged_between_rounds(self):
        self.run_state("init")  # commit_every_rounds=5: batch mode by default
        self.run_state("begin-round", "--title", "r1", "--reason", "x")
        self.add_file("a.py")
        self.run_state("complete-round", "--summary", "a")
        # User stages own work between rounds; the deferred round-1 work (a.py)
        # must stay committable, but the user file must not be swept.
        (self.repo / "user.txt").write_text("user work\n", encoding="utf-8")
        self.git("add", "user.txt")
        self.run_state("begin-round", "--title", "r2", "--reason", "x")
        self.assertEqual(
            self.read_json("state.json")["current_round"]["start_dirty_files"], ["user.txt"]
        )
        self.add_file("b.py")
        result = self.run_state("commit", "--summary", "batched")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("user.txt", result.stderr)
        self.assertIn("allow_uncommitted_changes", result.stderr)
        log = self.git("log", "-1", "--pretty=%s").stdout.strip()
        self.assertEqual(log, "initial")

    def test_batch_commit_allows_own_deferred_work(self):
        self.run_state("init")
        self.run_state("begin-round", "--title", "r1", "--reason", "x")
        self.add_file("a.py")
        self.run_state("complete-round", "--summary", "a")
        self.run_state("begin-round", "--title", "r2", "--reason", "x")
        self.add_file("b.py")
        result = self.run_state("commit", "--summary", "batched")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        files = self.git("show", "--stat", "--name-only", "--pretty=", "HEAD").stdout.strip().splitlines()
        self.assertIn("a.py", files)
        self.assertIn("b.py", files)

    def test_commit_allows_leftovers_of_blocked_round(self):
        self.run_state("init", "--commit-every-rounds", "1")
        self.run_state("begin-round", "--title", "r1", "--reason", "x")
        self.add_file("a.py")
        self.run_state("block-round", "--reason", "stuck")
        self.run_state("begin-round", "--title", "r2", "--reason", "x")
        self.add_file("b.py")
        result = self.run_state("commit", "--summary", "resume work")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_nonbatch_commit_intercepts_user_changes(self):
        self.run_state("init", "--commit-every-rounds", "1")
        self.run_state("begin-round", "--title", "r1", "--reason", "x")
        self.add_file("a.py")
        sha = self.git("rev-parse", "HEAD").stdout.strip()
        result = self.run_state("commit", "--summary", "work")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.run_state("complete-round", "--summary", "a", "--commit-sha", sha)
        # User stages own work between rounds; round 2 begins with it dirty.
        (self.repo / "user.txt").write_text("user work\n", encoding="utf-8")
        self.git("add", "user.txt")
        self.run_state("begin-round", "--title", "r2", "--reason", "x")
        self.add_file("b.py")
        # b.py alone is committable, but user.txt is staged alongside — refused.
        result = self.run_state("commit", "--summary", "own work")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("user.txt", result.stderr)
        # Unstage the user file: the round's own work commits.
        self.git("reset", "-q", "user.txt")
        result = self.run_state("commit", "--summary", "own work")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_commit_fails_open_without_start_dirty_files(self):
        self.run_state("init")
        self.run_state("begin-round", "--title", "r", "--reason", "x")
        state_path = self.repo / ".autopilot" / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        del state["current_round"]["start_dirty_files"]
        state_path.write_text(json.dumps(state), encoding="utf-8")
        self.add_file()
        result = self.run_state("commit", "--summary", "legacy state")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("start_dirty_files", result.stderr)

    def test_commit_round_fails_open_without_start_dirty_files(self):
        self.run_state("init")
        self.run_state("begin-round", "--title", "r", "--reason", "x")
        self.run_state("complete-round", "--summary", "done")
        state_path = self.repo / ".autopilot" / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        del state["history"][-1]["start_dirty_files"]
        state_path.write_text(json.dumps(state), encoding="utf-8")
        self.add_file()
        result = self.run_state("commit", "--round", "1", "--summary", "flush")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("start_dirty_files", result.stderr)

    # ---------- fix: --round history validation ----------

    def test_commit_round_unknown_refused_and_lists_available(self):
        self.run_state("init")
        self.add_file()
        # No completed rounds at all: the refusal lists "none".
        result = self.run_state("commit", "--round", "9", "--summary", "orphan")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no completed round", result.stderr)
        self.assertIn("none", result.stderr)
        # Clean the stage so the first begin-round sees a clean tree.
        self.git("reset", "-q", "feature.py")
        (self.repo / "feature.py").unlink()
        self.run_state("begin-round", "--title", "t1", "--reason", "r")
        self.run_state("complete-round", "--summary", "d1")
        self.run_state("begin-round", "--title", "t2", "--reason", "r")
        self.run_state("complete-round", "--summary", "d2")
        self.add_file()
        result = self.run_state("commit", "--round", "9", "--summary", "orphan")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no completed round", result.stderr)
        self.assertIn("1, 2", result.stderr)
        result = self.run_state("commit", "--round", "1", "--summary", "flush")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_commit_round_flush_reads_history_start_dirty_files(self):
        self.run_state("init")
        self.run_state("begin-round", "--title", "r1", "--reason", "x")
        self.add_file("a.py")
        self.run_state("complete-round", "--summary", "a")
        # User stages own work between rounds: round 2 begins with it dirty and
        # its history entry carries that snapshot.
        (self.repo / "user.txt").write_text("user work\n", encoding="utf-8")
        self.git("add", "user.txt")
        self.run_state("begin-round", "--title", "r2", "--reason", "x")
        self.run_state("complete-round", "--summary", "b")
        result = self.run_state("commit", "--round", "2", "--summary", "flush")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("user.txt", result.stderr)
        # A round that began clean flushes fine.
        result = self.run_state("commit", "--round", "1", "--summary", "flush")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    # ---------- fix: .autopilot/ forced-add guard ----------

    def test_commit_refuses_forced_autopilot_files(self):
        self.run_state("init")
        self.run_state("begin-round", "--title", "r", "--reason", "x")
        self.add_file()
        self.git("add", "-f", ".autopilot/state.json")
        result = self.run_state("commit", "--summary", "sneaky")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(".autopilot/state.json", result.stderr)
        self.assertIn("track_state", result.stderr)
        log = self.git("log", "-1", "--pretty=%s").stdout.strip()
        self.assertEqual(log, "initial")

    def test_commit_allows_autopilot_files_with_track_state(self):
        self.run_state("init", "--track-state")
        self.run_state("begin-round", "--title", "r", "--reason", "x")
        self.add_file()
        self.git("add", ".autopilot/state.json")
        result = self.run_state("commit", "--summary", "versioned state")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    # ---------- fail-closed paths (mocked git failures) ----------

    def test_secret_scan_fails_closed_when_diff_unreadable(self):
        self.run_state("init")
        failing = RunResult(128, "", "fatal: bad object HEAD")
        with mock.patch.object(ap_io, "run_git", return_value=failing):
            findings = commands_module.scan_staged_diff(self.repo)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["pattern"], "git-diff-failure")

    def test_staged_numstat_fails_closed(self):
        self.run_state("init")
        failing = RunResult(128, "", "fatal: unable to read tree")
        with mock.patch.object(ap_io, "run_git", return_value=failing):
            with self.assertRaises(SystemExit) as ctx:
                commands_module._staged_numstat_lines(self.repo)
        self.assertEqual(ctx.exception.code, 2)


class CommitCadenceWarnTests(RepoTest):
    def test_off_cadence_commit_warns_next_flush_round(self):
        self.run_state("init")  # commit_every_rounds defaults to 5
        self.run_state("begin-round", "--title", "r1", "--reason", "x")
        self.add_file("a.py", "a = 1\n")
        result = self.run_state("commit", "--summary", "early")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("not a flush round", result.stderr)
        self.assertIn("next flush round is 5", result.stderr)

    def test_on_cadence_commit_does_not_warn(self):
        self.run_state("init", "--commit-every-rounds", "2")
        self.run_state("begin-round", "--title", "r1", "--reason", "x")
        self.add_file("a.py", "a = 1\n")
        self.run_state("complete-round", "--summary", "a")
        self.run_state("begin-round", "--title", "r2", "--reason", "x")
        self.add_file("b.py", "b = 2\n")
        result = self.run_state("commit", "--summary", "flush")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("not a flush round", result.stderr)

    def test_round_flush_does_not_warn(self):
        self.run_state("init")  # every=5: rounds 1/2 are both off-cadence
        self.run_state("begin-round", "--title", "r1", "--reason", "x")
        self.add_file("a.py", "a = 1\n")
        self.run_state("complete-round", "--summary", "a")
        self.run_state("begin-round", "--title", "r2", "--reason", "x")
        self.add_file("b.py", "b = 2\n")
        self.run_state("complete-round", "--summary", "b")
        self.add_file("c.py", "c = 3\n")
        result = self.run_state("commit", "--round", "1", "--summary", "flush r1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("not a flush round", result.stderr)


class AgentModuleUnitTests(unittest.TestCase):
    """Direct unit coverage for scripts/autopilot/agent.py public helpers
    (detect_python / agent_profile / cmd_detect_agent). The detect_agent
    selection logic itself is covered end-to-end by DetectAgentTests."""

    def _load_agent(self):
        from autopilot import agent as ap_agent
        return ap_agent

    def test_detect_python_returns_first_runnable_launcher(self):
        ap_agent = self._load_agent()
        completed = mock.Mock(returncode=0)
        with mock.patch.object(ap_agent.shutil, "which", return_value="C:/bin/python.exe"), \
                mock.patch.object(ap_agent.subprocess, "run", return_value=completed) as run:
            self.assertEqual(ap_agent.detect_python(), "python")
        run.assert_called_once()
        self.assertEqual(run.call_args[0][0], ["python", "-V"])

    def test_detect_python_skips_dead_shims_in_fallback_order(self):
        ap_agent = self._load_agent()

        def fake_which(name):
            return "shim" if name in ("python", "py") else None

        def fake_run(argv, **kwargs):
            if argv[0] == "python":
                raise OSError("cannot execute shim")
            return mock.Mock(returncode=0)

        with mock.patch.object(ap_agent.shutil, "which", side_effect=fake_which), \
                mock.patch.object(ap_agent.subprocess, "run", side_effect=fake_run):
            self.assertEqual(ap_agent.detect_python(), "py")

    def test_detect_python_rejects_nonzero_probe(self):
        ap_agent = self._load_agent()

        def fake_run(argv, **kwargs):
            return mock.Mock(returncode=1 if argv[0] == "python" else 0)

        with mock.patch.object(ap_agent.shutil, "which", return_value="x"), \
                mock.patch.object(ap_agent.subprocess, "run", side_effect=fake_run):
            self.assertEqual(ap_agent.detect_python(), "python3")

    def test_detect_python_falls_back_to_python_when_nothing_probes(self):
        ap_agent = self._load_agent()
        with mock.patch.object(ap_agent.shutil, "which", return_value=None):
            self.assertEqual(ap_agent.detect_python(), "python")

    def test_agent_profile_known_and_unknown(self):
        ap_agent = self._load_agent()
        profile = ap_agent.agent_profile("claude-code")
        self.assertEqual(profile["label"], "Claude Code")
        self.assertEqual(profile["project_marker"], "CLAUDE.md")
        self.assertIs(ap_agent.agent_profile("nope"), ap_agent.AGENT_PROFILES["generic"])

    def _run_cmd_detect_agent(self, env_overrides):
        ap_agent = self._load_agent()
        env = dict(os.environ)
        env.pop("SKILL_DIR", None)
        env.update(env_overrides)
        args = mock.Mock(repo=os.path.join(tempfile.gettempdir(), "detect-agent-repo"), home=None)
        buf = StringIO()
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(ap_agent, "detect_agent", return_value=("codex", "test")), \
                mock.patch.object(ap_agent, "detect_python", return_value="py"), \
                mock.patch("sys.stdout", buf):
            rc = ap_agent.cmd_detect_agent(args)
        return rc, json.loads(buf.getvalue())

    def test_cmd_detect_agent_payload_keys_and_adaptation(self):
        rc, payload = self._run_cmd_detect_agent({})
        self.assertEqual(rc, 0)
        self.assertEqual(
            set(payload),
            {"agent", "label", "detected_by", "shell", "python_cmd", "skill_dir",
             "project_marker", "agent_config", "adaptation"},
        )
        self.assertEqual(payload["agent"], "codex")
        self.assertEqual(payload["detected_by"], "test")
        self.assertEqual(payload["python_cmd"], "py")
        self.assertEqual(
            payload["adaptation"],
            {"use_python": "py", "shell_syntax": "bash", "command_style": "bash"},
        )

    def test_cmd_detect_agent_skill_dir_env_override_wins(self):
        rc, payload = self._run_cmd_detect_agent({"SKILL_DIR": "S:/custom-skills"})
        self.assertEqual(rc, 0)
        self.assertEqual(payload["skill_dir"], "S:/custom-skills")

    def test_cmd_detect_agent_skill_dir_defaults_to_package_root(self):
        ap_agent = self._load_agent()
        rc, payload = self._run_cmd_detect_agent({})
        self.assertEqual(rc, 0)
        expected = Path(ap_agent.__file__).resolve().parents[2]
        self.assertEqual(Path(payload["skill_dir"]), expected)
        self.assertTrue((expected / "SKILL.md").exists())


class IoFoundationUnitTests(unittest.TestCase):
    """Direct unit coverage for the io.py foundation helpers (JSON atomic
    write/read, git state probes, identity check). CLI-level tests cover them
    indirectly; these pinpoint regressions to the IO layer itself."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="io-unit-"))

    def _git_repo(self):
        repo = self.tmp / "repo"
        repo.mkdir()
        for args in (["init", "-q"], ["config", "user.name", "t"], ["config", "user.email", "t@x"]):
            subprocess.run(["git", "-C", str(repo)] + args, capture_output=True)
        (repo / "f.txt").write_text("x\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "-A"], capture_output=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"], capture_output=True)
        return repo

    def test_load_json_missing_returns_default(self):
        self.assertIsNone(ap_io.load_json(self.tmp / "nope.json"))
        sentinel = {"a": 1}
        self.assertEqual(ap_io.load_json(self.tmp / "nope.json", default=sentinel), sentinel)

    def test_load_json_dies_clean_on_corruption(self):
        path = self.tmp / "bad.json"
        path.write_text("{truncated", encoding="utf-8")
        with self.assertRaises(SystemExit) as ctx:
            ap_io.load_json(path)
        self.assertEqual(ctx.exception.code, 2)

    def test_save_json_roundtrip_and_bom_tolerance(self):
        path = self.tmp / "dir" / "data.json"
        payload = {"zh": "中文", "n": 3, "nested": [1, 2]}
        ap_io.save_json(path, payload)
        self.assertEqual(ap_io.load_json(path), payload)

    def test_save_json_fails_closed_on_nan(self):
        path = self.tmp / "nan.json"
        with self.assertRaises(ValueError):
            ap_io.save_json(path, {"x": float("nan")})
        self.assertFalse(path.exists())

    def test_working_tree_dirty_and_uncommitted_paths(self):
        repo = self._git_repo()
        self.assertFalse(ap_io.working_tree_dirty(repo))
        (repo / "mod.txt").write_text("changed\n", encoding="utf-8")
        (repo / "new.txt").write_text("new\n", encoding="utf-8")
        self.assertTrue(ap_io.working_tree_dirty(repo))
        paths = ap_io.uncommitted_paths(repo)
        self.assertIn("mod.txt", paths)
        self.assertIn("new.txt", paths)

    def test_branch_exists_and_current_branch(self):
        repo = self._git_repo()
        self.assertTrue(ap_io.branch_exists(repo, "master") or ap_io.branch_exists(repo, "main"))
        self.assertFalse(ap_io.branch_exists(repo, "no-such-branch"))

    def test_git_identity_ok_reads_repo_local_config(self):
        repo = self._git_repo()
        ok, name, email = ap_io.git_identity_ok(repo)
        self.assertTrue(ok)
        self.assertEqual(name, "t")
        self.assertEqual(email, "t@x")


class ApiConsistencyFixTests(RepoTest):
    """Round-9 usability fixes: CLI flag aliases for natural agent phrasings,
    init-time config errors that point at the flag (not a nonexistent file),
    and a grace retry before stealing a just-created (empty) lock file."""

    def test_save_state_keeps_last_good_backup(self):
        self.run_state("init")
        state_path = self.repo / ".autopilot" / "state.json"
        backup_path = self.repo / ".autopilot" / "state.json.bak"
        self.assertFalse(backup_path.exists())  # first write: nothing to back up
        before = state_path.read_bytes()
        # A second save keeps the first generation as the backup.
        st = json.loads(state_path.read_text(encoding="utf-8"))
        st["round"] = 9
        ap_state.save_state(self.repo, st)
        self.assertTrue(backup_path.exists())
        self.assertEqual(json.loads(backup_path.read_text(encoding="utf-8"))["round"], 0)
        # Corrupting the live file leaves a restorable copy behind.
        state_path.write_text("{truncated", encoding="utf-8")
        restored = json.loads(backup_path.read_text(encoding="utf-8"))
        self.assertEqual(restored["round"], 0)
        self.assertEqual(before, backup_path.read_bytes())

    def test_config_set_check_commands_roundtrip(self):
        self.run_state("init", "--check-commands", "old-cmd")
        result = self.run_state("config-set", "--check-commands", "pytest -q", "--check-commands", "npm test")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.read_json("config.json")["check_commands"], ["pytest -q", "npm test"])
        cleared = self.run_state("config-set", "--clear-check-commands")
        self.assertEqual(cleared.returncode, 0, cleared.stderr)
        self.assertEqual(self.read_json("config.json")["check_commands"], [])
        conflict = self.run_state("config-set", "--check-commands", "x", "--clear-check-commands")
        self.assertNotEqual(conflict.returncode, 0)
        self.assertIn("mutually exclusive", conflict.stderr)

    def test_directive_add_and_backlog_remove_accept_aliases(self):
        self.run_state("init")
        ok = self.run_state("directive-add", "--directive", "rule via alias")
        self.assertEqual(ok.returncode, 0, ok.stderr)
        listed = json.loads(self.run_state("directive-list").stdout)
        self.assertTrue(any("rule via alias" in d["text"] for d in listed["directives"]))
        added = self.run_state("backlog-add", "--title", "t", "--reason", "r")
        self.assertEqual(added.returncode, 0, added.stderr)
        removed = self.run_state("backlog-remove", "--candidate-id", "candidate-001")
        self.assertEqual(removed.returncode, 0, removed.stderr)

    def test_init_secret_pattern_error_points_at_flag_not_file(self):
        bad = self.run_state("init", "--secret-pattern", "[")
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("values from init flags", bad.stderr)
        self.assertNotIn("delete the file", bad.stderr)

    def test_acquire_lock_grace_retries_before_stealing_fresh_lock(self):
        lock = ap_io.lock_path_for(self.repo)
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("", encoding="utf-8")  # mid-creation shape: no payload yet
        sleeps = []
        with mock.patch.object(ap_io, "_sleep", side_effect=sleeps.append):
            with ap_io.run_lock(self.repo):
                self.assertEqual(sleeps, [0.1])
        self.assertFalse(lock.exists(), "lock released after the run")


class PureFunctionUnitTests(unittest.TestCase):
    """Direct unit coverage for pure helpers: commands.emit_result's output
    contract and config's path/default functions."""

    def _args(self, json_mode=False):
        return mock.Mock(json=json_mode)

    def test_emit_result_json_mode_object_and_exit_code(self):
        buf = StringIO()
        with mock.patch("sys.stdout", buf):
            rc = commands_module.emit_result(self._args(json_mode=True), False, "boom", data={"x": 1})
        self.assertEqual(rc, 2)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload, {"ok": False, "message": "boom", "x": 1})

    def test_emit_result_text_mode_ok_goes_to_stdout_only(self):
        out, err = StringIO(), StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            rc = commands_module.emit_result(self._args(), True, "fine")
        self.assertEqual(rc, 0)
        self.assertIn("fine", out.getvalue())
        self.assertEqual(err.getvalue(), "")

    def test_emit_result_text_mode_error_goes_to_stderr_only(self):
        out, err = StringIO(), StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            rc = commands_module.emit_result(self._args(), False, "bad")
        self.assertEqual(rc, 2)
        self.assertIn("bad", err.getvalue())
        self.assertEqual(out.getvalue(), "")

    def test_config_path_helpers_lay_under_autopilot_dir(self):
        repo = Path(tempfile.mkdtemp(prefix="pure-fn-"))
        for helper in (config_module.config_path_for, config_module.state_path_for,
                       config_module.backlog_path_for):
            path = helper(repo)
            self.assertEqual(path.parent, repo / ".autopilot")

    def test_default_config_and_backlog_shapes(self):
        repo = Path(tempfile.mkdtemp(prefix="pure-fn-"))
        cfg = config_module.default_config(repo)
        self.assertEqual(cfg["repo"], str(repo))
        self.assertTrue(cfg["scan_secrets"])
        self.assertFalse(cfg["push"])
        self.assertIn("goals", cfg)
        backlog = config_module.default_backlog()
        self.assertEqual(backlog.get("candidates"), [])

    def test_save_config_roundtrips_through_load(self):
        repo = Path(tempfile.mkdtemp(prefix="pure-fn-"))
        cfg = config_module.default_config(repo)
        cfg["candidates_per_round"] = 7
        cfg["goals"] = ["g1"]
        config_module.save_config(repo, cfg)
        loaded = config_module.load_config(repo)
        self.assertEqual(loaded["candidates_per_round"], 7)
        self.assertEqual(loaded["goals"], ["g1"])

    def test_any_ready_candidates_counts_dependency_ready_pending(self):
        backlog = {"candidates": [
            {"id": "a", "status": "pending", "depends_on": []},
            {"id": "b", "status": "pending", "depends_on": ["ghost"]},
            {"id": "c", "status": "completed", "depends_on": []},
        ]}
        self.assertEqual(commands_module.any_ready_candidates(backlog), 1)

    def test_validate_config_source_labels_error_and_clean_config_passes(self):
        repo = Path(tempfile.mkdtemp(prefix="pure-fn-"))
        cfg = config_module.default_config(repo)
        cfg["secret_patterns"] = ["["]
        with self.assertRaises(SystemExit):
            config_module.validate_config(cfg, source="values from init flags")
        config_module.validate_config(config_module.default_config(repo))  # clean: no raise

    def test_now_iso_is_utc_isoformat(self):
        stamp = ap_io.now_iso()
        parsed = datetime.fromisoformat(stamp)
        self.assertIsNotNone(parsed.tzinfo)
        self.assertEqual(parsed.utcoffset(), timedelta(0))

    def test_git_dir_for_accepts_repo_and_dies_on_non_git(self):
        repo = self._git_repo()
        git_dir = ap_io.git_dir_for(repo)
        self.assertTrue(Path(git_dir).exists())
        plain = Path(tempfile.mkdtemp(prefix="pure-fn-plain"))
        with self.assertRaises(SystemExit) as ctx:
            ap_io.git_dir_for(plain)
        self.assertEqual(ctx.exception.code, 2)

    def _git_repo(self):
        repo = Path(tempfile.mkdtemp(prefix="pure-fn-git-"))
        for args in (["init", "-q"], ["config", "user.name", "t"], ["config", "user.email", "t@x"]):
            subprocess.run(["git", "-C", str(repo)] + args, capture_output=True)
        (repo / "f.txt").write_text("x\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "-A"], capture_output=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"], capture_output=True)
        return repo


class SuiteIntegrityTests(unittest.TestCase):
    """Meta-guard: discovery must collect every test method defined in this
    file. Two classes once shared the name ImportUnitTests and the second
    silently shadowed the first — 14 cases ran zero times while the suite
    stayed green. A duplicate class name or any other collection drift now
    fails the build."""

    def test_suite_collects_every_defined_test(self):
        suite = unittest.defaultTestLoader.discover(
            str(SCRIPT.parent), pattern=SCRIPT.name, top_level_dir=str(SCRIPT.parent),
        )
        collected = suite.countTestCases()
        # Line-anchored: the counting expression itself must not be counted.
        defined = len(re.findall(r"(?m)^\s*def test_", SCRIPT.read_text(encoding="utf-8")))
        self.assertEqual(
            collected, defined,
            "unittest discover collected {} tests but the source defines {} test methods "
            "(duplicate class name shadows cases?)".format(collected, defined),
        )


class BacklogRankViewTests(RepoTest):
    """The loop reads backlog-rank every round; on a real 30-round repo that
    was 18KB per round, 82% of it completed history and per-entry
    score_breakdown. The view flags trim what the reader sees without
    touching the ranking itself."""

    def _add(self, title, **extra):
        args = ["--title", title, "--reason", "r", "--value", "4", "--effort", "2"]
        for key, value in extra.items():
            args += ["--" + key.replace("_", "-"), value]
        result = self.run_state("backlog-add", *args)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip().splitlines()[-1]

    def _seed(self):
        self.run_state("init")
        return [self._add("pend{}".format(i)) for i in range(4)]

    def test_view_flags_are_opt_in(self):
        """No flags keeps the full shape: existing callers and the human
        debugging a score still get score_breakdown and the free text."""
        self._seed()
        ranked = json.loads(self.run_state("backlog-rank").stdout)
        self.assertIn("score_breakdown", ranked[0])
        self.assertIn("reason", ranked[0])

    def test_brief_drops_breakdown_and_free_text(self):
        self._seed()
        result = self.run_state("backlog-rank", "--brief")
        ranked = json.loads(result.stdout)
        entry = ranked[0]
        self.assertNotIn("score_breakdown", entry)
        self.assertNotIn("reason", entry)
        self.assertNotIn("created_at", entry)
        for key in ("id", "title", "type", "value", "effort", "status", "score", "selected"):
            self.assertIn(key, entry)
        self.assertTrue(entry["selected"])

    def test_view_flags_do_not_change_scores(self):
        self._seed()
        full = {e["id"]: e["score"] for e in json.loads(self.run_state("backlog-rank").stdout)}
        brief = {e["id"]: e["score"] for e in json.loads(self.run_state("backlog-rank", "--brief").stdout)}
        self.assertEqual(full, brief)

    def test_pending_only_drops_completed_history(self):
        ids = self._seed()
        self.run_state("backlog-update", "--id", ids[0], "--status", "completed")
        full = json.loads(self.run_state("backlog-rank").stdout)
        self.assertIn(ids[0], [e["id"] for e in full])
        pending_only = json.loads(self.run_state("backlog-rank", "--pending-only").stdout)
        self.assertNotIn(ids[0], [e["id"] for e in pending_only])
        self.assertTrue(len(pending_only) < len(full))

    def test_top_truncates_and_keeps_selected_head(self):
        self._seed()
        result = self.run_state("backlog-rank", "--top", "2")
        ranked = json.loads(result.stdout)
        self.assertEqual(len(ranked), 2)
        self.assertTrue(all(entry["selected"] for entry in ranked))
        # Truncation must announce itself: a silent short list reads as
        # "that is everything there is".
        self.assertIn("showing 2 of 4", result.stderr)

    def test_top_zero_is_rejected(self):
        self._seed()
        result = self.run_state("backlog-rank", "--top", "0")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--top must be a positive integer", result.stderr)

    def test_brief_entry_keeps_cut_reason_when_present(self):
        """A candidate pushed out of the batch by the diversity quota carries
        cut_reason; --brief must not hide why it was skipped."""
        self._seed()
        for i in range(3):
            self._add("same-type{}".format(i), type="docs")
        ranked = json.loads(self.run_state("backlog-rank", "--brief").stdout)
        self.assertTrue(any("cut_reason" in entry for entry in ranked))


class RoundPrepTests(RepoTest):
    """round-prep exists because the loop paid four script calls (and four
    tool round-trips) per round before it started working: check,
    analysis-load, backlog-rank, directive-list."""

    def _add(self, title, **extra):
        args = ["--title", title, "--reason", "r", "--value", "4", "--effort", "2"]
        for key, value in extra.items():
            args += ["--" + key.replace("_", "-"), value]
        result = self.run_state("backlog-add", *args)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip().splitlines()[-1]

    def _seed(self, count=4):
        self.run_state("init")
        return [self._add("pend{}".format(i)) for i in range(count)]

    def test_carries_every_check_brief_field(self):
        """Nothing the loop used to read from check may go missing."""
        self._seed()
        brief = json.loads(self.run_state("check", "--brief").stdout)
        prep = json.loads(self.run_state("round-prep").stdout)
        for key in brief:
            self.assertIn(key, prep, "round-prep dropped check field {!r}".format(key))
        self.assertEqual(brief["action_hint"], prep["action_hint"])
        self.assertEqual(brief["continue"], prep["continue"])

    def test_suggested_candidates_match_backlog_rank(self):
        self._seed()
        ranked = json.loads(self.run_state("backlog-rank", "--pending-only", "--top", "5", "--brief").stdout)
        prep = json.loads(self.run_state("round-prep").stdout)
        self.assertEqual([c["id"] for c in prep["candidates"]], [c["id"] for c in ranked])
        self.assertTrue(prep["recommended"])
        self.assertEqual(
            prep["recommended"],
            [c["id"] for c in prep["candidates"] if c["selected"]],
        )

    def test_next_round_number_matches_begin_round(self):
        """The number must come from the same monotonic sequence begin-round
        uses, not from the completed counters."""
        ids = self._seed()
        prep = json.loads(self.run_state("round-prep").stdout)
        self.assertEqual(prep["next_round_number"], 1)
        self.assertIsNone(prep["open_round"])
        result = self.run_state("begin-round", "--title", "t", "--reason", "r",
                                "--candidate-id", ids[0])
        self.assertEqual(result.returncode, 0, result.stderr)
        prep = json.loads(self.run_state("round-prep").stdout)
        self.assertEqual(prep["open_round"]["number"], 1)
        self.assertEqual(prep["open_round"]["candidate_ids"], [ids[0]])
        self.assertEqual(prep["next_round_number"], 2)
        self.assertIn("current_round is open", " ".join(prep["warnings"]))

    def test_top_limits_candidates_and_reports_withheld(self):
        self._seed(6)
        prep = json.loads(self.run_state("round-prep", "--top", "2").stdout)
        self.assertEqual(len(prep["candidates"]), 2)
        self.assertEqual(prep["withheld"], 4)

    def test_top_zero_is_rejected(self):
        self._seed()
        result = self.run_state("round-prep", "--top", "0")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--top must be a positive integer", result.stderr)

    def test_default_top_is_batch_plus_one(self):
        self._seed(6)
        self.run_state("config-set", "--candidates-per-round", "3")
        prep = json.loads(self.run_state("round-prep").stdout)
        self.assertEqual(len(prep["candidates"]), 4)

    def test_directives_and_cached_analysis_are_included(self):
        self._seed()
        self.run_state("directive-add", "--text", "always run the suite")
        self.run_state("analysis-save", "--content", '{"notes": "hello"}')
        prep = json.loads(self.run_state("round-prep").stdout)
        self.assertEqual([d["text"] for d in prep["directives"]], ["always run the suite"])
        loaded = json.loads(self.run_state("analysis-load").stdout)
        self.assertEqual(prep["analysis"]["cached"], loaded["analysis"])

    def test_not_initialized_is_a_clean_error(self):
        result = self.run_state("round-prep")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not initialized", result.stderr.lower())


class SmokeCommandTests(RepoTest):
    """smoke_commands exists because the loop verifies fully only every
    verify_every_rounds rounds; the cheap check in between used to be
    re-improvised each round, so it differed run to run and left no trace."""

    def test_defaults_to_empty_and_merges_into_old_configs(self):
        self.run_state("init")
        cfg_path = self.repo / ".autopilot" / "config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(cfg["smoke_commands"], [])
        # A config written before the field existed must still load (defaults merge).
        cfg.pop("smoke_commands")
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        from autopilot import config as ap_config
        self.assertEqual(ap_config.load_config(self.repo)["smoke_commands"], [])

    def test_validation_rejects_wrong_shapes(self):
        from autopilot import config as ap_config
        self.run_state("init")
        for bad in ("not-a-list", [1, 2], [None]):
            cfg = ap_config.load_config(self.repo)
            cfg["smoke_commands"] = bad
            with self.assertRaises(SystemExit):
                ap_config.validate_config(cfg)

    def test_init_flag_records_a_smoke_command(self):
        result = self.run_state("init", "--smoke-commands", "python -m compileall -q .")
        self.assertEqual(result.returncode, 0, result.stderr)
        from autopilot import config as ap_config
        self.assertEqual(ap_config.load_config(self.repo)["smoke_commands"],
                         ["python -m compileall -q ."])

    def test_config_set_round_trip(self):
        self.run_state("init")
        result = self.run_state("config-set", "--smoke-commands", "cargo check",
                                "--smoke-commands", "go vet ./...")
        self.assertEqual(result.returncode, 0, result.stderr)
        from autopilot import config as ap_config
        self.assertEqual(ap_config.load_config(self.repo)["smoke_commands"],
                         ["cargo check", "go vet ./..."])
        result = self.run_state("config-set", "--clear-smoke-commands")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(ap_config.load_config(self.repo)["smoke_commands"], [])

    def test_config_set_rejects_set_and_clear_together(self):
        self.run_state("init")
        result = self.run_state("config-set", "--smoke-commands", "cargo check",
                                "--clear-smoke-commands")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("mutually exclusive", result.stderr)

    def test_config_set_still_requires_a_field(self):
        self.run_state("init")
        result = self.run_state("config-set")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("at least one field", result.stderr)

    def test_detect_smoke_commands_suggests_per_technology(self):
        from autopilot.verify import detect_smoke_commands
        (self.repo / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        (self.repo / "Cargo.toml").write_text("[package]\n", encoding="utf-8")
        suggested = dict(detect_smoke_commands(self.repo))
        self.assertEqual(suggested["python"], "python -m compileall -q .")
        self.assertEqual(suggested["rust"], "cargo check")

    def test_detect_verify_reports_smoke_recommendation(self):
        self.run_state("init")
        (self.repo / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        result = self.run_state("detect-verify", "--json")
        payload = json.loads(result.stdout)
        self.assertIn("smoke_recommended", payload)
        self.assertTrue(any(entry["command"] == "python -m compileall -q ."
                            for entry in payload["smoke_recommended"]))
        # Report-only: detection must never write smoke_commands itself.
        from autopilot import config as ap_config
        self.assertEqual(ap_config.load_config(self.repo)["smoke_commands"], [])

    def test_detect_verify_does_not_overwrite_check_commands_with_smoke(self):
        """--apply keeps writing only check_commands: a smoke command silently
        promoted to the verification set would weaken every verify round."""
        self.run_state("init")
        (self.repo / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        result = self.run_state("detect-verify", "--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        from autopilot import config as ap_config
        cfg = ap_config.load_config(self.repo)
        self.assertEqual(cfg["smoke_commands"], [])
        self.assertNotIn("python -m compileall -q .", cfg["check_commands"])


class ExpansionBudgetTests(RepoTest):
    """Subagent spend from a Deep Expansion wave is invisible to max_tokens
    (estimated from diffs), so the wave cap is the only hard bound on it."""

    def _record_wave(self, lens="architecture"):
        return self.run_state("expansion-record", "--lens", lens)

    def test_wave_count_is_monotonic_past_the_rolling_window(self):
        from autopilot import io as ap_io
        from autopilot import state as ap_state
        self.run_state("init")
        st = {"expansion_waves": []}
        for _ in range(ap_io.EXPANSION_WAVES_LIMIT + 3):
            ap_state.append_expansion_wave(st, ["architecture"])
        self.assertEqual(len(st["expansion_waves"]), ap_io.EXPANSION_WAVES_LIMIT)
        self.assertEqual(ap_state.expansion_wave_count(st), ap_io.EXPANSION_WAVES_LIMIT + 3)

    def test_count_falls_back_to_window_for_old_state(self):
        from autopilot import state as ap_state
        self.assertEqual(ap_state.expansion_wave_count({"expansion_waves": [{}, {}]}), 2)
        self.assertEqual(ap_state.expansion_wave_count({}), 0)

    def test_default_is_uncapped(self):
        self.run_state("init")
        data = json.loads(self.run_state("check", "--brief").stdout)
        self.assertEqual(data["expansion_budget"], {"waves_used": 0, "max_waves": None, "waves_left": None})

    def test_cap_refuses_the_next_wave_and_check_warns(self):
        self.run_state("init", "--max-expansion-waves", "1")
        result = self._record_wave()
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self._record_wave("tests")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("wave cap reached", result.stderr)
        data = json.loads(self.run_state("check", "--brief").stdout)
        self.assertEqual(data["expansion_budget"]["waves_left"], 0)
        self.assertTrue(any("wave cap reached" in warning for warning in data["warnings"]))

    def test_cap_is_raisable_at_runtime(self):
        self.run_state("init", "--max-expansion-waves", "1")
        self._record_wave()
        result = self.run_state("config-set", "--max-expansion-waves", "2")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._record_wave("tests").returncode, 0)
        data = json.loads(self.run_state("check", "--brief").stdout)
        self.assertEqual(data["expansion_budget"], {"waves_used": 2, "max_waves": 2, "waves_left": 0})

    def test_clear_twin_and_mutual_exclusion(self):
        self.run_state("init", "--max-expansion-waves", "1")
        result = self.run_state("config-set", "--max-expansion-waves", "2",
                                "--clear-max-expansion-waves")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("mutually exclusive", result.stderr)
        result = self.run_state("config-set", "--clear-max-expansion-waves")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(self.run_state("check", "--brief").stdout)
        self.assertIsNone(data["expansion_budget"]["max_waves"])

    def test_config_set_rejects_negative_cap(self):
        self.run_state("init")
        result = self.run_state("config-set", "--max-expansion-waves", "-1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("non-negative", result.stderr)

    def test_init_rejects_negative_cap(self):
        # Fresh repo: init on an already-initialized one would fail for an
        # unrelated reason and hide a missing range check.
        result = self.run_state("init", "--max-expansion-waves", "-1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("non-negative", result.stderr)

    def test_validation_rejects_wrong_shape(self):
        from autopilot import config as ap_config
        self.run_state("init")
        for bad in ("three", -1, True):
            cfg = ap_config.load_config(self.repo)
            cfg["max_expansion_waves"] = bad
            with self.assertRaises(SystemExit):
                ap_config.validate_config(cfg)

    def test_uncapped_run_records_waves_without_extra_state_reads(self):
        """A run with no cap must not pay the probe: the counter only needs to
        exist, and recording stays monotonic."""
        self.run_state("init")
        self._record_wave()
        self._record_wave("tests")
        from autopilot import state as ap_state
        st = ap_state.load_state(self.repo)
        self.assertEqual(ap_state.expansion_wave_count(st), 2)


class DashboardConfigTests(AutopilotTestBase):
    """The 1.8 dashboard section of config.json: defaults present and
    disabled, a user-written section deep-merges over defaults, every field
    validates, and config-set can toggle it at runtime (config layer of the
    1.8.0 dashboard plan, Tasks 1-2)."""

    def test_dashboard_defaults_present_and_disabled(self):
        from autopilot import config as ap_config
        cfg = ap_config.default_config(self.repo)
        self.assertEqual(cfg["dashboard"], {
            "enabled": False, "auto_open": True, "port": 0, "domain_map": None,
        })

    def test_user_dashboard_section_deep_merges_over_defaults(self):
        from autopilot import config as ap_config
        ap_config.save_config(self.repo, {"dashboard": {"enabled": True}})
        cfg = ap_config.load_config(self.repo)
        # Full-shape pin: the user's enabled=True wins and every unspecified
        # default (auto_open/port/domain_map) survives the merge.
        self.assertEqual(cfg["dashboard"], {"enabled": True, "auto_open": True, "port": 0, "domain_map": None})

    def test_validate_rejects_bad_dashboard_values(self):
        from autopilot import config as ap_config
        # validate_config indexes merged dashboard keys directly (they are
        # guaranteed by default_config), so each bad fragment is deep-merged
        # over the defaults exactly like load_config does before validating a
        # user-written section.
        for bad in (
            {"enabled": "yes"},
            {"port": -1},
            {"port": True},
            {"port": 70000},
            {"domain_map": {"a": "b"}},  # plain string values are invalid
            {"domain_map": {"a": {"meaning": "no name"}}},
        ):
            cfg = ap_config.default_config(self.repo)
            cfg["dashboard"] = dict(cfg["dashboard"], **bad)
            with self.assertRaises(SystemExit):
                ap_config.validate_config(cfg)
        cfg = ap_config.default_config(self.repo)
        cfg["dashboard"] = []  # not an object at all
        with self.assertRaises(SystemExit):
            ap_config.validate_config(cfg)

    def test_validate_accepts_structured_domain_map(self):
        from autopilot import config as ap_config
        cfg = ap_config.default_config(self.repo)
        cfg["dashboard"]["domain_map"] = {
            "scripts/autopilot/miner.py": {"name": "supply-and-prospecting", "meaning": "ore survey and prospecting"},
        }
        ap_config.validate_config(cfg)  # no raise = pass

    def test_config_set_dashboard_flags(self):
        from autopilot import config as ap_config
        self.run_state("init")
        run = self.run_state("config-set", "--dashboard", "--dashboard-port", "8642")
        self.assertEqual(run.returncode, 0, run.stderr)
        cfg = ap_config.load_config(self.repo)
        self.assertTrue(cfg["dashboard"]["enabled"])
        self.assertEqual(cfg["dashboard"]["port"], 8642)
        run = self.run_state("config-set", "--no-dashboard")
        self.assertEqual(run.returncode, 0, run.stderr)
        cfg = ap_config.load_config(self.repo)
        self.assertFalse(cfg["dashboard"]["enabled"])
        # A partial dashboard write preserves sibling keys instead of
        # clobbering the section back to defaults.
        self.assertEqual(cfg["dashboard"]["port"], 8642)
        # Port 0 (random) is a valid choice, not a missing value: writing it
        # must stick instead of being dropped as falsy.
        run = self.run_state("config-set", "--dashboard-port", "0")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(ap_config.load_config(self.repo)["dashboard"]["port"], 0)
        run = self.run_state("config-set", "--dashboard-port", "70000")
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("0..65535", run.stderr)

    def test_config_set_normalizes_dashboard_null(self):
        """validate_config sanctions "dashboard": null and load_config passes
        it through as None; config-set must normalize it to an object before
        merging instead of crashing with AttributeError inside the run lock."""
        from autopilot import config as ap_config
        self.run_state("init")
        (self.repo / ".autopilot" / "config.json").write_text(
            json.dumps({"dashboard": None}), encoding="utf-8"
        )
        run = self.run_state("config-set", "--dashboard")
        self.assertEqual(run.returncode, 0, run.stderr)
        cfg = ap_config.load_config(self.repo)
        self.assertTrue(cfg["dashboard"]["enabled"])
        self.assertEqual(cfg["dashboard"]["auto_open"], True)
        self.assertEqual(cfg["dashboard"]["port"], 0)


class DashboardLifecycleTests(unittest.TestCase):
    """Lifecycle half of the 1.8 dashboard: dashboard.json write/read/probe,
    stale cleanup and the ensure hook. Plain tempdir fixture — these tests
    never run the CLI and never spawn a real server."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dashboard-lifecycle-"))
        self.repo = self.tmp / "repo"
        (self.repo / ".git").mkdir(parents=True)

    def test_info_roundtrip_and_stale_cleanup(self):
        from autopilot import dashboard as ap_dash
        ap_dash.write_info(self.repo, {"pid": os.getpid(), "port": 1234,
                                       "started_at": "t", "opened": False})
        info = ap_dash.read_info(self.repo)
        self.assertEqual(info["port"], 1234)
        ap_dash.write_info(self.repo, {"pid": 999999999, "port": 1,
                                       "started_at": "t", "opened": False})
        self.assertFalse(ap_dash.info_alive(self.repo))      # 死 pid → 不存活
        ap_dash.clear_stale_info(self.repo)
        self.assertIsNone(ap_dash.read_info(self.repo))      # 已清理

    def test_ensure_disabled_is_noop(self):
        from autopilot import dashboard as ap_dash
        ap_dash.ensure_dashboard(self.repo, {"dashboard": {"enabled": False}})
        self.assertIsNone(ap_dash.read_info(self.repo))


class DashboardDataTests(unittest.TestCase):
    """Pure tests of the dashboard data pipeline (no repo fixture, FakeIO
    injected): numstat parsing and per-round file-change aggregation."""

    def test_parse_numstat_lines_basic_rename_and_binary(self):
        from autopilot import dashboard_data as dd
        raw = (
            "12\t3\tscripts/autopilot/state.py\n"
            "0\t0\told/{name.py => renamed.py}\n"
            "-\t-\tassets/logo.png\n"
            "5\t1\t\"quoted/路径#.py\"\n"
        )
        changes = dd.parse_numstat(raw)
        by_path = {c["path"]: c for c in changes}
        self.assertEqual(by_path["scripts/autopilot/state.py"]["insertions"], 12)
        self.assertEqual(by_path["scripts/autopilot/state.py"]["deletions"], 3)
        self.assertEqual(by_path["old/renamed.py"]["insertions"], 0)      # rename 归新路径
        self.assertNotIn("old/name.py", by_path)
        self.assertEqual(by_path["assets/logo.png"]["insertions"], 0)     # 二进制计 0 行
        self.assertTrue(by_path["assets/logo.png"]["binary"])
        self.assertIn("quoted/路径#.py", by_path)                          # 已 unquote
        self.assertFalse(by_path["scripts/autopilot/state.py"]["renamed"])

    def test_parse_numstat_cross_directory_rename_keeps_prefix(self):
        """Brace-form renames wrap only the differing suffix around the shared
        prefix: `a/{x.py => sub/y.py}` normalizes to a/sub/y.py and whole-dir
        braces `d/{ => sub}/g.py` to d/sub/g.py — the prefix must survive."""
        from autopilot import dashboard_data as dd
        changes = dd.parse_numstat(
            "2\t1\ta/{x.py => sub/y.py}\n"
            "4\t0\t{olddir => newdir}/f.py\n"
            "1\t1\tdir/{ => sub}/g.py\n"
        )
        by_path = {c["path"]: c for c in changes}
        self.assertEqual(by_path["a/sub/y.py"]["renamed"], True)
        self.assertEqual(by_path["newdir/f.py"]["deletions"], 0)
        self.assertEqual(by_path["dir/sub/g.py"]["insertions"], 1)

    def test_compute_round_file_changes_skips_shaless_rounds(self):
        from autopilot import dashboard_data as dd
        history = [
            {"round": 1, "status": "completed", "commit_sha": "aaa"},
            {"round": 2, "status": "cancelled", "commit_sha": None},   # 零工作取消：无提交
            {"round": 3, "status": "completed", "commit_sha": "bbb"},
        ]
        calls = []

        class FakeIO:
            @staticmethod
            def run_git(repo, *args, **kw):
                calls.append(args)
                return "2\t1\tf.py\n" if "aaa..bbb" in args else "1\t0\ta.py\n"

        changes = dd.compute_round_file_changes("R", history, "000", gitio=FakeIO)
        self.assertEqual(changes["a.py"]["first_round"], 1)
        self.assertEqual(changes["f.py"]["first_round"], 3)
        self.assertEqual(changes["f.py"]["touches"], 1)
        self.assertEqual(len([c for c in calls if "aaa..bbb" in c]), 1)   # 取消轮不产生 diff

    def test_compute_round_file_changes_skips_blocked_and_aborted_rounds(self):
        """blocked / aborted rounds also close without a commit (work stayed
        uncommitted): no anchor is legitimate, they consume no diff and the
        anchor stays put — exactly one diff spans the two completed rounds."""
        from autopilot import dashboard_data as dd
        history = [
            {"round": 1, "status": "completed", "commit_sha": "aaa"},
            {"round": 2, "status": "blocked", "commit_sha": None},
            {"round": 3, "status": "aborted", "commit_sha": None},
            {"round": 4, "status": "completed", "commit_sha": "bbb"},
        ]
        calls = []

        class FakeIO:
            @staticmethod
            def run_git(repo, *args, **kw):
                calls.append(args)
                return "1\t0\ta.py\n" if "000..aaa" in args else "1\t0\tb.py\n"

        changes = dd.compute_round_file_changes("R", history, "000", gitio=FakeIO)
        # 4 轮历史只产生 2 次 diff：blocked/aborted 轮不产生 diff，锚点原地不动
        self.assertEqual([c[2] for c in calls], ["000..aaa", "aaa..bbb"])
        self.assertEqual(changes["a.py"]["rounds"], [1])
        self.assertEqual(changes["b.py"]["rounds"], [4])
        self.assertEqual(changes["b.py"]["touches"], 1)

    def test_compute_round_file_changes_shaless_completed_round_degrades_to_none(self):
        """A completed round without commit_sha (deferred/legacy state) cannot
        anchor its diff: the whole growth view degrades to None rather than
        presenting a partial picture."""
        from autopilot import dashboard_data as dd
        history = [
            {"round": 1, "status": "completed", "commit_sha": "aaa"},
            {"round": 2, "status": "completed", "commit_sha": None},
        ]

        calls = []

        class FakeIO:
            @staticmethod
            def run_git(repo, *args, **kw):
                calls.append(args)
                return "1\t0\ta.py\n"

        changes = dd.compute_round_file_changes("R", history, "000", gitio=FakeIO)
        self.assertIsNone(changes)
        self.assertEqual([c[2] for c in calls], ["000..aaa"])   # 缺锚点轮之前仅第 1 轮 diff

    def test_compute_round_file_changes_git_failure_degrades_to_none(self):
        """A failed git diff (run_git -> None) must degrade to None: silently
        rendering growth as empty would understate the run's work."""
        from autopilot import dashboard_data as dd
        history = [{"round": 1, "status": "completed", "commit_sha": "aaa"}]

        class FakeIO:
            @staticmethod
            def run_git(repo, *args, **kw):
                return None

        self.assertIsNone(dd.compute_round_file_changes("R", history, "000", gitio=FakeIO))

    def test_compute_round_file_changes_returns_none_when_anchor_missing(self):
        from autopilot import dashboard_data as dd
        self.assertIsNone(dd.compute_round_file_changes("R", [{"round": 1, "commit_sha": None}], "000"))

    def test_compute_round_file_changes_aggregates_multi_round_touches(self):
        from autopilot import dashboard_data as dd
        history = [
            {"round": 1, "status": "completed", "commit_sha": "aaa"},
            {"round": 2, "status": "completed", "commit_sha": "bbb"},
            {"round": 4, "status": "completed", "commit_sha": "ccc"},
        ]
        diffs = {
            "000..aaa": "5\t2\tcore.py\n",
            "aaa..bbb": "1\t0\tcore.py\n",
            "bbb..ccc": "0\t3\tcore.py\n3\t1\tother.py\n",
        }

        class FakeIO:
            @staticmethod
            def run_git(repo, *args, **kw):
                return diffs[args[-1]]

        changes = dd.compute_round_file_changes("R", history, "000", gitio=FakeIO)
        core = changes["core.py"]
        self.assertEqual(core["touches"], 3)
        self.assertEqual(core["insertions"], 6)
        self.assertEqual(core["deletions"], 5)
        self.assertEqual(core["rounds"], [1, 2, 4])
        self.assertEqual(core["first_round"], 1)
        self.assertEqual(changes["other.py"]["first_round"], 4)

    FIXTURE_CHANGES = {
        "scripts/autopilot/miner.py": {"first_round": 8, "touches": 6, "insertions": 180, "deletions": 44, "rounds": [8, 14, 20]},
        "scripts/autopilot/state.py": {"first_round": 1, "touches": 3, "insertions": 90, "deletions": 10, "rounds": [1, 16]},
        "scripts/test_autopilot_state.py": {"first_round": 1, "touches": 10, "insertions": 900, "deletions": 100, "rounds": [1, 18]},
        "references/config.md": {"first_round": 12, "touches": 2, "insertions": 30, "deletions": 4, "rounds": [12]},
        "Makefile": {"first_round": 2, "touches": 1, "insertions": 5, "deletions": 0, "rounds": [2]},
    }

    def test_aggregate_builtin_heuristics(self):
        """纯内置启发式归域：scripts/test_*.py 是测试而非工具（test 规则先于
        scripts/ 且匹配基名），scripts/ 下实现文件归工具与脚本，根散文件归
        配置与入口；touches 最大者 weight 归一为 1，域按 touches 降序稳定。"""
        from autopilot import dashboard_data as dd
        domains = dd.aggregate_modules(self.FIXTURE_CHANGES, None)
        by_name = {d["name"]: d for d in domains}
        self.assertIn("工具与脚本", by_name)
        self.assertIn("测试", by_name)
        self.assertIn("文档与知识", by_name)
        self.assertIn("配置与入口", by_name)                    # 根散文件 Makefile 无前缀命中
        self.assertNotIn("其他", by_name)                       # 不可达域名不得存在
        self.assertEqual(by_name["测试"]["modules"][0]["path"], "scripts/test_autopilot_state.py")
        self.assertEqual(by_name["测试"]["weight"], 1.0)        # touches 最大者归一为 1
        self.assertEqual(by_name["测试"]["meaning"], dd.BUILTIN_DOMAIN_MEANINGS["测试"])
        self.assertLess(by_name["工具与脚本"]["weight"], by_name["测试"]["weight"])
        self.assertEqual([d["name"] for d in domains], ["测试", "工具与脚本", "文档与知识", "配置与入口"])
        tools = by_name["工具与脚本"]
        self.assertEqual(tools["first_round"], 1)               # miner(8) 与 state(1) 取 min
        self.assertEqual(tools["active_rounds"], [1, 8, 14, 16, 20])
        miner = tools["modules"][0]
        self.assertEqual(miner["name"], "miner.py")
        self.assertEqual(miner["churn"], {"touches": 6, "insertions": 180, "deletions": 44})
        self.assertEqual(miner["files"], ["scripts/autopilot/miner.py"])
        self.assertEqual(dd.aggregate_modules({}, None), [])    # 空输入 → 空域
        legacy = dd.aggregate_modules({
            "a.py": {"first_round": None, "touches": 2, "insertions": 1, "deletions": 0, "rounds": [None]},
            "b.py": {"first_round": 3, "touches": 1, "insertions": 1, "deletions": 0, "rounds": [3]},
        }, None)
        self.assertEqual(legacy[0]["first_round"], 3)           # None 轮号不参与 min
        self.assertEqual(legacy[0]["active_rounds"], [3, None])  # None 轮号排最后

    def test_aggregate_domain_map_overrides_and_meaning(self):
        """domain_map 最长前缀优先于内置规则；meaning 显式给出则用之，与内置
        域名撞名则回退内置默认释义，全新域名未给 meaning 则留空。"""
        from autopilot import dashboard_data as dd
        domain_map = {
            "scripts/autopilot/miner.py": {"name": "供给与探矿", "meaning": "挖掘器决定迭代上限"},
            "scripts/autopilot/": {"name": "核心循环", "meaning": "每轮执行的主路径"},
            "references/": {"name": "文档与知识"},   # 撞内置名：meaning 回退内置默认
            "Makefile": {"name": "自定义入口"},       # 未给 meaning：留空
        }
        domains = dd.aggregate_modules(self.FIXTURE_CHANGES, domain_map)
        by_name = {d["name"]: d for d in domains}
        self.assertEqual(by_name["供给与探矿"]["meaning"], "挖掘器决定迭代上限")
        self.assertEqual(by_name["供给与探矿"]["modules"][0]["path"], "scripts/autopilot/miner.py")
        self.assertEqual(by_name["核心循环"]["modules"][0]["path"], "scripts/autopilot/state.py")
        self.assertEqual(by_name["供给与探矿"]["weight"], 0.6)   # 6/10，对自定义域同样归一
        self.assertEqual(by_name["文档与知识"]["meaning"], dd.BUILTIN_DOMAIN_MEANINGS["文档与知识"])
        self.assertEqual(by_name["自定义入口"]["meaning"], "")
        self.assertNotIn("工具与脚本", by_name)                  # scripts/* 全部被映射覆盖
        self.assertIn("测试", by_name)                           # 测试文件不在映射内，仍走内置

    def test_correlate_events_maps_domains_and_keeps_all_statuses(self):
        """history 序即展示序（state.history 本就旧→新）；cancelled/aborted 轮
        如实入史（由上游渲染灰卡）；缺 review_score 记 None；domains 取该轮
        改动归因到的域并典序稳定（Task 6 传入的是 set，sorted 同样适用）。"""
        from autopilot import dashboard_data as dd
        history = [
            {"round": 20, "status": "cancelled", "title": "exhausted 只计 apply run",
             "summary": "…", "review_score": None, "commit_sha": None},
            {"round": 22, "status": "completed", "title": "exhausted 只计 apply run",
             "summary": "只读探测不再打断零新增序列", "review_score": 4, "commit_sha": "20c6a6e"},
        ]
        round_domains = {20: ["supply"], 22: ["supply", "quality"]}
        events = dd.correlate_events(history, round_domains)
        self.assertEqual(events[0]["round"], 20)
        self.assertEqual(events[0]["status"], "cancelled")       # 灰卡如实入史
        self.assertIsNone(events[0]["score"])
        self.assertEqual(events[1]["domains"], ["quality", "supply"])  # 排序稳定
        self.assertEqual(events[1]["score"], 4)
        self.assertEqual(events[1]["title"], "exhausted 只计 apply run")
        self.assertEqual(events[1]["commit_sha"], "20c6a6e")

    def test_correlate_events_defaults_domains_to_empty(self):
        from autopilot import dashboard_data as dd
        events = dd.correlate_events([{"round": 1, "status": "completed", "title": "t",
                                       "summary": "", "review_score": None, "commit_sha": "x"}], {})
        self.assertEqual(events[0]["domains"], [])


class DashboardSnapshotTests(AutopilotTestBase):
    """build_snapshot 三板块组装（1.8.0 观察台 Task 6）：no-run 报错、缺锚点
    与坏 state 的诚实降级、真 git 提交下的完整 growth 统计与 backlog 计数。
    夹具直接落盘最小 state.json——快照只读 state.json，不走迁移不写任何文件。"""

    def _seed_state(self, **overrides):
        st = {
            "schema": "auto-iterate-state/1", "run_id": "run-test",
            "repo": str(self.repo), "branch": "main",
            "created_at": "2026-09-26T00:00:00+00:00",
            "started_at": "2026-09-26T00:00:00+00:00",
            "round": 1, "round_seq": 1,
            "blocked_rounds": 0, "cancelled_rounds": 0,
            # 故意与 history 不符：completed_rounds 必须从 history 统计，
            # 不能照抄 state 键（快照契约）。
            "completed_rounds": 99,
            "estimated_tokens_used": 4200,
            "goals": ["g1", "g2"], "completed_goals": ["g1"],
            "expansion_waves": [{"id": 1}],
            "history": [], "finished_at": None, "run_start_sha": None,
        }
        st.update(overrides)
        ap_io.save_json(self.repo / ".autopilot" / "state.json", st)
        return st

    def test_snapshot_no_run_reports_error(self):
        from autopilot import dashboard_data as dd
        self.assertEqual(dd.build_snapshot(self.repo), {"error": "no-run"})

    def test_snapshot_shape_and_degraded_growth(self):
        from autopilot import dashboard_data as dd
        from autopilot import __version__ as skill_version
        self._seed_state(history=[
            {"round": 1, "status": "completed", "title": "t1", "summary": "s1",
             "review_score": 4, "commit_sha": None, "estimated_tokens": 0},
        ])
        snap = dd.build_snapshot(self.repo)
        self.assertEqual(snap["meta"]["degraded"], ["growth"])   # 完成轮无锚点
        self.assertEqual(snap["meta"]["run_id"], "run-test")
        self.assertEqual(snap["meta"]["skill_version"], skill_version)
        self.assertTrue(snap["meta"]["generated_at"])
        self.assertEqual(snap["status"]["phase"], "running")
        self.assertEqual(snap["status"]["completed_rounds"], 1)  # 从 history 统计
        self.assertEqual(snap["status"]["goals"], {"total": 2, "met": 1})
        self.assertEqual(snap["status"]["budget"],
                         {"max_minutes": None, "estimated_tokens_used": 4200})
        self.assertEqual(snap["status"]["expansion_waves"], 1)
        self.assertEqual(snap["status"]["backlog"],
                         {"total": 0, "pending": 0, "ready": 0})
        self.assertEqual(snap["growth"]["domains"], [])
        self.assertEqual(snap["growth"]["rounds"], [])
        self.assertEqual(len(snap["growth"]["events"]), 1)       # 事件仍产出
        self.assertEqual(snap["growth"]["events"][0]["title"], "t1")
        self.assertEqual(snap["growth"]["events"][0]["domains"], [])
        self.assertFalse(snap["narrative"]["has_last_summary"])
        self.assertFalse(snap["narrative"]["retrospective_exists"])
        # 收尾后的 phase 与两份叙事文件的存在性
        (self.repo / ".autopilot" / "last-summary.md").write_text("s", encoding="utf-8")
        (self.repo / ".autopilot" / "retrospective.md").write_text("r", encoding="utf-8")
        self._seed_state(finished_at="2026-09-26T01:00:00+00:00",
                         history=[{"round": 1, "status": "completed", "title": "t1",
                                   "summary": "s1", "review_score": 4, "commit_sha": None}])
        snap = dd.build_snapshot(self.repo)
        self.assertEqual(snap["status"]["phase"], "finished")
        self.assertTrue(snap["narrative"]["has_last_summary"])
        self.assertTrue(snap["narrative"]["retrospective_exists"])

    def test_snapshot_full_growth_path(self):
        from autopilot import dashboard_data as dd
        (self.repo / "a.py").write_text("x = 1\n", encoding="utf-8")
        self.git("add", "a.py")
        self.git("commit", "-q", "-m", "r1")
        sha1 = self.git("rev-parse", "HEAD").stdout.strip()
        (self.repo / "a.py").write_text("x = 1\nx += 1\n", encoding="utf-8")
        (self.repo / "b.py").write_text("y = 2\n", encoding="utf-8")
        self.git("add", "a.py", "b.py")
        self.git("commit", "-q", "-m", "r2")
        sha2 = self.git("rev-parse", "HEAD").stdout.strip()
        ap_io.save_json(self.repo / ".autopilot" / "config.json", {
            "max_minutes": 30,
            "dashboard": {"domain_map": {"a.py": {"name": "入口域"}}},
        })
        self._seed_state(history=[
            {"round": 1, "status": "completed", "title": "one", "summary": "",
             "review_score": 5, "commit_sha": sha1},
            {"round": 2, "status": "completed", "title": "two", "summary": "",
             "review_score": 3, "commit_sha": sha2},
        ])
        calls = []

        class DelegatingIO:
            @staticmethod
            def run_git(repo_, *args):
                calls.append(args)
                result = ap_io.run_git(repo_, *args)
                return result.stdout if result.returncode == 0 else None

        snap = dd.build_snapshot(self.repo, gitio=DelegatingIO)
        self.assertEqual(snap["meta"]["degraded"], [])
        # gitio 透传：锚点 numstat 走查跑两遍（per-path 聚合与 per-round 统计
        # 各 2 轮 diff），共 4 次调用——未透传会是 0 次。
        self.assertEqual(len(calls), 4)
        self.assertEqual(snap["status"]["completed_rounds"], 2)
        self.assertEqual(snap["status"]["budget"]["max_minutes"], 30)
        self.assertEqual([d["name"] for d in snap["growth"]["domains"]],
                         ["入口域", "配置与入口"])   # domain_map 覆盖 + 内置归域
        events = snap["growth"]["events"]
        self.assertEqual(events[0]["domains"], ["入口域"])
        self.assertEqual(events[1]["domains"], ["入口域", "配置与入口"])
        self.assertEqual(events[0]["score"], 5)
        # 按轮独立统计：a.py 两轮各 +1 行——r2 的插入数是 a(1)+b(1)=2，而不是
        # 按路径聚合错把 r1 的 +1 重复计入 r2（那样会是 3）。
        self.assertEqual(snap["growth"]["rounds"], [
            {"round": 1, "status": "completed", "score": 5,
             "files_changed": 1, "insertions": 1, "deletions": 0},
            {"round": 2, "status": "completed", "score": 3,
             "files_changed": 2, "insertions": 2, "deletions": 0},
        ])

    def test_snapshot_corrupt_state_degrades_all(self):
        from autopilot import dashboard_data as dd
        (self.repo / ".autopilot").mkdir()
        (self.repo / ".autopilot" / "state.json").write_text("{oops", encoding="utf-8")
        snap = dd.build_snapshot(self.repo)
        self.assertEqual(snap["meta"]["degraded"], ["status", "growth", "narrative"])
        self.assertIsNone(snap["meta"]["run_id"])
        self.assertTrue(snap["meta"]["generated_at"])
        self.assertIsNone(snap["status"])
        self.assertIsNone(snap["growth"])
        self.assertIsNone(snap["narrative"])

    def test_backlog_summary_counts(self):
        from autopilot import dashboard_data as dd
        path = self.repo / ".autopilot" / "backlog.json"
        self.assertEqual(dd._backlog_summary(path),            # 缺失 → 全 0
                         {"total": 0, "pending": 0, "ready": 0})
        # 真实结构（本仓库实测）：{"next_id": n, "candidates": […]}，status
        # 值域 pending/picked/completed/blocked，候选带 1-5 的 value 整数。
        ap_io.save_json(path, {"next_id": 6, "candidates": [
            {"id": "candidate-001", "status": "pending", "value": 5},
            {"id": "candidate-002", "status": "pending", "value": 4},
            {"id": "candidate-003", "status": "pending", "value": 3},
            {"id": "candidate-004", "status": "completed", "value": 5},
            {"id": "candidate-005", "status": "picked", "value": 5},
        ]})
        self.assertEqual(dd._backlog_summary(path),
                         {"total": 5, "pending": 3, "ready": 2})
        path.write_text("{oops", encoding="utf-8")             # 坏 JSON 降级全 0
        self.assertEqual(dd._backlog_summary(path),
                         {"total": 0, "pending": 0, "ready": 0})
        ap_io.save_json(path, [{"status": "pending", "value": 5}])  # 裸 list 容忍
        self.assertEqual(dd._backlog_summary(path),
                         {"total": 1, "pending": 1, "ready": 1})

    def test_compute_round_stats_mirrors_anchor_rules(self):
        """与 compute_round_file_changes 同一条锚点走查：NO_ANCHOR 轮不产生
        条目且锚点原地不动；缺锚点的完成轮 / diff 失败整体降级 None。"""
        from autopilot import dashboard_data as dd
        history = [
            {"round": 1, "status": "completed", "commit_sha": "aaa"},
            {"round": 2, "status": "cancelled", "commit_sha": None},
            {"round": 3, "status": "completed", "commit_sha": "bbb"},
            {"round": 4, "status": "completed", "commit_sha": None},
        ]

        class FakeIO:
            @staticmethod
            def run_git(repo, *args, **kw):
                return "2\t1\ta.py\n" if "000..aaa" in args else "1\t0\tb.py\n1\t1\ta.py\n"

        self.assertEqual(dd.compute_round_stats("R", history[:3], "000", gitio=FakeIO), [
            {"round": 1, "status": "completed",
             "files_changed": 1, "insertions": 2, "deletions": 1},
            {"round": 3, "status": "completed",
             "files_changed": 2, "insertions": 2, "deletions": 1},
        ])
        self.assertEqual(dd.compute_round_stats("R", [], "000", gitio=FakeIO), [])
        self.assertIsNone(                                     # 缺锚点完成轮
            dd.compute_round_stats("R", history, "000", gitio=FakeIO))

        class FailingIO:
            @staticmethod
            def run_git(repo, *args, **kw):
                return None

        self.assertIsNone(dd.compute_round_stats("R", history[:3], "000", gitio=FailingIO))


class DashboardPageContractTests(unittest.TestCase):
    """dashboard.html page contract (1.8.0 dashboard Task 9): the page ships
    inside the package (the HTTP server reads it straight from there) and
    carries the dual-theme design tokens plus the single reduced-motion
    degradation block."""

    PAGE = (Path(autopilot.__file__).resolve().parent / "dashboard.html")

    def test_page_exists_and_carries_design_tokens(self):
        html = self.PAGE.read_text(encoding="utf-8")
        for token in ("--bg", "--surface", "--accent", "prefers-color-scheme",
                      "prefers-reduced-motion"):
            self.assertIn(token, html)


class DashboardServerTests(unittest.TestCase):
    """HTTP half of the 1.8 dashboard (Task 8): the two endpoints on a real
    loopback server (random port, daemon thread), the mtime-keyed snapshot
    cache and the read-only guarantee. Repo fixture mirrors
    DashboardLifecycleTests (fake .git, no state run)."""

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp(prefix="dashboard-server-"))
        (self.repo / ".git").mkdir()
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)

    def _start(self):
        from autopilot import dashboard as ap_dash
        server, port = ap_dash.start_in_thread(self.repo)
        # addCleanup 是 LIFO：server_close 注册在前，实际先执行 shutdown
        # 停掉 serve_forever、再关监听 socket，避免 unclosed-socket 告警。
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return port

    def test_endpoints_and_readonly(self):
        import json as _json
        import urllib.error
        import urllib.request
        port = self._start()
        base = "http://127.0.0.1:{}".format(port)
        with urllib.request.urlopen(base + "/", timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("text/html", resp.headers["Content-Type"])
        with urllib.request.urlopen(base + "/api/snapshot", timeout=5) as resp:
            snap = _json.loads(resp.read().decode("utf-8"))
            self.assertEqual(snap, {"error": "no-run"})
        try:
            urllib.request.urlopen(base + "/nope", timeout=5)
            self.fail("expected 404")
        except urllib.error.HTTPError as err:
            self.assertEqual(err.code, 404)
        # 只读保证：请求前后 .autopilot 目录内容一致
        ap_dir = self.repo / ".autopilot"
        before = sorted(p.name for p in ap_dir.glob("*")) if ap_dir.exists() else []
        with urllib.request.urlopen(base + "/api/snapshot", timeout=5):
            pass
        after = sorted(p.name for p in ap_dir.glob("*")) if ap_dir.exists() else []
        self.assertEqual(before, after)

    def test_cached_snapshot_hits_until_invalidated(self):
        import json as _json
        import urllib.request
        from autopilot import dashboard as ap_dash
        # 缓存断言读 meta.generated_at，而 no-run repo 只返回
        # {"error": "no-run"}：先种一个最小 state.json（空对象即可，
        # build_snapshot 照常组装全板块 meta）。
        ap_dir = self.repo / ".autopilot"
        ap_dir.mkdir()
        (ap_dir / "state.json").write_text("{}", encoding="utf-8")
        port = self._start()
        base = "http://127.0.0.1:{}/api/snapshot".format(port)
        first = _json.loads(urllib.request.urlopen(base, timeout=5).read())
        second = _json.loads(urllib.request.urlopen(base, timeout=5).read())
        self.assertEqual(first["meta"]["generated_at"], second["meta"]["generated_at"])
        ap_dash.invalidate_snapshot_cache()
        third = _json.loads(urllib.request.urlopen(base, timeout=5).read())
        self.assertNotEqual(second["meta"]["generated_at"], third["meta"]["generated_at"])


if __name__ == "__main__":
    unittest.main()
