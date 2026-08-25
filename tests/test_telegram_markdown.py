"""Markdown -> Telegram HTML conversion (telegram/markdown.py).

Pure unit tests — no network, no Telegram, no LLM. Covers the supported subset
(bold, italic, inline code, fenced code, links, headings), entity escaping,
snake_case safety, and the chunking invariant that no chunk ever dangles an
open tag.
"""

from __future__ import annotations

from fibrecase_agent_backend.telegram.markdown import (
    to_telegram_html,
    to_telegram_html_chunks,
)


# ---------------------------------------------------------------------------
# inline rendering
# ---------------------------------------------------------------------------
def test_bold_asterisks():
    assert to_telegram_html("a **bold** b") == "a <b>bold</b> b"


def test_bold_double_underscore():
    assert to_telegram_html("a __bold__ b") == "a <b>bold</b> b"


def test_italic_asterisks():
    assert to_telegram_html("a *em* b") == "a <i>em</i> b"


def test_italic_underscore():
    # A single _ between word chars is italic, not bold (was a bug: it mapped
    # to <b>).
    assert to_telegram_html("a _em_ b") == "a <i>em</i> b"


def test_strikethrough():
    assert to_telegram_html("a ~~gone~~ b") == "a <s>gone</s> b"
    assert to_telegram_html("~~del star~~") == "<s>del star</s>"


def test_snake_case_is_not_italic():
    # A single underscore between word chars must stay literal.
    assert to_telegram_html("foo_bar_baz") == "foo_bar_baz"
    # But a real emphasis span still works.
    assert to_telegram_html("x _hi_ y") == "x <i>hi</i> y"


def test_inline_code():
    assert to_telegram_html("run `pip install x` now") == "run <code>pip install x</code> now"


def test_inline_code_escapes_html():
    # < and > inside a code span must be escaped, not treated as tags.
    assert to_telegram_html("`a < b > c`") == "<code>a &lt; b &gt; c</code>"


def test_code_span_protected_from_emphasis_and_strikethrough():
    # Emphasis / strikethrough / links must NOT be applied inside `code` — a
    # snippet like `list(*args, **kwargs)` must stay literal code.
    assert to_telegram_html("call `f(*a, **kw)` now") == "call <code>f(*a, **kw)</code> now"
    assert to_telegram_html("`a ~~b~~ c`") == "<code>a ~~b~~ c</code>"
    assert to_telegram_html("`[not a link](x)`") == "<code>[not a link](x)</code>"


def test_link():
    assert (
        to_telegram_html("see [the docs](https://example.com) now")
        == 'see <a href="https://example.com">the docs</a> now'
    )


def test_heading_becomes_bold():
    assert to_telegram_html("# Title") == "<b>Title</b>"
    assert to_telegram_html("### Sub") == "<b>Sub</b>"


def test_ampersand_is_escaped_first():
    # & must not become &amp;amp; and must not be left raw.
    assert to_telegram_html("A & B") == "A &amp; B"


def test_multiple_marks_one_line():
    out = to_telegram_html("**bold** and `code` and [link](https://e.com)")
    assert "<b>bold</b>" in out
    assert "<code>code</code>" in out
    assert '<a href="https://e.com">link</a>' in out


# ---------------------------------------------------------------------------
# fenced code blocks
# ---------------------------------------------------------------------------
def test_code_block_wrapped_in_pre():
    md = "before\n\n```python\nx = 1\ny = 2\n```\n\nafter"
    out = to_telegram_html(md)
    assert "<pre><code>x = 1\ny = 2</code></pre>" in out
    assert out.startswith("before")
    assert out.endswith("after")


def test_code_block_content_not_interpreted_as_markdown():
    # ** inside a code block must stay literal, not become <b>.
    out = to_telegram_html("```text\n**not bold**\n```")
    assert "**not bold**" in out
    assert "<b>" not in out


def test_unterminated_code_block_is_closed():
    out = to_telegram_html("```python\nx = 1")
    assert out.startswith("<pre><code>")
    assert out.endswith("</code></pre>")


def test_html_escaped_inside_code_block():
    out = to_telegram_html("```\nprint('<ok>')\n```")
    assert "<code>print('&lt;ok&gt;')</code>" in out


# ---------------------------------------------------------------------------
# the user's real example
# ---------------------------------------------------------------------------
def test_user_example_renders():
    md = (
        "严格说，我自己不能主动读取你服务器或设备的实时时钟。\n\n"
        "1. **运行环境/客户端在对话上下文中提供时间戳**：我可以回答。\n"
        "2. **没有提供时间上下文**：我不应给出具体时间点。\n\n"
        "我刚才给出的 `2026年6月22日 22:25`，无法执行 `date` 查询系统时间。"
    )
    out = to_telegram_html(md)
    assert "<b>运行环境/客户端在对话上下文中提供时间戳</b>" in out
    assert "<b>没有提供时间上下文</b>" in out
    assert "<code>2026年6月22日 22:25</code>" in out
    assert "<code>date</code>" in out


def test_empty_input():
    assert to_telegram_html("") == ""


# ---------------------------------------------------------------------------
# chunking: tag balance is the load-bearing invariant
# ---------------------------------------------------------------------------
def _assert_tags_balanced(html: str) -> None:
    for tag in ("pre", "code", "b", "i"):
        assert html.count(f"<{tag}") == html.count(f"</{tag}>"), (
            f"unbalanced <{tag}>: {html.count(f'<{tag}')} open vs {html.count(f'</{tag}')} close"
        )


def test_code_block_is_never_split_mid_tag():
    # A fenced block is kept atomic: even if it exceeds the limit it is its own
    # chunk (the >limit single chunk is later sent as plain text by the caller),
    # and it is never cut so a <pre>/<code> dangles across a boundary.
    body = "\n".join(f"line_{i} = {i}  # a comment with a period." for i in range(400))
    md = "```\n" + body + "\n```\n"
    chunks = to_telegram_html_chunks(md, limit=1000)
    assert len(chunks) == 1  # the whole block is one atomic chunk
    _assert_tags_balanced(chunks[0].html)
    # It genuinely is a fenced block rendered in <pre>.
    assert chunks[0].html.startswith("<pre><code>")
    assert chunks[0].html.endswith("</code></pre>")


def test_multi_block_text_is_split_when_over_limit():
    # When there are several blocks, oversized *runs* split at block boundaries
    # into multiple tag-balanced chunks.
    paras = ["paragraph **bold** with many words number %d. " % i for i in range(200)]
    md = "\n\n".join(paras)
    chunks = to_telegram_html_chunks(md, limit=1000)
    assert len(chunks) > 1
    for c in chunks:
        _assert_tags_balanced(c.html)
        assert len(c.html) <= 1000


def test_short_text_is_single_chunk():
    chunks = to_telegram_html_chunks("**hi** and `code`", limit=4096)
    assert len(chunks) == 1
    assert chunks[0].html == "<b>hi</b> and <code>code</code>"


def test_chunk_fallback_text_preserves_content():
    md = "first paragraph with **bold**.\n\nsecond paragraph."
    chunks = to_telegram_html_chunks(md, limit=4096)
    # The plain fallback, joined, contains the original words.
    joined = "\n".join(c.text for c in chunks)
    assert "first paragraph with **bold**." in joined
    assert "second paragraph." in joined


def test_empty_chunks():
    chunks = to_telegram_html_chunks("")
    assert len(chunks) == 1
    assert chunks[0].html == ""
