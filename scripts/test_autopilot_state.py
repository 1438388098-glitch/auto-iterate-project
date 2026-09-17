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
from io import StringIO
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "autopilot_state.py"

# Direct import for pure-function adversarial tests (resolve_seed, scoring,
# selection): the package lives next to this file.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from autopilot import state as ap_state  # noqa: E402
from autopilot import io as ap_io  # noqa: E402
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
        covered by the explicit subprocess contract tests."""
        parser = build_parser()
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
        self.assertEqual(config["candidates_per_round"], 3)
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
        result = self.run_state("init")
        self.assertEqual(result.returncode, 0)
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
        self.run_state("goal-met", "--goal", "Ship feature")
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
        self.run_state("finish")
        current = self.git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        self.assertEqual(current, self.initial_branch)

    def test_finish_stay_keeps_branch(self):
        self.run_state("init", "--branch-mode", "feature")
        self.run_state("finish", "--stay")
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
        self.run_state("goal-met", "--goal", "Ship")
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
        self.run_state("finish", "--reason", "done")
        result = self.run_state("begin-round", "--title", "r", "--reason", "x")
        self.assertNotEqual(result.returncode, 0)


class OrphanCommitTests(RepoTest):
    def test_commit_refuses_without_open_round(self):
        self.run_state("init")
        self.add_file()
        result = self.run_state("commit", "--summary", "orphan")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no round is open", result.stderr.lower())

    def test_commit_round_override_allows_orphan(self):
        self.run_state("init")
        self.add_file()
        result = self.run_state("commit", "--round", "7", "--summary", "orphan")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        log = self.git("log", "-1", "--pretty=%s").stdout.strip()
        self.assertEqual(log, "autopilot(round-7): orphan")


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
        the observed candidate of the same type keeps a clean success rate."""
        self.run_state("init")
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


class ImportUnitTests(unittest.TestCase):
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

    # -- _next_sequential_id monotonic after truncation -----------------------
    def test_sequential_id_never_reuses_truncated_ids(self):
        st = {"goal_seeds": [{"id": "seed-{:03d}".format(i), "title": str(i)} for i in range(1, 51)]}
        self.assertEqual(ap_state._next_sequential_id(st, "goal_seeds", "seed-"), "seed-051")
        st["goal_seeds"] = st["goal_seeds"][1:]  # seed-001 truncated away
        self.assertEqual(ap_state._next_sequential_id(st, "goal_seeds", "seed-"), "seed-052")

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
        result = self.run_state("goal-met", "--goal", "提升测试质量\u200b")
        self.assertEqual(result.returncode, 0, result.stderr)
        # The near-duplicate must not leave the run unable to stop.
        data = json.loads(self.run_state("check", "--brief").stdout)
        self.assertTrue(data["goals_met"])
        self.assertFalse(data["continue"])

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
        must match SKILL.md frontmatter, agents/openai.yaml, README.md, and the
        newest CHANGELOG section — drift fails here instead of at release time."""
        import autopilot

        repo_root = Path(__file__).resolve().parent.parent
        version = autopilot.__version__
        skill = (repo_root / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("version: {}".format(version), skill)
        yaml_text = (repo_root / "agents" / "openai.yaml").read_text(encoding="utf-8")
        self.assertIn("version: {}".format(version), yaml_text)
        readme = (repo_root / "README.md").read_text(encoding="utf-8")
        self.assertIn("Version {}".format(version), readme)
        changelog = (repo_root / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIn("## {} (".format(version), changelog)

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


class ImportUnitTests(unittest.TestCase):
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
        self.assertAlmostEqual(score, 5.0 * 0.7, places=3)
        self.assertAlmostEqual(breakdown["saturation_factor"], 0.7, places=3)

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
        result = self.run_state("finish", "--reason", "done")
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
        result = self.run_state("commit", "--summary", "bin")
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
        result = self.run_state("finish", "--reason", "done", "--json")
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
        self.run_state("goal-met", "--goal", "G")

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
        self.run_state("finish")
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
                "continue", "stop_reason", "warnings", "goals_met", "phase",
                "next_verify_round", "next_commit_round", "next_checkpoint_round",
                "backlog", "action_hint",
            },
        )
        self.assertEqual(
            set(data["backlog"]),
            {"pending", "ready", "min_pending_candidates", "needs_expansion"},
        )
        self.assertIn(data["action_hint"], ("work", "expand", "stop"))

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
            "goal_events", "goal_seeds",
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
        self.assertEqual(data["action_hint"], "expand")
        self.assertTrue(data["backlog"]["needs_expansion"])
        self.assertEqual(data["backlog"]["pending"], 0)
        joined = " ".join(data["warnings"]).lower()
        self.assertIn("deep expansion", joined)
        self.assertIn("do not idle", joined)

    def test_thin_backlog_warns_below_min_pending(self):
        self.run_state("init", "--min-pending-candidates", "3")
        self.run_state("backlog-add", "--title", "A", "--reason", "r", "--value", "4", "--effort", "2")
        data = json.loads(self.run_state("check", "--brief").stdout)
        self.assertTrue(data["continue"])
        self.assertEqual(data["action_hint"], "expand")
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
        self.assertEqual(data["action_hint"], "expand")
        self.assertTrue(any("dependency-ready" in w or "Deep Expansion" in w for w in data["warnings"]))

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


if __name__ == "__main__":
    unittest.main()
