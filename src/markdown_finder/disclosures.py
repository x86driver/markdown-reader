"""Selective Markdown rules for native HTML disclosure elements.

Raw HTML stays disabled. Only details/summary tags are emitted, and only the
boolean ``open`` attribute is retained; all text still passes through Markdown.
"""

from bisect import bisect_left
from html.parser import HTMLParser
import re

from markdown_it import MarkdownIt
from markdown_it.common.html_re import HTML_TAG_RE
from markdown_it.rules_block import StateBlock
from markdown_it.rules_inline import StateInline


_TAG_START = re.compile(r"</?(details|summary)(?=[\s/>])", re.IGNORECASE)


class _DisclosureTag(HTMLParser):
    """Reconstruct a validated tag instead of copying document attributes."""

    def __init__(self, source: str):
        super().__init__()
        self.html = ""
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        opened = tag == "details" and any(name == "open" for name, _ in attrs)
        self.html = f'<{tag}{" open" if opened else ""}>'

    def handle_endtag(self, tag):
        self.html = f"</{tag}>"

    def handle_startendtag(self, tag, attrs):
        # HTML treats these non-void elements as opening tags even with '/>'.
        self.handle_starttag(tag, attrs)


def _tag_at(source: str, position: int = 0):
    if not _TAG_START.match(source, position):
        return None
    return HTML_TAG_RE.match(source[position:])


def _disclosure_inline(state: StateInline, silent: bool) -> bool:
    match = _tag_at(state.src, state.pos)
    if match is None or state.pos + match.end() > state.posMax:
        return False
    if not silent:
        token = state.push("html_inline", "", 0)
        token.content = _DisclosureTag(match.group()).html
    state.pos += match.end()
    return True


def _summary_end_line(state: StateBlock, start: int, end: int) -> int:
    """Find a closing summary outside comments, code spans, and escapes."""
    inline = StateInline(state.src, state.md, state.env, [])
    inline.pos = state.bMarks[start] + state.tShift[start]
    inline.posMax = state.eMarks[end - 1]
    while inline.pos < inline.posMax:
        line = bisect_left(state.eMarks, inline.pos, start, end)
        if line > start and (state.isEmpty(line) or state.sCount[line] < state.blkIndent):
            break
        position = state.bMarks[line] + state.tShift[line]
        # Quote/list prefixes have been removed from the block line marks.
        inline.pos = max(inline.pos, position)
        match = _tag_at(state.src, inline.pos)
        if match is not None:
            suffix = state.src[inline.pos + match.end():state.eMarks[line]]
            if _DisclosureTag(match.group()).html == "</summary>" and not suffix.strip():
                return line + 1
            if line > start and inline.pos == position:
                break
        # The inline rules skip entire comments/code spans, including any
        # blank lines or disclosure tags inside them, without producing tokens.
        state.md.inline.skipToken(inline)
    return start + 1


def _disclosure_block(state: StateBlock, start: int, end: int, silent: bool) -> bool:
    if state.is_code_block(start):
        return False
    position = state.bMarks[start] + state.tShift[start]
    line = state.src[position:state.eMarks[start]]
    match = _tag_at(line)
    if match is None:
        return False
    if silent:
        return True

    next_line = start + 1
    # A summary may span several lines. Keep its phrasing content out of a
    # paragraph wrapper so it remains a direct child of the details element.
    if _DisclosureTag(match.group()).html == "<summary>":
        next_line = _summary_end_line(state, start, end)

    # Inline tokenization respects code spans and escapes, and resolves links
    # after the whole document has been parsed. Omitting paragraph tokens lets
    # native summary elements stay directly inside details, including compact
    # one-line disclosures. Subsequent body lines use normal block parsing.
    token = state.push("inline", "", 0)
    token.map = [start, next_line]
    token.content = state.getLines(start, next_line, state.blkIndent, False).strip()
    token.children = []
    state.line = next_line
    return True


def disclosures_plugin(parser: MarkdownIt) -> None:
    parser.block.ruler.before(
        "html_block", "disclosure_block", _disclosure_block,
        {"alt": ["paragraph", "reference", "blockquote", "list"]},
    )
    parser.inline.ruler.before("html_inline", "disclosure_inline", _disclosure_inline)
