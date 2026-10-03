"""
ui.py - terminal look & feel (boxes, colours, status symbols, spinner).

Pure standard library. Colours are switched off automatically when output is
not a terminal or when the NO_COLOR environment variable is set. Box drawing
and symbols fall back to plain ASCII on terminals that cannot show Unicode.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import threading
import time
from contextlib import contextmanager

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_MAX_WIDTH = 92

_CODES = {
    "reset": "0", "bold": "1", "dim": "2",
    "red": "31", "green": "32", "yellow": "33", "blue": "34",
    "magenta": "35", "cyan": "36", "grey": "90",
}


def setup_terminal() -> None:
    """Enable UTF-8 output and ANSI colours (incl. Windows 10+ consoles)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    if os.name == "nt":
        os.system("")  # turns on VT/ANSI processing in the Windows console


def _unicode_ok() -> bool:
    enc = (getattr(sys.stdout, "encoding", "") or "").lower()
    return "utf" in enc


def colors_enabled() -> bool:
    return sys.stdout.isatty() and "NO_COLOR" not in os.environ


def c(text: str, *styles: str) -> str:
    """Wrap *text* in ANSI styles (no-op when colours are disabled)."""
    if not styles or not colors_enabled():
        return text
    codes = ";".join(_CODES[s] for s in styles)
    return f"\x1b[{codes}m{text}\x1b[0m"


def visible_len(text: str) -> int:
    return len(_ANSI.sub("", text))


def width() -> int:
    return max(60, min(_MAX_WIDTH, shutil.get_terminal_size((80, 24)).columns - 2))


def _box_chars() -> dict:
    if _unicode_ok():
        return dict(tl="╭", tr="╮", bl="╰", br="╯", h="─", v="│", ml="├", mr="┤")
    return dict(tl="+", tr="+", bl="+", br="+", h="-", v="|", ml="+", mr="+")


def symbol(kind: str) -> str:
    uni = {"ok": "✔", "warn": "⚠", "err": "✖", "info": "ℹ", "dot": "•", "arrow": "›"}
    asc = {"ok": "[OK]", "warn": "[!]", "err": "[X]", "info": "[i]", "dot": "*", "arrow": ">"}
    return (uni if _unicode_ok() else asc)[kind]


def _wrap(text: str, size: int) -> list[str]:
    """Word-wrap plain/ANSI text to *size* visible characters."""
    lines: list[str] = []
    for raw in str(text).split("\n"):
        if visible_len(raw) <= size:
            lines.append(raw)
            continue
        current = ""
        for word in raw.split(" "):
            if current and visible_len(current) + 1 + visible_len(word) > size:
                lines.append(current)
                current = word
            else:
                current = f"{current} {word}" if current else word
            while visible_len(current) > size and " " not in current:
                lines.append(current[:size])
                current = current[size:]
        lines.append(current)
    return lines


def box(title: str, lines: list[str] | str, color: str = "cyan") -> None:
    """Print *lines* inside a titled rounded box."""
    b = _box_chars()
    w = width()
    inner = w - 4
    if isinstance(lines, str):
        lines = [lines]
    head = f" {title} " if title else ""
    top = b["tl"] + c(head, "bold", color) + b["h"] * (w - 2 - visible_len(head)) + b["tr"]
    print(_colour_top(top, color, b))
    for line in lines:
        for part in _wrap(line, inner):
            pad = " " * (inner - visible_len(part))
            print(f"{c(b['v'], color)} {part}{pad} {c(b['v'], color)}")
    print(c(b["bl"] + b["h"] * (w - 2) + b["br"], color))


def _colour_top(top: str, color: str, b: dict) -> str:
    # top already contains a coloured title; colour only the frame pieces.
    return top.replace(b["tl"], c(b["tl"], color), 1).replace(b["tr"], c(b["tr"], color), 1)


def heading(text: str) -> None:
    print()
    print(c(f" {text} ", "bold", "cyan"))
    print(c(("═" if _unicode_ok() else "=") * min(width(), visible_len(text) + 2), "cyan"))


def ok(msg: str) -> None:
    print(f" {c(symbol('ok'), 'green', 'bold')} {msg}")


def warn(msg: str) -> None:
    print(f" {c(symbol('warn'), 'yellow', 'bold')} {c(msg, 'yellow')}")


def err(msg: str) -> None:
    print(f" {c(symbol('err'), 'red', 'bold')} {c(msg, 'red')}")


def info(msg: str) -> None:
    print(f" {c(symbol('info'), 'blue')} {msg}")


def step(n: int, total: int, msg: str) -> None:
    print(f"\n {c(f'[{n}/{total}]', 'magenta', 'bold')} {c(msg, 'bold')}")


def error_box(message: str, hint: str = "") -> None:
    lines = [c(message, "red", "bold")]
    if hint:
        lines += ["", f"{symbol('arrow')} {hint}"]
    box("Something needs attention", lines, color="red")


def table(headers: list[str], rows: list[list[str]], max_col: int = 46) -> None:
    """Print a simple aligned table (values truncated to *max_col*)."""
    def clip(s: str) -> str:
        s = str(s)
        return s if visible_len(s) <= max_col else s[: max_col - 1] + "…"

    cells = [[clip(x) for x in row] for row in rows]
    widths = [visible_len(h) for h in headers]
    for row in cells:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], visible_len(cell))
    line = "  ".join(c(h.ljust(widths[i]), "bold") for i, h in enumerate(headers))
    print(" " + line)
    print(" " + c("  ".join(("─" if _unicode_ok() else "-") * wd for wd in widths), "grey"))
    for row in cells:
        print(" " + "  ".join(
            cell + " " * (widths[i] - visible_len(cell)) for i, cell in enumerate(row)
        ))


STATUS_STYLE = {
    "discovered": ("◌", "o", "grey"),
    "configured": ("◐", "~", "yellow"),
    "verified": ("●", "*", "green"),
    "failed": ("✖", "x", "red"),
    "unavailable": ("⊘", "-", "magenta"),
}


def status_badge(status: str) -> str:
    uni, asc, color = STATUS_STYLE.get(status, ("?", "?", "grey"))
    return c(f"{uni if _unicode_ok() else asc} {status}", color)


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        value = input(f" {c(symbol('arrow'), 'cyan', 'bold')} {prompt}{suffix}: ").strip()
    except EOFError:
        return default
    return value or default


def confirm(prompt: str, default: bool = False) -> bool:
    hint = "Y/n" if default else "y/N"
    answer = ask(f"{prompt} ({hint})").lower()
    if not answer:
        return default
    return answer in ("y", "yes")


def menu(title: str, options: list[tuple[str, str]]) -> str:
    """Show a boxed menu of (key, label) options; return the chosen key."""
    box(title, [f"{c(f'[{key}]', 'bold', 'cyan')}  {label}" for key, label in options])
    return ask("Choose an option").lower()


@contextmanager
def spinner(message: str):
    """Show an animated spinner while a slow task runs (plain line if no TTY)."""
    if not sys.stdout.isatty():
        print(f" {symbol('dot')} {message}...")
        yield
        return
    frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏" if _unicode_ok() else "|/-\\"
    stop = threading.Event()

    def run() -> None:
        i = 0
        while not stop.is_set():
            sys.stdout.write(f"\r {c(frames[i % len(frames)], 'cyan')} {message}   ")
            sys.stdout.flush()
            i += 1
            time.sleep(0.09)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join()
        sys.stdout.write("\r" + " " * (visible_len(message) + 8) + "\r")
        sys.stdout.flush()


def progress(current: int, total: int, label: str = "") -> None:
    """Print a one-line progress bar, e.g.  [#####-----] 3/6 label."""
    total = max(total, 1)
    filled = int(20 * current / total)
    full, empty = ("█", "░") if _unicode_ok() else ("#", "-")
    bar = c(full * filled, "green") + c(empty * (20 - filled), "grey")
    print(f" {bar} {current}/{total} {label}")


def credits(author: str, instagram: str, original_author: str, original_repo: str) -> None:
    box("Thanks for using Subscription Form Tester", [
        f"Made by {c(author, 'bold')}",
        f"Instagram: @{instagram}",
        "",
        c(f"Based on the original project by {original_author}", "dim"),
        c(original_repo, "dim"),
        c("For authorized testing with inboxes you own only.", "dim"),
    ], color="magenta")
