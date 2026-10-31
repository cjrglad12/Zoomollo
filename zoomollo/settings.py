"""Load API keys from zoomollo-settings.txt (KEY=value lines) into the environment.

Looked for in the current folder, then the project folder. Values already set
in the environment win. Blank values and # comments are ignored.
"""

from __future__ import annotations

import os
from pathlib import Path

FILENAME = "zoomollo-settings.txt"


def settings_path() -> Path | None:
    for folder in (Path.cwd(), Path(__file__).resolve().parent.parent):
        p = folder / FILENAME
        if p.is_file():
            return p
    return None


def load_settings() -> Path | None:
    path = settings_path()
    if not path:
        return None
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and value and not os.environ.get(key):
            os.environ[key] = value
    return path
