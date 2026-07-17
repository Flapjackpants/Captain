"""Resolve Scripts host: hold `resolve`, serve the IPC bridge, spawn the UI.

This module is imported by ``scripts/Captain.py`` which runs *inside*
Resolve's scripting Python. It must only depend on the stdlib + captain.api /
captain.bridge (no PySide6 / faster-whisper).
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

from .api import ResolveError, ResolveHandler
from .bridge import BridgeServer

log = logging.getLogger("Captain.launcher")


def data_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Captain"
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", "")) / "Captain"
    return Path.home() / ".local" / "share" / "Captain"


def load_install() -> dict:
    path = data_dir() / "install.json"
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise ResolveError(
            "Captain is not installed. Run setupfiles/install-mac.sh from the "
            f"Captain repository first. ({e})"
        ) from e


def resolve_injected_object(namespace: dict | None = None):
    """Return the Resolve object injected into Scripts, or None."""
    ns = namespace if namespace is not None else globals()
    # Scripts menu injects `resolve` into the script's global namespace.
    obj = ns.get("resolve")
    if obj is not None:
        return obj
    # Some Resolve builds also expose it as a builtin when exec'ing the file.
    try:
        return resolve  # type: ignore[name-defined]  # noqa: F821
    except NameError:
        return None


def start_bridge(handler: ResolveHandler) -> BridgeServer:
    token = secrets.token_hex(16)
    server = BridgeServer(handler.bridge_dispatch, token=token)
    server.start()
    return server


def spawn_ui(install: dict, bridge: BridgeServer) -> subprocess.Popen:
    python = install["python"]
    app_dir = install["app_dir"]
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.join(app_dir, "src")
    env["CAPTAIN_BRIDGE_URL"] = bridge.url
    env["CAPTAIN_BRIDGE_TOKEN"] = bridge.token
    kwargs: dict = {"env": env, "cwd": app_dir}
    # Do NOT detach: Free compatibility requires the Scripts process (and its
    # live `resolve` + bridge) to stay alive for the life of the UI.
    return subprocess.Popen([python, "-m", "captain.main"], **kwargs)


def run(resolve_obj=None) -> None:
    """Host entry: connect to Resolve, serve bridge, wait for UI exit."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    install = load_install()
    handler = ResolveHandler()

    if resolve_obj is not None:
        handler.connect_from_object(resolve_obj)
    else:
        # Studio fallback if somehow launched without an injected object.
        try:
            handler.connect()
        except ResolveError:
            raise ResolveError(
                "Captain could not get a Resolve scripting handle. "
                "Launch via Workspace → Scripts → Captain. "
                "On Resolve Free this is required (external scriptapp is Studio-only)."
            )

    bridge = start_bridge(handler)
    print(f"Captain bridge on {bridge.url}")
    try:
        proc = spawn_ui(install, bridge)
        print("Captain UI launched. Keep this script running until you quit Captain.")
        while proc.poll() is None:
            time.sleep(0.4)
        log.info("UI exited with code %s", proc.returncode)
    finally:
        bridge.stop()
        print("Captain bridge stopped.")
