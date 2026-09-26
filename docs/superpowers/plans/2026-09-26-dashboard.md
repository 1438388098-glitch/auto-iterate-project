# 1.8.0 观察台（Dashboard）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 auto-iterate 增加 `autopilot dashboard` 只读观察台——独立常驻 stdlib HTTP server + 单文件原生前端，呈现纵向能力进化树 × 进化卡片流（双向联动、回放动画），零侵入迭代循环。

**Architecture:** 数据层为纯函数管道（`dashboard_data.py`：numstat 解析 → 模块聚合 → 事件关联 → `build_snapshot`）；服务层（`dashboard.py`：`ThreadingHTTPServer` 两端点 + mtime 缓存 + 空闲自退 + `dashboard.json` 生命周期）；前端为仓库内单文件 `dashboard.html`（设计 token 双主题，以 `docs/superpowers/specs/assets/dashboard-mockup.html` 为像素基准）。`begin-round` 末尾 ensure 拉起，失败只 warning。

**Tech Stack:** Python 3.13 stdlib（`http.server.ThreadingHTTPServer`、`subprocess`、`webbrowser`）、原生 HTML/CSS/JS（SVG 正交连线树）、unittest（仓库既有单文件 runner）。

**Spec:** [`../specs/2026-09-26-dashboard-design.md`](../specs/2026-09-26-dashboard-design.md)。视觉基准：[`../specs/assets/dashboard-mockup.html`](../specs/assets/dashboard-mockup.html)。

**约定（全程适用）：**
- 测试命令一律 `py -3.13 scripts/test_autopilot_state.py <TestClass> -v`（选择性）与 `py -3.13 scripts/test_autopilot_state.py`（全量，约 5 分钟）。新测试类加在 `scripts/test_autopilot_state.py`（仓库单文件测试约定，runner 是 `unittest.main()`，类名可直接作参数过滤）。
- 每个 Task 结束必须全绿再提交；提交信息中文、`feat/test/docs/chore:` 前缀。
- 新文件两个：`scripts/autopilot/dashboard_data.py`（纯函数）、`scripts/autopilot/dashboard.py`（IO/进程/HTTP）；前端一个：`scripts/autopilot/dashboard.html`。
- 复用既有设施：`io.run_git(repo, *args)`、`io._pid_alive(pid)`、`io.load_json/save_json`、`io._unquote_git_path`、`config._config_error`。

---

### Task 1: config 层——`dashboard` 字段（默认值 / 校验 / 深合并）

**Files:**
- Modify: `scripts/autopilot/config.py`（`default_config` 约 :17、`load_config` 约 :92、`validate_config` 约 :106）
- Test: `scripts/test_autopilot_state.py`（新类 `DashboardConfigTests`）

- [ ] **Step 1: 写失败测试**

```python
class DashboardConfigTests(unittest.TestCase):
    def setUp(self):
        self.repo = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (self.repo / ".git").mkdir()

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
        self.assertTrue(cfg["dashboard"]["enabled"])
        self.assertTrue(cfg["dashboard"]["auto_open"])   # 默认值未被浅合并冲掉
        self.assertEqual(cfg["dashboard"]["port"], 0)

    def test_validate_rejects_bad_dashboard_values(self):
        from autopilot import config as ap_config
        for bad in (
            {"dashboard": {"enabled": "yes"}},
            {"dashboard": {"port": -1}},
            {"dashboard": {"port": True}},
            {"dashboard": {"domain_map": {"a": "b"}}},                 # 纯字符串值非法
            {"dashboard": {"domain_map": {"a": {"meaning": "无 name"}}}},
            {"dashboard": []},
        ):
            with self.assertRaises(SystemExit):
                ap_config.validate_config(dict(ap_config.default_config(self.repo), **bad))

    def test_validate_accepts_structured_domain_map(self):
        from autopilot import config as ap_config
        cfg = ap_config.default_config(self.repo)
        cfg["dashboard"]["domain_map"] = {"scripts/autopilot/miner.py": {"name": "供给与探矿"}}
        ap_config.validate_config(cfg)  # 不抛即通过
```

- [ ] **Step 2: 跑确认失败**

Run: `py -3.13 scripts/test_autopilot_state.py DashboardConfigTests -v`
Expected: FAIL —— `KeyError: 'dashboard'`（default_config 无该键）

- [ ] **Step 3: 实现**

`config.py` `default_config` 的返回 dict 增加一项：

```python
        "dashboard": {"enabled": False, "auto_open": True, "port": 0, "domain_map": None},
```

`load_config` 中 `merged.update(config)` 之后、`validate_config(merged)` 之前加深合并：

```python
    if isinstance(config, dict) and isinstance(config.get("dashboard"), dict) \
            and isinstance(merged["dashboard"], dict):
        dash = dict(merged["dashboard"])
        dash.update(config["dashboard"])
        merged["dashboard"] = dash
```

`validate_config` 末尾追加：

```python
    dash = merged.get("dashboard")
    if dash is not None:
        if not isinstance(dash, dict):
            _config_error(path, "'dashboard' must be an object or null", source)
        for key in ("enabled", "auto_open"):
            if not isinstance(dash.get(key, False), bool):
                _config_error(path, "'dashboard.{}' must be a boolean".format(key), source)
        port = dash.get("port", 0)
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            _config_error(path, "'dashboard.port' must be an integer in 0..65535", source)
        domain_map = dash.get("domain_map")
        if domain_map is not None:
            if not isinstance(domain_map, dict):
                _config_error(path, "'dashboard.domain_map' must be an object or null", source)
            for prefix, value in domain_map.items():
                valid = isinstance(value, dict) and isinstance(value.get("name"), str) \
                    and (value.get("meaning") is None or isinstance(value.get("meaning"), str))
                if not valid:
                    _config_error(
                        path,
                        "'dashboard.domain_map[{}]' must be {{\"name\": str, \"meaning\"?: str}}".format(prefix),
                        source,
                    )
```

- [ ] **Step 4: 跑测试通过 + 全量回归**

Run: `py -3.13 scripts/test_autopilot_state.py DashboardConfigTests -v` → PASS；
Run: `py -3.13 scripts/test_autopilot_state.py` → 495+4 全绿（新计数以实际为准）

- [ ] **Step 5: Commit**

```bash
git add scripts/autopilot/config.py scripts/test_autopilot_state.py
git commit -m "feat: config 新增 dashboard 段（enabled/auto_open/port/domain_map，深合并+校验）"
```

---

### Task 2: config-set 旗标（--dashboard / --no-dashboard / --dashboard-port）

**Files:**
- Modify: `scripts/autopilot/cli.py`（config-set 参数区，`--expand-after-goals` 附近约 :318）
- Modify: `scripts/autopilot/commands.py`（`cmd_config_set` 写入区约 :187）
- Test: `scripts/test_autopilot_state.py`（类 `DashboardConfigTests` 追加）

- [ ] **Step 1: 写失败测试**

```python
    def test_config_set_dashboard_flags(self):
        from autopilot import config as ap_config
        run = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "autopilot_state.py"),
             "config-set", "--repo", str(self.repo), "--dashboard", "--dashboard-port", "8642"],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        cfg = ap_config.load_config(self.repo)
        self.assertTrue(cfg["dashboard"]["enabled"])
        self.assertEqual(cfg["dashboard"]["port"], 8642)
        run = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "autopilot_state.py"),
             "config-set", "--repo", str(self.repo), "--no-dashboard"],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertFalse(ap_config.load_config(self.repo)["dashboard"]["enabled"])
```

（`ROOT`/`subprocess`/`sys` 若测试文件顶部未导入则补导入，沿用文件内既有惯例——先查 `grep -n "^ROOT\|^import subprocess" scripts/test_autopilot_state.py`。）

- [ ] **Step 2: 跑确认失败**

Run: `py -3.13 scripts/test_autopilot_state.py DashboardConfigTests.test_config_set_dashboard_flags -v`
Expected: FAIL —— `unrecognized arguments: --dashboard`

- [ ] **Step 3: 实现**

`cli.py` config-set 参数区（模式照抄 `--expand-after-goals` 互斥组）：

```python
    dash_group = config_set_parser.add_mutually_exclusive_group()
    dash_group.add_argument("--dashboard", dest="dashboard_enabled", action="store_true", default=None,
                            help="enable the read-only observation dashboard")
    dash_group.add_argument("--no-dashboard", dest="dashboard_enabled", action="store_false", default=None,
                            help="disable the observation dashboard")
    config_set_parser.add_argument("--dashboard-port", type=int, default=None, metavar="N",
                                   help="dashboard port (0 = random)")
```

`commands.py` `cmd_config_set` 写入区（`cfg["expand_after_goals"]` 同款）：

```python
    if args.dashboard_enabled is not None:
        cfg.setdefault("dashboard", {})["enabled"] = bool(args.dashboard_enabled)
    if getattr(args, "dashboard_port", None) is not None:
        if not 0 <= args.dashboard_port <= 65535:
            sys.exit("[ERROR] --dashboard-port must be an integer in 0..65535.")
        cfg.setdefault("dashboard", {})["port"] = args.dashboard_port
```

（写入后既有的 reload+validate+save_config+指纹刷新路径原样生效；若该函数无 reload 步骤则按文件内相邻字段的既有流程对齐。）

- [ ] **Step 4: 跑通过 + 全量回归**
- [ ] **Step 5: Commit**

```bash
git add scripts/autopilot/cli.py scripts/autopilot/commands.py scripts/test_autopilot_state.py
git commit -m "feat: config-set 支持 --dashboard/--no-dashboard/--dashboard-port"
```

---

### Task 3: numstat 解析与逐轮文件改动 `compute_round_file_changes`

**Files:**
- Create: `scripts/autopilot/dashboard_data.py`
- Test: `scripts/test_autopilot_state.py`（新类 `DashboardDataTests`）

- [ ] **Step 1: 写失败测试**

```python
class DashboardDataTests(unittest.TestCase):
    def test_parse_numstat_lines_basic_rename_and_binary(self):
        from autopilot import dashboard_data as dd
        raw = (
            "12\t 3\tscripts/autopilot/state.py\n"
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

    def test_compute_round_file_changes_returns_none_when_anchor_missing(self):
        from autopilot import dashboard_data as dd
        self.assertIsNone(dd.compute_round_file_changes("R", [{"round": 1, "commit_sha": None}], "000"))
```

- [ ] **Step 2: 跑确认失败**

Run: `py -3.13 scripts/test_autopilot_state.py DashboardDataTests -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'autopilot.dashboard_data'`

- [ ] **Step 3: 实现 `scripts/autopilot/dashboard_data.py`**

```python
"""Pure data pipeline for the read-only dashboard: git numstat parsing,
round file changes, module aggregation, event correlation, snapshot
assembly. No process spawning, no state writes — tests feed it fakes."""
from __future__ import annotations

from autopilot import io as ap_io


def parse_numstat(raw):
    """Parse `git diff --numstat` output into [{path, insertions, deletions,
    binary, renamed}]. Rename syntax `{old => new}` (and full-line
    `old => new`) is normalized to the NEW path; quoted paths are unquoted."""
    changes = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        ins, dele, path = parts
        renamed = " => " in path
        if renamed:
            if path.startswith("{") and "}" in path:
                head, rest = path.split("}", 1)
                new_side = head.split(" => ", 1)[1] if " => " in head else ""
                path = new_side + rest
            else:
                path = path.split(" => ", 1)[1]
        binary = ins == "-" or dele == "-"
        changes.append({
            "path": ap_io._unquote_git_path(path),
            "insertions": 0 if binary else int(ins),
            "deletions": 0 if binary else int(dele),
            "binary": binary,
            "renamed": renamed,
        })
    return changes


def compute_round_file_changes(repo, history, run_start_sha, gitio=None):
    """Per-round `git diff --numstat` over each round's commit anchor.
    Rounds without commit_sha (zero-work cancels) consume no diff. Returns
    {path: {first_round, touches, insertions, deletions, rounds}} or None
    when any completed round lacks an anchor (pre-1.4 state → degraded)."""
    run_git = gitio.run_git if gitio is not None else ap_io.run_git
    changes = {}
    prev = run_start_sha
    for entry in history:
        sha = entry.get("commit_sha")
        if entry["round"] is not None and not sha:
            return None
        if not sha:
            continue
        raw = run_git(repo, "diff", "--numstat", "{}..{}".format(prev, sha)) or ""
        for item in parse_numstat(raw):
            agg = changes.setdefault(item["path"], {
                "first_round": entry["round"], "touches": 0,
                "insertions": 0, "deletions": 0, "rounds": [],
            })
            agg["touches"] += 1
            agg["insertions"] += item["insertions"]
            agg["deletions"] += item["deletions"]
            if entry["round"] not in agg["rounds"]:
                agg["rounds"].append(entry["round"])
        prev = sha
    return changes
```

- [ ] **Step 4: 跑通过 + 全量回归**
- [ ] **Step 5: Commit**

```bash
git add scripts/autopilot/dashboard_data.py scripts/test_autopilot_state.py
git commit -m "feat: dashboard 数据层——numstat 解析与逐轮文件改动（取消轮不产生 diff，缺锚点返回 None 降级）"
```

---

### Task 4: 模块聚合器 `aggregate_modules`（domain_map / 内置启发式 / weight）

**Files:**
- Modify: `scripts/autopilot/dashboard_data.py`
- Test: `scripts/test_autopilot_state.py`（类 `DashboardDataTests` 追加）

- [ ] **Step 1: 写失败测试**

```python
    FIXTURE_CHANGES = {
        "scripts/autopilot/miner.py": {"first_round": 8, "touches": 6, "insertions": 180, "deletions": 44, "rounds": [8, 14, 20]},
        "scripts/autopilot/state.py": {"first_round": 1, "touches": 3, "insertions": 90, "deletions": 10, "rounds": [1, 16]},
        "scripts/test_autopilot_state.py": {"first_round": 1, "touches": 10, "insertions": 900, "deletions": 100, "rounds": [1, 18]},
        "references/config.md": {"first_round": 12, "touches": 2, "insertions": 30, "deletions": 4, "rounds": [12]},
        "Makefile": {"first_round": 2, "touches": 1, "insertions": 5, "deletions": 0, "rounds": [2]},
    }

    def test_aggregate_builtin_heuristics(self):
        from autopilot import dashboard_data as dd
        domains = dd.aggregate_modules(self.FIXTURE_CHANGES, None)
        by_name = {d["name"]: d for d in domains}
        self.assertIn("核心实现", by_name)
        self.assertIn("测试", by_name)
        self.assertIn("文档与知识", by_name)
        self.assertIn("其他", by_name)                       # Makefile 无命中
        self.assertEqual(by_name["测试"]["modules"][0]["path"], "scripts/test_autopilot_state.py")
        core = by_name["核心实现"]
        self.assertEqual(core["first_round"], 1)
        self.assertEqual(core["weight"], 1.0)               # touches 最大者归一为 1
        self.assertLess(by_name["其他"]["weight"], core["weight"])

    def test_aggregate_domain_map_overrides_and_meaning(self):
        from autopilot import dashboard_data as dd
        domain_map = {
            "scripts/autopilot/miner.py": {"name": "供给与探矿", "meaning": "挖掘器决定迭代上限"},
            "scripts/autopilot/": {"name": "核心循环", "meaning": "每轮执行的主路径"},
        }
        domains = dd.aggregate_modules(self.FIXTURE_CHANGES, domain_map)
        by_name = {d["name"]: d for d in domains}
        self.assertEqual(by_name["供给与探矿"]["meaning"], "挖掘器决定迭代上限")
        self.assertEqual(by_name["供给与探矿"]["modules"][0]["path"], "scripts/autopilot/miner.py")
        self.assertEqual(by_name["核心循环"]["modules"][0]["path"], "scripts/autopilot/state.py")
        self.assertNotIn("核心实现", by_name)                # 全部被映射覆盖
```

- [ ] **Step 2: 跑确认失败**

Run: `py -3.13 scripts/test_autopilot_state.py DashboardDataTests -v`
Expected: FAIL —— `AttributeError: module 'autopilot.dashboard_data' has no attribute 'aggregate_modules'`

- [ ] **Step 3: 实现（追加到 dashboard_data.py）**

```python
# 内置通用启发式：(路径前缀, 域名)，按列表序匹配；用户 domain_map 最长前缀优先且先于内置。
BUILTIN_DOMAIN_RULES = [
    ("test", "测试"),
    ("docs/", "文档与知识"), ("references/", "文档与知识"),
    ("README", "文档与知识"), ("CHANGELOG", "文档与知识"), ("SKILL", "文档与知识"),
    ("scripts/", "工具与脚本"), ("tools/", "工具与脚本"), ("bin/", "工具与脚本"),
]
BUILTIN_DOMAIN_MEANINGS = {
    "测试": "回归防护网：改坏了立即知道",
    "文档与知识": "agent 与人共同的知识面",
    "工具与脚本": "构建与辅助工具链",
    "核心实现": "产品行为的所在",
    "其他": "尚未归类的杂项",
}


def _match_domain_map(path, domain_map):
    best, best_len = None, -1
    for prefix, value in (domain_map or {}).items():
        if path.startswith(prefix) and len(prefix) > best_len:
            best, best_len = value, len(prefix)
    return best


def _builtin_domain(path):
    for prefix, name in BUILTIN_DOMAIN_RULES:
        if path.startswith(prefix):
            return name
    if "/" not in path:
        return "配置与入口"
    return "核心实现"


def aggregate_modules(file_changes, domain_map):
    """Group {path: change} into domain trees. domain_map wins over builtins
    via longest-prefix; each domain carries first_round/active_rounds/weight
    (touches normalized to the busiest domain = 1.0) and its modules."""
    domains = {}
    for path, ch in file_changes.items():
        mapped = _match_domain_map(path, domain_map)
        if mapped is not None:
            name = mapped["name"]
            meaning = mapped.get("meaning") or ""
        else:
            name = _builtin_domain(path)
            meaning = BUILTIN_DOMAIN_MEANINGS.get(name, "")
        domain = domains.setdefault(name, {
            "name": name, "meaning": meaning, "modules": {},
            "first_round": ch["first_round"], "active_rounds": set(), "touches": 0,
        })
        domain["meaning"] = domain["meaning"] or meaning
        domain["first_round"] = min(domain["first_round"], ch["first_round"])
        domain["active_rounds"].update(ch["rounds"])
        domain["touches"] += ch["touches"]
        module = domain["modules"].setdefault(path, {
            "name": path.rsplit("/", 1)[-1], "path": path,
            "first_round": ch["first_round"], "churn": {
                "touches": ch["touches"], "insertions": ch["insertions"],
                "deletions": ch["deletions"],
            },
            "files": [path],
        })
        module["first_round"] = min(module["first_round"], ch["first_round"])
    max_touches = max((d["touches"] for d in domains.values()), default=1) or 1
    result = []
    for name in sorted(domains, key=lambda n: -domains[n]["touches"]):
        d = domains[name]
        modules = sorted(d["modules"].values(),
                         key=lambda m: -m["churn"]["touches"])
        result.append({
            "id": name, "name": name, "meaning": d["meaning"],
            "first_round": d["first_round"],
            "active_rounds": sorted(d["active_rounds"]),
            "weight": round(d["touches"] / max_touches, 3),
            "modules": modules,
        })
    return result
```

- [ ] **Step 4: 跑通过 + 全量回归**
- [ ] **Step 5: Commit**

```bash
git add scripts/autopilot/dashboard_data.py scripts/test_autopilot_state.py
git commit -m "feat: 模块聚合器——domain_map 最长前缀优先 + 内置启发式 + 生长强度归一"
```

---

### Task 5: 事件关联器与轮次趋势 `correlate_events`

**Files:**
- Modify: `scripts/autopilot/dashboard_data.py`
- Test: `scripts/test_autopilot_state.py`（类 `DashboardDataTests` 追加）

- [ ] **Step 1: 写失败测试**

```python
    def test_correlate_events_maps_domains_and_keeps_all_statuses(self):
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

    def test_correlate_events_defaults_domains_to_empty(self):
        from autopilot import dashboard_data as dd
        events = dd.correlate_events([{"round": 1, "status": "completed", "title": "t",
                                       "summary": "", "review_score": None, "commit_sha": "x"}], {})
        self.assertEqual(events[0]["domains"], [])
```

- [ ] **Step 2: 跑确认失败**

Run: `py -3.13 scripts/test_autopilot_state.py DashboardDataTests.test_correlate_events_maps_domains_and_keeps_all_statuses -v`
Expected: FAIL —— 无 `correlate_events`

- [ ] **Step 3: 实现（追加）**

```python
def correlate_events(history, round_domains):
    """history entries → evolution cards, oldest first. cancelled/aborted
    rounds are kept (rendered grey upstream); missing review_score → None."""
    events = []
    for entry in history:
        events.append({
            "round": entry["round"],
            "status": entry.get("status"),
            "title": entry.get("title"),
            "summary": entry.get("summary"),
            "score": entry.get("review_score"),
            "domains": sorted(round_domains.get(entry["round"], [])),
            "commit_sha": entry.get("commit_sha"),
        })
    return events
```

- [ ] **Step 4: 跑通过 + 全量回归**
- [ ] **Step 5: Commit**

```bash
git add scripts/autopilot/dashboard_data.py scripts/test_autopilot_state.py
git commit -m "feat: 事件关联器——轮次×域归因，cancelled/aborted 如实入史"
```

---

### Task 6: `build_snapshot` 组装（含 degraded 与 no-run）

**Files:**
- Modify: `scripts/autopilot/dashboard_data.py`
- Test: `scripts/test_autopilot_state.py`（类 `DashboardSnapshotTests`）

- [ ] **Step 1: 写失败测试**

```python
class DashboardSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.repo = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (self.repo / ".git").mkdir()

    def _seed_run(self):
        from autopilot import config as ap_config
        from autopilot import state as ap_state
        ap_config.save_config(self.repo, ap_config.default_config(self.repo))
        st = ap_state.new_state(self.repo)   # 若工厂名不同，用文件内既有测试夹具的建 state 方式
        st["history"] = [
            {"round": 1, "status": "completed", "title": "t1", "summary": "s1",
             "review_score": 4, "commit_sha": None, "estimated_tokens": 0},
        ]
        ap_state.save_state(self.repo, st)

    def test_snapshot_no_run_reports_error(self):
        from autopilot import dashboard_data as dd
        snap = dd.build_snapshot(self.repo)
        self.assertEqual(snap, {"error": "no-run"})

    def test_snapshot_shape_and_degraded_growth(self):
        from autopilot import dashboard_data as dd
        self._seed_run()
        snap = dd.build_snapshot(self.repo)
        self.assertEqual(snap["meta"]["degraded"], ["growth"])   # commit_sha 为 None
        self.assertEqual(snap["status"]["phase"], "running")
        self.assertEqual(snap["growth"]["events"][0]["title"], "t1")
        self.assertIn("goals", snap["status"])
        self.assertIn("backlog", snap["status"])
```

（`new_state`/`save_state` 的真实签名以 `grep -n "def new_state\|def save_state" scripts/autopilot/state.py` 为准，若不同按文件内其他测试类的建夹具方式对齐。）

- [ ] **Step 2: 跑确认失败**
- [ ] **Step 3: 实现（追加）**

```python
import json as _json
from pathlib import Path as _Path

import autopilot


def _autopilot_dir(repo):
    return _Path(repo) / ap_io.AUTOPILOT_DIR


def build_snapshot(repo, gitio=None):
    state_path = _autopilot_dir(repo) / "state.json"
    if not state_path.exists():
        return {"error": "no-run"}
    from autopilot import config as ap_config
    from autopilot import state as ap_state
    degraded = []
    state = ap_io.load_json(state_path, None)
    if state is None:
        return {"meta": {"generated_at": ap_io.now_iso(), "skill_version": autopilot.__version__,
                         "run_id": None, "degraded": ["status", "growth", "narrative"]},
                "status": None, "growth": None, "narrative": None}
    config = ap_config.load_config(repo)
    history = state.get("history", [])
    changes = compute_round_file_changes(repo, history, state.get("run_start_sha"), gitio=gitio)
    if changes is None:
        degraded.append("growth")
        domains, round_domains, rounds = [], {}, []
    else:
        domain_map = (config.get("dashboard") or {}).get("domain_map")
        domains = aggregate_modules(changes, domain_map)
        path_to_domain = {m["path"]: d["name"] for d in domains for m in d["modules"]}
        round_domains = {}
        for path, ch in changes.items():
            for r in ch["rounds"]:
                round_domains.setdefault(r, set()).add(path_to_domain[path])
        rounds = [{
            "round": entry["round"], "status": entry.get("status"),
            "score": entry.get("review_score"),
            "files_changed": len([p for p, c in changes.items() if entry["round"] in c["rounds"]]),
            "insertions": sum(c["insertions"] for c in changes.values() if entry["round"] in c["rounds"]),
            "deletions": sum(c["deletions"] for c in changes.values() if entry["round"] in c["rounds"]),
        } for entry in history]
    goals = state.get("goals", [])
    completed = [h for h in history if h.get("status") == "completed"]
    snapshot = {
        "meta": {
            "generated_at": ap_io.now_iso(),
            "skill_version": autopilot.__version__,
            "run_id": state.get("run_id"),
            "degraded": degraded,
        },
        "status": {
            "phase": "finished" if state.get("finished_at") else "running",
            "round": state.get("round"), "round_seq": state.get("round_seq"),
            "completed_rounds": len(completed),
            "blocked_rounds": state.get("blocked_rounds", 0),
            "cancelled_rounds": state.get("cancelled_rounds", 0),
            "budget": {
                "max_minutes": (config.get("max_minutes") if isinstance(config, dict) else None),
                "estimated_tokens_used": state.get("estimated_tokens_used", 0),
            },
            "goals": {"total": len(goals), "met": len(state.get("completed_goals", []))},
            "backlog": _backlog_summary(_autopilot_dir(repo) / "backlog.json"),
            "expansion_waves": len(state.get("expansion_waves", [])),
        },
        "growth": {
            "domains": domains,
            "events": correlate_events(history, round_domains),
            "rounds": rounds,
        },
        "narrative": {
            "has_last_summary": (_autopilot_dir(repo) / "last-summary.md").exists(),
            "retrospective_exists": (_autopilot_dir(repo) / "retrospective.md").exists(),
        },
    }
    return snapshot


def _backlog_summary(path):
    data = ap_io.load_json(path, None) or {}
    candidates = data.get("candidates", data if isinstance(data, list) else [])
    pending = [c for c in candidates if c.get("status") == "pending"]
    return {"total": len(candidates), "pending": len(pending),
            "ready": sum(1 for c in pending if c.get("value", 0) >= 4)}
```

（`backlog.json` 的真实结构与 `state.get("round_seq")` 等键名以 `scripts/autopilot/state.py` / `miner.py` 为准对齐；`now_iso` 已在 io 中存在。）

- [ ] **Step 4: 跑通过 + 全量回归**
- [ ] **Step 5: Commit**

```bash
git add scripts/autopilot/dashboard_data.py scripts/test_autopilot_state.py
git commit -m "feat: build_snapshot——状态/进化/叙事三板块组装，no-run 与 degraded 路径"
```

---

### Task 7: `dashboard.json` 生命周期（读写 / 探活 / ensure / --stop）

**Files:**
- Create: `scripts/autopilot/dashboard.py`
- Test: `scripts/test_autopilot_state.py`（新类 `DashboardLifecycleTests`）

- [ ] **Step 1: 写失败测试**

```python
class DashboardLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.repo = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (self.repo / ".git").mkdir()

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
```

（`os` 导入按文件顶部既有惯例。）

- [ ] **Step 2: 跑确认失败**

Run: `py -3.13 scripts/test_autopilot_state.py DashboardLifecycleTests -v`
Expected: FAIL —— `No module named 'autopilot.dashboard'`

- [ ] **Step 3: 实现 `scripts/autopilot/dashboard.py`**

```python
"""Read-only observation dashboard: lifecycle (spawn/probe/stop), the local
HTTP server, snapshot caching. Hard rule: dashboard problems must never
block the iteration loop — callers wrap ensure_dashboard in try/except."""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from autopilot import io as ap_io

IDLE_TIMEOUT_SECONDS = 30 * 60
SNAPSHOT_TTL_SECONDS = 2.0


def _info_path(repo):
    return Path(repo) / ap_io.AUTOPILOT_DIR / "dashboard.json"


def write_info(repo, info):
    path = _info_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(info), encoding="utf-8")


def read_info(repo):
    path = _info_path(repo)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def info_alive(repo):
    info = read_info(repo)
    return bool(info) and ap_io._pid_alive(info["pid"])


def clear_stale_info(repo):
    if _info_path(repo).exists() and not info_alive(repo):
        _info_path(repo).unlink(missing_ok=True)


def stop_server(repo):
    info = read_info(repo)
    if not info:
        return False
    try:
        subprocess.run(["taskkill", "/PID", str(info["pid"]), "/F"],
                       capture_output=True, timeout=10) if sys.platform == "win32" \
            else ap_io._pid_alive(info["pid"]) and _terminate(info["pid"])
    finally:
        _info_path(repo).unlink(missing_ok=True)
    return True


def _terminate(pid):
    import os
    import signal
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


def spawn_server(repo, config):
    dash = config.get("dashboard") or {}
    entry = Path(__file__).resolve().parent.parent / "autopilot_state.py"
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(
        [sys.executable, str(entry), "dashboard", "--serve",
         "--repo", str(repo), "--port", str(dash.get("port", 0))],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs,
    )
    deadline = time.time() + 5.0
    while time.time() < deadline:
        info = read_info(repo)
        if info and info.get("pid") != _UNSET:
            return info
        time.sleep(0.1)
    return None


def ensure_dashboard(repo, config):
    """begin-round hook: start the dashboard once if enabled. Never raises —
    the caller still wraps this in try/except and logs a warning."""
    dash = config.get("dashboard") or {}
    if not dash.get("enabled"):
        return None
    clear_stale_info(repo)
    if info_alive(repo):
        return read_info(repo)
    return spawn_server(repo, config)
```

（`_UNSET` 占位在 Task 8 的 serve 实现中替换为真实 pid 校验：`info.get("pid") and ap_io._pid_alive(info["pid"])`——spawn 后要等到**新进程**写好自己的 info，避免读到别的陈旧记录；测试在 Task 8 补。）

- [ ] **Step 4: 跑通过（生命周期部分）**
- [ ] **Step 5: Commit**

```bash
git add scripts/autopilot/dashboard.py scripts/test_autopilot_state.py
git commit -m "feat: dashboard 生命周期——info 读写/探活/陈旧清理/ensure/spawn/stop"
```

---

### Task 8: HTTP server（两端点 / mtime 缓存 / 空闲自退 / auto_open）+ CLI 子命令

**Files:**
- Modify: `scripts/autopilot/dashboard.py`
- Modify: `scripts/autopilot/cli.py`（`dashboard` 子命令注册）
- Modify: `scripts/autopilot/commands.py`（`cmd_dashboard`）
- Modify: `scripts/autopilot_state.py`（如需透传新子命令——按其既有转发方式）
- Test: `scripts/test_autopilot_state.py`（新类 `DashboardServerTests`）

- [ ] **Step 1: 写失败测试**

```python
class DashboardServerTests(unittest.TestCase):
    def setUp(self):
        self.repo = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (self.repo / ".git").mkdir()

    def _start(self):
        from autopilot import dashboard as ap_dash
        server, port = ap_dash.start_in_thread(self.repo)
        self.addCleanup(server.shutdown)
        return port

    def test_endpoints_and_readonly(self):
        import json as _json
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
        # 只读保证：请求前后 .autopilot 目录内容一致（dashboard.json 除外，由生命周期管理）
        before = sorted(p.name for p in (self.repo / ".autopilot").glob("*")) if (self.repo / ".autopilot").exists() else []
        with urllib.request.urlopen(base + "/api/snapshot", timeout=5):
            pass
        after = sorted(p.name for p in (self.repo / ".autopilot").glob("*")) if (self.repo / ".autopilot").exists() else []
        self.assertEqual(before, after)

    def test_cached_snapshot_hits_until_state_mtime_changes(self):
        import json as _json
        import urllib.request
        from autopilot import config as ap_config
        from autopilot import state as ap_state
        ap_config.save_config(self.repo, ap_config.default_config(self.repo))
        port = self._start()
        base = "http://127.0.0.1:{}/api/snapshot".format(port)
        first = _json.loads(urllib.request.urlopen(base, timeout=5).read())
        second = _json.loads(urllib.request.urlopen(base, timeout=5).read())
        self.assertEqual(first["meta"]["generated_at"], second["meta"]["generated_at"])
        time.sleep(0.02)
        # mtime 粒度粗（NTFS 100ns/ FAT 2s）：直接调内部失效函数验证语义
        from autopilot import dashboard as ap_dash
        ap_dash.invalidate_snapshot_cache()
        third = _json.loads(urllib.request.urlopen(base, timeout=5).read())
        self.assertNotEqual(second["meta"]["generated_at"], third["meta"]["generated_at"])
```

- [ ] **Step 2: 跑确认失败**
- [ ] **Step 3: 实现（dashboard.py 追加 + cli/commands 注册）**

```python
# ---- HTTP server ----
_snapshot_cache = {"key": None, "snapshot": None}


def invalidate_snapshot_cache():
    _snapshot_cache["key"] = None


def _cache_key(repo):
    from autopilot import dashboard_data as dd
    names = ("state.json", "backlog.json", "analysis.json", "config.json")
    mtimes = []
    for name in names:
        p = dd._autopilot_dir(repo) / name
        try:
            mtimes.append(p.stat().st_mtime_ns)
        except OSError:
            mtimes.append(None)
    return tuple(mtimes)


def get_snapshot(repo):
    key = _cache_key(repo)
    now = time.time()
    if _snapshot_cache["key"] == key and _snapshot_cache["snapshot"] is not None \
            and now - _snapshot_cache["at"] < SNAPSHOT_TTL_SECONDS:
        return _snapshot_cache["snapshot"]
    from autopilot import dashboard_data as dd
    snapshot = dd.build_snapshot(repo)
    _snapshot_cache.update(key=key, snapshot=snapshot, at=now)
    return snapshot


def _make_handler(repo):
    from autopilot import dashboard_data as dd
    page_path = Path(__file__).resolve().parent / "dashboard.html"
    page_bytes = page_path.read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            Handler.last_request_at = time.time()
            if self.path == "/" or self.path.startswith("/?"):
                self._send(200, page_bytes, "text/html; charset=utf-8")
            elif self.path == "/api/snapshot":
                try:
                    body = json.dumps(get_snapshot(repo), ensure_ascii=False).encode("utf-8")
                    self._send(200, body, "application/json; charset=utf-8")
                except Exception as err:                      # 单请求异常不杀进程
                    body = json.dumps({"error": "internal", "detail": str(err)}).encode("utf-8")
                    self._send(500, body, "application/json; charset=utf-8")
            else:
                self._send(404, b'{"error": "not-found"}', "application/json")

        def log_message(self, *args):                         # 静默访问日志
            pass

    Handler.last_request_at = time.time()
    return Handler


def start_in_thread(repo, host="127.0.0.1"):
    handler = _make_handler(repo)
    server = ThreadingHTTPServer((host, 0), handler)
    port = server.server_address[1]
    write_info(repo, {"pid": _current_pid(), "port": port,
                      "started_at": ap_io.now_iso(), "opened": False})
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port


def _current_pid():
    import os
    return os.getpid()


def serve(repo, port=0, auto_open=True, host="127.0.0.1"):
    """Blocking entry for `autopilot dashboard --serve`. Writes dashboard.json,
    optionally opens the browser once, exits after the idle timeout."""
    handler = _make_handler(repo)
    server = ThreadingHTTPServer((host, port), handler)
    port = server.server_address[1]
    write_info(repo, {"pid": _current_pid(), "port": port,
                      "started_at": ap_io.now_iso(), "opened": False})
    if auto_open:
        import webbrowser
        try:
            webbrowser.open("http://{}:{}/".format(host, port))
        except Exception:
            pass
    idle_watchdog(server)
    server.serve_forever()


def idle_watchdog(server, timeout=IDLE_TIMEOUT_SECONDS):
    """Replaces serve_forever's loop-forever: poll last_request_at and exit
    (deleting dashboard.json) after `timeout` idle seconds."""
    handler_type = type(server.RequestHandlerClass and server.RequestHandlerClass or object)
    while True:
        time.sleep(60)
        handler_cls = server.RequestHandlerClass
        if time.time() - handler_cls.last_request_at > timeout:
            break
    server.shutdown()
```

（`idle_watchdog` 的 last_request_at 读法在实现时以 `_make_handler` 挂的 `Handler.last_request_at` 类属性为准——上面 `handler_type` 两行是多余的，实现时删除，直接 `server.RequestHandlerClass.last_request_at`。）

`cli.py`（`subparsers` 上注册，模式照抄 `read`）：

```python
    dash_parser = subparsers.add_parser("dashboard", help="Run the read-only observation dashboard")
    dash_parser.add_argument("--repo", default=".")
    dash_parser.add_argument("--serve", action="store_true", help="run the server in the foreground")
    dash_parser.add_argument("--port", type=int, default=0)
    dash_parser.add_argument("--no-open", dest="auto_open", action="store_false", default=True)
    dash_parser.add_argument("--stop", action="store_true", help="stop a running dashboard")
    dash_parser.set_defaults(func=commands.cmd_dashboard)
```

`commands.py`：

```python
def cmd_dashboard(args):
    from autopilot import dashboard as ap_dash
    repo = Path(args.repo).resolve()
    if args.stop:
        sys.exit(0 if ap_dash.stop_server(repo) else 2)
    ap_dash.serve(repo, port=args.port, auto_open=args.auto_open)
```

`autopilot_state.py` 若是纯转发（`from autopilot.cli import main`）则无需改动。

- [ ] **Step 4: 跑通过 + 全量回归**
- [ ] **Step 5: Commit**

```bash
git add scripts/autopilot/dashboard.py scripts/autopilot/cli.py scripts/autopilot/commands.py scripts/test_autopilot_state.py
git commit -m "feat: dashboard HTTP server——两端点/mtime 缓存/空闲自退/auto_open + CLI 子命令"
```

---

### Task 9: 前端 `dashboard.html`——骨架、设计 token 双主题、布局（以 v5 样张为基准）

**Files:**
- Create: `scripts/autopilot/dashboard.html`
- Test: `scripts/test_autopilot_state.py`（类 `DashboardPageContractTests`，本任务先锁 CSS/结构断言）

- [ ] **Step 1: 写失败测试**

```python
class DashboardPageContractTests(unittest.TestCase):
    PAGE = (Path(autopilot.__file__).resolve().parent / "dashboard.html")

    def test_page_exists_and_carries_design_tokens(self):
        html = self.PAGE.read_text(encoding="utf-8")
        for token in ("--bg", "--surface", "--accent", "prefers-color-scheme",
                      "prefers-reduced-motion"):
            self.assertIn(token, html)
```

- [ ] **Step 2: 跑确认失败** —— `FileNotFoundError`
- [ ] **Step 3: 写页面骨架**

以 `docs/superpowers/specs/assets/dashboard-mockup.html` 为基底拷贝，替换静态数字为挂载点并补双主题：

1. `<style>` 的 `:root` 保留暗色 token，追加亮色块与主题切换变量：

```css
  @media (prefers-color-scheme: light) {
    :root:not([data-theme="dark"]) {
      --bg:#FAFAF8; --surface:#FFFFFF; --surface2:#F0F0EE; --line:#E4E4E0;
      --txt:#1A1A1A; --txt2:#5A5A62; --txt3:#98989E;
      --accent:#0E8A64; --accent-dim:#BFE3D6; --warm:#C77A3A;
    }
  }
  :root[data-theme="light"] {
    --bg:#FAFAF8; --surface:#FFFFFF; --surface2:#F0F0EE; --line:#E4E4E0;
    --txt:#1A1A1A; --txt2:#5A5A62; --txt3:#98989E;
    --accent:#0E8A64; --accent-dim:#BFE3D6; --warm:#C77A3A;
  }
```

2. 动效降级（全文件仅此一处动画声明口径）：

```css
  @media (prefers-reduced-motion: reduce) {
    *, *::before, *::after { animation: none !important; transition: none !important; }
  }
```

3. 统计行/树面板/卡片流/回放轴的静态文字改为挂载点：`<span id="stat-rounds">`、`<div id="tree"></div>`、`<div id="events"></div>`、`<div class="axis"><div class="fill" id="axis-fill"></div><div class="cursor" id="axis-cursor"></div></div>`；顶栏右上追加主题切换按钮 `<button id="theme-toggle">`（循环 auto→light→dark，写 `localStorage["dashboard-theme"]`，初始读它）。

- [ ] **Step 4: 跑契约测试通过**
- [ ] **Step 5: Commit**

```bash
git add scripts/autopilot/dashboard.html scripts/test_autopilot_state.py
git commit -m "feat: dashboard 页面骨架——双主题 token/布局挂载点/主题切换/动效降级口径"
```

---

### Task 10: 前端数据渲染——树布局器、卡片流、联动、轮询

**Files:**
- Modify: `scripts/autopilot/dashboard.html`
- Test: `scripts/test_autopilot_state.py`（类 `DashboardPageContractTests` 追加 API 契约断言）

- [ ] **Step 1: 写失败测试（字段契约，防 API 漂移）**

```python
    def test_page_api_references_exist_in_snapshot_shape(self):
        import re
        from autopilot import dashboard_data as dd
        html = self.PAGE.read_text(encoding="utf-8")
        refs = {m.group(1).split(".")[0]
                for m in re.finditer(r"\bsnapshot\.([A-Za-z_][A-Za-z0-9_]*)", html)}
        self.assertTrue(refs, "page must reference snapshot fields")
        allowed = {"meta", "status", "growth", "narrative", "error"}
        self.assertLessEqual(refs, allowed)
```

- [ ] **Step 2: 跑确认失败**（页面还没有任何 `snapshot.` 引用）
- [ ] **Step 3: 写渲染脚本（`<script>` 追加到页面）**

```js
const $ = (id) => document.getElementById(id);
let SNAPSHOT = null;
let expanded = new Set();          // 展开的域 id
let highlighted = null;            // 联动高亮的轮次 → 域集合
let replayRound = null;            // 回放态：仅显示 first_round <= replayRound

async function poll() {
  if (document.hidden) return;
  try {
    const res = await fetch("/api/snapshot");
    const next = await res.json();
    if (next.error === "no-run") { renderNoRun(); return; }
    if (!SNAPSHOT || next.meta.generated_at !== SNAPSHOT.meta.generated_at) {
      SNAPSHOT = next;
      renderAll();
    }
  } catch (err) { /* server 未起/重启中：保留旧画面 */ }
}
setInterval(poll, 3000);
poll();

function renderNoRun() {
  $("tree").innerHTML = '<div class="sub">此处尚无运行——在目标仓库执行 autopilot init 后刷新。</div>';
}

function visibleDomains() {
  const rounds = SNAPSHOT.growth.rounds || [];
  const cutoff = replayRound == null ? Infinity
    : (rounds.find(r => r.round === replayRound) || {}).round ?? replayRound;
  const domains = (SNAPSHOT.growth.domains || []).map(d => ({
    ...d,
    modules: replayRound == null ? d.modules
      : d.modules.filter(m => m.first_round <= cutoff),
  })).filter(d => d.modules.length || replayRound == null);
  return domains;
}

function layoutTree(domains) {
  // 正交连线布局：根(84,36) → 主干竖线 → 每域水平出枝 → 圆点+同基线文字。
  const nodes = [], edges = [];
  let y = 88;
  for (const d of domains) {
    const open = expanded.has(d.id);
    nodes.push({ kind: "domain", x: 152, y, id: d.id, label: d.name,
                 meta: d.meaning, weight: d.weight,
                 active: highlighted ? d.active_rounds.includes(latestActive(d)) : true });
    edges.push({ from: [84, y], to: [144, y], width: 1.2 + 2 * (d.weight || 0), accent: !highlighted || d.active_rounds.some(r => highlighted.has(d.id)) });
    if (open) {
      const mods = replayRound == null ? d.modules : visibleModules(d);
      let my = y + 30;
      edges.push({ from: [152, y + 8], to: [152, my + (mods.length - 1) * 24], width: 1.2, accent: false, elbow: true });
      for (const m of mods) {
        nodes.push({ kind: "module", x: 175, y: my, id: d.id + "::" + m.path, label: m.name,
                     meta: "r" + m.first_round + " 起", weight: 0.3, active: true, domainId: d.id });
        edges.push({ from: [152, my], to: [168, my], width: 1.2, accent: false });
        my += 24;
      }
      y = my + 18;
    } else {
      y += 58;
    }
  }
  return { nodes, edges, trunkEnd: y };
}

function renderTree() {
  if (!SNAPSHOT || !SNAPSHOT.growth) return;
  const domains = visibleDomains();
  const { nodes, edges, trunkEnd } = layoutTree(domains);
  const NS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", "0 0 560 " + Math.max(320, trunkEnd + 40));
  const mk = (tag, attrs) => { const el = document.createElementNS(NS, tag);
    for (const k in attrs) el.setAttribute(k, attrs[k]); return el; };
  // 主干
  svg.appendChild(mk("path", { d: "M 84 46 L 84 " + trunkEnd, stroke: "var(--line)", "stroke-width": 2, fill: "none" }));
  for (const e of edges) {
    const d = e.elbow
      ? "M " + e.from[0] + " " + e.from[1] + " L " + e.to[0] + " " + e.to[1]
      : "M " + e.from[0] + " " + e.from[1] + " L " + e.to[0] + " " + e.to[1];
    svg.appendChild(mk("path", { d, stroke: e.accent ? "var(--accent)" : "var(--line)",
      "stroke-width": e.width, fill: "none", opacity: e.accent ? .9 : 1 }));
  }
  for (const n of nodes) {
    const g = mk("g", { style: "cursor:pointer", "data-node": n.id });
    g.appendChild(mk("circle", { cx: n.x, cy: n.y, r: n.kind === "domain" ? 4.5 : 3,
      fill: n.active ? "var(--accent)" : "var(--txt3)" }));
    const label = mk("text", { x: n.x + 14, y: n.y + 4, fill: "var(--txt)",
      "font-size": n.kind === "domain" ? 12 : 11, "font-weight": n.kind === "domain" ? 600 : 400,
      "font-family": "Consolas,monospace" });
    label.textContent = n.kind === "domain" ? n.label : n.label;
    g.appendChild(label);
    if (n.meta) {
      const meta = mk("text", { x: n.x + 130, y: n.y + 4, fill: "var(--txt3)",
        "font-size": 10, "font-family": "Consolas,monospace" });
      meta.textContent = n.meta;
      g.appendChild(meta);
    }
    if (n.kind === "domain") {
      g.addEventListener("click", () => {
        expanded.has(n.id) ? expanded.delete(n.id) : expanded.add(n.id);
        renderTree();
      });
    }
    svg.appendChild(g);
  }
  const host = $("tree");
  host.innerHTML = "";
  host.appendChild(svg);
}

function latestActive(d) {
  return d.active_rounds.length ? d.active_rounds[d.active_rounds.length - 1] : d.first_round;
}

function visibleModules(d) {
  const cutoff = replayRound;
  return d.modules.filter(m => m.first_round <= cutoff);
}

function renderEvents() {
  const host = $("events");
  host.innerHTML = "";
  const events = [...(SNAPSHOT?.growth?.events || [])].reverse();
  const latestCompleted = events.find(e => e.status === "completed");
  for (const ev of events) {
    const card = document.createElement("div");
    card.className = "ev" + (ev === latestCompleted ? " hot" : "")
      + (ev.status === "cancelled" || ev.status === "aborted" ? " dim" : "");
    const dim = ev.status === "cancelled" || ev.status === "aborted";
    card.innerHTML =
      '<div class="meta mono">r' + ev.round + " · " + (ev.score == null ? "—" : "自评 " + ev.score + "/5")
      + " · " + ev.status + (ev.domains.length ? " → " + ev.domains.join("、") : "") + "</div>"
      + '<div class="t">' + escapeHtml(ev.title || "") + "</div>"
      + '<div class="s">' + escapeHtml(ev.summary || "") + "</div>";
    card.style.opacity = dim ? .65 : 1;
    card.addEventListener("click", () => {
      highlighted = highlighted ? null : new Set(ev.domains);
      renderTree(); renderEvents();
    });
    host.appendChild(card);
  }
}

function renderStats() {
  const s = SNAPSHOT.status;
  $("stat-rounds").textContent = s.round + " / " + (s.round_seq || s.round);
  $("stat-completed").textContent = s.completed_rounds;
  $("stat-goals").textContent = s.goals.met + " / " + s.goals.total;
  $("stat-tokens").textContent = Math.round((s.budget.estimated_tokens_used || 0) / 100) / 10 + "k";
  $("stat-scale").textContent = (SNAPSHOT.growth.domains || []).length + " 域 · "
    + SNAPSHOT.growth.domains.reduce((n, d) => n + d.modules.length, 0) + " 模块";
  // 目标行与轴
  $("goal-label").textContent = "目标 " + s.goals.met + "/" + s.goals.total;
  $("goal-fill").style.width = s.goals.total ? (100 * s.goals.met / s.goals.total) + "%" : "0%";
  const rounds = SNAPSHOT.growth.rounds || [];
  const last = rounds.length ? rounds[rounds.length - 1].round : 1;
  $("axis-cursor").style.right = (100 - 100 * (last || 1) / (last || 1)) + "%";
  $("axis-label").textContent = "r1 — r" + last;
}

function renderAll() { renderStats(); renderTree(); renderEvents(); }

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
```

（挂载点 id 与 Task 9 的骨架一一对应；`axis-cursor` 定位、`visibleDomains` 中 `??` 兜底在实现时以真实数据字段名核对——`rounds[].round` 已在 Task 6 定义。）

- [ ] **Step 4: 跑契约测试通过 + 手工核验**

Run: `py -3.13 scripts/test_autopilot_state.py DashboardPageContractTests -v` → PASS
手验：对本仓库真实数据起 server（`py -3.13 scripts/autopilot_state.py dashboard --repo . --no-open`），浏览器开 `http://127.0.0.1:<port>/`：22 轮历史、树/卡片/联动与样张观感一致。

- [ ] **Step 5: Commit**

```bash
git add scripts/autopilot/dashboard.html scripts/test_autopilot_state.py
git commit -m "feat: 前端渲染——正交树布局器/进化卡片流/点卡联动高亮/3s 轮询与 no-run 引导"
```

---

### Task 11: 前端回放（逐轮生长 stagger + 游标跳转）

**Files:**
- Modify: `scripts/autopilot/dashboard.html`
- Test: 契约测试追加 + 人工验收

- [ ] **Step 1: 写失败测试（契约：replay 入口存在）**

```python
    def test_replay_controls_present(self):
        html = self.PAGE.read_text(encoding="utf-8")
        for needle in ("replayRound", "axis-cursor", "stagger"):
            self.assertIn(needle, html)
```

- [ ] **Step 2: 跑确认失败**
- [ ] **Step 3: 实现回放**

```js
let playing = false;
$("play").addEventListener("click", () => playing ? stopReplay() : startReplay());

function startReplay() {
  playing = true;
  const rounds = SNAPSHOT.growth.rounds.map(r => r.round);
  let i = 0;
  const step = () => {
    if (!playing || i >= rounds.length) { stopReplay(); return; }
    replayRound = rounds[i++];
    renderTree(); renderEvents();
    document.querySelectorAll("#tree [data-node]").forEach((el, idx) => {
      el.style.animation = "grow-in .4s cubic-bezier(.22,1,.36,1) " + Math.min(idx * 60, 1200) + "ms backwards";
    });
    $("axis-cursor").style.right = (100 - 100 * replayRound / rounds[rounds.length - 1]) + "%";
    replayTimer = setTimeout(step, 650);
  };
  step();
}

function stopReplay() {
  playing = false;
  clearTimeout(replayTimer);
  replayRound = null;
  renderTree(); renderEvents();
}
```

CSS 追加（与 §4.6 约束一致：只动 opacity/transform，错峰封顶 1.2s）：

```css
  @keyframes grow-in { from { opacity:0; transform:translateY(8px); } to { opacity:1; transform:translateY(0); } }
```

游标拖动（input range 覆盖在轴上或 pointer 事件，一期实现 pointerdown/move 计算百分比 → `replayRound = roundAt(pct)` 即时重渲染，无动画）。

- [ ] **Step 4: 跑契约 + 手工验收（回放 22 轮、拖动跳转、播完回当前态）**
- [ ] **Step 5: Commit**

```bash
git add scripts/autopilot/dashboard.html scripts/test_autopilot_state.py
git commit -m "feat: 回放——逐轮生长 stagger（60ms/封顶1.2s）、游标拖动跳转、播完回当前态"
```

---

### Task 12: begin-round 挂点、dogfooding 配置、版本 bump、文档同步、全量验证

**Files:**
- Modify: `scripts/autopilot/commands.py`（`cmd_begin_round` 末尾）
- Modify: `.autopilot/config.json`（本仓库 dogfooding domain_map——若该文件不入库则改在 README 说明）
- Modify: `scripts/autopilot/__init__.py`、`SKILL.md`、`agents/openai.yaml`、`references/overview.md`、`README.md`、`CHANGELOG.md`（版本六处 → 1.8.0）
- Modify: `references/config.md`、`references/troubleshooting.md`
- Test: 全量

- [ ] **Step 1: begin-round 挂点（写失败测试先行）**

```python
    def test_begin_round_ensures_dashboard_and_never_blocks(self):
        # enabled=False：零副作用
        # enabled=True 且 spawn 失败（monkeypatch spawn_server 抛异常）：
        #   begin-round 仍正常完成，log.jsonl 出现 dashboard warning
```

（以 `DashboardLifecycleTests` 风格补两条：一条断言 disabled 时无 info 文件；一条 monkeypatch `ap_dash.spawn_server = raiser` 后跑 `cmd_begin_round` 断言 returncode 0。实现：`cmd_begin_round` 成功返回前追加——

```python
    try:
        from autopilot import dashboard as ap_dash
        ap_dash.ensure_dashboard(repo, cfg)
    except Exception as err:
        say("[WARN] dashboard ensure failed: {}".format(err))
```

）

- [ ] **Step 2: dogfooding——本仓库 domain_map 预置**

`.autopilot/config.json` 的 dashboard 段写入（与样张四域一致）：

```json
"dashboard": {
  "enabled": true,
  "auto_open": true,
  "port": 0,
  "domain_map": {
    "scripts/autopilot/miner.py": {"name": "供给与探矿", "meaning": "挖掘器决定迭代上限：backlog 从哪来、为什么值得做"},
    "scripts/autopilot/state.py": {"name": "核心循环", "meaning": "agent 每轮执行的主路径：开轮→取活→收轮"},
    "scripts/autopilot/commands.py": {"name": "核心循环"},
    "scripts/autopilot/config.py": {"name": "核心循环"},
    "scripts/autopilot/io.py": {"name": "核心循环"},
    "scripts/autopilot/cli.py": {"name": "核心循环"},
    "scripts/autopilot/guard.py": {"name": "核心循环"},
    "scripts/autopilot/agent.py": {"name": "核心循环"},
    "scripts/autopilot/verify.py": {"name": "核心循环"},
    "scripts/autopilot/dashboard": {"name": "观察台", "meaning": "1.8 长出的可视化面板"},
    "scripts/test_": {"name": "质量防护", "meaning": "回归防护网：改坏了立即知道"},
    "references/": {"name": "文档与知识"},
    "SKILL.md": {"name": "文档与知识"},
    "README.md": {"name": "文档与知识"},
    "CHANGELOG.md": {"name": "文档与知识"},
    "docs/": {"name": "文档与知识"}
  }
}
```

- [ ] **Step 3: 版本 bump 六处 → 1.8.0 + CHANGELOG 新段 + SKILL.md 观察台节 + config.md dashboard 字段表 + troubleshooting 三行（端口占用 / server 起不来 / 面板打不开）+ README 一句话**
- [ ] **Step 4: 全量测试 + 真机验收清单**

Run: `py -3.13 scripts/test_autopilot_state.py` → 全绿（预计 495 → 520+）
Run: `py -3.13 scripts/autopilot_state.py dashboard --repo .` → 浏览器自动打开，人工核对：
- [ ] 树与样张观感一致（正交连线、同基线、双主题切换、亮暗跟随系统）
- [ ] 22 轮卡片流、cancelled/aborted 灰卡、点卡联动高亮
- [ ] 回放逐轮生长、拖动跳转、播完回当前态
- [ ] `taskkill`/`--stop` 后 `dashboard.json` 清理；begin-round 在 disabled 下零开销

- [ ] **Step 5: Commit + Push**

```bash
git add -A
git commit -m "feat: 1.8.0 观察台——begin-round ensure 挂点/dogfooding domain_map/版本六处 bump/文档同步"
git push origin main
```

---

## 自审记录（writing-plans Self-Review）

1. **Spec 覆盖**：§2 架构→T7/T8；§3 管道→T3-T6；§4 页面→T9-T11；§5 配置与生命周期→T1/T2/T7/T8；§6 错误处理→T6（no-run/degraded）+T8（500/404）+T7（陈旧清理）；§7 测试→各任务内嵌 + T10 契约；§8 文档→T12；§9 版本→T12。无缺口。
2. **占位符扫描**：T7 中 `_UNSET` 占位已注明在 T8 替换为真实 pid 校验；T8 `idle_watchdog` 中多余两行已注明删除；T6 `new_state`/backlog 结构、T2 `ROOT` 导入均标注「以 grep 为准对齐既有惯例」——这些是**对接既有代码的核对接点**而非功能占位，每个都给了查找命令与预期形态。
3. **类型一致性**：`compute_round_file_changes(repo, history, run_start_sha, gitio=None)` 在 T3 定义、T6 调用（参数序一致）；`aggregate_modules(file_changes, domain_map)` T4 定义、T6 调用一致；`correlate_events(history, round_domains)` T5 定义、T6 调用一致；`ensure_dashboard(repo, config)` T7 定义、T12 调用一致；snapshot 顶层键（meta/status/growth/narrative/error）T6 定义、T10 契约白名单一致。
