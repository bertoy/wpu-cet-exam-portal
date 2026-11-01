"""Keeping the last few unhandled errors where an administrator can read them.

The error page tells the instructor that an administrator can see the details
on the System check page. That was not true: the traceback went to the Python
logger and, on a shared host, into a file the instructor has no obvious way
to find. Being sent to a page that does not have the answer is worse than
being told nothing, so this records them where the page says they are.

Written as newline-delimited JSON in the data directory rather than to the
database: an error often *is* a database problem, and a recorder that needs
the database to work would fall silent exactly when it is needed. The file is
trimmed to the most recent entries so it cannot grow without bound on a host
with little disk.
"""
from __future__ import annotations

import json
import os
import traceback
from datetime import datetime, timezone

KEEP = 50
FILENAME = "faults.jsonl"


def _path(app):
    return app.config["DATA_DIR"] / FILENAME


def record(app, error, path: str = "", user: str = "") -> None:
    """Write one fault. Never raises: a broken recorder must not add a second
    failure on top of the first."""
    try:
        entry = {
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "path": path or "",
            "user": user or "",
            "kind": type(error).__name__,
            "message": str(error)[:500],
            "traceback": "".join(
                traceback.format_exception(type(error), error, error.__traceback__)
            )[-6000:],
        }

        target = _path(app)
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")

        _trim(target)
    except Exception:                                   # noqa: BLE001
        pass


def _trim(target) -> None:
    try:
        if target.stat().st_size < 400_000:
            return
        with open(target, encoding="utf-8") as handle:
            lines = handle.readlines()[-KEEP:]
        with open(target, "w", encoding="utf-8") as handle:
            handle.writelines(lines)
    except OSError:
        pass


def recent(app, limit: int = 20) -> list[dict]:
    """The most recent faults, newest first."""
    target = _path(app)
    if not target.exists():
        return []

    out: list[dict] = []
    try:
        with open(target, encoding="utf-8") as handle:
            for line in handle.readlines()[-(limit * 2):]:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []

    out.reverse()
    return out[:limit]


def clear(app) -> None:
    try:
        os.remove(_path(app))
    except OSError:
        pass
