"""
storage.py - the newsletter URL database (a JSON file).

Every entry has one of five statuses. A URL is never silently promoted:

  discovered   the URL is known, but its form has not been inspected yet
  configured   email/submit selectors are saved, but no test has succeeded
  verified     a controlled test succeeded AND the email was confirmed
               (found over IMAP, or confirmed by you manually)
  failed       the last test or inspection failed (see last_test.detail)
  unavailable  the page is gone, blocked, or not a usable newsletter form
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from errors import AppError
from url_utils import normalize_url

STATUSES = ("discovered", "configured", "verified", "failed", "unavailable")


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def empty_fields() -> dict:
    return {
        "email": [], "submit": [], "checkboxes": [], "radios": [],
        "pre_clicks": [], "wait": 0,
    }


def has_form(entry: dict) -> bool:
    fields = entry.get("input_fields") or {}
    return bool(fields.get("email") and fields.get("submit"))


def make_entry(url: str, *, source: str = "manual", name: str = "",
               input_fields: dict | None = None,
               sender_hint: str = "", subject_hint: str = "") -> dict:
    fields = input_fields or empty_fields()
    entry = {
        "url": url,
        "name": name,
        "status": "discovered",
        "source": source,
        "added": now_iso(),
        "input_fields": fields,
        "verification": {"sender_hint": sender_hint, "subject_hint": subject_hint},
        "last_check": None,
        "last_test": None,
    }
    if has_form(entry):
        entry["status"] = "configured"
    return entry


def migrate_entry(raw: dict) -> dict | None:
    """Upgrade an entry from the old schema (``"verified": true/false``)."""
    url = normalize_url(raw.get("url", "")) if isinstance(raw, dict) else None
    if not url:
        return None
    fields = raw.get("input_fields") or {}
    merged = empty_fields()
    for key, value in fields.items():
        merged[key] = value
    entry = make_entry(url, source=raw.get("source", "imported"),
                       name=raw.get("name", ""), input_fields=merged)
    verification = raw.get("verification") or {}
    entry["verification"] = {
        "sender_hint": verification.get("sender_hint", ""),
        "subject_hint": verification.get("subject_hint", ""),
    }
    status = raw.get("status")
    if status in STATUSES:
        entry["status"] = status
    # An old ``"verified": true`` only meant "a submit button was clicked once".
    # That is weaker than today's definition, so it is not carried over.
    for key in ("added", "last_check", "last_test"):
        if raw.get(key):
            entry[key] = raw[key]
    if entry["status"] == "verified" and not entry.get("last_test"):
        entry["status"] = "configured"
    return entry


def load_entries(path: Path) -> list[dict]:
    """Load, migrate and de-duplicate the database. Missing file -> []."""
    path = Path(path)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AppError(
            f"The URL database {path.name} is not valid JSON ({exc.msg}, line {exc.lineno}).",
            "Fix or delete the file; a copy is never overwritten without your action.",
        ) from None
    if not isinstance(raw, list):
        raise AppError(f"{path.name} must contain a JSON list of entries.")
    entries, seen = [], set()
    for item in raw:
        entry = migrate_entry(item)
        if entry and entry["url"] not in seen:
            seen.add(entry["url"])
            entries.append(entry)
    return entries


def save_entries(path: Path, entries: list[dict]) -> None:
    """Write atomically so a crash can never leave a half-written file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(entries, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(tmp, path)
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise AppError(f"Could not save {path}: {exc.strerror or exc}.",
                       "Check that the folder is writable.") from None


def find_entry(entries: list[dict], url: str) -> dict | None:
    target = normalize_url(url)
    return next((e for e in entries if e["url"] == target), None)


def add_entry(entries: list[dict], entry: dict) -> bool:
    """Append *entry* unless its URL already exists. Returns True if added."""
    if find_entry(entries, entry["url"]):
        return False
    entries.append(entry)
    return True


def set_status(entry: dict, status: str) -> None:
    if status not in STATUSES:
        raise ValueError(f"unknown status: {status}")
    entry["status"] = status


def record_test(entry: dict, outcome: str, detail: str) -> None:
    entry["last_test"] = {"at": now_iso(), "outcome": outcome, "detail": detail}


def record_check(entry: dict, state: str, detail: str) -> None:
    entry["last_check"] = {"at": now_iso(), "state": state, "detail": detail}


def cooldown_remaining(entry: dict, minutes: int, now: datetime | None = None) -> timedelta:
    """Time left before this URL may be tested again (zero when allowed).

    One test per URL per cooldown window keeps this tool from ever hammering
    a third-party site, whatever the user does.
    """
    last = (entry.get("last_test") or {}).get("at")
    if not last:
        return timedelta(0)
    try:
        when = datetime.fromisoformat(last)
    except ValueError:
        return timedelta(0)
    now = now or datetime.now(timezone.utc)
    remaining = when + timedelta(minutes=minutes) - now
    return max(remaining, timedelta(0))


def counts(entries: list[dict]) -> dict[str, int]:
    result = {status: 0 for status in STATUSES}
    for entry in entries:
        result[entry.get("status", "discovered")] = result.get(entry.get("status"), 0) + 1
    return result
