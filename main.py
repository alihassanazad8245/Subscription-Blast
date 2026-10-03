"""
main.py - entry point and main menu of the Subscription Form Tester.

Run with:  python main.py
"""
from __future__ import annotations

import logging
import logging.handlers
import sys

import modes
import storage
import ui
from config import (APP_NAME, APP_VERSION, AUTHOR, INSTAGRAM, ORIGINAL_AUTHOR,
                    ORIGINAL_REPO, PROJECT_DIR, get_settings)
from errors import AppError

LOG_FILE = PROJECT_DIR / "logs" / "subscription_tester.log"


def setup_logging() -> None:
    LOG_FILE.parent.mkdir(exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=500_000, backupCount=2,
                                                   encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger("sft")
    root.setLevel(logging.INFO)
    root.addHandler(handler)


def banner(settings) -> None:
    try:
        c = storage.counts(storage.load_entries(settings.url_json))
        summary = (f"{sum(c.values())} URLs ({c['verified']} verified, {c['configured']} configured, "
                   f"{c['discovered']} discovered)")
        if c["failed"] or c["unavailable"]:
            summary += f", {c['failed'] + c['unavailable']} failed/unavailable"
    except AppError as exc:
        summary = exc.message
    ui.box(f"{APP_NAME}  v{APP_VERSION}", [
        "Authorized testing of newsletter signup forms with Selenium + Firefox.",
        "",
        f"Test email : {settings.test_email or 'not set - edit .env'}",
        f"URL list   : {summary}",
    ], color="cyan")


MENU = [
    ("1", "Single newsletter test      (guided, one submission)"),
    ("2", "Add a newsletter URL"),
    ("3", "Inspect a form              (nothing is submitted)"),
    ("4", "Manage the URL list"),
    ("5", "Find newsletter pages       (optional Search API)"),
    ("6", "Setup check"),
    ("0", "Exit"),
]


def dispatch(choice: str, settings) -> bool:
    """Run one menu choice. Returns False when the user wants to quit."""
    actions = {
        "1": modes.single_test, "2": modes.add_url, "3": modes.inspect_url,
        "4": modes.manage_list, "5": modes.search_flow, "6": modes.diagnostics,
    }
    if choice in ("0", "q", "exit"):
        return False
    action = actions.get(choice)
    if action is None:
        ui.warn("Please choose one of the numbers in the menu.")
        return True
    try:
        action(settings)
    except AppError as exc:
        ui.error_box(exc.message, exc.hint)
    except KeyboardInterrupt:
        print()
        ui.warn("Cancelled.")
    except Exception:  # a real bug: keep the details for debugging
        logging.getLogger("sft").exception("unexpected error in menu option %s", choice)
        ui.error_box("Unexpected error - the details were saved to logs/subscription_tester.log.",
                     "Please report it with that file if it keeps happening.")
    return True


def main() -> int:
    ui.setup_terminal()
    setup_logging()
    try:
        settings = get_settings()
    except AppError as exc:
        ui.error_box(exc.message, exc.hint)
        return 1
    try:
        banner(settings)
        while True:
            if not dispatch(ui.menu("Main menu", MENU), settings):
                break
    except (KeyboardInterrupt, EOFError):
        print()
    ui.credits(AUTHOR, INSTAGRAM, ORIGINAL_AUTHOR, ORIGINAL_REPO)
    return 0


if __name__ == "__main__":
    sys.exit(main())
