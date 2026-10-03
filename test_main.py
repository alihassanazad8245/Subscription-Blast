"""
Lightweight, non-destructive tests. No browser is started, no network is
used and nothing is ever submitted to a real site.

Run:  python -m pytest -q
"""
import json
import urllib.error
from datetime import datetime, timedelta, timezone
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest

import browser
import imap_utils
import search_api
import selector_utils as su
import storage
import ui
import url_utils
from config import Settings, is_valid_email
from errors import AppError, BrowserSetupError, ConfigError, PageError, SearchAPIError


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

def test_settings_defaults_and_overrides():
    s = Settings.from_env({"TEST_EMAIL": " me@example.com ", "HEADLESS": "yes", "PAGE_WAIT": "12"})
    assert s.test_email == "me@example.com"
    assert s.headless is True and s.page_wait == 12
    assert s.url_json.name == "email_subscription.json"
    assert not s.imap_configured and not s.search_configured


def test_settings_bad_number_is_friendly():
    with pytest.raises(ConfigError) as err:
        Settings.from_env({"PAGE_WAIT": "soon"})
    assert "PAGE_WAIT" in err.value.message and err.value.hint


def test_settings_clamps_numbers():
    assert Settings.from_env({"PAGE_WAIT": "9999"}).page_wait == 60
    assert Settings.from_env({"TEST_COOLDOWN_MINUTES": "1"}).cooldown_minutes == 10


def test_test_email_required_and_single():
    with pytest.raises(ConfigError):
        Settings.from_env({}).require_test_email()
    with pytest.raises(ConfigError):
        Settings.from_env({"TEST_EMAIL": "a@x.com,b@x.com"}).require_test_email()
    assert Settings.from_env({"TEST_EMAIL": "a@x.com"}).require_test_email() == "a@x.com"


def test_email_validation():
    assert is_valid_email("name.tag+x@mail.example.org")
    for bad in ("", "plain", "a@b", "a@@b.com", "a b@c.com"):
        assert not is_valid_email(bad)


def test_imap_problems_and_legacy_password_name():
    assert Settings.from_env({}).imap_problems() == ["IMAP is not configured (optional)."]
    partial = Settings.from_env({"IMAP_HOST": "imap.x.com"})
    assert any("IMAP_USER" in p for p in partial.imap_problems())
    full = Settings.from_env({"IMAP_HOST": "h", "IMAP_USER": "u", "IMAP_PASS": "legacy"})
    assert full.imap_configured and full.imap_password == "legacy"


def test_search_requires_key():
    with pytest.raises(ConfigError):
        Settings.from_env({}).require_search()


def test_relative_json_path_is_project_relative(tmp_path):
    s = Settings.from_env({"URL_JSON": "mine.json"})
    assert s.url_json.is_absolute() and s.url_json.name == "mine.json"
    assert Settings.from_env({"URL_JSON": str(tmp_path / "a.json")}).url_json == tmp_path / "a.json"


# --------------------------------------------------------------------------
# URLs
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("HTTPS://Example.com:443/news/?utm_source=x&a=1#frag", "https://example.com/news?a=1"),
    ("http://example.com:80", "http://example.com/"),
    ("https://example.com:8443/x", "https://example.com:8443/x"),
])
def test_normalize_url(raw, expected):
    assert url_utils.normalize_url(raw) == expected


@pytest.mark.parametrize("raw", ["ftp://example.com", "https://user:pw@example.com", "javascript:alert(1)",
                                 "not a url", "", None, "https://nodot"])
def test_normalize_rejects_bad(raw):
    assert url_utils.normalize_url(raw) is None


def test_validate_url_adds_scheme_and_errors_friendly():
    assert url_utils.validate_url("example.com/newsletter/") == "https://example.com/newsletter"
    with pytest.raises(PageError) as err:
        url_utils.validate_url("   ")
    assert err.value.hint
    with pytest.raises(PageError):
        url_utils.validate_url("https://user:pw@example.com")


def test_dedupe_urls_keeps_order_and_drops_invalid():
    urls = ["https://a.com/x/", "https://A.com/x?utm_medium=e", "bad", "https://b.com"]
    assert url_utils.dedupe_urls(urls) == ["https://a.com/x", "https://b.com/"]


def _http_error(code):
    return urllib.error.HTTPError("https://x.com", code, "msg", {}, BytesIO())


@pytest.mark.parametrize("code,state", [(404, "unavailable"), (410, "unavailable"),
                                        (403, "blocked"), (429, "blocked"), (503, "blocked")])
def test_availability_classification(code, state):
    with patch("url_utils.urllib.request.urlopen", side_effect=_http_error(code)):
        assert url_utils.check_url_available("https://x.com")[0] == state


def test_availability_ok_and_dns_failure():
    response = MagicMock(status=200)
    response.__enter__.return_value = response
    with patch("url_utils.urllib.request.urlopen", return_value=response):
        assert url_utils.check_url_available("https://x.com") == ("ok", "HTTP 200")
    import socket
    with patch("url_utils.urllib.request.urlopen", side_effect=urllib.error.URLError(socket.gaierror())):
        assert url_utils.check_url_available("https://x.com")[0] == "unavailable"


# --------------------------------------------------------------------------
# selector parsing
# --------------------------------------------------------------------------

def test_selector_from_config_shapes():
    f = su.selector_from_config
    assert f({"css": " input[type='email'] "}) == "input[type='email']"
    assert f({"id": "email"}) == "#email"
    assert f({"class": "btn primary"}) == ".btn.primary"
    assert f({"name": "q"}) == '[name="q"]'
    assert f({"name": 'a"b'}) == '[name="a\\"b"]'
    assert f({"value": "Sign up"}) == '[value="Sign up"]'
    assert f("nonsense") == "" and f({}) == ""


def test_parse_css_selector_list_respects_brackets():
    assert su.parse_css_selector_list("input[name='a,b'], button[type=submit]") == [
        {"css": "input[name='a,b']"}, {"css": "button[type=submit]"}]
    assert su.parse_css_selector_list(" , ,") == []


def test_looks_like_css():
    assert su.looks_like_css("input[type='email']")
    assert not su.looks_like_css("input[type='email'")
    assert not su.looks_like_css("")


def test_parse_selection_numbers_and_css():
    elements = [{"selector": "a"}, {"selector": "b", "frame_index": 2}]
    assert su.parse_selection("2", elements) == ([{"css": "b", "frame_index": 2}], [])
    fields, problems = su.parse_selection("7", elements)
    assert fields == [] and problems
    assert su.parse_selection("input[type=email]", elements)[0] == [{"css": "input[type=email]"}]
    assert su.parse_selection("input[", elements)[1]
    assert su.parse_selection("", elements) == ([], [])


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------

def test_storage_roundtrip_and_atomic_save(tmp_path):
    path = tmp_path / "db.json"
    entries = []
    assert storage.add_entry(entries, storage.make_entry("https://a.com/n"))
    assert not storage.add_entry(entries, storage.make_entry("https://a.com/n/"))  # duplicate
    storage.save_entries(path, entries)
    assert storage.load_entries(path) == entries
    assert not list(tmp_path.glob("*.tmp"))


def test_storage_missing_file_is_empty(tmp_path):
    assert storage.load_entries(tmp_path / "nope.json") == []


def test_storage_invalid_json_is_friendly(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{oops", encoding="utf-8")
    with pytest.raises(AppError):
        storage.load_entries(path)


def test_old_schema_migration_never_trusts_old_verified(tmp_path):
    path = tmp_path / "old.json"
    path.write_text(json.dumps([
        {"url": "https://a.com/x/?utm_source=s", "verified": True,
         "input_fields": {"email": [{"id": "e"}], "submit": [{"class": "b"}], "wait": 5}},
        {"url": "https://a.com/x", "verified": False},      # duplicate after normalisation
        {"url": "https://b.com", "verified": False},
        {"url": "garbage"},
    ]), encoding="utf-8")
    entries = storage.load_entries(path)
    assert [e["url"] for e in entries] == ["https://a.com/x", "https://b.com/"]
    assert entries[0]["status"] == "configured"   # NOT verified
    assert entries[1]["status"] == "discovered"


def test_status_rules():
    e = storage.make_entry("https://a.com/")
    assert e["status"] == "discovered"
    e["input_fields"]["email"] = [{"css": "x"}]
    e["input_fields"]["submit"] = [{"css": "y"}]
    assert storage.has_form(e)
    with pytest.raises(ValueError):
        storage.set_status(e, "working")
    assert storage.counts([e])["discovered"] == 1


def test_cooldown():
    e = storage.make_entry("https://a.com/")
    assert storage.cooldown_remaining(e, 30) == timedelta(0)
    storage.record_test(e, "submitted", "ok")
    assert storage.cooldown_remaining(e, 30) > timedelta(minutes=29)
    later = datetime.now(timezone.utc) + timedelta(minutes=31)
    assert storage.cooldown_remaining(e, 30, now=later) == timedelta(0)


def test_shipped_database_is_clean():
    from config import PROJECT_DIR
    entries = storage.load_entries(PROJECT_DIR / "email_subscription.json")
    urls = [e["url"] for e in entries]
    assert len(urls) == len(set(urls)) and entries
    assert all(e["status"] in storage.STATUSES for e in entries)
    assert not [e for e in entries if e["status"] == "verified"]   # nothing claimed unproven


# --------------------------------------------------------------------------
# form inference (pure logic, no browser)
# --------------------------------------------------------------------------

def el(**kw):
    base = dict(tag="input", type="text", id="", name="", **{"class": ""}, placeholder="",
                autocomplete="", aria_label="", required=False, value="", text="", label_text="",
                displayed=True, selector="", selector_index=0, frame_index=None, form_index=0)
    base.update(kw)
    return base


def test_infer_basic_newsletter_form():
    elements = [
        el(type="text", name="first_name", selector='input[name="first_name"]'),
        el(type="email", name="email", selector='input[name="email"]'),
        el(tag="button", type="submit", text="Subscribe", selector='button[type="submit"]'),
    ]
    fields, confidence = browser.infer_with_confidence(elements)
    assert fields["email"] == [{"css": 'input[name="email"]'}]
    assert fields["submit"] == [{"css": 'button[type="submit"]'}]
    assert confidence == "high"


def test_infer_ignores_login_and_search_forms():
    login = [el(type="email", name="email", selector="a"), el(type="password", selector="p"),
             el(tag="button", type="submit", text="Sign in", selector="b")]
    assert browser.infer_with_confidence(login)[1] == "none"
    search = [el(type="text", name="q", placeholder="Search", selector="q"),
              el(tag="button", type="submit", text="Search", selector="s")]
    assert browser.infer_with_confidence(search)[1] == "none"


def test_infer_requires_same_form_and_frame():
    elements = [el(type="email", selector="e", form_index=0, frame_index=None),
                el(tag="button", type="submit", text="Subscribe", selector="s", form_index=1)]
    assert browser.infer_with_confidence(elements)[1] == "none"
    elements = [el(type="email", selector="e", frame_index=0),
                el(tag="button", type="submit", text="Subscribe", selector="s", frame_index=None)]
    assert browser.infer_with_confidence(elements)[1] == "none"


def test_infer_iframe_and_consent_checkbox_and_visible_flag():
    elements = [
        el(type="email", selector="input.e", frame_index=1, selector_index=2, displayed=True),
        el(tag="button", type="submit", text="Join", selector="button.go", frame_index=1),
        el(type="checkbox", selector="#consent", required=True, frame_index=1),
        el(type="radio", selector="#r", frame_index=1),
    ]
    fields, _ = browser.infer_with_confidence(elements)
    assert fields["email"] == [{"css": "input.e", "frame_index": 1, "visible": True}]
    assert fields["checkboxes"] == [{"css": "#consent", "frame_index": 1}]
    assert fields["radios"] == []          # never guessed


def test_infer_plain_text_input_needs_email_evidence():
    elements = [el(type="text", placeholder="Your email address", selector="i"),
                el(tag="button", type="button", text="Sign up", selector="b")]
    assert browser.infer_with_confidence(elements)[1] == "medium"


def test_build_selector_preferences():
    assert browser.build_selector({"tag": "input", "type": "email", "name": "em", "form_id": "f1"}) \
        == '#f1 input[name="em"]'
    assert browser.build_selector({"tag": "input", "type": "email", "id": "email"}) == "#email"
    assert browser.build_selector({"tag": "input", "type": "email", "id": "x123456789"}) == 'input[type="email"]'
    assert browser.is_stable_id("newsletter-email") and not browser.is_stable_id("react-12345678")


# --------------------------------------------------------------------------
# page / result classification
# --------------------------------------------------------------------------

def test_classify_result():
    c = browser.classify_result
    assert c("Thank you for subscribing!")[0] == "confirmed_on_page"
    assert c("Please check your inbox")[0] == "confirmed_on_page"
    assert c("Please enter a valid email address")[0] == "rejected"
    assert c("You are already subscribed")[0] == "already_subscribed"
    assert c("Welcome to our site")[0] == "submitted"
    assert c("Thank you", challenge="hCaptcha challenge")[0] == "blocked"


def test_block_and_unavailable_detection():
    assert browser.detect_block("<div class='cf-turnstile'>") == "Cloudflare Turnstile challenge"
    assert browser.detect_block("<html>hello</html>") == ""
    assert browser.looks_unavailable("404 Not Found", "short", "https://x.com/a")
    assert browser.looks_unavailable("Home", "text", "https://x.com/login")
    assert browser.looks_unavailable("Newsletter", "x" * 3000, "https://x.com/n") == ""


def test_submit_result_flags():
    assert browser.SubmitResult("submitted", "").interaction_ok
    assert not browser.SubmitResult("blocked", "").interaction_ok
    assert not browser.SubmitResult("not_found", "").interaction_ok


# --------------------------------------------------------------------------
# Firefox lookup & friendly errors
# --------------------------------------------------------------------------

def test_invalid_firefox_path_is_friendly(tmp_path):
    s = Settings.from_env({"FIREFOX_PATH": str(tmp_path / "missing" / "firefox.exe")})
    with pytest.raises(BrowserSetupError) as err:
        browser.find_firefox(s)
    assert "FIREFOX_PATH" in err.value.message and err.value.hint


def test_valid_firefox_path_is_used(tmp_path):
    fake = tmp_path / "firefox.exe"
    fake.write_text("x")
    assert browser.find_firefox(Settings.from_env({"FIREFOX_PATH": f'"{fake}"'})) == str(fake)
    (tmp_path / "firefox").write_text("x")          # folder given instead of the exe
    assert browser.find_firefox(Settings.from_env({"FIREFOX_PATH": str(tmp_path)})) in (
        str(fake), str(tmp_path / "firefox"))


def test_missing_firefox_message_on_linux(monkeypatch):
    monkeypatch.setattr(browser, "find_firefox", lambda s: "")
    monkeypatch.setattr(browser, "can_autodetect_firefox", lambda: False)
    with pytest.raises(BrowserSetupError) as err:
        browser.create_driver(Settings.from_env({}))
    assert "mozilla.org" in err.value.hint


# --------------------------------------------------------------------------
# Search API (mocked, never hits the network)
# --------------------------------------------------------------------------

def _settings(**kw):
    return Settings.from_env({"SEARCH_API_KEY": "tvly-test", **kw})


def test_search_query_building():
    assert "newsletter" in search_api.build_query("python")
    assert search_api.build_query("python newsletter") == "python newsletter"
    with pytest.raises(SearchAPIError):
        search_api.build_query("  ")


@pytest.mark.parametrize("code", [401, 403])
def test_search_401_has_clear_explanation(code):
    with patch("search_api.urllib.request.urlopen", side_effect=_http_error(code)):
        with pytest.raises(SearchAPIError) as err:
            search_api.search_newsletter_urls(_settings(), "python")
    assert "API key" in err.value.message and "SEARCH_API_KEY" in err.value.hint


def test_search_without_key_is_config_error():
    with pytest.raises(ConfigError):
        search_api.search_newsletter_urls(Settings.from_env({}), "python")


def test_search_parses_and_dedupes():
    payload = {"results": [{"url": "https://a.com/n/"}, {"url": "https://a.com/n?utm_x=1"},
                           {"url": "junk"}, {"url": "https://b.com/s"}]}
    response = MagicMock()
    response.read.return_value = json.dumps(payload).encode()
    response.__enter__.return_value = response
    with patch("search_api.urllib.request.urlopen", return_value=response) as opened:
        urls = search_api.search_newsletter_urls(_settings(), "python")
    assert urls == ["https://a.com/n", "https://b.com/s"]
    request = opened.call_args[0][0]
    assert request.get_header("Authorization") == "Bearer tvly-test"


def test_search_bad_payload():
    with pytest.raises(SearchAPIError):
        search_api.parse_results({"nope": 1}, 5)


# --------------------------------------------------------------------------
# IMAP helpers (no network)
# --------------------------------------------------------------------------

def test_imap_hint_matching():
    m = imap_utils.matches_hints
    assert m("News <noreply@site.com>", "Please CONFIRM", "noreply@", "confirm")
    assert not m("a@b.com", "hi", "noreply", "")
    assert m("a@b.com", "hi", "", "")


def test_imap_unconfigured_raises_config_error():
    with pytest.raises(ConfigError):
        imap_utils.snapshot_ids(Settings.from_env({}))


def test_imap_login_failure_hides_password():
    import imaplib
    s = Settings.from_env({"IMAP_HOST": "h", "IMAP_USER": "u", "IMAP_PASSWORD": "SECRET-PW"})
    fake = MagicMock()
    fake.login.side_effect = imaplib.IMAP4.error("bad")
    with patch("imap_utils.imaplib.IMAP4_SSL", return_value=fake):
        with pytest.raises(AppError) as err:
            imap_utils.snapshot_ids(s)
    assert "SECRET-PW" not in err.value.message + err.value.hint
    assert "app password" in err.value.hint


# --------------------------------------------------------------------------
# UI helpers
# --------------------------------------------------------------------------

def test_ui_wrap_and_visible_len():
    assert ui.visible_len("\x1b[31mred\x1b[0m") == 3
    assert all(ui.visible_len(line) <= 10 for line in ui._wrap("word " * 20, 10))


def test_ui_box_renders(capsys):
    ui.box("Title", ["line one", "line two"])
    out = capsys.readouterr().out
    assert "Title" in out and "line one" in out
    widths = {ui.visible_len(line) for line in out.splitlines()}
    assert len(widths) == 1               # perfectly aligned box


def test_error_hierarchy():
    for cls in (ConfigError, BrowserSetupError, PageError, SearchAPIError):
        assert issubclass(cls, AppError)
