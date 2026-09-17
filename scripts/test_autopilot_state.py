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
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "autopilot_state.py"


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
                    "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR"):
            self.env.pop(var, None)
        self.global_config = Path(self.tmp) / "global-gitconfig"
        self.global_config.write_text("", encoding="utf-8")
        self.env["GIT_CONFIG_GLOBAL"] = str(self.global_config)
        self.env["GIT_CONFIG_SYSTEM"] = str(self.global_config)
        self.env["PYTHONIOENCODING"] = "utf-8"
        self.env["LC_ALL"] = "C"
        self.env["GIT_CEILING_DIRECTORIES"] = str(Path(self.tmp).parent).replace("\\", "/")
        self.git("init", "-q")
        self.git("config", "user.name", "Test User")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "commit.gpgsign", "false")

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
        return subprocess.run(
            [sys.executable, str(self.script), command, "--repo", str(self.repo)] + list(args),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            encoding="utf-8",
            errors="replace",
            env=self.env,
        )

    def read_json(self, name):
        return json.loads((self.repo / ".autopilot" / name).read_text(encoding="utf-8"))


class RepoTest(AutopilotTestBase):
    """A committed git repo with a stable branch name."""

    def setUp(self):
        super().setUp()
        (self.repo / "README.md").write_text("# Test\n", encoding="utf-8")
        self.git("add", "README.md")
        self.git("commit", "-q", "-m", "initial")
        self.initial_branch = self.git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()

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
        self.assertEqual(state["schema"], 5)

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
        self.assertEqual(data["schema"], 5)
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


class MigrationTests(RepoTest):
    def test_migration_persists_fields_on_read(self):
        self.run_state("init")
        state_path = self.repo / ".autopilot" / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["schema"], 5)
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
                "base_value", "success_rate", "calibration", "expected_value",
                "unlocks", "unlock_bonus", "risk", "risk_weight", "risk_factor",
                "saturation_factor", "mix_penalty", "effort_cost",
            },
        )

    def test_state_json_required_keys(self):
        self.run_state("init")
        state_data = self.read_json("state.json")
        for key in (
            "schema", "run_id", "repo", "branch", "origin_branch", "created_at",
            "started_at", "last_activity_at", "round", "completed_rounds",
            "blocked_rounds", "cancelled_rounds", "reverted_rounds",
            "estimated_tokens_used", "type_stats", "goals", "completed_goals",
            "current_round", "history", "stop_reason", "finished_at",
            "config_fingerprint",
        ):
            self.assertIn(key, state_data)


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
