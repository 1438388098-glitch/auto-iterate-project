"""Read-only observation dashboard: lifecycle (spawn/probe/stop), the local
HTTP server, snapshot caching. Hard rule: dashboard problems must never block
the iteration loop — callers wrap ensure_dashboard in try/except. Known limit:
a dead server's pid may be reused by the OS, so info_alive can false-positive
on an unrelated process; the info file is short-lived and probe-only."""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
    missing, unreadable, corrupt, or not a JSON object (a hand-edited `[]`
    must degrade, not crash info_alive/stop_server). Never raises."""
    path = _info_path(repo)
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return info if isinstance(info, dict) else None


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


# ---- HTTP server (Task 8) ----

# 单槽快照缓存：serve() 每进程只服务一个 repo，槽位足够。key 是受监视文件的
# mtime_ns 元组（缺失文件记 None，no-run repo 也能稳定命中）；TTL 兜底约束
# key 看不见的变化的陈旧上限。
_snapshot_cache = {"key": None, "snapshot": None, "at": 0.0}


def invalidate_snapshot_cache():
    """Force the next get_snapshot() to rebuild. The mtime key already
    self-invalidates when a watched file is rewritten; this is the explicit
    seam for callers/tests that know state changed another way."""
    _snapshot_cache["key"] = None


def _cache_key(repo):
    from . import dashboard_data as dd
    # Exactly the files build_snapshot reads (analysis.json is legacy state
    # the pipeline never consumes; the narrative .md files it does).
    names = (io.STATE_FILENAME, io.BACKLOG_FILENAME, io.CONFIG_FILENAME,
             "last-summary.md", "retrospective.md")
    mtimes = []
    for name in names:
        p = dd._autopilot_dir(repo) / name
        try:
            mtimes.append(p.stat().st_mtime_ns)
        except OSError:
            mtimes.append(None)
    return tuple(mtimes)


def get_snapshot(repo):
    """build_snapshot with a one-slot cache: repeated browser polling between
    state writes returns the identical dict (stable generated_at) instead of
    re-running the growth pipeline per request; a changed mtime or the TTL
    (SNAPSHOT_TTL_SECONDS) triggers one rebuild."""
    key = _cache_key(repo)
    now = time.time()
    if _snapshot_cache["key"] == key and _snapshot_cache["snapshot"] is not None \
            and now - _snapshot_cache["at"] < SNAPSHOT_TTL_SECONDS:
        return _snapshot_cache["snapshot"]
    from . import dashboard_data as dd
    snapshot = dd.build_snapshot(repo)
    _snapshot_cache.update(key=key, snapshot=snapshot, at=now)
    return snapshot


def _make_handler(repo):
    page_path = Path(__file__).resolve().parent / "dashboard.html"

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
                # 每请求现读磁盘：改页面无需重启服务器，也无需预缓存。
                self._send(200, page_path.read_bytes(), "text/html; charset=utf-8")
            elif self.path == "/api/snapshot":
                try:
                    body = json.dumps(get_snapshot(repo), ensure_ascii=False).encode("utf-8")
                    self._send(200, body, "application/json; charset=utf-8")
                # (Exception, SystemExit): io.load_json's corrupt-file guard
                # raises SystemExit (BaseException) — it must degrade to this
                # 500 JSON, not kill the request thread with a traceback.
                except (Exception, SystemExit) as err:
                    body = json.dumps({"error": "internal", "detail": str(err)}).encode("utf-8")
                    self._send(500, body, "application/json; charset=utf-8")
            else:
                self._send(404, b'{"error": "not-found"}', "application/json")

        def log_message(self, *args):                         # 静默访问日志
            pass

    Handler.last_request_at = time.time()
    return Handler


def start_in_thread(repo, host="127.0.0.1"):
    """Test/dev entry: serve on a random port in a daemon thread. Returns
    (server, port) — caller must server.shutdown()."""
    handler = _make_handler(repo)
    server = ThreadingHTTPServer((host, 0), handler)
    port = server.server_address[1]
    write_info(repo, {"pid": os.getpid(), "port": port,
                      "started_at": io.now_iso(), "opened": False})
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port


def serve(repo, port=0, auto_open=True, host="127.0.0.1"):
    """Blocking entry for `autopilot dashboard --serve`: write dashboard.json,
    open the browser once, serve requests from a daemon thread, and self-exit
    after IDLE_TIMEOUT_SECONDS without a request (main-thread watchdog, every
    60s; last_request_at starts at server start, so a never-visited server
    also retires). Exit path shuts the server down, closes the socket and
    removes the info file."""
    handler = _make_handler(repo)
    server = ThreadingHTTPServer((host, port), handler)
    port = server.server_address[1]
    write_info(repo, {"pid": os.getpid(), "port": port,
                      "started_at": io.now_iso(), "opened": False})
    if auto_open:
        import webbrowser
        try:
            webbrowser.open("http://{}:{}/".format(host, port))
        except Exception as err:
            # Headless/unattended hosts: stay silent on stderr, leave a trail.
            try:
                io.append_log(repo, "dashboard", "warn", reason="auto_open failed", detail=str(err))
            except Exception:
                pass
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    while True:
        time.sleep(60)
        if time.time() - server.RequestHandlerClass.last_request_at > IDLE_TIMEOUT_SECONDS:
            break
    server.shutdown()
    server.server_close()
    _info_path(repo).unlink(missing_ok=True)
