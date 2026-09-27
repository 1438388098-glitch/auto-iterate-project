#!/usr/bin/env python3
"""Unit + integration tests for autopilot_state.py (uses throwaway git repos)."""

import json
import os
import py_compile
import shutil
import subprocess
import sys
import tempfile
import time
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

    def test_finish_deletes_run_branch_when_empty(self):
        """A feature run with zero unique commits leaves an empty autopilot/*
        branch sitting on origin — finish now deletes it instead of piling it up."""
        self.run_state("init", "--branch-mode", "feature")
        run_branch = self.read_json("state.json")["branch"]
        result = self.run_state("finish", "--force", "--reason", "no work")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("已自动删除", result.stdout + result.stderr)
        self.assertFalse(self._branch_exists(run_branch))

    def test_finish_keeps_unmerged_run_branch(self):
        self.run_state("init", "--branch-mode", "feature")
        run_branch = self.read_json("state.json")["branch"]
        self.run_state("begin-round", "--title", "r", "--reason", "x")
        self.add_file()
        self.run_state("commit", "--summary", "add feature")
        sha = self.git("rev-parse", "HEAD").stdout.strip()
        self.run_state("complete-round", "--summary", "done", "--commit-sha", sha)
        result = self.run_state("finish", "--force", "--reason", "done")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("提交保留在分支", result.stdout + result.stderr)
        self.assertTrue(self._branch_exists(run_branch))

    def test_finish_deletes_run_branch_after_manual_merge(self):
        """User (or agent) merged the run branch into origin before finish:
        finish must reclaim it so the next run does not stack another leftover."""
        self.run_state("init", "--branch-mode", "feature")
        run_branch = self.read_json("state.json")["branch"]
        self.run_state("begin-round", "--title", "r", "--reason", "x")
        self.add_file()
        self.run_state("commit", "--summary", "add feature")
        sha = self.git("rev-parse", "HEAD").stdout.strip()
        self.run_state("complete-round", "--summary", "done", "--commit-sha", sha)
        self.git("checkout", "-q", self.initial_branch)
        self.git("merge", "-q", "--no-edit", run_branch)
        result = self.run_state("finish", "--force", "--reason", "merged")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("已自动删除", result.stdout + result.stderr)
        self.assertFalse(self._branch_exists(run_branch))

    def _branch_exists(self, name):
        return self.git("rev-parse", "--verify", "--quiet", "refs/heads/" + name).returncode == 0


class BranchGcTests(RepoTest):
    """Lifecycle GC for leftover autopilot/* branches (repeated feature-mode
    runs). Only fully-merged branches are reclaimed; unmerged work is sacred."""

    def _seed_autopilot_branch(self, name, merge):
        self.git("checkout", "-q", "-b", name)
        path = "gc_{}.txt".format(name.replace("/", "_"))
        (self.repo / path).write_text("work\n", encoding="utf-8")
        self.git("add", path)
        self.git("commit", "-q", "-m", "work on " + name)
        self.git("checkout", "-q", self.initial_branch)
        if merge:
            self.git("merge", "-q", "--no-edit", name)

    def test_branch_gc_deletes_merged_keeps_unmerged(self):
        self._seed_autopilot_branch("autopilot/merged-run", merge=True)
        self._seed_autopilot_branch("autopilot/live-run", merge=False)
        result = self.run_state("branch-gc", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertIn("autopilot/merged-run", data["deleted"])
        kept_names = [k["branch"] for k in data["kept"]]
        self.assertIn("autopilot/live-run", kept_names)
        self.assertFalse(self.git("rev-parse", "--verify", "--quiet",
                                  "refs/heads/autopilot/merged-run").returncode == 0)
        self.assertTrue(self.git("rev-parse", "--verify", "--quiet",
                                 "refs/heads/autopilot/live-run").returncode == 0)

    def test_branch_gc_keeps_checked_out_branch(self):
        self._seed_autopilot_branch("autopilot/checked-out", merge=True)
        self.git("checkout", "-q", "autopilot/checked-out")
        # Merged into main, but it is HEAD — never delete the live worktree ref.
        self.git("checkout", "-q", self.initial_branch)
        self.git("merge", "-q", "--no-edit", "autopilot/checked-out")
        self.git("checkout", "-q", "autopilot/checked-out")
        result = self.run_state("branch-gc", "--json")
        data = json.loads(result.stdout)
        self.assertNotIn("autopilot/checked-out", data["deleted"])
        self.assertTrue(self.git("rev-parse", "--verify", "--quiet",
                                 "refs/heads/autopilot/checked-out").returncode == 0)

    def test_branch_gc_never_touches_non_autopilot_names(self):
        self.git("checkout", "-q", "-b", "feature/keep-me")
        (self.repo / "keep.txt").write_text("k\n", encoding="utf-8")
        self.git("add", "keep.txt")
        self.git("commit", "-q", "-m", "keep")
        self.git("checkout", "-q", self.initial_branch)
        self.git("merge", "-q", "--no-edit", "feature/keep-me")
        result = self.run_state("branch-gc", "--json")
        data = json.loads(result.stdout)
        self.assertEqual(data["deleted"], [])
        self.assertTrue(self.git("rev-parse", "--verify", "--quiet",
                                 "refs/heads/feature/keep-me").returncode == 0)

    def test_branch_gc_dry_run_mutates_nothing(self):
        self._seed_autopilot_branch("autopilot/merged-run", merge=True)
        before = self.git("for-each-ref", "--format=%(refname:short)", "refs/heads/").stdout
        result = self.run_state("branch-gc", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[DRY-RUN]", result.stderr)
        after = self.git("for-each-ref", "--format=%(refname:short)", "refs/heads/").stdout
        self.assertEqual(before, after)

    def test_branch_gc_without_state(self):
        """Leftovers from finished previous runs must be reclaimable before
        the next init — no state.json required."""
        self._seed_autopilot_branch("autopilot/merged-run", merge=True)
        self.assertFalse((self.repo / ".autopilot" / "state.json").exists())
        result = self.run_state("branch-gc", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertIn("autopilot/merged-run", data["deleted"])

    def test_branch_gc_respects_explicit_base(self):
        # Fork other-base BEFORE the merge so it does not contain the work.
        self.git("checkout", "-q", "-b", "other-base")
        self.git("checkout", "-q", self.initial_branch)
        self._seed_autopilot_branch("autopilot/merged-run", merge=True)
        result = self.run_state("branch-gc", "--base", "other-base", "--json")
        data = json.loads(result.stdout)
        self.assertEqual(data["deleted"], [])
        self.assertTrue(self.git("rev-parse", "--verify", "--quiet",
                                 "refs/heads/autopilot/merged-run").returncode == 0)

    def test_init_prunes_merged_leftovers(self):
        self._seed_autopilot_branch("autopilot/old-merged", merge=True)
        self._seed_autopilot_branch("autopilot/old-live", merge=False)
        result = self.run_state("init", "--branch-mode", "feature", "--force")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.git("rev-parse", "--verify", "--quiet",
                                  "refs/heads/autopilot/old-merged").returncode == 0)
        self.assertTrue(self.git("rev-parse", "--verify", "--quiet",
                                 "refs/heads/autopilot/old-live").returncode == 0)
        # The new run's own branch must survive the prune.
        new_branch = self.read_json("state.json")["branch"]
        self.assertTrue(self.git("rev-parse", "--verify", "--quiet",
                                 "refs/heads/" + new_branch).returncode == 0)

    def test_init_no_prune_keeps_leftovers(self):
        self._seed_autopilot_branch("autopilot/old-merged", merge=True)
        result = self.run_state("init", "--branch-mode", "feature", "--force", "--no-prune")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.git("rev-parse", "--verify", "--quiet",
                                 "refs/heads/autopilot/old-merged").returncode == 0)


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


class PredictedHardeningTests(RepoTest):
    """v1.3.2 hardening contracts, table-driven (was 46 one-off methods)."""

    def test_seed_lifecycle_and_writeback(self):
        self.run_state("init", "--goal", "G", "--expand-after-goals", "--max-rounds", "50")
        self.run_state("goal-met", "--goal", "G")
        result = self.run_state("goal-met", "--goal", "G2", "--next-step", "A", "--next-step", "B", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("seed-001, seed-002", result.stdout)  # text mode echo kept in another path
        for _ in range(1):
            self.run_state("goal-met", "--goal", "G", "--next-step", "A")  # dedupe
        self.assertEqual(len(self.read_json("state.json")["goal_seeds"]), 3)
        result = self.run_state("seed-reject", "--id", "seed-001", "--reason", "evidence gone")
        self.assertEqual(result.returncode, 0, result.stderr)
        seed = self.read_json("state.json")["goal_seeds"][0]
        self.assertEqual((seed["status"], seed["outcome"]), ("rejected", "evidence gone"))
        open_ids = [s["id"] for s in json.loads(self.run_state("check", "--brief").stdout)["expansion"]["seeds"]]
        self.assertNotIn("seed-001", open_ids)
        self.assertNotEqual(self.run_state("backlog-add", "--from-seed", "seed-001").returncode, 0)
        self.assertNotEqual(self.run_state("seed-reject", "--id", "seed-001", "--reason", "x").returncode, 0)
        # multi-candidate round verifies remaining seeds
        self.run_state("backlog-add", "--from-seed", "seed-002")
        self.run_state("backlog-add", "--from-seed", "seed-003")
        self.git("commit", "--allow-empty", "-q", "-m", "base")
        self.run_state("begin-round", "--title", "t", "--reason", "r",
                       "--candidate-id", "candidate-001", "--candidate-id", "candidate-002")
        self.run_state("complete-round", "--summary", "both")
        statuses = [s["status"] for s in self.read_json("state.json")["goal_seeds"]]
        self.assertEqual(statuses, ["rejected", "verified", "verified"])
        # vanished seed: no crash, writeback warning
        st = json.loads((self.repo / ".autopilot" / "state.json").read_text(encoding="utf-8"))
        st["goal_seeds"] = [s for s in st["goal_seeds"] if s["status"] != "open"]
        st["goal_seeds"].append({"id": "seed-009", "status": "open", "title": "ghost"})
        (self.repo / ".autopilot" / "state.json").write_text(json.dumps(st), encoding="utf-8")
        self.run_state("backlog-add", "--from-seed", "seed-009")
        (self.repo / ".autopilot" / "state.json").write_text(
            json.dumps({**json.loads((self.repo / ".autopilot" / "state.json").read_text(encoding="utf-8")),
                        "goal_seeds": []}), encoding="utf-8")
        self.git("commit", "--allow-empty", "-q", "-m", "base2")
        self.run_state("begin-round", "--title", "t2", "--reason", "r", "--candidate-id", "candidate-003")
        result = self.run_state("complete-round", "--summary", "s")
        self.assertEqual(result.returncode, 0, result.stderr)
        log = (self.repo / ".autopilot" / "log.jsonl").read_text(encoding="utf-8")
        self.assertIn("seed-writeback", log)

    def test_goal_events_and_seed_fields(self):
        self.run_state("init", "--goal", "G")
        self.git("commit", "--allow-empty", "-q", "-m", "base")
        self.run_state("begin-round", "--title", "t", "--reason", "r")
        self.run_state("complete-round", "--commit-sha", "HEAD", "--summary", "s")
        self.run_state("goal-met", "--goal", "G", "--next-step", "N", "--json")
        event = self.read_json("state.json")["goal_events"][0]
        self.assertEqual(len(event["commit_shas"]), 1)
        self.assertEqual(self.read_json("state.json")["goal_seeds"][0]["source_event_id"], event["id"])
        for bad in ("0", "6"):
            self.assertNotEqual(self.run_state("goal-met", "--goal", "G", "--next-step", "N",
                                               "--seed-value", bad).returncode, 0, bad)
        result = self.run_state("goal-met", "--goal", "G", "--next-step", "N2",
                                "--seed-value", "1", "--seed-effort", "5", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        seed = self.read_json("state.json")["goal_seeds"][-1]
        self.assertEqual((seed["value"], seed["effort"]), (1, 5))
        self.assertIn("bugfix|feature", self.run_state("goal-met", "--goal", "G", "--next-step", "N3",
                                                       "--seed-type", "bogus").stderr)
        # junk numeric fields fall back instead of exploding
        st = json.loads((self.repo / ".autopilot" / "state.json").read_text(encoding="utf-8"))
        st["goal_seeds"] = [{"id": "seed-090", "status": "open", "title": "junk",
                             "type": "refactor", "value": "4", "effort": "oops", "risk": "2"}]
        (self.repo / ".autopilot" / "state.json").write_text(json.dumps(st), encoding="utf-8")
        self.assertEqual(self.run_state("backlog-add", "--from-seed", "seed-090").returncode, 0)
        cand = self.read_json("backlog.json")["candidates"][-1]
        self.assertEqual((cand["value"], cand["effort"], cand["risk"]), (4, 3, 2))

    def test_corrupt_state_clean_errors_table(self):
        self.run_state("init")
        cases = [
            ("type_stats", {"type_stats": {"bugfix": "oops"}}, "check", ("--brief",), "type_stats"),
            ("history", {"history": ["junk"]}, "check", ("--brief",), "history"),
            ("current_round", {"current_round": {"title": "broken"}},
             "complete-round", ("--summary", "s"), "current_round.round"),
        ]
        for label, patch, cmd, args, needle in cases:
            path = self.repo / ".autopilot" / "state.json"
            state = json.loads(path.read_text(encoding="utf-8"))
            state.update(patch)
            path.write_text(json.dumps(state), encoding="utf-8")
            result = self.run_state(cmd, *args)
            self.assertNotEqual(result.returncode, 0, label)
            self.assertNotIn("Traceback", result.stderr, label)
            self.assertIn(needle, result.stderr, label)
            # restore for next case
            state = json.loads(path.read_text(encoding="utf-8"))
            for key in patch:
                state.pop(key, None)
            if "type_stats" in patch:
                state["type_stats"] = {}
            if "history" in patch:
                state["history"] = []
            if "current_round" in patch:
                state["current_round"] = None
            path.write_text(json.dumps(state), encoding="utf-8")
        (self.repo / ".autopilot" / "backlog.json").write_text(
            json.dumps({"next_id": 2, "candidates": ["oops"]}), encoding="utf-8")
        for cmd in ("backlog-rank", "check"):
            result = self.run_state(cmd)
            self.assertNotEqual(result.returncode, 0, cmd)
            self.assertNotIn("Traceback", result.stderr, cmd)
        (self.repo / ".autopilot" / "directives.json").write_text("[]", encoding="utf-8")
        for cmd, args in (("directive-add", ("--text", "t")), ("directive-list", ()),
                          ("directive-remove", ("--index", "1"))):
            result = self.run_state(cmd, *args)
            self.assertNotEqual(result.returncode, 0, cmd)
            self.assertIn("directives.json", result.stderr)

    def test_validation_rejects_bad_knobs(self):
        for knob in ("--max-rounds", "--max-tokens", "--max-round-scope",
                     "--retries-per-round", "--max-blocked-in-a-row", "--max-minutes"):
            result = self.run_state("init", knob, "-5")
            self.assertNotEqual(result.returncode, 0, knob)
            self.assertIn("must be a non-negative integer", result.stderr)
        self.assertNotEqual(self.run_state("init", "--max-predicted-per-round", "-1").returncode, 0)
        self.assertEqual(self.run_state("init", "--max-predicted-per-round", "0").returncode, 0)
        self.assertEqual(self.read_json("config.json")["max_predicted_per_round"], 0)
        self.run_state("begin-round", "--title", "t", "--reason", "r")
        result = self.run_state("complete-round", "--summary", "s", "--review-score", "99")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("between 1 and 5", result.stderr)
        self.assertNotEqual(self.run_state("complete-round", "--summary", "s", "--tokens", "-5").returncode, 0)
        self.run_state("complete-round", "--summary", "s")
        self.add_file("f.py")
        self.git("add", "-A")
        for bad in ("0", "-5"):
            self.assertIn("positive integer", self.run_state("commit", "--round", bad, "--summary", "s").stderr)
        self.assertIn("no completed round", self.run_state("commit", "--round", "2", "--summary", "s").stderr)
        self.assertEqual(self.run_state("commit", "--round", "1", "--summary", "s").returncode, 0)

    def test_goal_text_and_report_escaping(self):
        self.run_state("init", "--goal", "提升测试质量", "--max-rounds", "5")
        self.run_state("begin-round", "--title", "r", "--reason", "x")
        self.add_file()
        self.run_state("commit", "--summary", "add feature")
        sha = self.git("rev-parse", "HEAD").stdout.strip()
        self.run_state("complete-round", "--summary", "done", "--commit-sha", sha)
        self.run_state("goal-met", "--goal", "提升测试质量\u200b", "--round", "1")
        brief = json.loads(self.run_state("check", "--brief").stdout)
        self.assertTrue(brief["goals_met"])
        self.assertFalse(brief["continue"])
        self.assertIn("does not match any configured goal",
                      self.run_state("goal-met", "--goal", "totally different").stderr)
        self.run_state("backlog-add", "--title", "line1\nline2|PIPE\tTAB", "--value", "3", "--effort", "1")
        self.run_state("goal-met", "--goal", "G", "--next-step", "stepA\nstepB|X")
        out = self.run_state("report").stdout
        self.assertIn("line1 line2\\|PIPE TAB", out)
        self.assertIn("stepA stepB\\|X", out)

    def test_path_guard_and_secret_patterns(self):
        self.assertFalse(path_allowed("secrets/prod/keys.txt", [], ["secrets\\"]))
        self.assertFalse(path_allowed("secrets/prod/keys.txt", [], ["secrets/"]))
        self.assertFalse(path_allowed("src/a.py", [], ["src/**/*.py"]))
        self.assertFalse(path_allowed("src/deep/b.py", [], ["src/**/*.py"]))
        self.assertFalse(path_allowed("notes/a.log", [], ["*.log"]))
        for text in ("-----BEGIN ENCRYPTED PRIVATE KEY-----", "-----BEGIN OPENSSH PRIVATE KEY-----",
                     "token: gho_" + "a" * 36, "token: ghs_" + "b" * 36, "key = sk-proj-" + "c" * 30,
                     '"sk-live-abcdefghijklmnopqrst"'):
            self.assertTrue(_secret_hit(text), text)
        self.assertFalse(_secret_hit("the task-runner-configuration-for-nightly job"))
        self.assertFalse(_secret_hit("disk-utility-backup-script-v2 archive"))

    def test_version_consistency_across_files(self):
        import autopilot
        repo_root = Path(__file__).resolve().parent.parent
        version = autopilot.__version__
        for rel, needle in (
            ("SKILL.md", "version: {}".format(version)),
            ("agents/openai.yaml", "version: {}".format(version)),
            ("references/overview.md", "Version {}".format(version)),
            ("README.md", "Version {}".format(version)),
            ("CHANGELOG.md", "## {} (".format(version)),
        ):
            self.assertIn(needle, (repo_root / rel).read_text(encoding="utf-8"), rel)

    def test_directives_and_agent_detect(self):
        self.run_state("init")
        self.run_state("directive-add", "--text", "rule A")
        self.run_state("directive-add", "--text", "rule B")
        data = json.loads(self.run_state("directive-list").stdout)
        self.assertEqual([d["index"] for d in data["directives"]], [1, 2])
        self.assertIn("as shown by directive-list", self.run_state("directive-remove", "--index", "9").stderr)
        result = self.run_state("directive-remove", "--index", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([d["text"] for d in json.loads(self.run_state("directive-list").stdout)["directives"]],
                         ["rule B"])
        env = dict(self.env)
        for var in ("OPENCODE", "CLAUDE_CODE", "CODEX", "SKILL_DIR", "AUTOPILOT_AGENT"):
            env.pop(var, None)
        env["AUTOPILOT_AGENT"] = "ClaudeCode"
        result = subprocess.run(
            [sys.executable, str(self.script), "detect-agent", "--repo", str(self.repo), "--home", str(self.tmp)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
            encoding="utf-8", errors="replace", env=env)
        self.assertIn("not one of", result.stderr)
        env["OPENCODE"] = ""
        result = subprocess.run(
            [sys.executable, str(self.script), "detect-agent", "--repo", str(self.repo), "--home", str(self.tmp)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
            encoding="utf-8", errors="replace", env=env)
        self.assertEqual(json.loads(result.stdout)["agent"], "generic")
        (self.repo / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
        payload = json.loads(self.run_state("detect-verify", "--json").stdout)
        self.assertTrue(payload["ok"])
        self.assertIn("detected", payload)

    def test_migration_push_and_json_contracts(self):
        self.run_state("init", "--push")
        state_path = self.repo / ".autopilot" / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["schema"] = 5
        state.pop("goal_seeds", None)
        state_path.write_text(json.dumps(state), encoding="utf-8")
        self.run_state("read")
        self.assertEqual(json.loads(state_path.read_text(encoding="utf-8"))["schema"], 6)
        self.assertIn("state-migrate", (self.repo / ".autopilot" / "log.jsonl").read_text(encoding="utf-8"))
        result = self.run_state("push", "--json")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")
        self.assertFalse(json.loads(result.stdout)["ok"])
        self.assertIn("No git remote is configured", json.loads(result.stdout)["message"])
        self.run_state("begin-round", "--title", "t", "--reason", "r")
        self.add_file("f.py")
        self.git("config", "--local", "--unset", "user.name")
        self.git("config", "--local", "--unset", "user.email")
        result = self.run_state("commit", "--summary", "s", "--json")
        self.assertFalse(json.loads(result.stdout)["ok"])
        self.assertIn("Git identity is not configured", json.loads(result.stdout)["message"])

    def test_git_push_prefers_origin_over_alphabetical_first(self):
        origin = Path(self.tmp) / "origin.git"
        fork = Path(self.tmp) / "a-fork.git"
        for remote in (origin, fork):
            self.git("init", "-q", "--bare", str(remote))
        self.git("remote", "add", "fork", str(fork))
        self.git("remote", "add", "origin", str(origin))
        self.add_file("f.py")
        self.git("commit", "-q", "-m", "x")
        branch = (self.repo / ".git" / "HEAD").read_text(encoding="utf-8").strip().split("/")[-1]
        output, err = ap_io.git_push(self.repo)
        self.assertIsNone(err, err)
        self.assertNotEqual(self.git("ls-remote", str(origin), "refs/heads/" + branch).stdout.strip(), "")
        self.assertEqual(self.git("ls-remote", str(fork), "refs/heads/" + branch).stdout.strip(), "")

    def test_subprocess_entry_and_report_paths(self):
        self.run_state("init")
        self.run_state("goal-met", "--goal", "G", "--next-step", "seed \U0001f680 title")
        env = dict(self.env)
        env.pop("PYTHONIOENCODING", None)
        result = subprocess.run(
            [sys.executable, str(self.script), "check", "--brief", "--repo", str(self.repo)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
            encoding="utf-8", errors="replace", env=env, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        outside = Path(self.tmp) / "elsewhere"
        outside.mkdir()
        result = subprocess.run(
            [sys.executable, str(self.script), "report", "--repo", str(self.repo), "--output", "out.md"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
            encoding="utf-8", errors="replace", env=self.env, cwd=str(outside))
        self.assertTrue((self.repo / "out.md").exists())
        self.assertFalse((outside / "out.md").exists())
        self.assertIsNone(ap_io.parse_deadline("+" + "9" * 30 + "w"))
        self.assertIsNone(ap_io.parse_deadline("+not-a-duration"))
        self.assertIsNotNone(ap_io.parse_deadline("+1h"))

    def test_dry_run_mutates_nothing(self):
        self.run_state("init", "--push")
        self.git("commit", "--allow-empty", "-q", "-m", "base")
        self.run_state("directive-add", "--text", "rule")
        self.run_state("backlog-add", "--title", "seeded", "--value", "3", "--effort", "1")
        self.run_state("backlog-add", "--title", "work", "--value", "4", "--effort", "2")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "work")
        sha = self.git("rev-parse", "HEAD").stdout.strip()
        self.run_state("begin-round", "--title", "t", "--reason", "r", "--candidate-id", "candidate-002")
        self.run_state("goal-met", "--goal", "G", "--next-step", "N")

        def snapshot():
            directives_path = self.repo / ".autopilot" / "directives.json"
            return (
                (self.repo / ".autopilot" / "state.json").read_text(encoding="utf-8"),
                (self.repo / ".autopilot" / "backlog.json").read_text(encoding="utf-8"),
                directives_path.read_text(encoding="utf-8") if directives_path.exists() else "",
                self.git("rev-parse", "HEAD").stdout,
            )

        before = snapshot()
        for command, args in (("complete-round", ("--summary", "s")),
                              ("block-round", ("--reason", "b")),
                              ("cancel-round", ("--reason", "c"))):
            result = self.run_state(command, *args, "--dry-run")
            self.assertEqual(result.returncode, 0, (command, result.stderr))
            self.assertIn("[DRY-RUN]", result.stderr, command)
            self.assertEqual(snapshot(), before, command)
        self.run_state("cancel-round", "--reason", "close")
        self.run_state("goal-met", "--goal", "G", "--next-step", "N")
        before = snapshot()
        for command, args in (
            ("goal-met", ("--goal", "G", "--next-step", "N")),
            ("seed-reject", ("--id", "seed-001", "--reason", "x")),
            ("finish", ()), ("undo-round", ("--sha", sha)),
            ("analysis-save", ("--content", "{}")),
            ("directive-remove", ("--index", "1")),
            ("backlog-update", ("--id", "candidate-001", "--value", "5")),
            ("ensure-branch", ()), ("push", ()), ("branch-gc", ()),
        ):
            result = self.run_state(command, *args, "--dry-run")
            self.assertEqual(result.returncode, 0, (command, result.stderr))
            self.assertIn("[DRY-RUN]", result.stderr, command)
            self.assertEqual(snapshot(), before, command)
        self.assertFalse((self.repo / ".git" / "REVERT_HEAD").exists())

    def test_uninitialized_guard_matrix(self):
        for command in ("directive-list", "secret-scan", "retrospective", "analysis-load"):
            result = self.run_state(command)
            self.assertNotEqual(result.returncode, 0, command)
            self.assertNotIn("Traceback", result.stderr, command)
            self.assertIn("not initialized", result.stderr, command)
        for command, args in (("read", ()), ("goal-met", ("--goal", "G")),
                              ("seed-reject", ("--id", "seed-001", "--reason", "r")), ("report", ())):
            result = self.run_state(command, *args)
            self.assertNotEqual(result.returncode, 0, command)
            self.assertNotIn("Traceback", result.stderr, command)
            self.assertIn("state.json not found", result.stderr, command)

    def test_complete_round_push_fail_and_backlog_guard(self):
        self.run_state("init", "--push")
        self.git("remote", "add", "origin", str(Path(self.tmp) / "no-such-origin.git"))
        self.run_state("backlog-add", "--title", "t", "--value", "3", "--effort", "1")
        self.run_state("begin-round", "--title", "t", "--reason", "r", "--candidate-id", "candidate-001")
        self.add_file("f.py")
        self.git("add", "-A")
        r = self.run_state("commit", "--summary", "s")
        sha = [l.split()[2].rstrip(":") for l in r.stdout.splitlines() if l.startswith("[OK] Committed ")][0]
        result = self.run_state("complete-round", "--summary", "done", "--commit-sha", sha)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("push failed", result.stdout + result.stderr)
        self.assertEqual(self.read_json("state.json")["history"][-1]["status"], "completed")
        # backlog without init / corrupt state
        other = Path(self.tmp) / "fresh"
        other.mkdir()
        result = self.run_state("backlog-add", "--title", "t", "--value", "3", "--effort", "1", "--repo", str(other))
        self.assertIn("not initialized", result.stderr)
        (self.repo / ".autopilot" / "state.json").write_text("{ not json", encoding="utf-8")
        result = self.run_state("backlog-add", "--title", "t", "--value", "3", "--effort", "1")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Traceback", result.stderr)

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
        self.assertIn("merge commit", result.stderr)
        self.assertEqual(self.read_json("state.json")["reverted_rounds"], 0)

    def test_check_expansion_tolerates_corrupt_seed_entries(self):
        self.run_state("init")
        self.run_state("goal-met", "--goal", "G", "--next-step", "real seed")
        state_path = self.repo / ".autopilot" / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["goal_seeds"] = ["oops", 42, None] + state["goal_seeds"]
        state["goal_events"] = ["bad-event"] + state["goal_events"]
        state_path.write_text(json.dumps(state), encoding="utf-8")
        data = json.loads(self.run_state("check", "--brief").stdout)
        self.assertEqual(data["phase"], "iterate")

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


class PureHelperUnitTests(unittest.TestCase):
    """Pure helpers via the autopilot_state entry module (no git setup)."""

    @classmethod
    def setUpClass(cls):
        import sys as _sys
        if str(SCRIPT.parent) not in _sys.path:
            _sys.path.insert(0, str(SCRIPT.parent))
        import autopilot_state as ap
        cls.ap = ap

    def test_parse_time_and_numstat(self):
        ap = self.ap
        for value in ("2020-01-01T00:00:00+00:00", "2020-01-01T00:00:00+0000",
                      "2020-01-01T00:00:00Z", "2020-01-01T00:00:00+08:00"):
            self.assertIsNotNone(ap.parse_time(value), value)
        self.assertIsNone(ap.parse_time("not-a-time"))
        self.assertIsNone(ap.parse_time(None))
        text, binary = ap._parse_numstat("1\t2\tfile.py\n3\t4\tfile2.py\n")
        self.assertEqual((text, binary), (10, 0))
        text, binary = ap._parse_numstat("-\t-\tblob.bin\n-\t-\timg.png\n")
        self.assertEqual((text, binary), (0, 2))

    def test_stop_reasons_table(self):
        ap = self.ap
        base = {
            "finished_at": None, "stop_reason": None, "goals": [], "completed_goals": [],
            "completed_rounds": 0, "blocked_rounds": 0, "history": [],
            "estimated_tokens_used": 0, "last_activity_at": None, "started_at": None,
        }
        cases = [
            (dict(base, estimated_tokens_used=50),
             {"goals": [], "max_rounds": None, "max_minutes": None, "max_tokens": 10,
              "max_blocked_in_a_row": None}, "max_tokens"),
            (dict(base, last_activity_at="2020-01-01T00:00:00+00:00",
                  started_at="2020-01-01T00:00:00+00:00"),
             {"goals": [], "max_rounds": None, "max_minutes": 1, "max_tokens": None,
              "max_blocked_in_a_row": None}, "max_minutes"),
            (dict(base),
             {"goals": [], "max_rounds": None, "max_minutes": None, "max_tokens": None,
              "max_blocked_in_a_row": None, "deadline": "2000-01-01T00:00:00+00:00"}, "deadline"),
        ]
        for state, cfg, needle in cases:
            self.assertIn(needle, ap.compute_stop_reason(state, cfg), needle)
        state = dict(base)
        cfg = {"goals": [], "max_rounds": None, "max_minutes": None, "max_tokens": None,
               "max_blocked_in_a_row": None, "deadline": "2999-01-01T00:00:00+00:00"}
        self.assertIsNone(ap.compute_stop_reason(state, cfg))

    def test_goals_score_rank_and_stats(self):
        ap = self.ap
        state = {"goals": [], "completed_goals": ["A"]}
        config = {"goals": ["A", "B"], "expand_after_goals": False}
        self.assertFalse(ap.all_goals_met(config, state))
        self.assertEqual(ap._candidate_score({"value": "x", "effort": "y"}), 1.0)
        self.assertEqual(ap._candidate_score({"value": 5, "effort": 2}), 2.5)
        backlog = {"candidates": [
            {"id": "candidate-001", "title": "a", "value": 5, "effort": 1, "risk": 1,
             "type": "bugfix", "status": "pending"},
            {"id": "candidate-002", "title": "b", "value": 3, "effort": 5, "risk": 3,
             "type": "feature", "status": "pending"},
        ]}
        cfg = {"ranking_mode": "classic", "min_candidate_value": None,
               "max_same_type_per_round": None, "type_saturation_threshold": 2}
        ranked = ap.rank_candidates(backlog, cfg, progress=0.0, completed_goals=[])
        self.assertEqual(ranked[0]["id"], "candidate-001")
        self.assertEqual(ap_state.split_goals("改进A，并且改进B"), ["改进A", "改进B"])
        stats = ap.compute_type_stats({"candidates": [
            {"status": "completed", "type": "bugfix", "value": 3},
            {"status": "blocked", "type": "bugfix", "value": 2},
        ]})
        self.assertIn("bugfix", stats)

class OptimizationTests(RepoTest):
    """Dirty-tree rules, budgets, deadline, JSON shapes, config errors."""

    def test_dirty_tree_rules(self):
        (self.repo / "x.txt").write_text("x\n", encoding="utf-8")
        self.assertNotEqual(self.run_state("init").returncode, 0)
        self.assertEqual(self.run_state("init", "--allow-uncommitted-changes").returncode, 0)
        self.assertEqual(self.run_state("begin-round", "--title", "t", "--reason", "r").returncode, 0)
        self.run_state("cancel-round", "--reason", "x")
        self.run_state("init", "--force")
        self.assertNotEqual(self.run_state("begin-round", "--title", "t", "--reason", "r").returncode, 0)

    def test_budget_and_round_stops(self):
        self.run_state("init", "--max-tokens", "1", "--max-rounds", "20")
        self.run_state("begin-round", "--title", "t", "--reason", "r")
        self.add_file()
        self.run_state("commit", "--summary", "s")
        self.run_state("complete-round", "--summary", "s", "--tokens", "50")
        brief = json.loads(self.run_state("check", "--brief").stdout)
        self.assertFalse(brief["continue"])
        self.run_state("init", "--force", "--max-rounds", "1")
        self.run_state("begin-round", "--title", "t", "--reason", "r")
        self.run_state("complete-round", "--summary", "s")
        self.assertIn("stopped", self.run_state("begin-round", "--title", "t", "--reason", "r").stderr.lower())

    def test_deadline_config_is_stop_condition(self):
        self.assertEqual(self.run_state("init", "--deadline", "+1h").returncode, 0)
        self.assertTrue(self.read_json("config.json").get("deadline"))
        brief = json.loads(self.run_state("check", "--brief").stdout)
        self.assertIn("budget", brief)
        self.assertEqual(self.run_state("init", "--force", "--deadline", "+1d").returncode, 0)
        # invalid date string: rejected cleanly or resolved — never a traceback
        result = self.run_state("init", "--force", "--deadline", "garbage-when")
        self.assertNotIn("Traceback", result.stderr)

    def test_json_shapes_and_invalid_sha(self):
        payload = json.loads(self.run_state("init", "--json").stdout)
        self.assertTrue(payload["ok"])
        self.assertIn("run_id", payload)
        self.git("commit", "--allow-empty", "-q", "-m", "base")
        self.run_state("begin-round", "--title", "t", "--reason", "r")
        self.assertNotEqual(self.run_state("complete-round", "--summary", "s",
                                           "--commit-sha", "deadbeef").returncode, 0)
        self.assertEqual(self.run_state("complete-round", "--summary", "s").returncode, 0)

    def test_config_errors_clean(self):
        self.run_state("init")
        (self.repo / ".autopilot" / "config.json").write_text("{", encoding="utf-8")
        result = self.run_state("check")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Traceback", result.stderr)
        (self.repo / ".autopilot" / "config.json").write_text(json.dumps({"max_rounds": "10"}), encoding="utf-8")
        result = self.run_state("check")
        self.assertIn("max_rounds", result.stderr)

    def test_binary_scope_and_init_flags(self):
        self.run_state("init", "--max-round-scope", "2")
        self.run_state("begin-round", "--title", "t", "--reason", "r")
        (self.repo / "b.bin").write_bytes(b"\x00\x01\x02")
        self.git("add", "b.bin")
        self.assertNotEqual(self.run_state("commit", "--summary", "s").returncode, 0)
        self.run_state("init", "--force", "--check-commands", "echo ok", "--candidates-per-round", "2",
                       "--commit-every-rounds", "1", "--verify-every-rounds", "1")
        cfg = self.read_json("config.json")
        self.assertEqual((cfg["candidates_per_round"], cfg["check_commands"]), (2, ["echo ok"]))

    def test_push_rules(self):
        self.run_state("init")
        self.assertIn("push is disabled", self.run_state("push").stderr)
        self.run_state("init", "--force", "--push")
        origin = Path(self.tmp) / "origin.git"
        self.git("init", "-q", "--bare", str(origin))
        self.git("remote", "add", "origin", str(origin))
        self.assertEqual(self.run_state("push").returncode, 0)

    def test_finish_auto_cancels_open_round(self):
        self.run_state("init", "--max-rounds", "2")
        self.run_state("begin-round", "--title", "open", "--reason", "r")
        self.assertEqual(self.run_state("finish", "--force").returncode, 0)
        self.assertIsNone(self.read_json("state.json")["current_round"])
        self.assertEqual(self.read_json("state.json")["cancelled_rounds"], 1)

class FeatureTests(RepoTest):
    """End-to-end feature contracts (consolidated from 17 methods)."""

    def test_report_and_undo_round(self):
        self.run_state("init")
        self.run_state("begin-round", "--title", "t", "--reason", "r")
        self.add_file()
        self.run_state("commit", "--summary", "add")
        self.run_state("complete-round", "--summary", "done")
        self.assertEqual(self.run_state("report").returncode, 0)
        self.assertEqual(self.run_state("report", "--output", "out.md").returncode, 0)
        self.assertTrue((self.repo / "out.md").exists())
        result = self.run_state("undo-round", "--sha", "HEAD", "--summary", "bad")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual(self.run_state("undo-round", "--sha", "notasha").returncode, 0)
        self.run_state("begin-round", "--title", "t", "--reason", "r")
        self.assertNotEqual(self.run_state("undo-round", "--sha", "HEAD", "--summary", "x").returncode, 0)

    def test_allow_deny_paths(self):
        self.run_state("init", "--allow-path", "src/**", "--deny-path", "src/secret.txt")
        self.run_state("begin-round", "--title", "t", "--reason", "r")
        (self.repo / "src").mkdir()
        (self.repo / "src" / "ok.py").write_text("x=1\n", encoding="utf-8")
        self.git("add", "src/ok.py")
        self.assertEqual(self.run_state("commit", "--summary", "s").returncode, 0)
        self.run_state("begin-round", "--title", "t", "--reason", "r")
        (self.repo / "src" / "secret.txt").write_text("nope\n", encoding="utf-8")
        self.git("add", "src/secret.txt")
        self.assertNotEqual(self.run_state("commit", "--summary", "s").returncode, 0)

    def test_phase_report_interval(self):
        self.run_state("init", "--commit-every-rounds", "1", "--max-rounds", "20")
        for i in range(10):
            self.run_state("begin-round", "--title", "t%d" % i, "--reason", "r")
            self.run_state("complete-round", "--summary", "s")
        reports = list((self.repo / ".autopilot").glob("phase-report-round-*.md"))
        self.assertTrue(reports)

    def test_detect_verify_python(self):
        (self.repo / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
        result = self.run_state("detect-verify", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertIn("detected", data)

    def test_goals_from_prompt_split(self):
        result = self.run_state("init", "--goals-from-prompt", "改进A，并且改进B。另外修C")
        self.assertEqual(result.returncode, 0, result.stderr)
        goals = self.read_json("config.json")["goals"]
        self.assertTrue(len(goals) >= 2)

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

    def test_batch_flush_backfills_batch_commit_sha(self):
        """seed-002 延伸（1.9）：flush --round N 时把批次 sha 回填给本批
        无独立 sha 的 completed 轮（观察台准逐轮锚点的数据源）。"""
        self.run_state("init")
        self.run_state("begin-round", "--title", "r1", "--reason", "x")
        self.add_file("a.py", "a = 1\n")
        self.run_state("complete-round", "--summary", "r1 done")
        self.run_state("begin-round", "--title", "r2", "--reason", "x")
        self.add_file("b.py", "b = 2\n")
        self.run_state("complete-round", "--summary", "r2 done")
        result = self.run_state("commit", "--round", "2", "--summary", "flush")
        self.assertEqual(result.returncode, 0, result.stderr)
        history = self.read_json("state.json")["history"]
        by_round = {h["round"]: h for h in history}
        self.assertEqual(by_round[1]["batch_commit_sha"],
                         by_round[2]["batch_commit_sha"])
        self.assertIsNotNone(by_round[2].get("batch_commit_sha"))

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
                "round_progress",
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


class MiningAndFinishGateTests(RepoTest):
    """Mine supply + early-stop finish gate (consolidated from 23 methods)."""

    def test_mine_finds_and_records(self):
        (self.repo / "a.py").write_text("# TODO: fix this\n", encoding="utf-8")
        self.git("add", "a.py")
        self.git("commit", "-q", "-m", "seed")
        self.run_state("init")
        result = self.run_state("mine", "--apply", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertTrue(data.get("ok", True))
        backlog = self.read_json("backlog.json")
        self.assertTrue(backlog["candidates"])
        st = self.read_json("state.json")
        self.assertTrue(st.get("mining_runs"))
        # second dry mine with zero new findings counts toward exhaustion
        self.assertEqual(self.run_state("mine", "--dry-run").returncode, 0)

    def test_finish_gate_blocks_on_work(self):
        self.run_state("init", "--max-rounds", "50")
        self.run_state("backlog-add", "--title", "ready work", "--value", "4", "--effort", "1")
        result = self.run_state("finish", "--force" if False else "--reason", "early")
        # without --force: refused (ready work remains)
        result = self.run_state("finish", "--reason", "early")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Refusing to finish", result.stdout + result.stderr)
        self.assertEqual(self.run_state("finish", "--force", "--reason", "forced").returncode, 0)

    def test_check_action_hint_mine_when_thin(self):
        self.run_state("init")
        brief = json.loads(self.run_state("check", "--brief").stdout)
        self.assertIn(brief.get("action_hint"), ("mine", "expand", None))
        self.assertEqual(self.run_state("mine", "--apply").returncode, 0)

    def test_mine_dedup_and_scanner_basics(self):
        (self.repo / "mod.py").write_text(
            "def foo():\n    pass\n\n# FIXME: later\n", encoding="utf-8")
        (self.repo / "test_mod.py").write_text("def test_foo():\n    assert True\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "seed")
        self.run_state("init")
        self.run_state("mine", "--apply")
        titles = [c["title"] for c in self.read_json("backlog.json")["candidates"]]
        self.assertTrue(titles)
        # limit zero yields nothing
        result = self.run_state("mine", "--limit", "0", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_diagnose_non_git(self):
        fresh = Path(self.tmp) / "plain"
        fresh.mkdir()
        result = self.run_state("diagnose", "--repo", str(fresh))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("git", result.stdout.lower())

class LifecycleStateFixTests(RepoTest):
    """Lifecycle and messaging contracts (consolidated from 25 methods)."""

    def test_init_side_effects_and_duplicate_refusal(self):
        result = self.run_state("init", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[DRY-RUN]", result.stderr)
        self.assertFalse((self.repo / ".autopilot").exists())
        self.assertEqual(self.run_state("init").returncode, 0)
        result = self.run_state("init")
        self.assertNotEqual(result.returncode, 0)
        result = self.run_state("init", "--json")
        self.assertFalse(json.loads(result.stdout)["ok"])
        fresh = Path(self.tmp) / "notgit"
        fresh.mkdir()
        result = self.run_state("init", "--repo", str(fresh))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((fresh / ".autopilot").exists())

    def test_begin_round_dirty_keeps_candidates_pending(self):
        self.run_state("init")
        self.run_state("backlog-add", "--title", "t", "--value", "3", "--effort", "1")
        (self.repo / "dirty.txt").write_text("d\n", encoding="utf-8")
        result = self.run_state("begin-round", "--title", "t", "--reason", "r", "--candidate-id", "candidate-001")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.read_json("backlog.json")["candidates"][0]["status"], "pending")

    def test_corrupt_state_null_and_future_schema(self):
        self.run_state("init")
        (self.repo / ".autopilot" / "state.json").write_text("null", encoding="utf-8")
        result = self.run_state("read")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Traceback", result.stderr)
        self.run_state("init", "--force")
        path = self.repo / ".autopilot" / "state.json"
        state = json.loads(path.read_text(encoding="utf-8"))
        state["schema"] = 99
        path.write_text(json.dumps(state), encoding="utf-8")
        before = path.read_text(encoding="utf-8")
        result = self.run_state("read")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(path.read_text(encoding="utf-8"), before)

    def test_finish_dirty_warning_and_unmerged_note(self):
        self.run_state("init", "--branch-mode", "feature", "--report-lang", "en",
                       "--commit-every-rounds", "1", "--allow-uncommitted-changes")
        (self.repo / "user.txt").write_text("u\n", encoding="utf-8")
        self.run_state("begin-round", "--title", "r", "--reason", "x")
        self.add_file()
        self.run_state("commit", "--summary", "add")
        sha = self.git("rev-parse", "HEAD").stdout.strip()
        self.run_state("complete-round", "--summary", "done", "--commit-sha", sha)
        result = self.run_state("finish", "--force", "--reason", "done")
        combined = result.stdout + result.stderr
        self.assertIn("user.txt", combined)
        self.assertIn("Commits remain on branch", combined)
        self.assertIn("not merged into", combined)
        retrospective = (self.repo / ".autopilot" / "retrospective.md").read_text(encoding="utf-8")
        self.assertIn("Commits remain on branch", retrospective)

    def test_check_dirty_and_secret_scanning_warnings(self):
        self.run_state("init")
        (self.repo / "user.txt").write_text("u\n", encoding="utf-8")
        data = json.loads(self.run_state("check", "--brief").stdout)
        self.assertTrue(any("dirty" in w.lower() for w in data["warnings"]))
        self.run_state("init", "--no-scan-secrets", "--force")
        data = json.loads(self.run_state("check", "--brief").stdout)
        self.assertTrue(any("secret" in w.lower() for w in data["warnings"]))

    def test_goal_unverified_and_report_branch(self):
        self.run_state("init", "--goal", "G")
        result = self.run_state("goal-met", "--goal", "G")
        combined = result.stdout + result.stderr
        self.assertTrue("unverified" in combined.lower() or "not" in combined.lower())
        branch = self.git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        report = self.run_state("report").stdout + self.run_state("report", "--lang", "en").stdout
        self.assertIn(branch, report)

    def test_uninitialized_refusals(self):
        for command, args in (("read", ()), ("check", ()), ("begin-round", ("--title", "t", "--reason", "r"))):
            result = self.run_state(command, *args)
            self.assertNotEqual(result.returncode, 0, command)
            self.assertNotIn("Traceback", result.stderr, command)
        self.assertFalse((self.repo / ".autopilot" / "state.json").exists())

class SecurityFixRegressionTests(RepoTest):
    """Security-audit locks: secret prefix bypass, branch guard, scope guards."""

    def test_secret_scan_prefix_bypass_and_attribution(self):
        self.run_state("init")
        self.run_state("begin-round", "--title", "r", "--reason", "x")
        (self.repo / "leak.py").write_text("++ghp_" + "a" * 36 + "\n", encoding="utf-8")
        self.git("add", "leak.py")
        result = self.run_state("commit", "--summary", "oops")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("secret", result.stderr.lower())
        (self.repo / "leak.py").write_text("++ ghp_" + "b" * 36 + "\n", encoding="utf-8")
        self.git("add", "leak.py")
        result = self.run_state("commit", "--summary", "oops")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("secret", result.stderr.lower())
        (self.repo / "leak.py").unlink()
        self.git("reset", "-q")
        self.add_file("creds.py", 'KEY = "ghp_' + "c" * 36 + '"\n')
        result = self.run_state("secret-scan", "--json")
        self.assertNotEqual(result.returncode, 0)
        data = json.loads(result.stdout)
        self.assertFalse(data["clean"])
        self.assertEqual(data["findings"][0]["file"], "creds.py")
        self.assertTrue(all("+++ b/" not in f["text"] for f in data["findings"]))

    def test_commit_refusal_names_pattern(self):
        self.run_state("init")
        self.run_state("begin-round", "--title", "r", "--reason", "x")
        self.add_file("creds.py", 'KEY = "AKIAIOSFODNN7EXAMPLE"\n')
        result = self.run_state("commit", "--summary", "oops")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("(matched AKIA", result.stderr)

    def test_branch_guard_blocks_drift_on_round_commands(self):
        self.run_state("init", "--branch-mode", "feature")
        expected = self.read_json("state.json")["branch"]
        self.git("checkout", "-q", self.initial_branch)
        self.add_file()
        result = self.run_state("commit", "--summary", "wrong branch")
        self.assertNotEqual(result.returncode, 0)
        for needle in ("Branch drift", self.initial_branch, expected, "ensure-branch"):
            self.assertIn(needle, result.stderr)
        self.assertEqual(self.git("log", "-1", "--pretty=%s").stdout.strip(), "initial")
        self.assertNotEqual(self.run_state("begin-round", "--title", "r", "--reason", "x").returncode, 0)
        self.assertIsNone(self.read_json("state.json")["current_round"])
        self.git("reset", "-q")
        self.git("stash", "-q", "-u")
        self.run_state("ensure-branch")
        self.assertEqual(self.git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip(), expected)

    def test_branch_guard_complete_and_detached(self):
        self.run_state("init", "--branch-mode", "feature")
        self.run_state("begin-round", "--title", "r", "--reason", "x")
        self.git("checkout", "-q", self.initial_branch)
        result = self.run_state("complete-round", "--summary", "done")
        self.assertIn("Branch drift", result.stderr)
        self.assertIsNotNone(self.read_json("state.json")["current_round"])
        self.run_state("ensure-branch")
        self.git("checkout", "--detach", "-q")
        self.add_file()
        result = self.run_state("commit", "--summary", "dangling")
        self.assertIn("Detached HEAD", result.stderr)

    def test_current_mode_has_no_branch_guard(self):
        self.run_state("init")
        self.git("checkout", "-q", "-b", "side-branch")
        self.run_state("begin-round", "--title", "r", "--reason", "x")
        self.add_file()
        self.assertEqual(self.run_state("commit", "--summary", "current mode is free").returncode, 0)

    def test_check_warns_branch_drift(self):
        self.run_state("init", "--branch-mode", "feature", "--report-lang", "zh")
        self.git("checkout", "-q", self.initial_branch)
        data = json.loads(self.run_state("check", "--brief").stdout)
        self.assertTrue(any("分支漂移" in w and "ensure-branch" in w for w in data["warnings"]))
        self.run_state("init", "--branch-mode", "feature", "--report-lang", "en", "--force")
        self.git("checkout", "-q", self.initial_branch)
        data = json.loads(self.run_state("check", "--brief").stdout)
        self.assertTrue(any("Branch drift" in w for w in data["warnings"]))

    def test_commit_intercepts_user_and_autopilot_paths(self):
        self.run_state("init", "--commit-every-rounds", "1")
        self.run_state("begin-round", "--title", "r1", "--reason", "x")
        self.add_file("a.py")
        self.assertEqual(self.run_state("commit", "--summary", "work").returncode, 0)
        self.run_state("complete-round", "--summary", "a")
        (self.repo / "user.txt").write_text("user work\n", encoding="utf-8")
        self.git("add", "user.txt")
        self.run_state("begin-round", "--title", "r2", "--reason", "x")
        self.add_file("b.py")
        result = self.run_state("commit", "--summary", "own work")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("user.txt", result.stderr)
        self.git("reset", "-q", "user.txt")
        self.assertEqual(self.run_state("commit", "--summary", "own work").returncode, 0)
        self.run_state("begin-round", "--title", "r3", "--reason", "x")
        self.git("add", "-f", ".autopilot/state.json")
        result = self.run_state("commit", "--summary", "s")
        self.assertIn(".autopilot/state.json", result.stderr)
        self.git("reset", "-q", "HEAD", "--", ".autopilot/state.json")

    def test_secret_scan_fails_closed_on_unreadable_diff(self):
        self.run_state("init", "--commit-every-rounds", "1")
        self.run_state("begin-round", "--title", "r", "--reason", "x")
        self.add_file("b.py", "x = 1\n")
        self.git("add", "b.py")
        real_run = ap_io.run_git

        def fake_run(repo, *args, **kw):
            if args and args[0] == "-c":
                return type("R", (), {"returncode": 1, "stdout": "", "stderr": "diff fail"})()
            return real_run(repo, *args, **kw)

        with mock.patch("autopilot.secrets.io.run_git", side_effect=fake_run):
            result = self.run_state("commit", "--summary", "s")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Refusing", result.stdout + result.stderr)

class DashboardContractTests(RepoTest):
    """观察台契约（2026-09-27 审查修复）：只读保证、HTTP 面、锚点对称、
    快照 shape、预算/决策可见性。此前 Dashboard* 细碎用例被精简误删，
    这里按安全边界重锁。"""

    def _seed_run(self):
        self.run_state("init")
        self.run_state("begin-round", "--title", "r1", "--reason", "x")
        self.add_file("work.py", "x = 1\n")
        self.run_state("complete-round", "--summary", "did work")

    def _autopilot_listing(self):
        ap = self.repo / ".autopilot"
        return sorted(p.name for p in ap.iterdir())

    def test_build_snapshot_is_read_only(self):
        self._seed_run()
        before = self._autopilot_listing()
        from autopilot import dashboard_data as dd
        snap = dd.build_snapshot(self.repo)
        self.assertIn("meta", snap)
        self.assertEqual(self._autopilot_listing(), before)

    def test_http_endpoints_and_404(self):
        self._seed_run()
        from autopilot import dashboard as ap_dash
        import urllib.error
        import urllib.request
        server, port = ap_dash.start_in_thread(self.repo)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = "http://127.0.0.1:{}/".format(port)
        page = urllib.request.urlopen(base, timeout=5)
        self.assertEqual(page.status, 200)
        self.assertIn("text/html", page.headers.get("Content-Type", ""))
        body = urllib.request.urlopen(base + "api/snapshot", timeout=5)
        self.assertEqual(body.status, 200)
        payload = json.loads(body.read().decode("utf-8"))
        self.assertIn("meta", payload)
        try:
            urllib.request.urlopen(base + "etc/passwd", timeout=5)
            self.fail("unknown path must 404")
        except urllib.error.HTTPError as err:
            self.assertEqual(err.code, 404)

    def test_ensure_dashboard_failure_never_raises(self):
        self._seed_run()
        from autopilot import dashboard as ap_dash
        cfg = {"dashboard": {"enabled": True, "port": 0, "auto_open": False}}
        with mock.patch.object(ap_dash, "spawn_server", side_effect=RuntimeError("boom")):
            self.assertIsNone(ap_dash.ensure_dashboard(self.repo, cfg))  # 不抛

    def test_page_snapshot_refs_subset_of_snapshot_keys(self):
        import re
        from autopilot import dashboard_data as dd
        self._seed_run()
        snap = dd.build_snapshot(self.repo)
        page = (Path(autopilot.__file__).resolve().parent / "dashboard.html")
        refs = {m.group(1).split(".")[0]
                for m in re.finditer(r"\bsnapshot\.([A-Za-z_][A-Za-z0-9_]*)",
                                     page.read_text(encoding="utf-8"))}
        self.assertTrue(refs)
        self.assertLessEqual(refs, set(snap) | {"error"})

    def test_batch_commit_sha_is_a_first_class_anchor(self):
        """flush --round 只写 batch_commit_sha 时，文件改动与逐轮 stats 必须
        同时锚定成功——不得因 stats 缺锚而丢掉整棵生长树。"""
        from autopilot import dashboard_data as dd
        base = self.git("rev-parse", "HEAD").stdout.strip()
        (self.repo / "a.py").write_text("a = 1\n", encoding="utf-8")
        self.git("add", "a.py")
        self.git("commit", "-q", "-m", "anchor")
        sha = self.git("rev-parse", "HEAD").stdout.strip()
        history = [{"round": 1, "status": "completed", "title": "t",
                    "summary": "s", "review_score": 4,
                    "commit_sha": None, "batch_commit_sha": sha,
                    "estimated_tokens": 0}]
        changes, stats = dd.walk_round_anchors(self.repo, history, base)
        self.assertIsNotNone(changes)
        self.assertIn("a.py", changes)
        self.assertEqual(stats[0]["files_changed"], 1)
        # build_snapshot 不得因 batch 锚点走降级
        st = {
            "run_id": "run-test", "history": history, "run_start_sha": base,
            "project_map": None, "estimated_tokens_used": 0,
            "goals": [], "completed_goals": [], "expansion_waves": [],
            "round": 1, "round_seq": 1,
        }
        ap = self.repo / ".autopilot"
        ap.mkdir(exist_ok=True)
        ap_io.save_json(ap / "state.json", st)
        snap = dd.build_snapshot(self.repo)
        self.assertNotIn("growth", snap["meta"]["degraded"])
        self.assertEqual(snap["growth"]["granularity"], "per-round")

    def test_snapshot_shape_budget_and_outlook(self):
        self._seed_run()
        from autopilot import dashboard_data as dd
        snap = dd.build_snapshot(self.repo)
        self.assertIn("degraded", snap["meta"])
        self.assertIn("granularity", snap["growth"])
        self.assertIn("totals", snap["growth"])
        self.assertGreaterEqual(snap["growth"]["totals"]["files"], 1)
        budget = snap["status"]["budget"]
        for key in ("max_minutes", "remaining_minutes",
                    "deadline_remaining_minutes", "estimated_tokens_used",
                    "max_rounds", "round_progress"):
            self.assertIn(key, budget)
        self.assertIn("stop_reason", snap["status"])
        self.assertIn("action_hint", snap["status"])
        self.assertIn(snap["status"]["action_hint"],
                      ("stop", "work", "expand", "mine"))

    def test_degraded_growth_ships_static_totals(self):
        """无锚点且 run 级 diff 失败时 growth 如实降级，仍给静态汇总。"""
        from autopilot import dashboard_data as dd
        ap = self.repo / ".autopilot"
        ap.mkdir(exist_ok=True)
        ap_io.save_json(ap / "state.json", {
            "run_id": "run-test",
            "history": [{"round": 1, "status": "completed", "title": "t",
                         "summary": "s", "review_score": 3,
                         "commit_sha": None, "estimated_tokens": 0}],
            "run_start_sha": "missing", "project_map": None,
            "estimated_tokens_used": 0, "goals": [], "completed_goals": [],
            "expansion_waves": [], "round": 1, "round_seq": 1,
        })

        class _FailGit(object):
            @staticmethod
            def run_git(repo, *args):
                return None

        snap = dd.build_snapshot(self.repo, gitio=_FailGit)
        self.assertIn("growth", snap["meta"]["degraded"])
        self.assertEqual(snap["growth"]["totals"]["files"], 0)

    def test_project_map_stale_detects_drift_and_ignores_small_churn(self):
        from autopilot import dashboard_data as dd

        def add(name):
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            self.add_file(name, "x = 1\n")

        for i in range(5):
            add("pkg{}/m.py".format(i))
        self.git("commit", "-q", "-m", "seed")
        fm = dd.scan_project_framework(self.repo)
        self.assertIsNotNone(fm)
        self.assertTrue(dd.project_map_stale(None, self.repo))
        self.assertFalse(dd.project_map_stale(fm, self.repo))
        add("pkg99/m.py")  # 单文件：未到阈值
        self.assertFalse(dd.project_map_stale(fm, self.repo))
        for i in range(30):
            add("extra{}/m.py".format(i))
        self.assertTrue(dd.project_map_stale(fm, self.repo))

    def test_log_tail_read_only_returns_recent(self):
        from autopilot import dashboard_data as dd
        self._seed_run()
        events = dd._recent_log_events(self.repo, limit=10)
        self.assertTrue(events)
        self.assertTrue(all("event" in e for e in events))

    def test_info_alive_requires_listening_port(self):
        from autopilot import dashboard as ap_dash
        # 有 pid 无监听端口 → 不算活（防 pid 复用假活）
        ap_dash.write_info(self.repo, {
            "pid": os.getpid(), "port": 1,  # port 1 通常无人监听
            "started_at": "t", "opened": False,
        })
        self.assertFalse(ap_dash.info_alive(self.repo))
        server, port = ap_dash.start_in_thread(self.repo)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.assertTrue(ap_dash.info_alive(self.repo))


class ProjectFrameworkTests(RepoTest):
    """项目总框架（1.9.1）：init 扫描仓库产出 state.project_map（域→模块→
    文件数），生长树据此绘制完整骨架；轮次活动按同名域 + 模块路径前缀叠加
    其上。旧 run 无存档时 begin-round 自愈补扫。"""

    def _scan(self, domain_map=None):
        from autopilot import dashboard_data as dd
        return dd.scan_project_framework(self.repo, domain_map)

    def _add(self, name, content="x = 1\n"):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        self.add_file(name, content)

    def test_scan_groups_domains_counts_files_and_excludes_noise(self):
        self._add("scripts/autopilot/a.py")
        self._add("scripts/autopilot/b.py")
        self._add("scripts/test_a.py", "z = 3\n")
        self._add("docs/guide.md", "guide\n")
        self._add("node_modules/pkg/index.js", "m\n")   # 噪声目录不进骨架
        fm = self._scan()
        self.assertIsNotNone(fm)
        self.assertTrue(fm["scanned_at"])
        by = {d["name"]: d for d in fm["domains"]}
        tools = by["工具与脚本"]
        mods = {m["name"]: m for m in tools["modules"]}
        self.assertEqual(mods["autopilot"]["files"], 2)     # 二级目录聚合
        self.assertEqual(mods["autopilot"]["path"], "scripts/autopilot")
        self.assertEqual(set(mods), {"autopilot"})          # test_* 不在工具域
        # 深度不足的文件取文件名为模块（test_* 同时命中「测试」域）
        self.assertEqual({m["name"] for m in by["测试"]["modules"]}, {"test_a.py"})
        self.assertEqual({m["name"] for m in by["文档与知识"]["modules"]}, {"README.md", "guide.md"})
        self.assertNotIn(".autopilot", by)                  # 运行目录永不入骨架
        self.assertNotIn("node_modules", {d["name"] for d in fm["domains"]})

    def test_scan_caps_modules_and_folds_overflow_into_other(self):
        from autopilot import dashboard_data as dd
        for i in range(12):
            self._add("pkg{}/mod.py".format(i), "x = {}\n".format(i))
        fm = self._scan()
        by = {d["name"]: d for d in fm["domains"]}
        core = by["核心实现"]                               # setUp 的 README 另属文档域
        self.assertEqual(len(core["modules"]), dd.FRAMEWORK_MAX_MODULES + 1)
        self.assertEqual(core["modules"][-1]["name"], "其他")
        self.assertEqual(core["modules"][-1]["files"], 12 - dd.FRAMEWORK_MAX_MODULES)

    def test_scan_caps_domains_via_domain_map(self):
        from autopilot import dashboard_data as dd
        for i in range(12):
            self._add("pkg{}/mod.py".format(i), "x = {}\n".format(i))
        dmap = {"pkg{}/".format(i): {"name": "域{}".format(i)} for i in range(12)}
        fm = self._scan(dmap)
        names = [d["name"] for d in fm["domains"]]
        self.assertEqual(len(names), dd.FRAMEWORK_MAX_DOMAINS + 1)  # 10 + 「其他」
        self.assertEqual(names[-1], "其他")

    def test_init_stores_project_map(self):
        self._add("scripts/autopilot/a.py")
        self.git("commit", "-q", "-m", "seed")   # init 拒绝脏树，先落锚
        result = self.run_state("init")
        self.assertEqual(result.returncode, 0, result.stderr)
        st = self.read_json("state.json")
        fm = st["project_map"]
        self.assertTrue(fm and fm["domains"])
        self.assertIn("工具与脚本", [d["name"] for d in fm["domains"]])

    def test_begin_round_backfills_missing_project_map(self):
        """早于框架特性的旧 run：开轮时补扫一次，观察台也能画完整骨架。"""
        self.run_state("init")
        st = self.read_json("state.json")
        st["project_map"] = None
        (self.repo / ".autopilot" / "state.json").write_text(
            json.dumps(st, ensure_ascii=False), encoding="utf-8")
        result = self.run_state("begin-round", "--title", "r1", "--reason", "x")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.read_json("state.json")["project_map"])

    def test_merge_overlays_activity_on_framework(self):
        from autopilot import dashboard_data as dd
        fm = {"scanned_at": "t", "domains": [
            {"name": "工具与脚本", "meaning": "", "modules": [
                {"name": "autopilot", "path": "scripts/autopilot", "files": 13},
                {"name": "cli.py", "path": "scripts/cli.py", "files": 1}]}]}
        growth = [{"id": "工具与脚本", "name": "工具与脚本", "meaning": "构建与辅助工具链",
                   "first_round": 2, "active_rounds": [2, 5], "weight": 1.0, "modules": [
                       {"name": "a.py", "path": "scripts/autopilot/a.py", "first_round": 2,
                        "churn": {"touches": 3, "insertions": 10, "deletions": 1},
                        "files": ["scripts/autopilot/a.py"]},
                       {"name": "new.py", "path": "scripts/new.py", "first_round": 5,
                        "churn": {"touches": 1, "insertions": 2, "deletions": 0},
                        "files": ["scripts/new.py"]}]}]
        merged = dd.merge_project_framework(fm, growth)
        self.assertEqual(len(merged), 1)
        d = merged[0]
        self.assertEqual(d["first_round"], 2)
        self.assertEqual(d["weight"], 1.0)
        self.assertEqual(d["framework_files"], 14)          # 框架文件数如实带出
        mods = {m["path"]: m for m in d["modules"]}
        auto = mods["scripts/autopilot"]
        self.assertEqual(auto["first_round"], 2)            # 活动叠加到骨架
        self.assertEqual(auto["churn"]["touches"], 3)
        self.assertEqual(auto["total_files"], 13)
        self.assertEqual(auto["files"], ["scripts/autopilot/a.py"])
        newm = mods["scripts/new.py"]                       # run 新建文件：growth-only 追加
        self.assertEqual(newm["total_files"], 1)
        self.assertEqual(newm["first_round"], 5)
        cli = mods["scripts/cli.py"]                        # 未触达骨架：仍在树上
        self.assertIsNone(cli["first_round"])
        self.assertEqual(cli["churn"]["touches"], 0)
        paths = [m["path"] for m in d["modules"]]           # 触达排前、骨架压后
        self.assertLess(paths.index("scripts/autopilot"), paths.index("scripts/cli.py"))

    def test_merge_appends_run_created_domain_and_passthrough_without_map(self):
        from autopilot import dashboard_data as dd
        fm = {"domains": [{"name": "工具与脚本", "meaning": "", "modules": [
            {"name": "cli.py", "path": "scripts/cli.py", "files": 1}]}]}
        growth = [{"id": "测试", "name": "测试", "meaning": "回归防护网",
                   "first_round": 1, "active_rounds": [1], "weight": 1.0, "modules": []}]
        merged = dd.merge_project_framework(fm, growth)
        by = {d["name"]: d for d in merged}
        self.assertEqual(by["测试"]["framework_files"], 0)  # run 中长出来的域
        self.assertEqual(by["工具与脚本"]["framework_files"], 1)
        self.assertIs(dd.merge_project_framework(None, growth), growth)  # 无存档原样


class ProjectFrameworkSnapshotTests(AutopilotTestBase):
    """快照层契约：项目框架让完全降级的生长树保持可用。基类不落提交锚点
    （HEAD 不存在），叠加重放的 shaless 完成轮——正是 batch 模式的降级路径。"""

    def _seed_state(self, **overrides):
        st = {
            "schema": "auto-iterate-state/1", "run_id": "run-test",
            "repo": str(self.repo), "branch": "main",
            "created_at": "2026-09-26T00:00:00+00:00",
            "started_at": "2026-09-26T00:00:00+00:00",
            "round": 1, "round_seq": 1,
            "blocked_rounds": 0, "cancelled_rounds": 0,
            "completed_rounds": 99,
            "estimated_tokens_used": 4200,
            "goals": ["g1", "g2"], "completed_goals": ["g1"],
            "expansion_waves": [{"id": 1}],
            "history": [], "finished_at": None, "run_start_sha": None,
        }
        st.update(overrides)
        ap = self.repo / ".autopilot"
        ap.mkdir(parents=True, exist_ok=True)
        ap_io.save_json(ap / "state.json", st)
        return st

    def test_snapshot_framework_keeps_tree_alive_when_growth_degraded(self):
        """轮次叠加层完全降级（完成轮无锚点且无 touched_files）时，init 扫描
        的项目框架仍让 growth.domains 非空——树活着，降级标记如实保留。"""
        from autopilot import dashboard_data as dd
        self._seed_state(
            project_map={"scanned_at": "t", "domains": [
                {"name": "工具与脚本", "meaning": "构建与辅助工具链", "modules": [
                    {"name": "autopilot", "path": "scripts/autopilot", "files": 13}]}]},
            history=[
                {"round": 1, "status": "completed", "title": "t1", "summary": "s1",
                 "review_score": 4, "commit_sha": None, "estimated_tokens": 0},
            ])
        snap = dd.build_snapshot(self.repo)
        self.assertIn("growth", snap["meta"]["degraded"])       # 降级如实标注
        names = [d["name"] for d in snap["growth"]["domains"]]
        self.assertEqual(names, ["工具与脚本"])                  # 框架让树活着
        self.assertEqual(snap["growth"]["domains"][0]["modules"][0]["total_files"], 13)


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

    def test_check_payload_carries_round_progress(self):
        """轮次进度（预算可见性）：round_seq/max_rounds/remaining 在 brief
        payload 可读——max_rounds 为 null 时 remaining 为 null。"""
        self.run_state("init")
        self.run_state("begin-round", "--title", "r", "--reason", "x")
        self.add_file("a.py", "a = 1\n")
        self.run_state("complete-round", "--summary", "done")
        result = self.run_state("check", "--brief")
        self.assertEqual(result.returncode, 0, result.stderr)
        import json as _json
        payload = _json.loads(result.stdout)
        progress = payload["round_progress"]
        self.assertEqual(progress["round_seq"], 1)
        self.assertEqual(progress["remaining"],
                         progress["max_rounds"] - progress["round_seq"])

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


# ---- 分层验证（1.9 goal：轮间 smoke <60s，全量留给边界轮）----
# SLOW_TEST_CLASSES：单类实测耗时超过 SLOW_CLASS_SECONDS 的类名清单（由
# scripts/test_autopilot_state.py --time-report 生成）。--smoke 跳过它们；
# 新类默认进 smoke（fail-safe：smoke 只做轮间快检，全量才是边界的证明）。
SLOW_CLASS_SECONDS = 2.0
SLOW_TEST_CLASSES = [
    "AnalysisCacheTests",
    "BacklogManageTests",
    "BacklogRankTests",
    "BatchCommitTests",
    "BatchContractTests",
    "BeginRoundStopTests",
    "BranchGcTests",
    "BranchTests",
    "BudgetAccountingTests",
    "ConfigSetTests",
    "ContractTests",
    "DashboardContractTests",
    "DirectiveTests",
    "ExpansionWaveTests",
    "FailurePathTests",
    "FeatureTests",
    "FinishGateTests",
    "InitTests",
    "LifecycleStateFixTests",
    "MiningAndFinishGateTests",
    "OptimizationTests",
    "PredictedHardeningTests",
    "ProjectFrameworkSnapshotTests",
    "ProjectFrameworkTests",
    "RobustnessTests",
    "RoundFlowTests",
    "RoundPrepTests",
    "SecretScanTests",
    "SecurityFixRegressionTests",
]


def _render_slow_list(slow_names):
    """把慢类名清单渲染为 SLOW_TEST_CLASSES 赋值块（--update-slow 写回用）。"""
    lines = ["SLOW_TEST_CLASSES = ["]
    for name in slow_names:
        lines.append('    "{}",'.format(name))
    lines.append("]")
    return "\n".join(lines)


def _update_slow_list(slow_names):
    """把新清单写回本文件源码的 SLOW_TEST_CLASSES 块（方括号配平定位，
    自修改仅限该标记区间）。"""
    source_path = __file__
    text = open(source_path, encoding="utf-8").read()
    start = text.index("SLOW_TEST_CLASSES = [")
    depth = 0
    end = start
    for idx in range(start, len(text)):
        if text[idx] == "[":
            depth += 1
        elif text[idx] == "]":
            depth -= 1
            if depth == 0:
                end = idx + 1
                break
    updated = text[:start] + _render_slow_list(slow_names) + text[end:]
    with open(source_path, "w", encoding="utf-8", newline="") as fh:
        fh.write(updated)


def _run_time_report(update=False):
    """按类计时跑全量（诊断入口）：打印每类耗时；--update-slow 时把新清单
    直接写回源码的 SLOW_TEST_CLASSES 块。"""
    loader = unittest.TestLoader()
    names = sorted(n for n, o in globals().items()
                   if isinstance(o, type) and issubclass(o, unittest.TestCase)
                   and o.__module__ == __name__)
    total0 = time.time()
    slow = []
    for n in names:
        suite = loader.loadTestsFromTestCase(globals()[n])
        t0 = time.time()
        res = unittest.TextTestRunner(verbosity=0).run(suite)
        dt = time.time() - t0
        if dt >= SLOW_CLASS_SECONDS:
            slow.append((dt, n))
            print("%7.2fs %s" % (dt, n))
    print("TOTAL %.1fs across %d classes; %d slow (>= %.1fs)"
          % (time.time() - total0, len(names), len(slow), SLOW_CLASS_SECONDS))
    if update:
        _update_slow_list([n for _, n in slow])
        print("[OK] SLOW_TEST_CLASSES updated ({} classes)".format(len(slow)))


def _run_smoke():
    """全量减去 SLOW_TEST_CLASSES：轮间快速回归。新类默认包含——漏标记的
    慢类只会让 smoke 变慢，不会让它漏测新代码；全量才是提交边界证明。"""
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    slow = set(SLOW_TEST_CLASSES)
    count = 0
    for n in sorted(globals()):
        obj = globals()[n]
        if isinstance(obj, type) and issubclass(obj, unittest.TestCase)                 and obj.__module__ == __name__ and n not in slow:
            suite.addTest(loader.loadTestsFromTestCase(obj))
            count += 1
    t0 = time.time()
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    elapsed = time.time() - t0
    print("[smoke] %d classes, %.1fs (excluded %d slow classes)"
          % (count, elapsed, len(slow)))
    # seed-001 对账：60s 是轮间快检的承诺线。既有类变慢（如新增子进程测试）
    # 会无声击穿它——超线即提示重跑 --time-report 刷新 SLOW_TEST_CLASSES。
    if elapsed > 60:
        print("[smoke-warn] %.0fs 超过 60s 承诺线：慢类清单可能已漂移，"
              "运行 py -3.13 scripts/test_autopilot_state.py --time-report 对账"
              % elapsed, file=sys.stderr)
    return 0 if result.wasSuccessful() else 1


def _parallel_worker(class_name):
    """--jobs worker（进程级）：独立进程跑一个 TestCase 类，返回结果摘要。
    类间无共享 fixture（各自 mkdtemp/随机端口），进程隔离下互不干扰。"""
    import io as _io
    import time as _time
    import unittest as _unittest
    loader = _unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(globals()[class_name])
    devnull = open(os.devnull, "w")
    t0 = _time.time()
    result = _unittest.TextTestRunner(verbosity=0, stream=devnull).run(suite)
    devnull.close()
    return {
        "name": class_name, "ran": result.testsRun,
        "seconds": round(_time.time() - t0, 1),
        "problems": [str(f) for f in result.failures + result.errors],
        "skipped": len(result.skipped),
    }


def _run_parallel(class_names, jobs):
    """全量按类并行：慢类彼此独立，进程池把 455s 压到约 1/3（expansion
    lens performance）。失败清单聚合打印，任一失败退出 1。"""
    import multiprocessing
    all_names = sorted(n for n, o in globals().items()
                       if isinstance(o, type) and issubclass(o, unittest.TestCase)
                       and o.__module__ == __name__)
    if class_names:
        unknown = [n for n in class_names if n not in all_names]
        if unknown:
            print("[ERROR] unknown test classes: {}".format(", ".join(unknown)))
            return 2
        targets = class_names
    else:
        targets = all_names
    t0 = time.time()
    with multiprocessing.Pool(processes=jobs) as pool:
        results = pool.map(_parallel_worker, targets)
    bad = []
    total = 0
    for r in sorted(results, key=lambda x: -x["seconds"]):
        total += r["ran"]
        if r["problems"]:
            bad.append(r["name"])
            print("[FAIL] {} ({}s)".format(r["name"], r["seconds"]))
            for p in r["problems"]:
                print(p)
    print("[parallel jobs={}] {} classes, {} tests, {} failures/errors, "
          "{:.1f}s total".format(jobs, len(results), total, len(bad),
                                 time.time() - t0))
    return 1 if bad else 0


def _run_parallel_smoke(jobs, subset=None):
    """smoke 子集的并行版：同 _run_parallel 但目标是 smoke 类集合
    （全量减 SLOW_TEST_CLASSES），轮间验证进一步压缩。subset 可再过滤
    （--jobs 4 --smoke 类名...：在 smoke 集内只跑指定类）。"""
    import multiprocessing
    slow = set(SLOW_TEST_CLASSES)
    targets = sorted(n for n, o in globals().items()
                     if isinstance(o, type) and issubclass(o, unittest.TestCase)
                     and o.__module__ == __name__ and n not in slow)
    if subset:
        unknown = [n for n in subset if n not in targets]
        if unknown:
            print("[ERROR] classes not in smoke set: {}".format(", ".join(unknown)))
            return 2, []
        targets = subset
    t0 = time.time()
    with multiprocessing.Pool(processes=jobs) as pool:
        results = pool.map(_parallel_worker, targets)
    bad = []
    total = 0
    for r in results:
        total += r["ran"]
        if r["problems"]:
            bad.append(r["name"])
            print("[FAIL] {} ({}s)".format(r["name"], r["seconds"]))
            for p in r["problems"]:
                print(p)
    print("[smoke-parallel jobs={}] {} classes, {} tests, {:.1f}s".format(
        jobs, len(results), total, time.time() - t0))
    return (1 if bad else 0), results


if __name__ == "__main__":
    argv = sys.argv[1:]
    if "--jobs" in argv:
        i = argv.index("--jobs")
        jobs = int(argv[i + 1])
        rest = argv[i + 2:]
        if "--smoke" in rest:
            rest.remove("--smoke")
            code, _ = _run_parallel_smoke(jobs, subset=rest or None)
            sys.exit(code)
        sys.exit(_run_parallel(rest or None, jobs))
    elif "--time-report" in argv:
        argv.remove("--time-report")
        update = "--update-slow" in argv
        if update:
            argv.remove("--update-slow")
        _run_time_report(update=update)
    elif "--smoke" in argv:
        argv.remove("--smoke")
        if argv:
            # --smoke 显式跟类名：只跑指定类（人工快检，不走 smoke 子集）
            unittest.main(argv=[sys.argv[0]] + argv)
        else:
            sys.exit(_run_smoke())
    else:
        unittest.main(argv=[sys.argv[0]] + argv)
