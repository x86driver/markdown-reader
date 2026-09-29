"""Offline Markdown-to-HTML rendering, independent of the Qt user interface.

Document HTML and scripts are never executed. Remote subresources are blocked
by the generated Content Security Policy; local and data-URI images work.
Navigation is handled separately by the reader widget.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from html import escape
from importlib.resources import files
from pathlib import Path
import re
import unicodedata

from markdown_it import MarkdownIt
from markdown_it.token import Token
from mdit_py_plugins.footnote import footnote_plugin
from mdit_py_plugins.tasklists import tasklists_plugin
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name
from pygments.util import ClassNotFound


@dataclass(frozen=True)
class Heading:
    level: int
    title: str
    anchor: str


@dataclass(frozen=True)
class RenderedDocument:
    path: Path
    title: str
    html: str
    headings: tuple[Heading, ...]


def _highlight_code(source: str, language: str, _attributes: str) -> str:
    if language:
        try:
            lexer = get_lexer_by_name(language, stripnl=False, ensurenl=False)
        except ClassNotFound:
            pass
        else:
            return highlight(source, lexer, HtmlFormatter(nowrap=True))
    return escape(source, quote=False)


@lru_cache(maxsize=1)
def _stylesheet() -> str:
    css = files("markdown_finder").joinpath("assets", "reader.css").read_text(encoding="utf-8")
    light_code = HtmlFormatter(style="friendly").get_style_defs("html[data-theme='light'] pre")
    dark_code = HtmlFormatter(style="monokai").get_style_defs("html[data-theme='dark'] pre")
    return f"{css}\n{light_code}\n{dark_code}"


def _inline_title(tokens: list[Token] | None) -> str:
    """Extract visible text, including code and image alt text, for the outline."""
    pieces = []
    for token in tokens or ():
        if token.type in {"text", "code_inline"}:
            pieces.append(token.content)
        elif token.type in {"softbreak", "hardbreak"}:
            pieces.append(" ")
        elif token.type == "image":
            pieces.append(_inline_title(token.children))
    return "".join(pieces).strip()


def _slug(title: str) -> str:
    # Keep Unicode letters and numbers, including CJK, while dropping punctuation.
    text = unicodedata.normalize("NFC", title).lower()
    text = "".join(char for char in text if char.isalnum() or char in "_-" or char.isspace())
    return re.sub(r"\s+", "-", text).strip("-") or "section"


def render_document(path: Path) -> RenderedDocument:
    """Read a UTF-8 document (optionally BOM-prefixed) and render it.

    Filesystem and decoding errors propagate to the caller for user-visible
    handling. ``absolute`` deliberately preserves symlinks: assets are relative
    to the path the user opened, not to a symlink's physical target directory.
    """
    path = Path(path).expanduser().absolute()
    return render_markdown(path.read_text(encoding="utf-8-sig"), path=path)


def render_markdown(source: str, *, path: Path) -> RenderedDocument:
    """Render Markdown as a complete offline HTML document with an outline."""
    path = Path(path).expanduser().absolute()
    parser = MarkdownIt("commonmark", {"html": False, "highlight": _highlight_code})
    parser.enable(["table", "strikethrough"])
    parser.use(tasklists_plugin).use(footnote_plugin)
    environment: dict = {}
    tokens = parser.parse(source.removeprefix("\ufeff"), environment)

    headings = []
    used_anchors = set()
    # Footnote plugins generate IDs while rendering, so reserve those names too.
    for token in tokens:
        if token.type in {"footnote_open", "footnote_ref"}:
            footnote_id = token.meta.get("id", 0) + 1
            used_anchors.update((f"fn{footnote_id}", f"fnref{footnote_id}"))
    for index, token in enumerate(tokens):
        if token.type != "heading_open":
            continue
        title = _inline_title(tokens[index + 1].children)
        base = _slug(title)
        anchor = base
        suffix = 1
        while anchor in used_anchors:
            anchor = f"{base}-{suffix}"
            suffix += 1
        used_anchors.add(anchor)
        token.attrSet("id", anchor)
        headings.append(Heading(int(token.tag[1:]), title, anchor))

    body = parser.renderer.render(tokens, parser.options, environment)
    title = next((heading.title for heading in headings if heading.level == 1 and heading.title), path.name)
    base_uri = path.parent.as_uri().rstrip("/") + "/"
    policy = (
        "default-src 'none'; img-src file: data:; style-src 'unsafe-inline'; "
        "script-src 'none'; object-src 'none'; frame-src 'none'; "
        "connect-src 'none'; media-src 'none'; form-action 'none'; base-uri file:"
    )
    html = (
        '<!doctype html>\n<html lang="zh-Hant" data-theme="light">\n<head>\n'
        '<meta charset="utf-8">\n'
        f'<meta http-equiv="Content-Security-Policy" content="{escape(policy, quote=True)}">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f'<base href="{escape(base_uri, quote=True)}">\n'
        f'<title>{escape(title)}</title>\n'
        f'<style>\n{_stylesheet()}\n</style>\n'
        f'</head>\n<body><main class="markdown-body">\n{body}</main></body>\n</html>\n'
    )
    return RenderedDocument(path=path, title=title, html=html, headings=tuple(headings))
