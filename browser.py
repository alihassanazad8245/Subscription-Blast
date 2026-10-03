"""
browser.py - Firefox/Selenium: starting the browser, inspecting a page's
form, and performing ONE controlled test submission.

Design notes
------------
* Selenium Manager (built into Selenium 4) downloads geckodriver for you.
  Only Mozilla Firefox itself must be installed.
* Inspection never types into or submits a form.
* Nothing here tries to defeat CAPTCHAs, bot protection or rate limits. If a
  page shows such a challenge the result is reported as "blocked".
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin, urlsplit, urlunsplit

from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    InvalidSelectorException,
    NoSuchDriverException,
    SessionNotCreatedException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.firefox.service import Service

from config import PROJECT_DIR, Settings
from errors import BrowserSetupError, PageError
from selector_utils import css_string, selector_from_config

log = logging.getLogger("sft.browser")
LOG_DIR = PROJECT_DIR / "logs"
Reporter = Callable[[str], None]


def _noop(_message: str) -> None:
    return None


# ---------------------------------------------------------------------------
# Locating Firefox and starting the driver
# ---------------------------------------------------------------------------

def _windows_registry_firefox() -> str:
    try:
        import winreg  # type: ignore
    except ImportError:
        return ""
    key_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\firefox.exe"
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hive, key_path) as key:
                value, _ = winreg.QueryValueEx(key, "")
                if value and Path(value).is_file():
                    return value
        except OSError:
            continue
    return ""


def find_firefox(settings: Settings) -> str:
    """Return the path to firefox, or "" if Selenium Manager should look.

    Order: FIREFOX_PATH from .env -> PATH -> common install locations.
    An explicit but wrong FIREFOX_PATH is an error, never silently ignored.
    """
    if settings.firefox_path:
        path = Path(settings.firefox_path).expanduser()
        if path.is_dir():
            path = path / ("firefox.exe" if os.name == "nt" else "firefox")
        if not path.is_file():
            raise BrowserSetupError(
                f"FIREFOX_PATH points to a file that does not exist: {settings.firefox_path}",
                r'Example: FIREFOX_PATH=C:\Program Files\Mozilla Firefox\firefox.exe  '
                "(or remove the line to auto-detect).",
            )
        return str(path)

    found = shutil.which("firefox")
    if found:
        return found
    candidates: list[str] = []
    if os.name == "nt":
        for var in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            base = os.environ.get(var)
            if base:
                candidates.append(os.path.join(base, "Mozilla Firefox", "firefox.exe"))
        registry = _windows_registry_firefox()
        if registry:
            candidates.append(registry)
    elif sys.platform == "darwin":
        candidates.append("/Applications/Firefox.app/Contents/MacOS/firefox")
    else:
        candidates += ["/usr/bin/firefox", "/usr/lib/firefox/firefox", "/snap/bin/firefox"]
    return next((p for p in candidates if os.path.isfile(p)), "")


def firefox_status(settings: Settings) -> tuple[bool, str]:
    """For the diagnostics screen: (found, description)."""
    try:
        path = find_firefox(settings)
    except BrowserSetupError as exc:
        return False, exc.message
    return (True, path) if path else (False, "not found (install Firefox or set FIREFOX_PATH)")


def create_driver(settings: Settings, headless: bool | None = None) -> webdriver.Firefox:
    """Start Firefox. Raises BrowserSetupError with a readable message."""
    binary = find_firefox(settings)
    if not binary and not can_autodetect_firefox():
        raise _firefox_missing()
    options = Options()
    if binary:
        options.binary_location = binary
    if settings.headless if headless is None else headless:
        options.add_argument("-headless")
    options.set_preference("intl.accept_languages", "en-US, en")
    try:
        driver = webdriver.Firefox(options=options, service=Service())
    except NoSuchDriverException as exc:
        log.exception("driver lookup failed")
        raise BrowserSetupError(
            "Could not set up geckodriver (the Firefox driver).",
            "Selenium downloads it automatically the first time, so you need an "
            "internet connection. If Firefox is not installed, get it from "
            "https://www.mozilla.org/firefox/ . Details: " + _short(exc),
        ) from None
    except (SessionNotCreatedException, WebDriverException) as exc:
        log.exception("firefox start failed")
        text = str(exc).lower()
        if any(k in text for k in ("binary", "no such file", "cannot find firefox", "not found")):
            raise _firefox_missing(binary) from None
        raise BrowserSetupError(
            "Firefox was found but could not be started.",
            "Close stuck Firefox/geckodriver processes (Task Manager) and try "
            "again, or update Firefox. Details: " + _short(exc),
        ) from None
    driver.set_page_load_timeout(45)
    return driver


def can_autodetect_firefox() -> bool:
    """Selenium Manager can find Firefox itself on Windows/macOS via system
    records, so a missing path is only fatal on platforms where it cannot."""
    return os.name == "nt" or sys.platform == "darwin"


def _firefox_missing(path: str = "") -> BrowserSetupError:
    where = f" at {path}" if path else ""
    return BrowserSetupError(
        f"Mozilla Firefox could not be found{where}.",
        "Install Firefox from https://www.mozilla.org/firefox/ . If it is "
        r"installed in a custom folder, set FIREFOX_PATH in .env "
        r"(e.g. C:\Program Files\Mozilla Firefox\firefox.exe).",
    )


def _short(exc: Exception) -> str:
    return " ".join(str(exc).split())[:220]


@contextmanager
def open_browser(settings: Settings, headless: bool | None = None):
    """Context manager that always closes the browser."""
    driver = create_driver(settings, headless)
    try:
        yield driver
    finally:
        try:
            driver.quit()
        except Exception:  # browser already closed by the user
            pass


# ---------------------------------------------------------------------------
# Page loading helpers
# ---------------------------------------------------------------------------

def load_page(driver, url: str, wait: int) -> None:
    """Navigate to *url* and wait for the page and its (JS) form to appear."""
    try:
        driver.get(url)
    except TimeoutException:
        raise PageError(
            "The page took too long to load.",
            "Check your connection or try again; slow sites may need a retry.",
        ) from None
    except WebDriverException as exc:
        text = str(exc).lower()
        if any(k in text for k in ("dnsnotfound", "neterror", "connectionfailure", "reached error page")):
            raise PageError(
                "The page could not be reached (DNS or connection error).",
                "Check the URL spelling and your internet connection.",
            ) from None
        raise PageError("The browser could not open this page.", _short(exc)) from None
    _wait_for_form_controls(driver, wait)


def _wait_for_form_controls(driver, seconds: float) -> None:
    """Wait for readyState, then (briefly) for any visible form control."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            ready = driver.execute_script("return document.readyState")
            if ready == "complete" and _count_controls(driver) > 0:
                return
        except WebDriverException:
            pass
        time.sleep(0.25)


def _count_controls(driver) -> int:
    script = (
        "return document.querySelectorAll('input:not([type=hidden]),textarea,select,"
        "button').length + document.querySelectorAll('iframe').length;"
    )
    try:
        driver.switch_to.default_content()
        return int(driver.execute_script(script) or 0)
    except WebDriverException:
        return 0


_UNAVAILABLE_TITLE = re.compile(
    r"\b(404|410)\b|page not found|not found|no longer available|access denied|forbidden",
    re.IGNORECASE)
_LOGIN_PATHS = ("/login", "/log-in", "/signin", "/sign-in", "/account/login", "/register")

_BLOCK_SIGNATURES = (
    ("_incapsula_resource", "Incapsula protection page"),
    ("incapsula incident id", "Incapsula access block"),
    ("cf-chl-", "Cloudflare challenge"),
    ("cf-turnstile", "Cloudflare Turnstile challenge"),
    ("checking your browser", "browser verification page"),
    ("verify you are human", "human-verification challenge"),
    ("attention required", "bot-protection page"),
    ("g-recaptcha", "reCAPTCHA challenge"),
    ("h-captcha", "hCaptcha challenge"),
    ("hcaptcha.com", "hCaptcha challenge"),
)


def looks_unavailable(title: str, body_text: str, url: str) -> str:
    """Return a reason when the loaded page is clearly not a usable page."""
    path = urlsplit(url).path.lower()
    if any(part in path for part in _LOGIN_PATHS):
        return "the page redirected to a login/registration page"
    if _UNAVAILABLE_TITLE.search(title or "") and len((body_text or "").strip()) < 1500:
        return f"the page looks like an error page ('{(title or '').strip()[:60]}')"
    return ""


def detect_block(source: str, title: str = "", url: str = "") -> str:
    """Name a bot-protection challenge visible in *source*, else ''."""
    combined = " ".join((source or "", title or "", url or "")).lower()
    for signature, description in _BLOCK_SIGNATURES:
        if signature in combined:
            return description
    return ""


def page_problem(driver) -> tuple[str, str]:
    """Return (kind, reason) where kind is 'blocked', 'unavailable' or ''."""
    try:
        driver.switch_to.default_content()
        title = driver.title or ""
        url = driver.current_url or ""
        source = driver.page_source or ""
        body = driver.execute_script("return document.body ? document.body.innerText : ''") or ""
    except WebDriverException:
        return "", ""
    reason = detect_block(source, title, url)
    if reason and "captcha" not in reason.lower() and "turnstile" not in reason.lower():
        return "blocked", reason
    reason2 = looks_unavailable(title, body, url)
    if reason2:
        return "unavailable", reason2
    return "", ""


def captcha_notice(driver) -> str:
    """Name a CAPTCHA widget on the page (informational only)."""
    try:
        driver.switch_to.default_content()
        return detect_block(driver.page_source or "")
    except WebDriverException:
        return ""


# ---------------------------------------------------------------------------
# Finding configured elements
# ---------------------------------------------------------------------------

def find_configured_element(driver, field: dict, wait: float = 0.0):
    """Locate a saved field (switching into its iframe), retrying for *wait* s."""
    deadline = time.monotonic() + wait
    while True:
        try:
            return _locate_once(driver, field)
        except (LookupError, IndexError, InvalidSelectorException, WebDriverException):
            driver.switch_to.default_content()
            if time.monotonic() >= deadline:
                raise LookupError(f"element not found: {selector_from_config(field)}") from None
            time.sleep(0.25)


def _locate_once(driver, field: dict):
    driver.switch_to.default_content()
    frame_index = field.get("frame_index")
    if isinstance(frame_index, int):
        frames = driver.find_elements(By.CSS_SELECTOR, "iframe, frame")
        driver.switch_to.frame(frames[frame_index])
    selector = selector_from_config(field)
    if not selector:
        raise LookupError("empty selector")
    matches = driver.find_elements(By.CSS_SELECTOR, selector)
    if field.get("visible"):
        for element in matches:
            if element.is_displayed():
                return element
        raise LookupError(f"no visible match for {selector}")
    index = field.get("index", 0)
    index = index if isinstance(index, int) else 0
    if index >= len(matches):
        raise LookupError(f"no match for {selector}")
    return matches[index]


def _click(driver, element) -> None:
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
    try:
        element.click()
    except (ElementClickInterceptedException, WebDriverException):
        driver.execute_script("arguments[0].click();", element)  # overlay fallback


# ---------------------------------------------------------------------------
# Inspecting the form (read-only)
# ---------------------------------------------------------------------------

_COLLECT_JS = r"""
const forms = Array.from(document.forms);
const shown = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length)
                   && getComputedStyle(e).visibility !== 'hidden';
const keep = ['data-zjs-newsletter','data-list-id','data-identity-name','data-role',
              'aria-label','title'];
return Array.from(document.querySelectorAll('input,select,textarea,button')).map(e => {
  const f = e.form;
  const attrs = {};
  keep.forEach(k => { const v = e.getAttribute(k); if (v) attrs[k] = v; });
  return {
    el: e, tag: e.tagName.toLowerCase(),
    type: (e.getAttribute('type') || e.type || e.tagName).toLowerCase(),
    id: e.id || '', name: e.getAttribute('name') || '',
    cls: (e.getAttribute('class') || '').trim(),
    placeholder: e.getAttribute('placeholder') || '',
    autocomplete: e.getAttribute('autocomplete') || '',
    aria_label: e.getAttribute('aria-label') || '',
    required: e.required === true,
    value: e.getAttribute('value') || '',
    text: (e.innerText || '').trim().slice(0, 100),
    label_text: Array.from(e.labels || []).map(l => l.innerText || l.textContent || '')
                     .join(' ').trim().slice(0, 300),
    displayed: shown(e), attrs: attrs,
    form_index: f ? forms.indexOf(f) : -1,
    form_id: f ? (f.id || '') : '',
    form_aria: f ? (f.getAttribute('aria-label') || '') : '',
    form_name: f ? (f.getAttribute('name') || '') : '',
    form_displayed: f ? shown(f) : false
  };
});
"""


def is_stable_id(value: str) -> bool:
    """True for ids that are readable and not generated per page render."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", value or ""):
        return False
    if re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{8,}", value, re.I):
        return False
    return re.search(r"\d{5,}", value) is None


def _form_selector(info: dict) -> str:
    if is_stable_id(info.get("form_id", "")):
        return f"#{info['form_id']}"
    if info.get("form_aria"):
        return f'form[aria-label="{css_string(info["form_aria"])}"]'
    if info.get("form_name"):
        return f'form[name="{css_string(info["form_name"])}"]'
    return ""


def build_selector(info: dict) -> str:
    """Pick the most stable CSS selector for an inspected element.

    Preference: name -> semantic data/aria attributes -> stable id ->
    form-scoped tag/type -> class -> bare tag/type.
    """
    tag, el_type = info.get("tag", ""), info.get("type", "")
    parent = _form_selector(info)
    if info.get("name"):
        selector = f'{tag}[name="{css_string(info["name"])}"]'
        return f"{parent} {selector}" if parent else selector
    for attr in ("data-zjs-newsletter", "data-list-id", "data-identity-name",
                 "data-role", "aria-label", "title"):
        value = (info.get("attrs") or {}).get(attr, "")
        if value:
            return f'{tag}[{attr}="{css_string(value)}"]'
    if is_stable_id(info.get("id", "")):
        return f"#{info['id']}"
    if parent:
        suffix = tag
        if el_type and el_type != tag:
            suffix += f'[type="{css_string(el_type)}"]'
        if info.get("required"):
            suffix += "[required]"
        return f"{parent} {suffix}"
    first_class = (info.get("cls") or "").split()
    if first_class and re.fullmatch(r"-?[A-Za-z_][\w-]*", first_class[0]):
        return f"{tag}.{first_class[0]}"
    if tag == "input" and el_type not in ("input", "text"):
        return f'input[type="{css_string(el_type)}"]'
    if tag == "button" and el_type == "submit":
        return 'button[type="submit"]'
    return tag


def _collect_context(driver, frame_index: int | None, include_hidden: bool) -> list[dict]:
    try:
        raw = driver.execute_script(_COLLECT_JS) or []
    except WebDriverException:
        return []
    elements = []
    for info in raw:
        if info.get("type") == "hidden":
            continue
        if not info.get("displayed"):
            if not (include_hidden and info.get("form_index", -1) >= 0 and info.get("form_displayed")):
                continue
        css = build_selector(info)
        try:
            index = driver.execute_script(
                "return Array.from(document.querySelectorAll(arguments[0]))"
                ".indexOf(arguments[1]);", css, info["el"])
        except WebDriverException:
            index = 0
        elements.append({
            "tag": info["tag"], "type": info["type"], "id": info["id"], "name": info["name"],
            "class": info["cls"], "placeholder": info["placeholder"],
            "autocomplete": info["autocomplete"], "aria_label": info["aria_label"],
            "required": bool(info["required"]), "value": info["value"], "text": info["text"],
            "label_text": info["label_text"], "displayed": bool(info["displayed"]),
            "selector": css, "selector_index": index if isinstance(index, int) and index >= 0 else 0,
            "frame_index": frame_index, "form_index": info["form_index"],
        })
    return elements


def collect_form_elements(driver, include_hidden: bool = False) -> list[dict]:
    """Collect form controls from the page and its first-level iframes."""
    driver.switch_to.default_content()
    elements = _collect_context(driver, None, include_hidden)
    try:
        frames = driver.find_elements(By.CSS_SELECTOR, "iframe, frame")
    except WebDriverException:
        frames = []
    for index, frame in enumerate(frames):
        try:
            driver.switch_to.frame(frame)
            elements.extend(_collect_context(driver, index, include_hidden))
        except WebDriverException:
            continue
        finally:
            driver.switch_to.default_content()
    return elements


# --- inference (pure; unit-tested without a browser) ------------------------

_SUBMIT_WORDS = ("subscribe", "sign up", "signup", "join", "newsletter", "register",
                 "get updates", "notify me", "send me", "keep me")
_NOT_SUBMIT_WORDS = ("log in", "login", "sign in", "search", "unsubscribe", "password",
                     "cancel", "close", "reset")


def _text_for(element: dict) -> str:
    # Evidence attached to the control itself only. Form-wide text is shared
    # by every input and once made "first_name" look like the email field.
    keys = ("id", "name", "class", "placeholder", "autocomplete", "aria_label",
            "value", "text", "label_text")
    return " ".join(str(element.get(key, "")) for key in keys).lower()


def _field_for(element: dict) -> list[dict]:
    selector = str(element.get("selector", "")).strip()
    if not selector:
        return []
    field = {"css": selector}
    if isinstance(element.get("frame_index"), int):
        field["frame_index"] = element["frame_index"]
    index = element.get("selector_index")
    if isinstance(index, int) and index > 0:
        if element.get("displayed") is True:
            field["visible"] = True   # pick the first *visible* match
        else:
            field["index"] = index
    return [field]


def infer_with_confidence(elements: list[dict]) -> tuple[dict, str]:
    """Pick the best newsletter email/submit pair from inspected elements.

    Returns ``(fields, confidence)``; confidence is ``"high"``, ``"medium"``
    or ``"none"``. Empty ``email``/``submit`` lists mean "not confident
    enough" - the caller should offer manual selection. Radio buttons are
    never chosen automatically (picking an option for you would be a guess);
    only *required* consent checkboxes in the same form are ticked.
    """
    email_candidates, submit_candidates = [], []
    for index, element in enumerate(elements):
        tag = str(element.get("tag", "")).lower()
        kind = str(element.get("type", "")).lower()
        text = _text_for(element)

        if kind in ("email", "text") or tag == "textarea":
            score = 0
            if kind == "email":
                score += 100
            if str(element.get("autocomplete", "")).lower() == "email":
                score += 80
            if "email" in text or "e-mail" in text:
                score += 60
            if "newsletter" in text or "subscribe" in text:
                score += 20
            if score and not any(w in text for w in ("search", "password")):
                email_candidates.append((score, -index, element))

        if kind != "reset" and tag in ("button", "input") and kind not in (
                "checkbox", "radio", "text", "email", "password"):
            score = 0
            if kind == "submit":
                score += 100
            if any(w in text for w in _SUBMIT_WORDS):
                score += 60
            if tag == "button":
                score += 10
            if any(w in text for w in _NOT_SUBMIT_WORDS):
                score -= 120
            if score >= 60:
                submit_candidates.append((score, -index, element))

    pairs = []
    for e_score, e_order, email in email_candidates:
        for s_score, s_order, submit in submit_candidates:
            if email.get("frame_index") != submit.get("frame_index"):
                continue
            e_form, s_form = email.get("form_index", -1), submit.get("form_index", -1)
            if e_form != s_form and (e_form >= 0 or s_form >= 0):
                continue
            if e_form >= 0 and any(
                str(o.get("type", "")).lower() == "password"
                and o.get("frame_index") == email.get("frame_index")
                and o.get("form_index", -1) == e_form for o in elements
            ):
                continue  # email + password = login/registration, not a newsletter
            bonus = 100 if e_form >= 0 and e_form == s_form else 0
            pairs.append((e_score + s_score + bonus, e_order + s_order, email, submit))

    empty = {"email": [], "submit": [], "checkboxes": [], "radios": [],
             "pre_clicks": [], "wait": 0}
    if not pairs:
        return empty, "none"
    pairs.sort(reverse=True, key=lambda p: (p[0], p[1]))
    _, _, email, submit = pairs[0]
    frame, form = email.get("frame_index"), email.get("form_index", -1)
    consent: list[dict] = []
    for element in elements:
        if (str(element.get("type", "")).lower() == "checkbox" and element.get("required")
                and element.get("frame_index") == frame
                and (form < 0 or element.get("form_index", -1) == form)):
            consent.extend(_field_for(element))
    fields = dict(empty, email=_field_for(email), submit=_field_for(submit), checkboxes=consent)
    strong_email = str(email.get("type")) == "email" or str(email.get("autocomplete")).lower() == "email"
    same_form = form >= 0 and form == submit.get("form_index", -1)
    confidence = "high" if strong_email and same_form else "medium"
    return fields, confidence


def infer_subscription_fields(elements: list[dict]) -> dict:
    """Convenience wrapper returning only the inferred field mapping."""
    return infer_with_confidence(elements)[0]


@dataclass
class Inspection:
    url: str
    final_url: str = ""
    title: str = ""
    elements: list[dict] = field(default_factory=list)
    fields: dict = field(default_factory=dict)
    confidence: str = "none"
    problem_kind: str = ""        # "", "blocked", "unavailable"
    problem: str = ""
    captcha: str = ""
    suggested_links: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return bool(self.fields.get("email") and self.fields.get("submit"))


def inspect_page(driver, url: str, wait: int, on_step: Reporter = _noop) -> Inspection:
    """Open *url* and describe its newsletter form. Never types or submits."""
    on_step(f"Opening {url}")
    load_page(driver, url, wait)
    result = Inspection(url=url, final_url=driver.current_url, title=driver.title or "")
    result.problem_kind, result.problem = page_problem(driver)
    if result.problem_kind:
        return result
    result.captcha = captcha_notice(driver)
    on_step("Scanning the page (and iframes) for form fields")
    result.elements = collect_form_elements(driver)
    result.fields, result.confidence = infer_with_confidence(result.elements)
    if not result.usable:
        on_step("No form visible yet - looking for a button that reveals one")
        revealed = reveal_subscription_form(driver, wait)
        if revealed:
            result.fields, result.confidence = revealed, "medium"
            result.elements = collect_form_elements(driver, include_hidden=True)
    if not result.usable:
        result.suggested_links = find_subscription_links(url, driver)
    return result


def _reveal_candidates(driver) -> list[dict]:
    """Visible non-submit buttons that look like 'choose a newsletter'."""
    candidates = []
    driver.switch_to.default_content()
    contexts: list[tuple[int | None, object]] = [(None, None)]
    try:
        contexts += list(enumerate(driver.find_elements(By.CSS_SELECTOR, "iframe, frame")))
    except WebDriverException:
        pass
    for frame_index, frame in contexts:
        try:
            driver.switch_to.default_content()
            if frame is not None:
                driver.switch_to.frame(frame)
            for order, button in enumerate(driver.find_elements(By.CSS_SELECTOR, 'button[type="button"]')):
                if not button.is_displayed() or not button.is_enabled():
                    continue
                text = " ".join(filter(None, (
                    button.text, button.get_attribute("aria-label"), button.get_attribute("title"),
                    button.get_attribute("data-list-id"), button.get_attribute("data-zjs-newsletter"),
                ))).lower()
                if "remove " in text or not any(t in text for t in ("newsletter", "subscribe", "sign up", "signup")):
                    continue
                score = (80 if "newsletter" in text else 0) + (60 if "subscribe" in text or "sign up" in text else 0)
                css = build_selector({
                    "tag": "button", "type": "button",
                    "attrs": {k: button.get_attribute(k) or "" for k in (
                        "data-zjs-newsletter", "data-list-id", "aria-label", "title")},
                    "id": button.get_attribute("id") or "", "name": button.get_attribute("name") or "",
                    "cls": button.get_attribute("class") or "",
                })
                item = {"css": css}
                try:
                    occurrence = driver.find_elements(By.CSS_SELECTOR, css).index(button)
                    if occurrence:
                        item["index"] = occurrence
                except (ValueError, WebDriverException):
                    pass
                if frame_index is not None:
                    item["frame_index"] = frame_index
                candidates.append((score, -order, item))
        except WebDriverException:
            continue
        finally:
            driver.switch_to.default_content()
    candidates.sort(reverse=True, key=lambda c: (c[0], c[1]))
    return [c[2] for c in candidates]


def reveal_subscription_form(driver, wait: float, max_actions: int = 3) -> dict | None:
    """Click up to *max_actions* likely 'pick a newsletter' buttons (never a
    submit control) and infer the form that appears. Saved as ``pre_clicks``."""
    for action in _reveal_candidates(driver)[:max_actions]:
        try:
            _click(driver, find_configured_element(driver, action))
        except (LookupError, WebDriverException):
            continue
        finally:
            driver.switch_to.default_content()
        deadline = time.monotonic() + min(wait, 3)
        while True:
            fields, _ = infer_with_confidence(collect_form_elements(driver, include_hidden=True))
            if fields["email"] and fields["submit"]:
                fields["pre_clicks"] = [action]
                return fields
            if time.monotonic() >= deadline:
                break
            time.sleep(0.25)
    return None


def find_subscription_links(base_url: str, driver, limit: int = 3) -> list[str]:
    """Same-site links that probably lead to a newsletter form (suggestions)."""
    try:
        anchors = driver.find_elements(By.CSS_SELECTOR, "a[href]")
    except WebDriverException:
        return []
    base = urlsplit(base_url)
    terms = ("newsletter", "subscribe", "subscription", "sign-up", "signup",
             "email-updates", "mailing-list", "mailing list")
    skip = ("unsubscribe", "/login", "/log-in", "/signin", "/sign-in", "/account", "/register")
    ranked, seen = [], set()
    for index, anchor in enumerate(anchors):
        try:
            absolute = urlsplit(urljoin(base_url, anchor.get_attribute("href") or ""))
            if absolute.scheme not in ("http", "https") or absolute.hostname != base.hostname:
                continue
            url = urlunsplit((absolute.scheme, absolute.netloc.lower(), absolute.path or "/", absolute.query, ""))
            if url == base_url or url in seen:
                continue
            text = " ".join((absolute.path, absolute.query, anchor.text or "",
                             anchor.get_attribute("title") or "")).lower()
            if any(s in text for s in skip):
                continue
            score = sum(1 for t in terms if t in text)
            if score:
                ranked.append((score, -index, url))
                seen.add(url)
        except WebDriverException:
            continue
    ranked.sort(reverse=True)
    return [r[2] for r in ranked[:limit]]


# ---------------------------------------------------------------------------
# The single controlled test submission
# ---------------------------------------------------------------------------

_SUCCESS_WORDS = ("thank you", "thanks for", "check your email", "check your inbox",
                  "confirm your", "confirmation email", "you're subscribed", "you are subscribed",
                  "successfully subscribed", "almost there", "one more step", "welcome aboard",
                  "you're in", "you've been added", "subscription confirmed", "success")
_ERROR_WORDS = ("invalid email", "valid email", "enter a valid", "please enter your email",
                "is required", "something went wrong", "try again later", "error occurred",
                "could not subscribe", "unable to subscribe")
_ALREADY_WORDS = ("already subscribed", "already on our list", "already signed up",
                  "already registered", "already a subscriber")


def classify_result(text: str, challenge: str = "") -> tuple[str, str]:
    """Classify the page text after submitting. Pure and unit-tested.

    Returns (outcome, detail) with outcome in:
      blocked, already_subscribed, rejected, confirmed_on_page, submitted
    "submitted" means the click worked but the page gave no clear signal -
    check the inbox to know for sure.
    """
    lowered = (text or "").lower()
    if challenge:
        return "blocked", f"the site showed a {challenge}; this tool does not bypass those"
    if any(w in lowered for w in _ALREADY_WORDS):
        return "already_subscribed", "the site says this address is already subscribed"
    if any(w in lowered for w in _ERROR_WORDS):
        return "rejected", "the page showed an error message after submitting"
    if any(w in lowered for w in _SUCCESS_WORDS):
        return "confirmed_on_page", "the page showed a thank-you / confirmation message"
    return "submitted", "form was submitted but the page gave no clear confirmation"


@dataclass
class SubmitResult:
    outcome: str                 # see classify_result + not_found/blocked/timeout/error
    detail: str
    final_url: str = ""
    screenshot: str = ""

    @property
    def interaction_ok(self) -> bool:
        return self.outcome in ("confirmed_on_page", "submitted", "already_subscribed")


def _page_text(driver) -> str:
    chunks = []
    try:
        driver.switch_to.default_content()
        chunks.append(driver.execute_script("return document.body ? document.body.innerText : ''") or "")
        for frame in driver.find_elements(By.CSS_SELECTOR, "iframe, frame")[:5]:
            try:
                driver.switch_to.frame(frame)
                chunks.append(driver.execute_script("return document.body ? document.body.innerText : ''") or "")
            except WebDriverException:
                pass
            finally:
                driver.switch_to.default_content()
    except WebDriverException:
        pass
    return "\n".join(chunks)


def _save_failure_screenshot(driver, label: str) -> str:
    try:
        LOG_DIR.mkdir(exist_ok=True)
        path = LOG_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{label}.png"
        driver.save_screenshot(str(path))
        return str(path)
    except Exception:
        return ""


def run_single_test(driver, url: str, email: str, fields: dict, wait: int,
                    on_step: Reporter = _noop) -> SubmitResult:
    """Fill the form with *email* and submit it exactly once.

    The caller is responsible for the cooldown / confirmation prompts. This
    function performs no retries and never reloads to submit twice.
    """
    try:
        on_step(f"Opening {url}")
        load_page(driver, url, wait)
        kind, reason = page_problem(driver)
        if kind:
            return SubmitResult("blocked" if kind == "blocked" else "unavailable", reason, driver.current_url)

        for action in fields.get("pre_clicks", []):
            on_step("Opening the newsletter form (reveal step)")
            _click(driver, find_configured_element(driver, action, wait))
            driver.switch_to.default_content()

        for box in fields.get("checkboxes", []):
            element = find_configured_element(driver, box, wait)
            if not element.is_selected():
                _click(driver, element)
            on_step("Ticked a required consent checkbox")

        email_field = fields["email"][0]
        element = find_configured_element(driver, email_field, wait)
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
        try:
            element.clear()
        except WebDriverException:
            pass
        element.send_keys(email)
        typed = element.get_attribute("value") or ""
        if typed != email:
            return SubmitResult("error", "the email field did not accept the typed address "
                              f"(it contains '{typed[:40]}')", driver.current_url,
                              _save_failure_screenshot(driver, "email-field"))
        on_step("Typed the test email into the form")

        for radio in fields.get("radios", []):
            _click(driver, find_configured_element(driver, radio, wait))

        submit = find_configured_element(driver, fields["submit"][0], wait)
        before_url = driver.current_url
        on_step("Clicking the submit button (once)")
        _click(driver, submit)
    except LookupError as exc:
        return SubmitResult("not_found", f"{exc}. The page layout may have changed - "
                          "re-inspect the form.", "", _save_failure_screenshot(driver, "selector"))
    except PageError as exc:
        return SubmitResult("timeout" if "too long" in exc.message else "unavailable", exc.message)
    except TimeoutException:
        return SubmitResult("timeout", "the page timed out", "", _save_failure_screenshot(driver, "timeout"))
    except WebDriverException as exc:
        log.exception("selenium error during test")
        return SubmitResult("error", f"browser error: {_short(exc)}", "",
                          _save_failure_screenshot(driver, "browser-error"))
    finally:
        try:
            driver.switch_to.default_content()
        except WebDriverException:
            pass

    on_step("Waiting for the site's response")
    settle = max(wait, int(fields.get("wait", 0) or 0))
    deadline = time.monotonic() + settle
    outcome, detail = "submitted", ""
    while time.monotonic() < deadline:
        time.sleep(1)
        text = _page_text(driver)
        challenge = detect_block(driver.page_source or "") if _challenge_visible(driver) else ""
        outcome, detail = classify_result(text, challenge)
        if outcome != "submitted":
            break
    shot = "" if outcome in ("confirmed_on_page", "submitted", "already_subscribed") else \
        _save_failure_screenshot(driver, outcome)
    final_url = driver.current_url
    if outcome == "submitted" and final_url != before_url:
        detail += " (the page navigated after submitting)"
    return SubmitResult(outcome, detail, final_url, shot)


def _challenge_visible(driver) -> bool:
    """True when a human-verification challenge is actually showing."""
    script = ("return !!document.querySelector("
              "'iframe[src*=\"hcaptcha.com/captcha\"],iframe[src*=\"recaptcha/api2/bframe\"],"
              "iframe[title*=\"challenge\"],#challenge-form,.cf-turnstile-wrapper');")
    try:
        driver.switch_to.default_content()
        return bool(driver.execute_script(script))
    except WebDriverException:
        return False
