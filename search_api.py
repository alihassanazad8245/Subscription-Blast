"""
search_api.py - OPTIONAL newsletter-page discovery through a Tavily-style
search API (POST JSON, ``Authorization: Bearer <key>``, ``results[].url``).

It only returns *candidate* URLs for you to review. Nothing here opens a page
or submits anything. Without SEARCH_API_KEY the rest of the app is unaffected.
"""
from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request

from config import Settings
from errors import SearchAPIError
from url_utils import USER_AGENT, dedupe_urls

_TIMEOUT = 20


def build_query(topic: str) -> str:
    """Aim a topic at actual signup pages rather than articles about them."""
    topic = " ".join((topic or "").split())
    if not topic:
        raise SearchAPIError("Enter a topic to search for.", "For example: python weekly newsletter")
    lowered = topic.lower()
    if not any(w in lowered for w in ("newsletter", "subscribe", "sign up")):
        topic += " newsletter subscribe signup page"
    return topic


def explain_http_error(code: int, url: str) -> SearchAPIError:
    """Map an HTTP status to a clear, actionable message (no repeated noise)."""
    if code in (401, 403):
        return SearchAPIError(
            f"The Search API rejected your API key (HTTP {code}).",
            "Check SEARCH_API_KEY in .env: no quotes, no extra spaces, and the key "
            "must be active on your provider's dashboard (Tavily keys start with 'tvly-').")
    if code == 429:
        return SearchAPIError("The Search API rate limit or quota was reached (HTTP 429).",
                              "Wait a while or check your plan's quota. The tool does not retry.")
    if code == 400:
        return SearchAPIError("The Search API did not accept the request (HTTP 400).",
                              "Check SEARCH_API_URL points at a Tavily-compatible /search endpoint.")
    if code == 404:
        return SearchAPIError(f"The Search API address was not found (HTTP 404): {url}",
                              "Check SEARCH_API_URL (default: https://api.tavily.com/search).")
    return SearchAPIError(f"The Search API returned HTTP {code}.", "Try again later.")


def parse_results(payload: object, limit: int) -> list[str]:
    """Extract unique, valid URLs from a Tavily-style payload."""
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        raise SearchAPIError("The Search API reply had no 'results' list.",
                             "SEARCH_API_URL must be a Tavily-compatible endpoint.")
    urls = [item.get("url", "") for item in results if isinstance(item, dict)]
    return dedupe_urls(urls)[:limit]


def search_newsletter_urls(settings: Settings, topic: str, limit: int | None = None) -> list[str]:
    """Return candidate newsletter URLs for *topic* (one API call)."""
    settings.require_search()
    limit = max(1, min(20, limit or settings.search_max_results))
    body = json.dumps({"query": build_query(topic), "max_results": limit}).encode("utf-8")
    request = urllib.request.Request(
        settings.search_api_url, data=body, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT,
                 "Authorization": f"Bearer {settings.search_api_key}"})
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise explain_http_error(exc.code, settings.search_api_url) from None
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, socket.gaierror):
            raise SearchAPIError("The Search API host could not be found.",
                                 "Check SEARCH_API_URL and your internet connection.") from None
        raise SearchAPIError(f"Could not reach the Search API ({exc.reason}).",
                             "Check your internet connection and SEARCH_API_URL.") from None
    except (socket.timeout, TimeoutError):
        raise SearchAPIError("The Search API timed out.", "Try again in a moment.") from None
    except json.JSONDecodeError:
        raise SearchAPIError("The Search API reply was not valid JSON.",
                             "SEARCH_API_URL must be a Tavily-compatible endpoint.") from None
    return parse_results(payload, limit)
