"""Pure data pipeline for the read-only dashboard: git numstat parsing,
round file changes, module aggregation, event correlation, snapshot
assembly. No process spawning, no state writes — tests feed it fakes."""

import json

from pathlib import Path

from . import io


# History statuses that legitimately close a round without a commit: their
# work stayed uncommitted, so a missing commit_sha is expected, not degraded.
NO_ANCHOR_STATUSES = ("cancelled", "aborted", "blocked")


def _rename_new_side(path):
    """Reduce a numstat rename path to its destination. The brace form wraps
    only the differing suffix around the shared prefix — `src/{a.py => b.py}`
    → ``src/b.py``, `a/{x.py => sub/y.py}` → ``a/sub/y.py``,
    `d/{ => sub}/g.py` → ``d/sub/g.py`` — while the brace-less full form
    `old/name.py => new/renamed.py` keeps just the right side."""
    if "{" in path and " => " in path.split("{", 1)[1].split("}", 1)[0]:
        prefix, rest = path.split("{", 1)
        inner, suffix = rest.split("}", 1)
        new_tail = inner.split(" => ", 1)[1]
        return prefix + new_tail + suffix
    return path.split(" => ", 1)[1]


def parse_numstat(raw):
    """Parse `git diff --numstat` output into [{path, insertions, deletions,
    binary, renamed}]. Rename syntax `{old => new}` (and full-line
    `old => new`) is normalized to the NEW path; git-quoted paths are
    unquoted; binary rows (``-`` columns) count 0 lines. The rename
    normalization is a heuristic: without ``-z`` a path literally containing
    " => " (a legal unquoted filename) is indistinguishable from a rename —
    the same lenient-parsing limit as io._parse_numstat."""
    changes = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        ins, dele, path = parts
        if not ((ins.isdigit() and dele.isdigit()) or (ins == "-" and dele == "-")):
            # A returncode==0 numstat should not emit such lines; skip them
            # leniently like io._parse_numstat instead of raising on int().
            continue
        renamed = " => " in path
        if renamed:
            path = _rename_new_side(path)
        binary = ins == "-"
        changes.append({
            "path": io._unquote_git_path(path),
            "insertions": 0 if binary else int(ins),
            "deletions": 0 if binary else int(dele),
            "binary": binary,
            "renamed": renamed,
        })
    return changes


def _resolve_run_git(gitio):
    """The growth pipeline's shared run_git seam: ``gitio.run_git`` when a
    fake is injected, else a wrapper over io.run_git returning stdout text
    with None on failure (used by compute_round_file_changes and
    compute_round_stats)."""
    if gitio is not None:
        return gitio.run_git

    def run_git(repo_, *args):
        result = io.run_git(repo_, *args)
        return result.stdout if result.returncode == 0 else None

    return run_git


def compute_round_file_changes(repo, history, run_start_sha, gitio=None):
    """Per-round `git diff --numstat` over each round's commit anchor: every
    anchored round diffs prev-anchor..sha and advances the anchor. Rounds
    closed without a commit legitimately (see NO_ANCHOR_STATUSES) consume no
    diff and leave the anchor untouched; any OTHER shaless round (completed /
    legacy statusless) cannot anchor its work, so the whole growth view
    degrades to None rather than showing a partial picture — the same for a
    failed git diff, which must never render as an empty one. The ``gitio``
    seam exposes run_git(repo, *args) -> stdout text with None on failure
    (the default path wraps io.run_git's CompletedProcess). Returns
    {path: {first_round, touches, insertions, deletions, rounds}} where
    touches counts round appearances, rounds dedups, first_round is the min."""
    run_git = _resolve_run_git(gitio)

    changes = {}
    prev = run_start_sha
    for entry in history:
        # batch_commit_sha（flush --round 回填）与 commit_sha 同为合法锚点；
        # 同批多轮共享同一 sha，随后各轮 prev==sha 产生空 diff——行数归属在
        # 批次边界，依然真实。
        sha = entry.get("commit_sha") or entry.get("batch_commit_sha")
        if sha and sha == prev:
            # 同批多轮共享同一回填 sha：无新改动可 diff，静默跳过
            continue
        if sha:
            base = prev or io.EMPTY_TREE
            raw = run_git(repo, "diff", "--numstat", "{}..{}".format(base, sha))
            if raw is None:
                return None
            for item in parse_numstat(raw):
                agg = changes.setdefault(item["path"], {
                    "first_round": entry.get("round"), "touches": 0,
                    "insertions": 0, "deletions": 0, "rounds": [],
                })
                agg["touches"] += 1
                agg["insertions"] += item["insertions"]
                agg["deletions"] += item["deletions"]
                rnd = entry.get("round")
                if rnd not in agg["rounds"]:
                    agg["rounds"].append(rnd)
                if agg["first_round"] is None or (rnd is not None and rnd < agg["first_round"]):
                    agg["first_round"] = rnd
            prev = sha
        elif entry.get("status") in NO_ANCHOR_STATUSES:
            continue
        else:
            return None
    return changes


def compute_round_stats(repo, history, run_start_sha, gitio=None):
    """Per-round churn for the rounds chart, walking the same anchors as
    compute_round_file_changes (identical seam, anchor advance and
    NO_ANCHOR_STATUSES skip; any other shaless round or a failed diff degrades
    the whole view to None). Returns [{round, status, files_changed,
    insertions, deletions}] in history order, one entry per anchored round,
    counted from that round's own numstat rows — a path touched in several
    rounds contributes to each of them, so nothing is double-counted across
    rounds (the per-path lifetime aggregate lives in
    compute_round_file_changes; running the anchor walk twice keeps both
    contracts intact at the cost of doubled — and cheap — numstat diffs).
    Unanchored NO_ANCHOR rounds produce no entry; build_snapshot renders them
    as zeros."""
    run_git = _resolve_run_git(gitio)
    stats = []
    prev = run_start_sha
    for entry in history:
        sha = entry.get("commit_sha")
        if sha:
            base = prev or io.EMPTY_TREE
            raw = run_git(repo, "diff", "--numstat", "{}..{}".format(base, sha))
            if raw is None:
                return None
            rows = parse_numstat(raw)
            stats.append({
                "round": entry.get("round"), "status": entry.get("status"),
                "files_changed": len(rows),
                "insertions": sum(r["insertions"] for r in rows),
                "deletions": sum(r["deletions"] for r in rows),
            })
            prev = sha
        elif entry.get("status") in NO_ANCHOR_STATUSES:
            continue
        else:
            return None
    return stats


# 内置通用启发式：(路径前缀, 域名)，按列表序匹配；用户 domain_map 最长前缀
# 优先且先于内置。test 规则必须在 scripts/ 之前：scripts/test_*.py 是测试
# 而非工具。不带斜杠的前缀（test/README/…）同时匹配文件基名，让嵌在任何
# 目录下的测试文件命中「测试」；带斜杠的前缀只锚定路径本身。
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
    "配置与入口": "根散文件：装配与启动",
}


def _match_domain_map(path, domain_map):
    """domain_map 命中：最长前缀优先，返回 {"name", "meaning"?} 或 None。"""
    best, best_len = None, -1
    for prefix, value in (domain_map or {}).items():
        if path.startswith(prefix) and len(prefix) > best_len:
            best, best_len = value, len(prefix)
    return best


def _builtin_domain(path):
    base = path.rsplit("/", 1)[-1]
    for prefix, name in BUILTIN_DOMAIN_RULES:
        if path.startswith(prefix) or (not prefix.endswith("/") and base.startswith(prefix)):
            return name
    if "/" not in path:
        return "配置与入口"
    return "核心实现"


def aggregate_modules(file_changes, domain_map):
    """Group {path: change} (compute_round_file_changes 的产物) into domain
    trees. domain_map 以最长前缀命中且优先于内置启发式；自定义域名与内置撞名
    且未给 meaning 时回退内置默认释义，全新域名未给 meaning 则留空；同域多条目
    时首个非空 meaning 胜出（依赖 file_changes 插入序）。每域聚合
    first_round（min，None 轮号不参与）、active_rounds（并集，None 排最后）、
    weight（touches 对最忙域归一，最忙 = 1.0）与按 touches 降序的 modules；
    域本身也按 touches 降序，同分按域名典序，保证快照间树形稳定。模块字段
    遵循快照契约（设计文档 §3.1）：{name, path, first_round, churn, files}。
    空输入返回 []。"""
    domains = {}
    for path, ch in file_changes.items():
        mapped = _match_domain_map(path, domain_map)
        if mapped is not None:
            name = mapped["name"]
            meaning = mapped.get("meaning") or BUILTIN_DOMAIN_MEANINGS.get(name, "")
        else:
            name = _builtin_domain(path)
            meaning = BUILTIN_DOMAIN_MEANINGS.get(name, "")
        domain = domains.setdefault(name, {
            "name": name, "meaning": meaning, "modules": {},
            "first_round": ch["first_round"], "active_rounds": set(), "touches": 0,
        })
        domain["meaning"] = domain["meaning"] or meaning
        cur = ch["first_round"]
        if domain["first_round"] is None or (cur is not None and cur < domain["first_round"]):
            domain["first_round"] = cur
        domain["active_rounds"].update(ch["rounds"])
        domain["touches"] += ch["touches"]
        domain["modules"][path] = {
            "name": path.rsplit("/", 1)[-1], "path": path,
            "first_round": cur,
            "churn": {"touches": ch["touches"], "insertions": ch["insertions"],
                      "deletions": ch["deletions"]},
            "files": [path],
        }
    max_touches = max((d["touches"] for d in domains.values()), default=1) or 1
    result = []
    for name in sorted(domains, key=lambda n: (-domains[n]["touches"], n)):
        d = domains[name]
        modules = sorted(d["modules"].values(),
                         key=lambda m: (-m["churn"]["touches"], m["path"]))
        result.append({
            "id": name, "name": name, "meaning": d["meaning"],
            "first_round": d["first_round"],
            "active_rounds": sorted(d["active_rounds"], key=lambda r: (r is None, r)),
            "weight": round(d["touches"] / max_touches, 3),
            "modules": modules,
        })
    return result


def correlate_events(history, round_domains):
    """History entries → evolution cards, oldest first (input order preserved;
    state.history is already old→new). round is hard-required (join key —
    malformed history raises); cancelled/aborted rounds are kept (rendered
    grey upstream); missing review_score → None."""
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


def _autopilot_dir(repo):
    """The run's .autopilot directory (Path-wrapped; repo may be str)."""
    return Path(repo) / io.AUTOPILOT_DIR


# 项目总框架扫描（init 时一次）：树可读性优先，域/模块超出上限折叠进「其他」；
# 隐藏条目与构建噪声目录不进骨架。
FRAMEWORK_MAX_DOMAINS = 10
FRAMEWORK_MAX_MODULES = 8
FRAMEWORK_EXCLUDE_DIRS = {"node_modules", "__pycache__", "dist", "build",
                          "venv", ".venv", "target", "vendor", "out"}


def scan_project_framework(repo, domain_map=None, gitio=None):
    """init 时扫描仓库，产出项目总框架（域→模块→文件数），生长树据此绘制
    完整骨架而非只有轮次改过的文件。文件清单取 `git ls-files --cached
    --others --exclude-standard`（已跟踪 + 未忽略的新文件）；排除 .autopilot
    与隐藏/噪声目录。每个文件走与轮次叠加层同一套分类器（domain_map 最长
    前缀 + 内置启发式）——框架域名与活动域名必然同名，合并时才对得上。
    模块取二级目录（深度不足取文件名），聚合文件数；域按文件数降序，超
    上限的域整体折叠进「其他」，域内超限模块同理。git 不可用返回 None：
    调用方留空 project_map，树回退纯轮次叠加层。"""
    run_git = _resolve_run_git(gitio)
    raw = run_git(repo, "ls-files", "--cached", "--others", "--exclude-standard")
    if raw is None:
        return None
    domains = {}
    for path in raw.splitlines():
        path = io._unquote_git_path(path.strip())
        if not path or path.startswith(io.AUTOPILOT_DIR + "/"):
            continue
        segments = path.split("/")
        if any(seg.startswith(".") or seg in FRAMEWORK_EXCLUDE_DIRS
               for seg in segments):
            continue
        mapped = _match_domain_map(path, domain_map)
        name = (mapped or {}).get("name") or _builtin_domain(path)
        meaning = (mapped or {}).get("meaning") or BUILTIN_DOMAIN_MEANINGS.get(name, "")
        domain = domains.setdefault(name, {"name": name, "meaning": meaning,
                                           "modules": {}, "files": 0})
        # ≥3 段（scripts/autopilot/a.py）→ 模块取二级目录；不足取文件名
        module_key = "/".join(segments[:2]) if len(segments) >= 3 else path
        module_name = segments[1] if len(segments) >= 3 else segments[-1]
        module = domain["modules"].setdefault(
            module_key, {"name": module_name, "path": module_key, "files": 0})
        module["files"] += 1
        domain["files"] += 1
    if not domains:
        return None
    ranked = sorted(domains.values(), key=lambda d: (-d["files"], d["name"]))
    result = []
    for d in ranked[:FRAMEWORK_MAX_DOMAINS]:
        mods = sorted(d["modules"].values(), key=lambda m: (-m["files"], m["path"]))
        overflow = mods[FRAMEWORK_MAX_MODULES:]
        if overflow:
            mods = mods[:FRAMEWORK_MAX_MODULES] + [{
                "name": "其他", "path": d["name"] + "/…",
                "files": sum(m["files"] for m in overflow)}]
        result.append({"name": d["name"], "meaning": d["meaning"], "modules": mods})
    folded = ranked[FRAMEWORK_MAX_DOMAINS:]
    if folded:
        rest = []
        for d in folded:
            for m in d["modules"].values():
                rest.append({"name": d["name"] + "/" + m["name"],
                             "path": d["name"] + "/" + m["path"], "files": m["files"]})
        rest.sort(key=lambda m: (-m["files"], m["path"]))
        result.append({"name": "其他", "meaning": "",
                       "modules": rest[:FRAMEWORK_MAX_MODULES]})
    return {"scanned_at": io.now_iso(), "domains": result}


def merge_project_framework(project_map, domains):
    """项目总框架（state.project_map，init 扫描）并入轮次叠加层：树画出完
    整骨架，轮次活动（first_round/active_rounds/churn）按域同名、模块路径
    精确或前缀匹配叠加。run 中新建而无框架模块承载的文件作为 growth-only
    模块追加（total_files=1）；框架扫描后新长出来的域整体追加（framework_
    files=0）。无 project_map 时原样返回——纯叠加层行为不变。"""
    if not isinstance(project_map, dict) or not project_map.get("domains"):
        return domains
    growth = {d["name"]: d for d in domains}
    merged = []
    for fd in project_map["domains"]:
        name = fd["name"]
        gd = growth.pop(name, None)
        gmods = {m["path"]: m for m in (gd or {}).get("modules", [])}
        modules = []
        claimed = set()
        for fm in fd.get("modules") or []:
            children = [(p, m) for p, m in gmods.items()
                        if p == fm["path"] or p.startswith(fm["path"] + "/")]
            claimed.update(p for p, _ in children)
            first_round = None
            touches = insertions = deletions = 0
            for _, m in children:
                if m["first_round"] is not None and (
                        first_round is None or m["first_round"] < first_round):
                    first_round = m["first_round"]
                touches += m["churn"]["touches"]
                insertions += m["churn"]["insertions"]
                deletions += m["churn"]["deletions"]
            modules.append({
                "name": fm["name"], "path": fm["path"],
                "first_round": first_round,
                "churn": {"touches": touches, "insertions": insertions,
                          "deletions": deletions},
                "files": [f for _, c in children for f in c["files"]],
                "total_files": fm["files"],
            })
        for p, m in gmods.items():
            if p not in claimed:
                modules.append(dict(m, total_files=1))
        modules.sort(key=lambda m: (-(m["churn"]["touches"] or 0), m["path"]))
        merged.append({
            "id": name, "name": name,
            "meaning": (gd or {}).get("meaning") or fd.get("meaning") or "",
            "first_round": (gd or {}).get("first_round"),
            "active_rounds": (gd or {}).get("active_rounds", []),
            "weight": (gd or {}).get("weight", 0),
            "framework_files": sum(fm["files"] for fm in fd.get("modules") or []),
            "modules": modules,
        })
    for d in domains:
        if d["name"] in growth:
            merged.append(dict(d, framework_files=0))
    merged.sort(key=lambda d: (-(d.get("weight") or 0),
                               -(d.get("framework_files") or 0), d["name"]))
    return merged


def compute_touched_file_changes(history):
    """Batch-mode fallback #2 (seed-002): complete-round records each round's
    touched_files (worktree names minus pre-round dirty files), giving
    per-round domain attribution without any git diff anchor. Same
    {path: change} shape; insertions/deletions are honestly 0 (no diff).
    None when no entry carries touched_files — callers keep their chain:
    per-round diff → touched files → run-level → degraded."""
    changes = {}
    for entry in history:
        files = entry.get("touched_files")
        if not files:
            continue
        rnd = entry.get("round")
        for path in files:
            agg = changes.setdefault(path, {
                "first_round": rnd, "touches": 0,
                "insertions": 0, "deletions": 0, "rounds": [],
            })
            agg["touches"] += 1
            if rnd not in agg["rounds"]:
                agg["rounds"].append(rnd)
            if agg["first_round"] is None or (rnd is not None and rnd < agg["first_round"]):
                agg["first_round"] = rnd
    return changes or None


def compute_run_file_changes(repo, run_start_sha, gitio=None):
    """Run-level fallback for batch-commit runs whose history carries no
    per-round shas: one numstat over run_start_sha..HEAD (EMPTY_TREE base
    when unanchored, same as the per-round walk). Returns the same
    {path: change} shape as compute_round_file_changes with first_round=None
    and rounds=[] — the tree renders, the timeline does not exist. None when
    HEAD cannot be resolved or the diff fails (fail-closed, callers keep
    their degraded path)."""
    run_git = _resolve_run_git(gitio)
    head_raw = run_git(repo, "rev-parse", "--verify", "-q", "HEAD")
    if not head_raw:
        return None
    head = head_raw.strip().splitlines()[0].strip()
    base = run_start_sha or io.EMPTY_TREE
    raw = run_git(repo, "diff", "--numstat", "{}..{}".format(base, head))
    if raw is None:
        return None
    changes = {}
    for item in parse_numstat(raw):
        changes[item["path"]] = {
            "first_round": None, "touches": 1,
            "insertions": item["insertions"], "deletions": item["deletions"],
            "rounds": [],
        }
    return changes


def _recent_log_events(repo, limit=10):
    """log.jsonl 尾部事件（可观测性）：每条 {ts, event, status}，坏行跳过。
    无日志（新 run）→ 空列表；绝不抛错打断快照。"""
    path = _autopilot_dir(repo) / "log.jsonl"
    events = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if isinstance(d, dict):
                    events.append({
                        "ts": d.get("ts"), "event": d.get("event"),
                        "status": d.get("status"),
                    })
    except OSError:
        return []
    return events[-limit:]


def build_snapshot(repo, gitio=None):
    """Assemble the read-only dashboard snapshot (design doc §3.1): a ``meta``
    header (generated_at, skill_version, run_id, degraded), a ``status``
    section (phase, round counters, budget, goals, backlog, expansion waves),
    the ``growth`` section (domains, events, per-round churn) and
    ``narrative`` (last-summary / retrospective presence). Reads .autopilot
    files directly — never state.load_state: no migrations, no writes,
    nothing mutated. A repo without a state run reports {"error": "no-run"}; a
    state file that cannot be read as an object (corrupt JSON — io.load_json
    raises SystemExit, so it is caught here — or any non-object document)
    degrades ALL sections to None instead of guessing. Growth degrades when
    rounds cannot be anchored to commits (shaless completed rounds — legal
    under deferred-commit batches — or failed diffs): domains and rounds go
    empty while events still render, attributed to no domain. Degradation is
    always preferred over fabrication. ``gitio`` forwards to the growth
    pipeline's run_git seam; the numstat anchor walk runs twice (per-path
    aggregation + per-round churn), negligible at run scale with the snapshot
    cached upstream."""
    from . import config as ap_config
    from . import __version__   # 惰性取版本号，待包 __init__ 完成（同 cli.main）

    repo = Path(repo)
    state_path = _autopilot_dir(repo) / io.STATE_FILENAME
    if not state_path.exists():
        return {"error": "no-run"}
    try:
        state = io.load_json(state_path, None)
    except SystemExit:
        # io.load_json 对坏 JSON/不可读文件会打印并 SystemExit(2)——只读快照
        # 降级为全空板块，而不是拖垮渲染进程。
        state = None
    if not isinstance(state, dict):
        return {
            "meta": {"generated_at": io.now_iso(), "skill_version": __version__,
                     "run_id": None, "degraded": ["status", "growth", "narrative"]},
            "status": None, "growth": None, "narrative": None,
        }
    try:
        config = ap_config.load_config(repo)
    except SystemExit:
        # 损坏/非法 config 同样不拖垮快照：预算与 domain_map 回退为空。
        config = None
    history = state.get("history") or []
    run_start_sha = state.get("run_start_sha")
    changes = compute_round_file_changes(repo, history, run_start_sha, gitio=gitio)
    round_stats = compute_round_stats(repo, history, run_start_sha, gitio=gitio)
    degraded = []
    granularity = "per-round"
    if changes is None or round_stats is None:
        # Batch-commit runs (default cadence) carry no per-round shas: fall
        # back so the tree stays alive, honestly labelled. First per-round
        # file attribution from touched_files (seed-002), then a coarse
        # run-level aggregation.
        touched = compute_touched_file_changes(history)
        if touched is not None:
            changes = touched
            granularity = "per-round-files"
        else:
            changes = compute_run_file_changes(repo, run_start_sha, gitio=gitio)
            granularity = "run" if changes is not None else None
        round_stats = []
        if changes is None:
            degraded.append("growth")
            domains, round_domains, rounds = [], {}, []
        else:
            round_domains, rounds = {}, []
    if granularity is not None:
        dash_cfg = (config.get("dashboard") or {}) if isinstance(config, dict) else {}
        domain_map = dash_cfg.get("domain_map")
        domains = aggregate_modules(changes, domain_map)
        path_to_domain = {m["path"]: d["name"] for d in domains for m in d["modules"]}
        round_domains = {}
        for path, ch in changes.items():
            for rnd in ch["rounds"]:
                round_domains.setdefault(rnd, set()).add(path_to_domain[path])
        stats_by_round = {st["round"]: st for st in round_stats}
        rounds = []
        for entry in history:
            # round 是事件关联的硬关联键（correlate_events 同款纪律）：
            # 缺失即 malformed history，直接 KeyError 而非静默编一条。
            st = stats_by_round.get(entry["round"])
            rnd = entry["round"]
            rounds.append({
                "round": rnd, "status": entry.get("status"),
                "score": entry.get("review_score"),
                # per-round / per-round-files：按 changes 的 rounds 归属计数；
                # run 级无逐轮信息，无提交的取消/阻塞轮如实计 0。
                "files_changed": len([p for p, c in changes.items()
                                      if rnd in c["rounds"]]),
                "insertions": st["insertions"] if st else 0,
                "deletions": st["deletions"] if st else 0,
            })
    # 项目总框架并入：即使轮次叠加层完全降级（无提交锚点），init 扫描的
    # 骨架也让树保持可用——降级标记只影响活动叠加，不灭整棵树。
    domains = merge_project_framework(state.get("project_map"), domains)
    completed = [h for h in history if h.get("status") == "completed"]
    return {
        "meta": {
            "generated_at": io.now_iso(),
            "skill_version": __version__,
            "run_id": state.get("run_id"),
            "degraded": degraded,
        },
        "status": {
            "phase": "finished" if state.get("finished_at") else "running",
            "round": state.get("round"), "round_seq": state.get("round_seq"),
            "completed_rounds": len(completed),   # 从 history 统计，不照抄 state 键
            "blocked_rounds": state.get("blocked_rounds") or 0,
            "cancelled_rounds": state.get("cancelled_rounds") or 0,
            "budget": {
                "max_minutes": (config.get("max_minutes")
                                if isinstance(config, dict) else None),
                "estimated_tokens_used": state.get("estimated_tokens_used") or 0,
                "max_rounds": (config.get("max_rounds")
                               if isinstance(config, dict) else None),
            },
            "goals": {"total": len(state.get("goals") or []),
                      "met": len(state.get("completed_goals") or [])},
            "backlog": _backlog_summary(_autopilot_dir(repo) / io.BACKLOG_FILENAME),
            "expansion_waves": len(state.get("expansion_waves") or []),
        },
        "growth": {
            "granularity": granularity,
            "domains": domains,
            "events": correlate_events(history, round_domains),
            "rounds": rounds,
        },
        "narrative": {
            # SKILL.md 流程把上一轮总结落在 .autopilot/last-summary.md。
            "has_last_summary": (_autopilot_dir(repo) / "last-summary.md").exists(),
            "retrospective_exists": (_autopilot_dir(repo)
                                     / io.RETROSPECTIVE_FILENAME).exists(),
            "retrospective_excerpt": _read_head(
                _autopilot_dir(repo) / io.RETROSPECTIVE_FILENAME, 600),
            "recent_events": _recent_log_events(repo),
        },
    }


def _read_head(path, limit=600):
    """First `limit` chars of a text file, or None when absent/unreadable —
    lets the panel show the retrospective opening without shipping the file."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return None


def _backlog_summary(path):
    """{total, pending, ready} over backlog.json（真实结构：{"next_id": …,
    "candidates": […]；status 值域 pending/picked/completed/blocked，候选带
    1-5 的 value 整数）。ready 取轻量启发式 value>=4 的待办——commands 的
    依赖就绪语义（candidate_deps_status）对只读快照过重；顶层裸 list 亦容忍。
    文件缺失、损坏（io.load_json 对坏 JSON 会 SystemExit(2)，此处降级捕获）
    一律按全 0 计，绝不抛错打断快照。"""
    try:
        data = io.load_json(path, None)
    except SystemExit:
        data = None
    if isinstance(data, dict):
        candidates = data.get("candidates") or []
    elif isinstance(data, list):
        candidates = data
    else:
        candidates = []
    pending = [c for c in candidates
               if isinstance(c, dict) and c.get("status") == "pending"]
    # 迭代方向板块（原始设计四板块之一）：价值序待办明细，供右栏看板。
    # 只取精简字段——快照是只读聚合，不搬运整个 backlog。
    def _score(c):
        v = c.get("score")
        return v if isinstance(v, (int, float)) else 0.0

    top_pending = [
        {"id": c.get("id"), "title": c.get("title"),
         "value": c.get("value"), "effort": c.get("effort"),
         "type": c.get("type"), "score": _score(c)}
        for c in sorted(pending, key=_score, reverse=True)[:6]
    ]
    return {
        "total": len(candidates),
        "pending": len(pending),
        "ready": sum(1 for c in pending
                     if isinstance(c.get("value"), (int, float)) and c["value"] >= 4),
        "candidates": top_pending,
    }
