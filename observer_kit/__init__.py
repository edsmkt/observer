"""Observer Kit — package-owned runtime for supervised agent data workflows.

Import the observed-run API from this package (not from a skill directory):

    from observer_kit.runguard import start_observed_run, ledger

CLI entry: ``observer-kit`` or ``python -m observer_kit``.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

__all__ = [
    "__version__",
    "runguard",
    "version_info",
    "detect_install_skew",
    "upgrade_command",
]
__version__ = "0.3.0"

# Re-export the runguard module for ``from observer_kit import runguard``.
from observer_kit import runguard as runguard  # noqa: E402


def _git_sha(max_len: int = 12) -> str:
    """Best-effort short git SHA when running from a source checkout."""
    env_sha = os.environ.get("OBSERVER_KIT_GIT_SHA") or os.environ.get("GITHUB_SHA")
    if env_sha:
        return str(env_sha)[:max_len]
    try:
        root = Path(__file__).resolve().parent.parent
        if not (root / ".git").exists():
            return ""
        out = subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "--short=12", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        ).strip()
        return out[:max_len] if out else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def version_info() -> dict:
    """Package version metadata for --version and AXI/doctor surfaces."""
    path = Path(__file__).resolve()
    return {
        "version": __version__,
        "git_sha": _git_sha() or "unknown",
        "package_path": str(path.parent),
        "python": sys.executable,
    }


def upgrade_command() -> str:
    """Canonical reinstall command when PATH and package disagree."""
    root = Path(__file__).resolve().parent.parent
    if (root / "pyproject.toml").is_file() or (root / "setup.cfg").is_file():
        return f"python3 -m pip install -e {root}"
    return "python3 -m pip install -U git+https://github.com/edsmkt/observer-kit.git"


def _parse_version_line(text: str) -> dict:
    """Parse `observer-kit X.Y.Z (sha=... path=...)` style output."""
    info: dict = {}
    m = re.search(r"observer-kit\s+(\S+)", text)
    if m:
        info["version"] = m.group(1)
    m = re.search(r"sha=([^\s)]+)", text)
    if m:
        info["git_sha"] = m.group(1)
    m = re.search(r"path=([^\s)]+)", text)
    if m:
        info["package_path"] = m.group(1)
    return info


def _path_binary_probe() -> dict | None:
    """Probe the ``observer-kit`` on PATH (may be a different install)."""
    binary = shutil.which("observer-kit")
    if not binary:
        return None
    result: dict = {
        "binary": str(Path(binary).resolve()),
        "version": None,
        "package_path": None,
        "has_axi": None,
        "error": None,
    }
    try:
        proc = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"},
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode == 0 and "observer-kit" in out.lower():
            result.update(_parse_version_line(out))
            result["has_axi"] = True
            return result
        # Older binary: --version may fail; probe help for axi.
        help_proc = subprocess.run(
            [binary, "--help"],
            capture_output=True,
            text=True,
            timeout=5,
            env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"},
        )
        help_out = (help_proc.stdout or "") + (help_proc.stderr or "")
        result["has_axi"] = bool(re.search(r"\baxi\b", help_out))
        return result
    except (OSError, subprocess.SubprocessError) as exc:
        result["error"] = str(exc)
        return result


def detect_install_skew() -> dict:
    """Detect when PATH ``observer-kit`` does not match this loaded package.

    Agents often run whatever is on PATH; if that binary is stale, ``axi`` and
    other commands appear to not exist. Returns a TOON-friendly dict.
    """
    local = version_info()
    path_info = _path_binary_probe()
    if path_info is None:
        return {
            "install_skew": False,
            "path_binary": "none",
            "package_version": local["version"],
            "package_path": local["package_path"],
            "git_sha": local["git_sha"],
            "upgrade": upgrade_command(),
            "reason": "no observer-kit on PATH; use python3 -m observer_kit",
        }
    skew = False
    reasons: list[str] = []
    path_ver = path_info.get("version")
    path_pkg = path_info.get("package_path")
    local_pkg = local["package_path"]
    if path_info.get("has_axi") is False:
        skew = True
        reasons.append("PATH binary lacks axi (stale CLI surface)")
    if path_ver and path_ver != local["version"]:
        skew = True
        reasons.append(f"PATH version {path_ver} != package {local['version']}")
    if path_pkg and local_pkg:
        try:
            if Path(path_pkg).resolve() != Path(local_pkg).resolve():
                skew = True
                reasons.append("PATH package path differs from this process")
        except OSError:
            pass
    return {
        "install_skew": skew,
        "path_binary": path_info.get("binary"),
        "path_version": path_ver or "unknown",
        "path_package": path_pkg or "unknown",
        "path_has_axi": path_info.get("has_axi"),
        "package_version": local["version"],
        "package_path": local_pkg,
        "git_sha": local["git_sha"],
        "upgrade": upgrade_command(),
        "reason": "; ".join(reasons) if reasons else "ok",
    }
