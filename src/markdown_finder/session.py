"""Validate the small, versioned reading session stored in QSettings."""

from __future__ import annotations

import json
import math
from pathlib import Path


SESSION_KEY = "reader_session"


def _number(value, default, minimum, maximum):
    if type(value) not in (int, float):
        return default
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return default
    return max(minimum, min(maximum, number)) if math.isfinite(number) else default


def _path(value):
    return value if isinstance(value, str) and "\0" not in value and Path(value).is_absolute() else None


def read_session(value) -> dict | None:
    if not isinstance(value, str):
        return None
    try:
        session = json.loads(value)
    except (ValueError, RecursionError):
        return None
    if not isinstance(session, dict) or session.get("version") != 1:
        return None
    if not isinstance(session.get("tabs"), list):
        return None
    tabs = []
    seen = set()
    for item in session["tabs"]:
        if not isinstance(item, dict):
            continue
        path = _path(item.get("path"))
        if path is None or path in seen:
            continue
        seen.add(path)
        tabs.append({
            "path": path,
            "zoom": _number(item.get("zoom"), 1.0, 0.25, 5.0),
            "fit_width": item.get("fit_width") is True,
            "scroll_x": _number(item.get("scroll_x"), 0.0, 0.0, 1e9),
            "scroll_y": _number(item.get("scroll_y"), 0.0, 0.0, 1e9),
            "outline_visible": item.get("outline_visible") is not False,
            "outline_width": int(_number(item.get("outline_width"), 230, 130, 480)),
        })
    return {
        "version": 1,
        "tabs": tabs,
        "active_path": _path(session.get("active_path")),
        "last_reader_path": _path(session.get("last_reader_path")),
        "search_tab_index": int(_number(session.get("search_tab_index"), 0, 0, len(tabs))),
    }
