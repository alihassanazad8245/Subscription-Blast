"""
url_utils.py - URL validation, normalisation and a polite availability check.
"""
from __future__ import annotations

import socket
import urllib.error
import urllib.request
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from errors import PageError

# Honest, identifiable User-Agent (no browser impersonation).
USER_AGENT = "SubscriptionFormTester/2.0 (+authorized form testing)"

_TRACKING_PARAMS = {
    "ref", "fbclid", "gclid", "mc_cid", "mc_eid", "igshid", "source", "medium",
}


def _is_tracking(name: str) -> bool:
    name = name.lower()
    return name.startswith("utm_") or name in _TRACKING_PARAMS


def normalize_url(url: str) -> str | None:
    """Return a canonical http(s) URL for storage/de-duplication, or None.

    Lower-cases the host, drops default ports, fragments, trailing slashes and
    common tracking parameters. URLs with embedded credentials are rejected.
    """
    if not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if scheme not in ("http", "https") or not host or "." not in host and host != "localhost":
        return None
    if parts.username or parts.password:
        return None
    netloc = f"[{host}]" if ":" in host else host
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{netloc}:{port}"
    path = parts.path or "/"
    if path != "/":
        path = path.rstrip("/") or "/"
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                       if not _is_tracking(k)])
    return urlunsplit((scheme, netloc, path, query, ""))


def validate_url(raw: str) -> str:
    """Normalise user input (adds https:// if missing) or raise PageError."""
    raw = (raw or "").strip()
    if not raw:
        raise PageError("No URL entered.", "Paste a full newsletter page address.")
    if "://" not in raw:
        raw = "https://" + raw
    url = normalize_url(raw)
    if not url:
        raise PageError(
            f"'{raw}' is not a valid web address.",
            "Use a full http:// or https:// URL without a username or password.",
        )
    return url


def dedupe_urls(urls: list[str]) -> list[str]:
    """Normalise and de-duplicate while preserving order; invalid ones dropped."""
    seen: set[str] = set()
    result: list[str] = []
    for raw in urls:
        url = normalize_url(raw)
        if url and url not in seen:
            seen.add(url)
            result.append(url)
    return result


def check_url_available(url: str, timeout: float = 12.0) -> tuple[str, str]:
    """Light HTTP check. Returns (state, detail).

    state is one of:
      ok          - the server answered with a success/redirect status
      unavailable - clearly gone (404/410, DNS failure, connection refused)
      blocked     - the server refused automated requests (401/403/429/5xx);
                    the page may still work in a real browser
      unknown     - timeout or another unclear result
    """
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return "ok", f"HTTP {response.status}"
    except urllib.error.HTTPError as exc:
        if exc.code in (404, 410):
            return "unavailable", f"HTTP {exc.code} (page not found)"
        if exc.code in (401, 403, 429) or exc.code >= 500:
            return "blocked", f"HTTP {exc.code} (server refused or is struggling)"
        return "unknown", f"HTTP {exc.code}"
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, socket.gaierror):
            return "unavailable", "domain name does not resolve"
        if isinstance(reason, (socket.timeout, TimeoutError)):
            return "unknown", "timed out"
        if isinstance(reason, ConnectionRefusedError):
            return "unavailable", "connection refused"
        return "unknown", str(reason)[:120]
    except (socket.timeout, TimeoutError):
        return "unknown", "timed out"
    except Exception as exc:  # malformed responses, SSL oddities, ...
        return "unknown", f"{type(exc).__name__}: {str(exc)[:100]}"
