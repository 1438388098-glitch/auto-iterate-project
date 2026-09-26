"""Pure data pipeline for the read-only dashboard: git numstat parsing,
round file changes, module aggregation, event correlation, snapshot
assembly. No process spawning, no state writes — tests feed it fakes."""

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
    if gitio is not None:
        run_git = gitio.run_git
    else:
        def run_git(repo_, *args):
            result = io.run_git(repo_, *args)
            return result.stdout if result.returncode == 0 else None

    changes = {}
    prev = run_start_sha
    for entry in history:
        sha = entry.get("commit_sha")
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
