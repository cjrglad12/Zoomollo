"""Load API keys from zoomollo-settings.txt (KEY=value lines) into the environment.

Looked for in the current folder, then the project folder. Values already set
in the environment win. Blank values and # comments are ignored.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

FILENAME = "zoomollo-settings.txt"


def settings_path() -> Path | None:
    for folder in (Path.cwd(), Path(__file__).resolve().parent.parent):
        p = folder / FILENAME
        if p.is_file():
            return p
    return None


GMAIL = {"SMTP_HOST": "smtp.gmail.com", "SMTP_PORT": "587", "IMAP_HOST": "imap.gmail.com", "IMAP_PORT": "993"}


def apply_presets() -> None:
    """GMAIL_ADDRESS + GMAIL_APP_PASSWORD fill in the SMTP / IMAP settings for Gmail."""
    addr = os.environ.get("GMAIL_ADDRESS", "").strip()
    pw = re.sub(r"\s+", "", os.environ.get("GMAIL_APP_PASSWORD", ""))  # Google shows it as "abcd efgh ijkl mnop"
    if not (addr and pw):
        return
    for k, v in {**GMAIL, "SMTP_USER": addr, "SMTP_PASSWORD": pw}.items():
        if not os.environ.get(k):
            os.environ[k] = v


def save_values(values: dict[str, str]) -> Path:
    """Write KEY=value lines into zoomollo-settings.txt on this computer (created if
    missing), replacing existing keys, and apply them right away."""
    path = settings_path() or (Path(__file__).resolve().parent.parent / FILENAME)
    if not path.exists():
        example = path.with_name("zoomollo-settings.example.txt")
        path.write_text(example.read_text() if example.exists() else "", encoding="utf-8")
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    todo = dict(values)
    out = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if key in todo:
            out.append(f"{key}={todo.pop(key)}")
        else:
            out.append(line)
    out += [f"{k}={v}" for k, v in todo.items()]
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)  # only you can read it
    except OSError:
        pass
    for k, v in values.items():
        if v:
            os.environ[k] = v
        else:
            os.environ.pop(k, None)
    return path


def load_settings() -> Path | None:
    path = settings_path()
    if not path:
        apply_presets()
        return None
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and value and not os.environ.get(key):
            os.environ[key] = value
    apply_presets()
    return path
