"""
errors.py - friendly, user-facing exceptions.

Normal user mistakes (missing Firefox, bad URL, wrong API key...) raise one of
these. main.py catches them and prints the message plus a hint instead of a
raw Python traceback. Genuine programming errors are NOT wrapped: they are
written to the log file for debugging.
"""


class AppError(Exception):
    """Base class: a problem the user can fix, with an optional hint."""

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


class ConfigError(AppError):
    """A required .env setting is missing or invalid."""


class BrowserSetupError(AppError):
    """Firefox / geckodriver could not be found or started."""


class PageError(AppError):
    """A page could not be loaded or does not contain a usable form."""


class SearchAPIError(AppError):
    """The optional Search API rejected or failed a request."""


class IMAPError(AppError):
    """The optional IMAP mailbox check failed."""
