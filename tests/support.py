"""Shared helpers for the AskSoul test suite."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import signal
import stat
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parent.parent
ASKSOUL = ROOT / "bin" / "asksoul"
TALK = ROOT / "bot" / "asksoul-talk"


def load_asksoul(name: str = "asksoul_under_test") -> ModuleType:
    """Load bin/asksoul (no .py suffix) with importlib/SourceFileLoader."""
    loader = importlib.machinery.SourceFileLoader(name, str(ASKSOUL))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def clean_env() -> dict[str, str]:
    """The current environment without ASKSOUL_* leakage from the caller."""
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("ASKSOUL_")
    }


def make_env(
    home: Path,
    webhook_url: str,
    *,
    bot_name: str = "Soul",
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """A throwaway HOME plus the env the CLI needs for a loopback session."""
    env = clean_env()
    env.update(
        {
            "HOME": str(home),
            "TERM": "xterm-256color",
            "XDG_STATE_HOME": str(home / "state"),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "ASKSOUL_CONFIG": str(home / ".config" / "asksoul" / "config.toml"),
            "ASKSOUL_WEBHOOK_URL": webhook_url,
            "ASKSOUL_WEBHOOK_SECRET": "test-secret",
            "ASKSOUL_BOT_NAME": bot_name,
            "ASKSOUL_BIND": "127.0.0.1",
            "ASKSOUL_ACCEPT": "127.0.0.1",
        }
    )
    if extra:
        env.update(extra)
    return env


def write_private(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def kill_session_daemons(state_home: Path) -> None:
    """Best-effort cleanup: stop any background session process a test left."""
    root = state_home / "asksoul" / "sessions"
    if not root.is_dir():
        return
    for meta_path in root.glob("*/meta.json"):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            pid = int(meta.get("pid"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
        if pid <= 1:
            continue
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
