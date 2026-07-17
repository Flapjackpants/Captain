"""Captain launcher for DaVinci Resolve (Workspace → Scripts → Captain).

Runs inside Resolve's scripting process so Free and Studio both get a live
`resolve` object. Starts a localhost IPC bridge, then opens the external
PySide6 UI which talks to Resolve only through that bridge.
"""

from __future__ import annotations

import json
import os
import sys


def _data_dir():
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/Captain")
    if sys.platform == "win32":
        return os.path.join(os.environ.get("APPDATA", ""), "Captain")
    return os.path.expanduser("~/.local/share/Captain")


def _bootstrap():
    install_file = os.path.join(_data_dir(), "install.json")
    try:
        with open(install_file) as f:
            install = json.load(f)
        app_dir = install["app_dir"]
    except (OSError, KeyError, ValueError):
        print(
            "Captain is not installed. Run setupfiles/install-mac.sh from the "
            "Captain repository first."
        )
        return None

    src = os.path.join(app_dir, "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    return install


def main():
    if _bootstrap() is None:
        return

    # `resolve` is injected by DaVinci Resolve when this file is run from
    # Workspace → Scripts. Capture it from this module's globals.
    resolve_obj = globals().get("resolve")

    from captain.api import ResolveError
    from captain.launcher import run

    try:
        run(resolve_obj)
    except ResolveError as e:
        print(f"Captain error: {e}")
    except Exception as e:
        print(f"Captain failed: {e}")
        raise


main()
