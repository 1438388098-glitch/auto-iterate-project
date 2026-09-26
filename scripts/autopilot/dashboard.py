"""Read-only observation dashboard: lifecycle (spawn/probe/stop), the local
HTTP server, snapshot caching. Hard rule: dashboard problems must never block
the iteration loop — callers wrap ensure_dashboard in try/except. Known limit:
a dead server's pid may be reused by the OS, so info_alive can false-positive
on an unrelated process; the info file is short-lived and probe-only."""

import json
import subprocess
import sys
import time
from pathlib import Path

from . import io


IDLE_TIMEOUT_SECONDS = 30 * 60
SNAPSHOT_TTL_SECONDS = 2.0


def _info_path(repo):
    return Path(repo) / io.AUTOPILOT_DIR / "dashboard.json"


def write_info(repo, info):
    """Persist the server's lifecycle info (pid/port/started_at/opened) as
    plain JSON. Deliberately not io.save_json: a torn or corrupt file must
    degrade to read_info() → None, never SystemExit like io.load_json."""
    path = _info_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(info), encoding="utf-8")


def read_info(repo):
    """Return the recorded dashboard info dict, or None when the file is
    missing, unreadable or corrupt. Never raises."""
    path = _info_path(repo)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def info_alive(repo):
    """True when the recorded pid still exists (io._pid_alive fails closed on
    probe trouble; see the pid-reuse note in the module docstring)."""
    info = read_info(repo)
    return bool(info) and io._pid_alive(info.get("pid"))


def clear_stale_info(repo):
    """Drop the info file when its server is gone, so ensure/spawn never
    mistake a dead server's record for a live one."""
    if _info_path(repo).exists() and not info_alive(repo):
        _info_path(repo).unlink(missing_ok=True)


def _terminate(pid):
    import os
    import signal
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


def stop_server(repo):
    """Kill the recorded server (taskkill /F on Windows, SIGTERM elsewhere)
    and always remove the info file. Returns False when nothing is recorded."""
    info = read_info(repo)
    if not info:
        return False
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(info["pid"]), "/F"],
                           capture_output=True, timeout=10)
        else:
            _terminate(info["pid"])
    finally:
        _info_path(repo).unlink(missing_ok=True)
    return True


def spawn_server(repo, config):
    """Start `autopilot dashboard --serve` as a detached process and wait for
    it to write its own dashboard.json (≤5s). Returns the new info or None.
    The info != base_info comparison guards against reading our own stale
    record from a previous dead server; a same-instant third-party write
    could still slip through, but callers re-probe the pid (accepted for v1)."""
    dash = config.get("dashboard") or {}
    entry = Path(__file__).resolve().parent.parent / "autopilot_state.py"
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    base_info = read_info(repo)
    subprocess.Popen(
        [sys.executable, str(entry), "dashboard", "--serve",
         "--repo", str(repo), "--port", str(dash.get("port", 0))],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs,
    )
    deadline = time.time() + 5.0
    while time.time() < deadline:
        info = read_info(repo)
        if info and info != base_info:   # 新进程写了自己的 info
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
