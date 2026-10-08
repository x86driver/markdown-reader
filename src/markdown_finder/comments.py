"""Hide HTML comments without enabling other raw HTML.

Block rules consume comment contents before headings or reference definitions
can be parsed. Inline rules run after code spans and escapes, so literal
examples remain visible. Unclosed block comments end at the container boundary;
unclosed inline comments remain text, following CommonMark's HTML rules.
"""

import re

from markdown_it import MarkdownIt
from markdown_it.common.html_re import comment as HTML_COMMENT_PATTERN
from markdown_it.rules_block import StateBlock
from markdown_it.rules_inline import StateInline


_COMMENT_RE = re.compile(HTML_COMMENT_PATTERN)


def _comment_inline(state: StateInline, silent: bool) -> bool:
    if not state.src.startswith("<!--", state.pos):
        return False
    match = _COMMENT_RE.match(state.src, state.pos, state.posMax)
    if match is None:
        return False
    if not silent:
        token = state.push("html_comment", "", 0)
        token.hidden = True
    state.pos = match.end()
    return True


def _comment_block(state: StateBlock, start: int, end: int, silent: bool) -> bool:
    if state.is_code_block(start):
        return False
    position = state.bMarks[start] + state.tShift[start]
    if not state.src.startswith("<!--", position, state.eMarks[start]):
        return False
    if silent:
        return True

    next_line = start + 1
    closing = state.src.find("-->", position, state.eMarks[start])
    while closing < 0 and next_line < end:
        if state.sCount[next_line] < state.blkIndent:
            break
        position = state.bMarks[next_line] + state.tShift[next_line]
        closing = state.src.find("-->", position, state.eMarks[next_line])
        next_line += 1

    state.line = next_line
    token = state.push("html_comment", "", 0)
    token.map = [start, next_line]
    token.hidden = True

    # Preserve text and disclosure tags after the closing delimiter. Use
    # normal inline parsing so other HTML is still escaped and links resolve
    # after all document reference definitions have been collected.
    if closing >= 0:
        suffix = state.src[closing + 3:state.eMarks[next_line - 1]].strip()
        if suffix:
            token = state.push("inline", "", 0)
            token.map = [next_line - 1, next_line]
            token.content = suffix
            token.children = []
    return True


def comments_plugin(parser: MarkdownIt) -> None:
    parser.block.ruler.before(
        "html_block", "comment_block", _comment_block,
        {"alt": ["paragraph", "reference", "blockquote", "list"]},
    )
    parser.inline.ruler.before("html_inline", "comment_inline", _comment_inline)
