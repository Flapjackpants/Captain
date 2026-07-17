"""Captain launcher for DaVinci Resolve.

Installed into Resolve's Fusion/Scripts/Utility folder so it appears under
Workspace > Scripts > Captain. It spawns the external Captain app (which has
its own Python virtualenv) as a detached process.

The install location is read from install.json in Captain's data directory,
written by setupfiles/install-mac.sh.
"""

import json
import os
import subprocess
import sys


def _data_dir():
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/Captain")
    if sys.platform == "win32":
        return os.path.join(os.environ.get("APPDATA", ""), "Captain")
    return os.path.expanduser("~/.local/share/Captain")


def main():
    install_file = os.path.join(_data_dir(), "install.json")
    try:
        with open(install_file) as f:
            install = json.load(f)
        python = install["python"]
        app_dir = install["app_dir"]
    except (OSError, KeyError, ValueError):
        print(
            "Captain is not installed. Run setupfiles/install-mac.sh from the "
            "Captain repository first."
        )
        return

    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.join(app_dir, "src")
    kwargs = {"env": env, "cwd": app_dir}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x00000008  # DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([python, "-m", "captain.main"], **kwargs)
    print("Captain launched.")


main()
