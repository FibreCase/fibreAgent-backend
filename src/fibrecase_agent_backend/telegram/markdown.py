"""Markdown -> Telegram HTML conversion for outgoing messages.

The model replies in Markdown (``**bold**``, ``*italic*``, ``~~strikethrough~~``,
`` `code` ``, ``` ``` ``` fences, links, ``#`` headings), but Telegram does **not**
render Markdown. This module translates that Markdown into the small *HTML* subset
Telegram actually supports (``<b>`` ``<i>`` ``<s>`` ``<code>`` ``<pre>`` ``<a
href=...>``), so the bot can send with ``parse_mode=HTML`` and get real bold /
italic / strikethrough / code styling instead of literal ``**`` and ``` ` ``` in the
chat.

Why HTML and not ``MarkdownV2``? ``MarkdownV2`` requires escaping ~19 special
characters everywhere and is easy to get wrong (a single unescaped ``.`` makes
Telegram 400 and the reply is lost). HTML only needs ``& < >`` escaped, so it is
far more robust against arbitrary LLM output.

Design goals, given that a model reply is unpredictable:

* **Never dangle a tag across a 4096-char split.** :func:`to_telegram_html_chunks`
  splits the *source* into self-contained blocks (a fenced code block is one
  block; runs of text split at blank lines) *before* rendering, so no chunk
  ever begins with an unclosed ``<pre>`` / ``**``.
* **A reply is never lost.** Each chunk carries both its rendered ``html`` and
  its original plain ``text``; if Telegram rejects the HTML (400 "can't parse")
  the caller falls back to sending the plain ``text``.

Only the Telegram adapter uses this — the agent service, LLM client, tools and
database are untouched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Telegram's hard message limit is 4096 UTF-16 units; callers pass a slightly
# smaller limit (CHUNK_SIZE) as the target.
DEFAULT_LIMIT = 4096

# Inline (single-line) markdown. Applied to already-escaped text, in this order:
# code spans first (so their contents aren't re-processed), then links,
# strikethrough, then emphasis (double-marker before single-marker). ``re.M`` /
# ``re.S`` are intentionally NOT used — each pattern runs on exactly one line.
_CODESPAN = re.compile(r"(`+)([^`]+)\1")
_LINK = re.compile(r"\[([^\]\n]+?)\]\((https?://[^)\s]+)\)")
_STRIKE = re.compile(r"~~([^\n~]+?)~~")
# Double markers are matched before single markers so `**b**`/`__b__` aren't
# eaten by the single-`*`/`_` (italic) rules.
_BOLD_STAR = re.compile(r"\*\*([^\n]+?)\*\*")
_BOLD_UNDER = re.compile(r"__(?!\s)([^_\n]+?)__(?!\w)")
_ITALIC_STAR = re.compile(r"(?<!\*)\*([^*\n]+?)\*(?!\*)")
_ITALIC_UNDER = re.compile(r"(?<![\w])_([^_\n]+?)_(?![\w])")
_HEADING = re.compile(r"^ {0,3}#{1,6}\s+(.*?)\s*$")


def _esc(text: str) -> str:
    """Escape the three HTML-significant chars so they can't form a tag.

    ``&`` first so we don't double-escape the ``&`` in ``&lt;``/``&gt;``.
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _render_code_block(lines: list[str]) -> str:
    inner = "\n".join(_esc(line) for line in lines)
    return f"<pre><code>{inner}</code></pre>"


def _convert_emph(s: str) -> str:
    """Render links / strikethrough / emphasis of a non-code text span.

    Code spans are handled by the caller (kept verbatim), so this never sees a
    `` ` `` pair.
    """
    s = _LINK.sub(lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', s)
    s = _STRIKE.sub(lambda m: f"<s>{m.group(1)}</s>", s)
    s = _BOLD_STAR.sub(lambda m: f"<b>{m.group(1)}</b>", s)
    s = _BOLD_UNDER.sub(lambda m: f"<b>{m.group(1)}</b>", s)
    s = _ITALIC_STAR.sub(lambda m: f"<i>{m.group(1)}</i>", s)
    s = _ITALIC_UNDER.sub(lambda m: f"<i>{m.group(1)}</i>", s)
    return s


def _convert_with_code(s: str) -> str:
    """Convert a line, keeping `` `code` `` spans verbatim.

    Emphasis / links / strikethrough are applied only to the text *between*
    code spans, so a `` `list(*args, **kwargs)` `` never gets ``<b>``/``<i>``
    injected into the code.
    """
    parts: list[str] = []
    last = 0
    for m in _CODESPAN.finditer(s):
        parts.append(_convert_emph(s[last : m.start()]))
        parts.append(f"<code>{m.group(2)}</code>")
        last = m.end()
    parts.append(_convert_emph(s[last:]))
    return "".join(parts)


def _convert_inline(line: str) -> str:
    """Render the inline markdown of a single line to Telegram HTML.

    Mapping (CommonMark-like): ``**b**``/``__b__`` -> bold, ``*i*``/``_i_`` ->
    italic, ``~~s~~`` -> strikethrough, `` `c` `` -> inline code (verbatim),
    ``[x](url)`` -> link, ``# heading`` -> bold. A single ``_`` between word
    chars (as in ``snake_case``) is left literal.
    """
    line = _esc(line)
    heading = _HEADING.match(line)
    if heading:  # a heading wraps its whole (code-aware) content in <b>
        content = line[heading.start(1) : heading.end(1)]
        return f"<b>{_convert_with_code(content)}</b>"
    return _convert_with_code(line)


def to_telegram_html(text: str) -> str:
    """Convert a whole Markdown string to Telegram HTML (no chunking).

    Every input line ends up in the output (fenced code is wrapped in
    ``<pre><code>``, other lines are inline-converted), so no text is dropped.
    """
    if not text:
        return ""
    out: list[str] = []
    in_fence = False
    fence_lines: list[str] = []
    for line in str(text).split("\n"):
        stripped = line.strip()
        if not in_fence and stripped.startswith("```"):
            in_fence = True
            fence_lines = []
            continue
        if in_fence:
            if stripped.startswith("```"):
                out.append(_render_code_block(fence_lines))
                in_fence = False
            else:
                fence_lines.append(line)
            continue
        out.append(_convert_inline(line))
    if in_fence:  # unterminated fence: close it so the tag isn't dangling
        out.append(_render_code_block(fence_lines))
    return "\n".join(out)


def _split_blocks(text: str) -> list[str]:
    """Split ``text`` into self-contained source blocks.

    A fenced code block (``` ... ```) is a single block. Runs of non-fenced text
    are split at blank lines into paragraphs. Blank lines act as separators and
    are not themselves kept (paragraphs are rejoined with a single newline by
    the chunker). Every *non-blank* line lands in exactly one block.
    """
    blocks: list[str] = []
    cur: list[str] = []
    fence: list[str] = []
    in_fence = False
    for line in text.split("\n"):
        stripped = line.strip()
        if not in_fence and stripped.startswith("```"):
            if cur:
                blocks.append("\n".join(cur))
                cur = []
            fence = [line]
            in_fence = True
            continue
        if in_fence:
            fence.append(line)
            if stripped.startswith("```"):
                blocks.append("\n".join(fence))
                fence = []
                in_fence = False
            continue
        if stripped == "":
            if cur:
                blocks.append("\n".join(cur))
                cur = []
        else:
            cur.append(line)
    if in_fence and fence:
        blocks.append("\n".join(fence))
    if cur:
        blocks.append("\n".join(cur))
    return [b for b in blocks if b.strip()]


@dataclass(frozen=True)
class HtmlChunk:
    """A piece of an outgoing message: rendered HTML plus its plain fallback.

    ``text`` is the original (unformatted) source of this piece, used if
    Telegram rejects the HTML; ``html`` is the preferred, styled version.
    """

    text: str
    html: str


def to_telegram_html_chunks(text: str, limit: int = DEFAULT_LIMIT) -> list[HtmlChunk]:
    """Chunk ``text`` so each piece renders to tag-balanced, <=limit HTML.

    Because blocks are split *before* rendering, no piece starts mid-tag. A
    single block whose HTML alone exceeds ``limit`` becomes its own piece (it may
    then be sent as plain text by the caller if Telegram still rejects it) — the
    reply is never lost.
    """
    if not text:
        return [HtmlChunk(text or "", "")]

    chunks: list[HtmlChunk] = []
    cur_text = ""
    cur_html = ""
    for block in _split_blocks(text):
        block_html = to_telegram_html(block)
        if len(block_html) > limit:
            # One block is already too big: isolate it (don't merge with others).
            if cur_html:
                chunks.append(HtmlChunk(cur_text, cur_html))
                cur_text = cur_html = ""
            chunks.append(HtmlChunk(block, block_html))
            continue
        if cur_html and len(cur_html) + len(block_html) + 1 > limit:
            chunks.append(HtmlChunk(cur_text, cur_html))
            cur_text = cur_html = ""
        if cur_html:
            cur_text = cur_text + "\n" + block
            cur_html = cur_html + "\n" + block_html
        else:
            cur_text = block
            cur_html = block_html
    if cur_html:
        chunks.append(HtmlChunk(cur_text, cur_html))
    return chunks or [HtmlChunk(text, "")]
