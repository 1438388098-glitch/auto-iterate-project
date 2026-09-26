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
