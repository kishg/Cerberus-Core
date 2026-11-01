"""Owner notes on findings: what the owner knows that the data can't show.

"Tightened the MPPT lugs", "breaker replaced", "van in storage, packs off".
A note belongs to a finding (its key) and is shown on that finding's card.
It also gives the finding context it could not have on its own
(cerberus.findings.apply_notes): a marker on the evidence chart, and whether
things changed after the note, so the owner sees whether a fix worked.

A note can mark its finding *known*: the owner knows and accepts it for now,
so it stops counting as needing attention, until it happens again after the
note (cerberus.findings.known_holds).

Written by the UI, so it lives in a small file beside config.yaml, not in the
database (the UI never writes the database). Like the other beside-config
files it is per-box state: git-ignored, and carried over by cerberus.migrate.
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
import threading
import time
from pathlib import Path

NOTES_FILE = ".cerberus-notes.json"
MAX_TEXT = 1000
MAX_NOTES = 500
KEY_MAX = 80

# The UI server is threaded: one add or delete at a time, so two phones
# saving at once can't drop each other's note.
_lock = threading.Lock()


class NoteError(ValueError):
    pass


def path_for(config_path: str | Path) -> Path:
    return Path(config_path).with_name(NOTES_FILE)


def read(config_path: str | Path) -> list[dict]:
    """Every note, oldest first. A missing or damaged file is no notes."""
    try:
        raw = json.loads(path_for(config_path).read_text())
    except (FileNotFoundError, ValueError):
        return []
    out = []
    for n in raw.get("notes", []) if isinstance(raw, dict) else []:
        try:
            out.append({"id": str(n["id"]), "key": str(n["key"]), "ts": int(n["ts"]),
                        "text": str(n["text"]), "title": str(n.get("title") or ""),
                        "known": n.get("known") is True})
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(out, key=lambda n: n["ts"])


def _write(config_path: str | Path, notes: list[dict]) -> None:
    p = path_for(config_path)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".notes-", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"notes": notes}, f, indent=1)
        os.replace(tmp, p)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def add(config_path: str | Path, key, text, title="", now: int | None = None,
        known=False) -> dict:
    """Add a note to finding ``key``. ``title`` is the finding's title at the
    time, kept so the note still reads sensibly after the finding is gone.
    ``known`` marks the finding known (see the module docstring)."""
    key = str(key or "").strip()
    text = str(text or "").strip()
    if not key or len(key) > KEY_MAX:
        raise NoteError("a note needs the finding it belongs to")
    if not text:
        raise NoteError("the note is empty")
    if len(text) > MAX_TEXT:
        raise NoteError(f"keep a note under {MAX_TEXT} characters")
    note = {"id": secrets.token_hex(6), "key": key,
            "ts": int(time.time()) if now is None else int(now),
            "text": text, "title": str(title or "")[:200], "known": known is True}
    with _lock:
        notes = read(config_path)
        if len(notes) >= MAX_NOTES:
            raise NoteError(f"there are already {MAX_NOTES} notes; delete some first")
        _write(config_path, notes + [note])
    return note


def delete(config_path: str | Path, note_id) -> bool:
    with _lock:
        notes = read(config_path)
        kept = [n for n in notes if n["id"] != str(note_id)]
        if len(kept) == len(notes):
            return False
        _write(config_path, kept)
    return True


def set_known(config_path: str | Path, note_id, known) -> bool:
    """Mark or unmark a note's finding known, keeping the note's text."""
    with _lock:
        notes = read(config_path)
        hit = next((n for n in notes if n["id"] == str(note_id)), None)
        if hit is None:
            return False
        hit["known"] = known is True
        _write(config_path, notes)
    return True


def by_key(notes: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for n in notes:
        out.setdefault(n["key"], []).append(n)
    return out
