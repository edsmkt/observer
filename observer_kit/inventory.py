"""Process inventory: dashboards, watchers, terminate helpers."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from observer_kit._util import pid_alive as _pid_alive

DASHBOARD_META_NAME = ".observer-dashboard.json"
# Ports scanned by ``ps`` / ``stop`` when discovering live dashboards without a meta file.
_DASHBOARD_PORT_SCAN = range(8484, 8521)

def _read_json_file(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}

def _dashboard_meta_path(state_dir: Path) -> Path:
    return state_dir.expanduser().resolve() / DASHBOARD_META_NAME

def _load_dashboard_meta(state_dir: Path) -> dict | None:
    path = _dashboard_meta_path(state_dir)
    if not path.is_file():
        return None
    meta = _read_json_file(path)
    return meta or None

def _probe_dashboard_port(port: int) -> dict | None:
    """Return live dashboard info from /api/meta when something answers on port."""
    try:
        with urlopen(f"http://127.0.0.1:{port}/api/meta", timeout=0.25) as response:
            if response.status != 200:
                return None
            payload = json.loads(response.read().decode("utf-8") or "{}")
    except (OSError, URLError, json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    raw = payload.get("state_dir") or payload.get("runguard") or ""
    if not raw:
        return None
    try:
        state = str(Path(raw).expanduser().resolve())
    except OSError:
        state = str(raw)
    pid = payload.get("pid")
    parent = payload.get("parent_pid")
    return {
        "kind": "dashboard",
        "source": "live",
        "port": int(payload.get("port") or port),
        "pid": pid,
        "parent_pid": parent,
        "state_dir": state,
        "idle_timeout": payload.get("idle_timeout"),
        "active": True,
        "orphan": bool(parent) and not _pid_alive(parent),
        "pid_alive": _pid_alive(pid) if pid is not None else True,
    }

def dashboard_records(state_dirs: list[Path] | None, *, scan_ports: bool) -> list[dict]:
    """Inventory dashboards from meta files and/or live port probes."""
    found: dict[tuple, dict] = {}

    def _key(rec: dict) -> tuple:
        return (rec.get("port"), rec.get("state_dir"), rec.get("pid"))

    def _add(rec: dict) -> None:
        if not rec:
            return
        found[_key(rec)] = rec

    for state in state_dirs or []:
        meta = _load_dashboard_meta(state)
        if not meta:
            continue
        pid = meta.get("pid")
        parent = meta.get("parent_pid")
        alive = _pid_alive(pid) if pid is not None else False
        port = meta.get("port")
        live = _probe_dashboard_port(int(port)) if port is not None and alive else None
        rec = {
            "kind": "dashboard",
            "source": "meta",
            "port": port,
            "pid": pid,
            "parent_pid": parent,
            "state_dir": str(state.expanduser().resolve()),
            "idle_timeout": meta.get("idle_timeout"),
            "started": meta.get("started"),
            "active": bool(meta.get("active", True)) and alive,
            "pid_alive": alive,
            "orphan": bool(parent) and not _pid_alive(parent) and alive,
        }
        if live:
            rec["source"] = "meta+live"
            rec["orphan"] = live.get("orphan", rec["orphan"])
            rec["pid"] = live.get("pid") or rec["pid"]
        _add(rec)

    if scan_ports:
        for port in _DASHBOARD_PORT_SCAN:
            live = _probe_dashboard_port(port)
            if live:
                _add(live)

    return sorted(
        found.values(),
        key=lambda r: (int(r.get("port") or 0), str(r.get("state_dir") or "")),
    )

def watcher_records(state_dir: Path) -> list[dict]:
    out = []
    for watcher in _active_watchers(state_dir):
        parent = watcher.get("parent_pid")
        pid = watcher.get("pid")
        dead_parent = (
            parent is not None and not _pid_alive(parent) and _pid_alive(pid)
        )
        out.append({
            "kind": "watcher",
            "pid": pid,
            "parent_pid": parent,
            "mode": watcher.get("mode"),
            "run": watcher.get("run"),
            "key": watcher.get("key"),
            "started": watcher.get("started"),
            "state_dir": str(state_dir.expanduser().resolve()),
            "pid_alive": _pid_alive(pid),
            # Orphan = still running after the owning parent died.
            "orphan": dead_parent,
            "independent": parent is None,
        })
    return out

def _terminate_pid(pid: object, *, wait: float = 2.0) -> str:
    """SIGTERM then SIGKILL a process. Returns action label."""
    try:
        pid_i = int(pid)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "skip"
    if pid_i <= 0 or not _pid_alive(pid_i):
        return "dead"
    try:
        os.kill(pid_i, 15)  # SIGTERM
    except OSError:
        return "error"
    deadline = time.time() + wait
    while time.time() < deadline:
        if not _pid_alive(pid_i):
            return "terminated"
        time.sleep(0.05)
    try:
        os.kill(pid_i, 9)  # SIGKILL
    except OSError:
        return "error"
    return "killed"

def _active_watchers(state_dir: Path) -> list[dict]:
    watchers = []
    for path in state_dir.glob(".observer-watcher-*.lock"):
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if meta.get("active") and _pid_alive(meta.get("pid")):
            watchers.append(meta)
    return sorted(watchers, key=lambda item: str(item.get("started", "")))

# Back-compat aliases used by older call sites
_dashboard_records = dashboard_records
_watcher_records = watcher_records
