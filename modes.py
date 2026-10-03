"""
modes.py - the menu workflows.

  single_test      guided, one-submission test of ONE newsletter form
  add_url          add a URL (optionally inspecting its form)
  inspect_url      look at a form without typing or submitting anything
  manage_list      view / re-check / re-status / delete entries
  search_flow      optional Search API discovery (candidates only)
  diagnostics      check .env, Firefox, IMAP and the database

There is deliberately no "run against every URL" mode: one test, one URL,
one address you own, with a cooldown before the same URL can be tested again.
"""
from __future__ import annotations

import logging
import time

import browser
import imap_utils
import search_api
import storage
import ui
from config import Settings, is_valid_email
from errors import AppError
from selector_utils import describe_field, parse_selection
from url_utils import check_url_available, validate_url

log = logging.getLogger("sft.modes")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _load(settings: Settings) -> list[dict]:
    return storage.load_entries(settings.url_json)


def _save(settings: Settings, entries: list[dict]) -> None:
    storage.save_entries(settings.url_json, entries)


def _list_table(entries: list[dict]) -> None:
    rows = [[str(i), e["url"], ui.status_badge(e["status"]),
             (e.get("last_test") or {}).get("outcome", "-")]
            for i, e in enumerate(entries, 1)]
    ui.table(["#", "URL", "Status", "Last test"], rows, max_col=56)


def _choose_entry(entries: list[dict], prompt: str = "Pick a number") -> dict | None:
    if not entries:
        return None
    _list_table(entries)
    raw = ui.ask(f"{prompt} (or 'q' to cancel)")
    if not raw or raw.lower() == "q":
        return None
    if raw.isdigit() and 1 <= int(raw) <= len(entries):
        return entries[int(raw) - 1]
    ui.warn("That is not a number from the list.")
    return None


def _show_inspection(result: browser.Inspection) -> None:
    lines = [f"Page title : {result.title or '(none)'}",
             f"Final URL  : {result.final_url}"]
    if result.captcha:
        lines.append(f"Note       : a {result.captcha} is present - the test may be rejected "
                     "(this tool never bypasses it).")
    ui.box("Form inspection", lines, color="blue")
    if result.problem_kind:
        ui.warn(f"Page problem ({result.problem_kind}): {result.problem}")
        return
    if result.usable:
        ui.ok(f"Form detected (confidence: {result.confidence})")
        ui.info(f"Email field  : {describe_field(result.fields['email'][0])}")
        ui.info(f"Submit button: {describe_field(result.fields['submit'][0])}")
        for box in result.fields.get("checkboxes", []):
            ui.info(f"Consent box  : {describe_field(box)}")
        for step in result.fields.get("pre_clicks", []):
            ui.info(f"Reveal click : {describe_field(step)}")
    else:
        ui.warn("No newsletter form could be detected automatically.")
        for link in result.suggested_links:
            ui.info(f"Maybe try: {link}")


def _elements_table(elements: list[dict]) -> None:
    rows = []
    for i, el in enumerate(elements, 1):
        kind = el["tag"] if el["type"] in ("", el["tag"]) else f"{el['tag']}[{el['type']}]"
        hint = el.get("placeholder") or el.get("text") or el.get("value") or el.get("label_text") or ""
        frame = "-" if el.get("frame_index") is None else f"iframe {el['frame_index']}"
        rows.append([str(i), kind, el.get("id", ""), el.get("name", ""), hint[:26], frame])
    ui.table(["#", "Element", "id", "name", "hint", "where"], rows, max_col=28)


def manual_mapping(elements: list[dict]) -> dict | None:
    """Fallback: let the user assign fields from the table or raw CSS."""
    ui.box("Manual field selection", [
        "Type the row number(s) from the table, or a CSS selector such as",
        "input[type='email']. Leave optional items empty.",
    ], color="yellow")
    if elements:
        _elements_table(elements)

    def pick(prompt: str, required: bool) -> list[dict] | None:
        while True:
            fields, problems = parse_selection(ui.ask(prompt), elements)
            for problem in problems:
                ui.warn(problem)
            if fields or not required:
                return fields
            if not ui.confirm("A value is required. Try again?", default=True):
                return None

    email = pick("EMAIL field", True)
    if email is None:
        return None
    submit = pick("SUBMIT button", True)
    if submit is None:
        return None
    result = storage.empty_fields()
    result.update(email=email, submit=submit,
                  checkboxes=pick("Consent CHECKBOX(es) to tick (optional)", False) or [],
                  radios=pick("RADIO button(s) to select (optional)", False) or [])
    wait = ui.ask("Seconds to wait after submit", "5")
    result["wait"] = int(wait) if wait.isdigit() else 5
    return result


def _apply_inspection(settings: Settings, entries: list[dict], entry: dict,
                      result: browser.Inspection, driver_elements: list[dict]) -> bool:
    """Store the outcome of an inspection on *entry*. Returns True if usable."""
    entry["name"] = entry.get("name") or result.title[:80]
    if result.problem_kind:
        storage.set_status(entry, "unavailable")
        storage.record_check(entry, result.problem_kind, result.problem)
        return False
    fields = result.fields if result.usable else None
    if fields is None:
        ui.info("You can still choose the fields yourself.")
        if ui.confirm("Select the fields manually?", default=True):
            fields = manual_mapping(driver_elements)
    if not fields:
        storage.set_status(entry, "failed")
        storage.record_check(entry, "no_form", "no usable newsletter form found")
        return False
    entry["input_fields"] = fields
    storage.set_status(entry, "configured")
    storage.record_check(entry, "ok", "form inspected")
    return True


# ---------------------------------------------------------------------------
# 2. Add URL
# ---------------------------------------------------------------------------

def add_url(settings: Settings) -> None:
    ui.heading("Add a newsletter URL")
    url = validate_url(ui.ask("Newsletter signup page URL"))
    entries = _load(settings)
    if storage.find_entry(entries, url):
        ui.warn("That URL is already in your list.")
        return
    with ui.spinner("Checking that the page responds"):
        state, detail = check_url_available(url)
    {"ok": ui.ok, "unavailable": ui.err}.get(state, ui.warn)(f"Availability: {state} ({detail})")
    if state == "unavailable" and not ui.confirm("The page looks unavailable. Add it anyway?"):
        return
    entry = storage.make_entry(url, source="manual")
    storage.record_check(entry, state, detail)
    storage.add_entry(entries, entry)
    _save(settings, entries)
    ui.ok("Saved as 'discovered'.")
    if ui.confirm("Inspect its form now (opens Firefox, submits nothing)?", default=True):
        _inspect_entry(settings, entries, entry)


def _inspect_entry(settings: Settings, entries: list[dict], entry: dict) -> bool:
    ui.heading("Inspecting form")
    with browser.open_browser(settings) as driver:
        result = browser.inspect_page(driver, entry["url"], settings.page_wait, ui.info)
        _show_inspection(result)
        usable = _apply_inspection(settings, entries, entry, result, result.elements)
    _save(settings, entries)
    ui.ok("Saved.") if usable else ui.warn(f"Status is now '{entry['status']}'.")
    return usable


# ---------------------------------------------------------------------------
# 3. Inspect only
# ---------------------------------------------------------------------------

def inspect_url(settings: Settings) -> None:
    ui.heading("Inspect a form (nothing is typed or submitted)")
    entries = _load(settings)
    entry = None
    if entries and ui.confirm("Pick from your saved list?", default=True):
        entry = _choose_entry(entries)
    if entry is None:
        url = validate_url(ui.ask("URL to inspect"))
        entry = storage.find_entry(entries, url)
        if entry is None:
            entry = storage.make_entry(url, source="manual")
            storage.add_entry(entries, entry)
    _inspect_entry(settings, entries, entry)


# ---------------------------------------------------------------------------
# 1. The safe single-test workflow
# ---------------------------------------------------------------------------

def single_test(settings: Settings) -> None:
    ui.heading("Single newsletter test")
    ui.info("One URL, one submission, one address that you own.")

    ui.step(1, 6, "Test email")
    email = settings.require_test_email()
    ui.ok(f"Using TEST_EMAIL: {email}")
    if not ui.confirm("Is this an inbox you personally own or are authorized to test?"):
        ui.warn("Cancelled. Set TEST_EMAIL in .env to an address you control.")
        return

    ui.step(2, 6, "Choose the newsletter page")
    entries = _load(settings)
    entry = None
    if entries and ui.confirm("Pick from your saved list?", default=True):
        entry = _choose_entry(entries)
        if entry is None:
            return
    else:
        url = validate_url(ui.ask("Newsletter signup page URL"))
        entry = storage.find_entry(entries, url)
        if entry is None:
            entry = storage.make_entry(url, source="manual")
            storage.add_entry(entries, entry)
            _save(settings, entries)

    wait_left = storage.cooldown_remaining(entry, settings.cooldown_minutes)
    if wait_left.total_seconds() > 0:
        minutes = int(wait_left.total_seconds() // 60) + 1
        ui.warn(f"This URL was tested recently. To avoid repeat requests to the site, "
                f"please wait about {minutes} more minute(s).")
        return

    with browser.open_browser(settings) as driver:
        ui.step(3, 6, "Inspect the form")
        if storage.has_form(entry) and entry["status"] != "failed" and not ui.confirm(
                "Saved selectors found. Re-inspect the page anyway?"):
            ui.info(f"Email field  : {describe_field(entry['input_fields']['email'][0])}")
            ui.info(f"Submit button: {describe_field(entry['input_fields']['submit'][0])}")
        else:
            result = browser.inspect_page(driver, entry["url"], settings.page_wait, ui.info)
            _show_inspection(result)
            if not _apply_inspection(settings, entries, entry, result, result.elements):
                _save(settings, entries)
                ui.warn("The test cannot continue without a usable form.")
                return
            _save(settings, entries)

        ui.step(4, 6, "Confirm")
        use_imap = settings.imap_configured
        hints = entry.setdefault("verification", {"sender_hint": "", "subject_hint": ""})
        if use_imap:
            ui.info("IMAP is configured: the tool will watch your mailbox for the reply.")
            hints["sender_hint"] = ui.ask("Expected sender contains (optional)", hints.get("sender_hint", ""))
            hints["subject_hint"] = ui.ask("Expected subject contains (optional)", hints.get("subject_hint", ""))
        ui.box("About to submit", [f"Address : {email}", f"Page    : {entry['url']}",
                                  "Requests: exactly ONE form submission"], color="yellow")
        if not ui.confirm("Submit the form once now?"):
            ui.warn("Cancelled. Nothing was submitted.")
            _save(settings, entries)
            return

        known = None
        if use_imap:
            try:
                with ui.spinner("Taking a snapshot of your inbox"):
                    known = imap_utils.snapshot_ids(settings)
            except AppError as exc:
                ui.error_box(exc.message, exc.hint)
                if not ui.confirm("Continue without the mailbox check?"):
                    return
                use_imap = False

        ui.step(5, 6, "Run the test")
        result = browser.run_single_test(driver, entry["url"], email, entry["input_fields"],
                                         settings.page_wait, ui.info)

    storage.record_test(entry, result.outcome, result.detail)
    _report_result(entry, result)
    ui.step(6, 6, "Check the email")
    if not result.interaction_ok:
        storage.set_status(entry, "unavailable" if result.outcome in ("blocked", "unavailable") else "failed")
        _save(settings, entries)
        ui.info("Details were written to logs/subscription_tester.log.")
        if result.screenshot:
            ui.info(f"Screenshot: {result.screenshot}")
        return
    storage.set_status(entry, "configured")
    _verify_delivery(settings, entry, use_imap, known)
    _save(settings, entries)


def _report_result(entry: dict, result: browser.SubmitResult) -> None:
    log.info("test url=%s outcome=%s detail=%s", entry["url"], result.outcome, result.detail)
    title = {"confirmed_on_page": "Form submitted - site confirmed",
             "submitted": "Form submitted - no explicit confirmation",
             "already_subscribed": "Form submitted - already subscribed"}.get(
        result.outcome, f"Test did not succeed ({result.outcome})")
    color = "green" if result.interaction_ok else "red"
    ui.box(title, [f"Outcome : {result.outcome}", f"Detail  : {result.detail}",
                   f"Ended on: {result.final_url or '(unknown)'}"], color=color)


def _verify_delivery(settings: Settings, entry: dict, use_imap: bool, known) -> None:
    hints = entry.get("verification", {})
    if use_imap and known is not None:
        ui.info(f"Watching your inbox for up to {settings.imap_timeout}s...")
        try:
            message = imap_utils.wait_for_new_email(
                settings, known, hints.get("sender_hint", ""), hints.get("subject_hint", ""),
                on_wait=lambda left: ui.info(f"No new matching mail yet ({left}s left)"))
        except AppError as exc:
            ui.error_box(exc.message, exc.hint)
            message = None
        if message:
            ui.ok(f"Email received from {message['from']}: {message['subject']}")
            storage.set_status(entry, "verified")
            return
        ui.warn("No matching email arrived in time. Check spam/promotions and the double-opt-in "
                "confirmation; the status stays 'configured'.")
    else:
        ui.info("Open your inbox (also check spam/promotions) and look for a welcome or "
                "confirmation email.")
    if ui.confirm("Did the email arrive? (marks this URL 'verified')"):
        storage.set_status(entry, "verified")
        ui.ok("Marked as verified.")
    else:
        ui.info("Status stays 'configured'. You can mark it later from 'Manage the list'.")


# ---------------------------------------------------------------------------
# 4. Manage list
# ---------------------------------------------------------------------------

def manage_list(settings: Settings) -> None:
    while True:
        entries = _load(settings)
        ui.heading(f"Your newsletter list ({len(entries)})")
        if not entries:
            ui.info("The list is empty. Use 'Add a newsletter URL' first.")
            return
        _list_table(entries)
        counts = storage.counts(entries)
        ui.info("  ".join(f"{k}: {v}" for k, v in counts.items()))
        action = ui.menu("Manage", [("v", "View details of one entry"),
                                    ("c", "Check availability (one HTTP request per URL)"),
                                    ("s", "Change an entry's status"),
                                    ("d", "Delete an entry"), ("b", "Back")])
        if action in ("b", "q", ""):
            return
        if action == "c":
            _check_availability(settings, entries)
        elif action in ("v", "s", "d"):
            entry = _choose_entry(entries)
            if entry is None:
                continue
            if action == "v":
                _show_entry(entry)
            elif action == "s":
                status = ui.ask(f"New status ({', '.join(storage.STATUSES)})")
                if status in storage.STATUSES:
                    storage.set_status(entry, status)
                    _save(settings, entries)
                    ui.ok("Status updated.")
                else:
                    ui.warn("Unknown status.")
            elif ui.confirm(f"Delete {entry['url']}?"):
                entries.remove(entry)
                _save(settings, entries)
                ui.ok("Deleted.")
        else:
            ui.warn("Unknown option.")


def _show_entry(entry: dict) -> None:
    fields = entry.get("input_fields", {})
    lines = [f"URL     : {entry['url']}", f"Status  : {ui.status_badge(entry['status'])}",
             f"Source  : {entry.get('source', '-')}   Added: {entry.get('added', '-')}"]
    for key in ("email", "submit", "checkboxes", "pre_clicks"):
        for item in fields.get(key, []):
            lines.append(f"{key:<8}: {describe_field(item)}")
    for label, key in (("Check", "last_check"), ("Test", "last_test")):
        info = entry.get(key)
        if info:
            lines.append(f"Last {label.lower():<4}: {info.get('at', '')} - "
                         f"{info.get('state') or info.get('outcome')} - {info.get('detail', '')}")
    ui.box("Entry details", lines, color="blue")


def _check_availability(settings: Settings, entries: list[dict]) -> None:
    ui.info("Sending one plain request per URL, with a short pause between them.")
    for i, entry in enumerate(entries, 1):
        ui.progress(i, len(entries), entry["url"][:50])
        state, detail = check_url_available(entry["url"])
        storage.record_check(entry, state, detail)
        if state == "unavailable":
            storage.set_status(entry, "unavailable")
        elif state == "ok" and entry["status"] == "unavailable":
            storage.set_status(entry, "configured" if storage.has_form(entry) else "discovered")
        (ui.ok if state == "ok" else ui.warn)(f"{state}: {detail}")
        time.sleep(1.0)
    _save(settings, entries)


# ---------------------------------------------------------------------------
# 5. Search API (optional)
# ---------------------------------------------------------------------------

def search_flow(settings: Settings) -> None:
    ui.heading("Find newsletter pages (Search API)")
    settings.require_search()
    topic = ui.ask("Topic (e.g. python, personal finance)")
    with ui.spinner("Searching"):
        urls = search_api.search_newsletter_urls(settings, topic)
    entries = _load(settings)
    fresh = [u for u in urls if not storage.find_entry(entries, u)]
    if not fresh:
        ui.info("No new URLs found (everything returned is already in your list).")
        return
    ui.table(["#", "Candidate URL"], [[str(i), u] for i, u in enumerate(fresh, 1)], max_col=80)
    raw = ui.ask("Numbers to add, e.g. 1,3 ('all' or empty to skip)")
    if not raw:
        return
    chosen = fresh if raw.lower() == "all" else [
        fresh[int(p) - 1] for p in raw.replace(" ", "").split(",")
        if p.isdigit() and 1 <= int(p) <= len(fresh)]
    for url in chosen:
        storage.add_entry(entries, storage.make_entry(url, source="search"))
    _save(settings, entries)
    ui.ok(f"Added {len(chosen)} URL(s) as 'discovered'. Inspect each one before testing.")


# ---------------------------------------------------------------------------
# 6. Diagnostics
# ---------------------------------------------------------------------------

def diagnostics(settings: Settings) -> None:
    ui.heading("Setup check")
    rows = []

    def row(name: str, good: bool | None, text: str) -> None:
        mark = ui.c(ui.symbol("ok"), "green") if good else (
            ui.c(ui.symbol("warn"), "yellow") if good is None else ui.c(ui.symbol("err"), "red"))
        rows.append([name, mark, text])

    email = settings.test_email
    row("TEST_EMAIL", is_valid_email(email) if email else False,
        email or "not set (required for tests)")
    found, where = browser.firefox_status(settings)
    row("Firefox", found if found else (None if browser.can_autodetect_firefox() else False), where)
    try:
        entries = _load(settings)
        row("URL database", True, f"{settings.url_json.name}: {len(entries)} entries")
    except AppError as exc:
        row("URL database", False, exc.message)
    row("Search API", True if settings.search_configured else None,
        "key set" if settings.search_configured else "not configured (optional)")
    problems = settings.imap_problems()
    row("IMAP", True if settings.imap_configured else None,
        f"{settings.imap_host} / {settings.imap_folder}" if settings.imap_configured else problems[0])
    ui.table(["Check", "", "Result"], rows, max_col=64)
    if settings.imap_configured and ui.confirm("Test the IMAP login now?"):
        try:
            with ui.spinner("Connecting to the mail server"):
                count = imap_utils.test_connection(settings)
            ui.ok(f"IMAP login works ({count} messages in {settings.imap_folder}).")
        except AppError as exc:
            ui.error_box(exc.message, exc.hint)
