"""
config.py - settings loaded from the environment / .env file.

Nothing here is hard-coded secret material. Settings are read into a small
dataclass so they are easy to test: ``Settings.from_env({...})``.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from errors import ConfigError

PROJECT_DIR = Path(__file__).resolve().parent
APP_NAME = "Subscription Form Tester"
APP_VERSION = "2.0.0"

# Credits shown on the CLI exit screen and in the README.
AUTHOR = "Ali Hassan"
INSTAGRAM = "ali_hassan8245"
ORIGINAL_AUTHOR = "Chung Man Cheng"
ORIGINAL_REPO = "https://github.com/ChungmanCheng/Subscription-Bomb"

DEFAULT_SEARCH_URL = "https://api.tavily.com/search"
_EMAIL_RE = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]{2,}$")


def is_valid_email(value: str) -> bool:
    """Loose syntax check (one address, has a domain with a dot)."""
    return bool(_EMAIL_RE.match((value or "").strip()))


def _int(env: dict, name: str, default: int, lo: int, hi: int) -> int:
    raw = (env.get(name) or "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        raise ConfigError(
            f"{name} must be a whole number, got '{raw}'.",
            f"Fix {name} in your .env file (allowed range {lo}-{hi}).",
        ) from None
    return max(lo, min(hi, value))


def _bool(env: dict, name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    test_email: str = ""
    firefox_path: str = ""
    headless: bool = False
    page_wait: int = 8
    url_json: Path = PROJECT_DIR / "email_subscription.json"
    cooldown_minutes: int = 30
    search_api_url: str = DEFAULT_SEARCH_URL
    search_api_key: str = ""
    search_max_results: int = 10
    imap_host: str = ""
    imap_port: int = 993
    imap_user: str = ""
    imap_password: str = ""
    imap_folder: str = "INBOX"
    imap_timeout: int = 90

    # ----- construction -------------------------------------------------
    @classmethod
    def from_env(cls, env: dict | None = None) -> "Settings":
        env = dict(os.environ if env is None else env)
        raw_json = (env.get("URL_JSON") or "").strip() or "email_subscription.json"
        json_path = Path(raw_json).expanduser()
        if not json_path.is_absolute():
            json_path = PROJECT_DIR / json_path
        return cls(
            test_email=(env.get("TEST_EMAIL") or "").strip(),
            firefox_path=(env.get("FIREFOX_PATH") or "").strip().strip('"'),
            headless=_bool(env, "HEADLESS", False),
            page_wait=_int(env, "PAGE_WAIT", 8, 2, 60),
            url_json=json_path,
            cooldown_minutes=_int(env, "TEST_COOLDOWN_MINUTES", 30, 10, 1440),
            search_api_url=(env.get("SEARCH_API_URL") or "").strip() or DEFAULT_SEARCH_URL,
            search_api_key=(env.get("SEARCH_API_KEY") or "").strip(),
            search_max_results=_int(env, "SEARCH_MAX_RESULTS", 10, 1, 20),
            imap_host=(env.get("IMAP_HOST") or "").strip(),
            imap_port=_int(env, "IMAP_PORT", 993, 1, 65535),
            imap_user=(env.get("IMAP_USER") or "").strip(),
            imap_password=(env.get("IMAP_PASSWORD") or env.get("IMAP_PASS") or "").strip(),
            imap_folder=(env.get("IMAP_FOLDER") or "").strip() or "INBOX",
            imap_timeout=_int(env, "IMAP_TIMEOUT", 90, 10, 600),
        )

    # ----- feature checks ----------------------------------------------
    def require_test_email(self) -> str:
        if not self.test_email:
            raise ConfigError(
                "TEST_EMAIL is not set.",
                "Copy .env.example to .env and set TEST_EMAIL to an inbox you own.",
            )
        if not is_valid_email(self.test_email):
            raise ConfigError(
                f"TEST_EMAIL '{self.test_email}' is not a valid single email address.",
                "Use exactly one address you own, e.g. TEST_EMAIL=me@example.com.",
            )
        return self.test_email

    @property
    def search_configured(self) -> bool:
        return bool(self.search_api_key)

    def require_search(self) -> None:
        if not self.search_api_key:
            raise ConfigError(
                "SEARCH_API_KEY is not set, so the Search API is disabled.",
                "Add SEARCH_API_KEY to .env (see 'Search API' in the README), "
                "or add URLs manually instead.",
            )

    def imap_problems(self) -> list[str]:
        """Return a list of human-readable problems; empty means usable."""
        given = [self.imap_host, self.imap_user, self.imap_password]
        if not any(given):
            return ["IMAP is not configured (optional)."]
        problems = []
        if not self.imap_host:
            problems.append("IMAP_HOST is missing.")
        if not self.imap_user:
            problems.append("IMAP_USER is missing.")
        if not self.imap_password:
            problems.append("IMAP_PASSWORD is missing.")
        return problems

    @property
    def imap_configured(self) -> bool:
        return not self.imap_problems()


_cached: Settings | None = None


def get_settings(reload: bool = False) -> Settings:
    """Load .env (once) and return the cached Settings."""
    global _cached
    if _cached is None or reload:
        load_dotenv(PROJECT_DIR / ".env")
        _cached = Settings.from_env()
    return _cached
