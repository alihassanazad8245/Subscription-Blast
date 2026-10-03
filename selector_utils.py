"""
selector_utils.py - turning saved field configs into CSS selectors, and
parsing selector text typed by the user. Pure functions, no I/O.
"""
from __future__ import annotations


def css_string(value: str) -> str:
    """Escape *value* for use inside a double-quoted CSS attribute selector."""
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def selector_from_config(field_config) -> str:
    """Build a CSS selector from a saved field config.

    Supported shapes:
      {"css": "input[type='email']"}   raw CSS, returned as-is
      {"id": "email"}                  -> #email
      {"class": "btn primary"}         -> .btn.primary
      {"name": "subscribe"}            -> [name="subscribe"]
      {"value": "Sign Up"}             -> [value="Sign Up"]
    Several attribute keys are combined into one compound selector.
    """
    if not isinstance(field_config, dict):
        return ""
    raw_css = str(field_config.get("css", "")).strip()
    if raw_css:
        return raw_css
    selector = ""
    if field_config.get("class"):
        selector += "".join(f".{name}" for name in str(field_config["class"]).split())
    if field_config.get("id"):
        selector += f"#{field_config['id']}"
    if field_config.get("name"):
        selector += f'[name="{css_string(field_config["name"])}"]'
    if field_config.get("value"):
        selector += f'[value="{css_string(field_config["value"])}"]'
    return selector


def split_top_level(raw: str, sep: str = ",") -> list[str]:
    """Split on *sep* but not inside quotes/brackets/parentheses.

    ``"input[name='a,b'], button"`` -> ``["input[name='a,b']", "button"]``
    """
    parts, depth, quote, current = [], 0, "", []
    for ch in raw:
        if quote:
            if ch == quote:
                quote = ""
        elif ch in ("'", '"'):
            quote = ch
        elif ch in "([":
            depth += 1
        elif ch in ")]":
            depth = max(0, depth - 1)
        elif ch == sep and depth == 0:
            parts.append("".join(current).strip())
            current = []
            continue
        current.append(ch)
    parts.append("".join(current).strip())
    return [p for p in parts if p]


def parse_css_selector_list(raw_input: str) -> list[dict]:
    """Turn ``"a, b"`` into ``[{"css": "a"}, {"css": "b"}]`` (blanks ignored)."""
    return [{"css": part} for part in split_top_level(raw_input or "")]


def looks_like_css(selector: str) -> bool:
    """Cheap syntax sanity check (balanced quotes/brackets, not empty)."""
    selector = (selector or "").strip()
    if not selector:
        return False
    stack, quote = [], ""
    pairs = {")": "(", "]": "["}
    for ch in selector:
        if quote:
            if ch == quote:
                quote = ""
        elif ch in ("'", '"'):
            quote = ch
        elif ch in "([":
            stack.append(ch)
        elif ch in ")]":
            if not stack or stack.pop() != pairs[ch]:
                return False
    return not stack and not quote


def parse_selection(raw: str, elements: list[dict]) -> tuple[list[dict], list[str]]:
    """Interpret what the user typed when assigning a field.

    * ``"2"`` or ``"2,5"``  -> pick rows from the inspected-elements table
    * anything else         -> raw CSS selector(s)

    Returns ``(fields, problems)``; *fields* is a list of saved field configs.
    """
    raw = (raw or "").strip()
    if not raw:
        return [], []
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if parts and all(p.isdigit() for p in parts):
        fields, problems = [], []
        for part in parts:
            idx = int(part) - 1
            if 0 <= idx < len(elements):
                fields.append(field_from_element(elements[idx]))
            else:
                problems.append(f"Row {part} is not in the table.")
        return fields, problems
    fields, problems = [], []
    for field in parse_css_selector_list(raw):
        if looks_like_css(field["css"]):
            fields.append(field)
        else:
            problems.append(f"'{field['css']}' does not look like a valid CSS selector.")
    return fields, problems


def field_from_element(element: dict) -> dict:
    """Build a saved field config from an inspected element dict."""
    field = {"css": str(element.get("selector", "")).strip()}
    frame_index = element.get("frame_index")
    if isinstance(frame_index, int):
        field["frame_index"] = frame_index
    selector_index = element.get("selector_index")
    if isinstance(selector_index, int) and selector_index > 0:
        if element.get("displayed") is True:
            field["visible"] = True
        else:
            field["index"] = selector_index
    return field


def describe_field(field: dict) -> str:
    """Short human-readable form of a saved field config."""
    text = selector_from_config(field) or "(empty)"
    if isinstance(field.get("frame_index"), int):
        text += f"  [iframe #{field['frame_index']}]"
    return text
