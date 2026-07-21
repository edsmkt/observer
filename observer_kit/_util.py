"""Tiny shared helpers used by runtime modules.

Kept dependency-free so dashboard/watch scripts and runguard can share one
implementation without redefining pid checks and timestamps.
"""
from __future__ import annotations

import os
import time


def pid_alive(pid: object) -> bool:
    """True when ``pid`` names a live process (signal 0 probe)."""
    try:
        p = int(pid)  # type: ignore[arg-type]
        if p <= 0:
            return False
        os.kill(p, 0)
        return True
    except (TypeError, ValueError, OSError):
        return False


def timestamp() -> str:
    """UTC RFC 3339 with nanoseconds for stable ledger/chat ordering."""
    ns = time.time_ns()
    secs, nsec = divmod(ns, 1_000_000_000)
    base = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(secs))
    return f"{base}.{nsec:09d}Z"


def lane_from_run_id(run_id: object) -> str:
    """Strip runguard: prefix / .jsonl / path noise to a lane folder name."""
    import os

    raw = str(run_id or "").strip()
    if not raw or raw == "all":
        return ""
    _kind, sep, name = raw.partition(":")
    if sep:
        raw = name
    raw = os.path.basename(raw)
    if raw.endswith(".jsonl"):
        raw = raw[:-6]
    if not raw or raw in {".", ".."}:
        return ""
    return raw
