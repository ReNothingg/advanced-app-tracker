from __future__ import annotations

import os
import sys
from typing import List

from app_tracker.paths import project_root


def python_executable() -> str:
    exe = sys.executable
    if sys.platform == "win32" and exe:
        candidate = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(candidate):
            return candidate
    return exe


def launcher_path() -> str:
    return str(project_root() / "run.py")


def launch_command() -> List[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [python_executable(), launcher_path()]


def launch_command_string() -> str:
    return " ".join(f'"{part}"' for part in launch_command())
