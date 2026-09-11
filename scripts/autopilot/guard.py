"""Commit path guards: allow_paths/deny_paths matching.

Matching is conservative: patterns are matched case-sensitively and
case-insensitively, so a deny rule cannot be escaped by case games on any
platform. A pattern ending in "/" (or a bare directory name without glob
characters) matches everything under that directory, as documented in
references/config.md."""

import fnmatch
import os

_GLOB_CHARS = "*?["


def _fnmatch_any(value, pattern):
    return fnmatch.fnmatchcase(value, pattern) or fnmatch.fnmatchcase(
        value.lower(), pattern.lower()
    )


def path_allowed(path, allow_paths, deny_paths):
    """A staged path is allowed unless deny_paths matches it, and unless
    allow_paths is non-empty and does not match it. Patterns are fnmatch globs
    matched against the full path and the basename; a pattern ending in '/' (or a
    bare directory name without glob characters) also matches everything under
    that directory."""
    def matches(p, pattern):
        if not pattern:
            return False
        p_norm = p.replace("\\", "/")
        if pattern.endswith("/"):
            if p_norm.startswith(pattern) or p_norm.lower().startswith(pattern.lower()):
                return True
        elif not any(ch in pattern for ch in _GLOB_CHARS):
            low = p_norm.lower()
            if p_norm == pattern or low.startswith(pattern.lower() + "/"):
                return True
        if _fnmatch_any(p_norm, pattern):
            return True
        base = os.path.basename(p_norm)
        return bool(base and _fnmatch_any(base, pattern))

    if any(matches(path, pattern) for pattern in (deny_paths or [])):
        return False
    if allow_paths:
        if not any(matches(path, pattern) for pattern in allow_paths):
            return False
    return True
